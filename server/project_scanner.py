"""扫描本机 ``~/.claude/projects/`` 目录，列出已存在的 claude 项目和历史会话。

claude 把每个工作目录的会话存在 ``~/.claude/projects/<encoded-cwd>/<uuid>.jsonl``。
encoded-cwd 是把 cwd 里的 ``/`` 全换成 ``-``，**反解不可逆**（原路径里的 ``-``
跟 ``/`` 编码后是同样的字符）。

所以我们不靠目录名反解：每条 jsonl 行里都带 ``cwd`` 字段，是真实绝对路径。
我们读任意一行 cwd 当作权威，目录名只作为找不到时的回退。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

_HOME = Path.home()


def _is_tool_dir(cwd: str) -> bool:
    """过滤掉隐藏工具目录（如 ~/.claude-mem、~/.claude 等），它们不是用户项目。"""
    try:
        rel = Path(cwd).relative_to(_HOME)
        # home 下第一段以 . 开头 → 工具/配置目录
        return rel.parts[0].startswith(".")
    except ValueError:
        return False


@dataclass
class ProjectInfo:
    """一个本机 claude 项目目录。"""
    cwd: str                 # 真实绝对路径（从 jsonl cwd 字段读取）
    name: str                # basename(cwd)，给飞书展示用
    encoded_dirname: str     # 在 projects 下的目录名
    session_count: int
    latest_mtime: float


@dataclass
class SessionInfo:
    """一个历史会话。"""
    claude_session_id: str   # = jsonl 文件名去掉 .jsonl
    cwd: str
    mtime: float
    first_user_text: Optional[str]
    jsonl_path: Optional[Path] = field(default=None, compare=False)
    file_size: int = field(default=0, compare=False)  # jsonl 字节数


# 兜底反解：信息已丢失，只做粗略恢复
def _decode_dirname_fallback(dirname: str) -> str:
    if not dirname:
        return ""
    path = dirname.replace("-", "/")
    if not path.startswith("/"):
        path = "/" + path.lstrip("/")
    return path


# 从 jsonl 文件里找第一行带 cwd 字段的，返回真实 cwd
def _peek_cwd_from_jsonl(jsonl_path: Path) -> Optional[str]:
    try:
        with jsonl_path.open("r", encoding="utf-8", errors="replace") as fp:
            for line in fp:
                line = line.strip()
                if not line or '"cwd"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = obj.get("cwd")
                if isinstance(cwd, str) and cwd:
                    return cwd
    except FileNotFoundError:
        return None
    return None


# 从 jsonl 第一行尝试抽取首条 user 文本，作为摘要
def _peek_first_user_text(jsonl_path: Path, max_chars: int = 80) -> Optional[str]:
    try:
        with jsonl_path.open("r", encoding="utf-8", errors="replace") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "user":
                    continue
                msg = obj.get("message") or {}
                content = msg.get("content")
                text = _flatten_message_content(content)
                if not text:
                    continue
                text = text.strip().replace("\n", " ")
                if len(text) > max_chars:
                    text = text[:max_chars] + "…"
                return text
    except FileNotFoundError:
        return None
    return None


# 把 message.content（可能是 str 或 list[block]）压平成纯文本
def _flatten_message_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
        return "\n".join(parts)
    return ""


# 从 jsonl 读取最近 n 个 user/assistant 对，返回格式化摘要（用于恢复会话时展示上下文）
def read_recent_turns(jsonl_path: Path, n_turns: int = 3) -> str:
    turns: List[tuple] = []
    current_user: Optional[str] = None
    current_assistant_parts: List[str] = []
    try:
        with jsonl_path.open("r", encoding="utf-8", errors="replace") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = obj.get("type")
                if t == "user":
                    msg = obj.get("message") or {}
                    text = _flatten_message_content(msg.get("content")).strip()
                    if not text:
                        continue  # 纯 tool_result，跳过
                    if current_user is not None and current_assistant_parts:
                        turns.append((current_user, "\n\n".join(current_assistant_parts)))
                    current_user = text
                    current_assistant_parts = []
                elif t == "assistant":
                    content = (obj.get("message") or {}).get("content") or []
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text = (block.get("text") or "").strip()
                                if text:
                                    current_assistant_parts.append(text)
        if current_user is not None and current_assistant_parts:
            turns.append((current_user, "\n\n".join(current_assistant_parts)))
    except (FileNotFoundError, OSError):
        return ""
    if not turns:
        return ""
    recent = turns[-n_turns:]
    total = len(recent)
    parts = [f"📋 **恢复会话 · 最近 {total} 次对话**"]
    for i, (user_text, assistant_text) in enumerate(recent, 1):
        parts.append(f"**─── 第 {i}/{total} 轮 ───**\n\n**👤 用户**\n{user_text}\n\n**🤖 Claude**\n{assistant_text}")
    return "\n\n".join(parts)


# 列出所有项目，按最近活跃时间倒序
def list_projects() -> List[ProjectInfo]:
    if not CLAUDE_PROJECTS_DIR.is_dir():
        return []
    results: List[ProjectInfo] = []
    for child in CLAUDE_PROJECTS_DIR.iterdir():
        if not child.is_dir():
            continue
        jsonl_files = list(child.glob("*.jsonl"))
        if not jsonl_files:
            continue
        latest = max(f.stat().st_mtime for f in jsonl_files)
        # 优先用最新 jsonl 里的 cwd 字段；找不到再回退到目录名反解
        newest = max(jsonl_files, key=lambda f: f.stat().st_mtime)
        cwd = _peek_cwd_from_jsonl(newest) or _decode_dirname_fallback(child.name)
        if _is_tool_dir(cwd):
            continue
        results.append(ProjectInfo(
            cwd=cwd,
            name=os.path.basename(cwd) or cwd,
            encoded_dirname=child.name,
            session_count=len(jsonl_files),
            latest_mtime=latest,
        ))
    results.sort(key=lambda p: p.latest_mtime, reverse=True)
    return results


# 列出指定项目的所有历史 session，按时间倒序
def list_sessions(project: ProjectInfo, limit: int = 20) -> List[SessionInfo]:
    project_dir = CLAUDE_PROJECTS_DIR / project.encoded_dirname
    if not project_dir.is_dir():
        return []
    sessions: List[SessionInfo] = []
    for jsonl in project_dir.glob("*.jsonl"):
        st = jsonl.stat()
        sessions.append(SessionInfo(
            claude_session_id=jsonl.stem,
            cwd=project.cwd,
            mtime=st.st_mtime,
            first_user_text=_peek_first_user_text(jsonl),
            jsonl_path=jsonl,
            file_size=st.st_size,
        ))
    sessions.sort(key=lambda s: s.mtime, reverse=True)
    return sessions[:limit]


# 拿最近活跃的那个项目，没有就 None（fallback 到 $HOME 或当前目录由上层决定）
def most_recent_project() -> Optional[ProjectInfo]:
    projects = list_projects()
    return projects[0] if projects else None
