"""把飞书来的文本解析为命令并执行。

选择项目和会话通过向导流程完成：
    projects → [输入项目编号] → [输入会话编号 / 0 新建] → 开始对话
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional

from server import project_scanner
from server.manager import CCBridgeManager
from server.project_scanner import ProjectInfo, SessionInfo

logger = logging.getLogger(__name__)


def _git_root(path: str) -> str:
    """返回 path 所在 git 仓库的根目录；不在 git 仓库时原路返回。"""
    try:
        root = subprocess.check_output(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            timeout=3,
        ).decode().strip()
        return root if root else path
    except Exception:
        return path

HELP_TEXT = (
    "📖 feishu-cc 命令：\n"
    "`projects` 列本机 claude 项目，按编号进入会话向导\n"
    "`start [路径]` 在指定路径（默认最近项目）新建会话\n"
    "`list` 列出当前活跃会话\n"
    "`use <session_id>` 切换默认会话\n"
    "`stop [session_id]` 关闭会话\n"
    "`help` 显示本帮助\n"
    "Claude 内置斜杠命令（如 `/compact`）→ 发给当前会话\n"
    "其他任意文本 → 发给当前会话"
)


@dataclass
class _UserState:
    """用户的向导状态与列表缓存。"""
    state: Literal["idle", "awaiting_project", "awaiting_session"] = "idle"
    projects: List[ProjectInfo] = field(default_factory=list)
    sessions: List[SessionInfo] = field(default_factory=list)
    selected_project: Optional[ProjectInfo] = None


_user_states: Dict[str, _UserState] = {}


def _state(open_id: str) -> _UserState:
    return _user_states.setdefault(open_id, _UserState())


def _humanize(ts: float) -> str:
    delta = _dt.datetime.now().timestamp() - ts
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    return _dt.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def _fmt_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f}MB"
    if n >= 1024:
        return f"{n / 1024:.0f}KB"
    return f"{n}B"


# ===== 入口分发 =====

async def dispatch(manager: CCBridgeManager, open_id: str, chat_id: str, text: str) -> None:
    text = text.strip()
    if not text:
        return

    # 优先处理权限响应（不区分大小写，支持中英文 + 数字）
    low = text.lower()
    if low in ("允许", "y", "yes", "1"):
        if await manager.resolve_permission(open_id, allow=True):
            return
    elif low in ("始终允许", "2"):
        if await manager.resolve_permission(open_id, allow=True, always=True):
            return
    elif low in ("拒绝", "n", "no", "3"):
        if await manager.resolve_permission(open_id, allow=False):
            return

    st = _state(open_id)

    # 向导进行中：数字直接交给向导处理
    if st.state != "idle" and text.isdigit():
        await _handle_wizard(manager, open_id, chat_id, int(text), st)
        return

    # 非数字输入取消向导
    if st.state != "idle":
        st.state = "idle"

    sub, args = _parse(text)
    if sub is not None:
        handler = _ROUTES.get(sub, _cmd_help)
        try:
            await handler(manager, open_id, chat_id, args)
        except Exception:
            logger.exception("命令处理失败 sub=%s", sub)
            await manager._notify(chat_id, f"❌ 命令 `{sub}` 处理异常，详见 server 日志")
        return

    await manager.send_prompt(open_id, chat_id, text)


# ===== 向导步骤 =====

async def _handle_wizard(
    manager: CCBridgeManager, open_id: str, chat_id: str, num: int, st: _UserState
) -> None:
    if st.state == "awaiting_project":
        idx = num - 1
        if not (0 <= idx < len(st.projects)):
            await manager._notify(chat_id, f"❌ 编号 `{num}` 超出范围，重新发 `projects` 查看列表")
            st.state = "idle"
            return
        proj = st.projects[idx]
        st.selected_project = proj
        sessions = project_scanner.list_sessions(proj)
        st.sessions = sessions
        st.state = "awaiting_session"
        lines = [f"📜 **{proj.name}** 的历史会话：", "**0.** 新建会话"]
        for i, s in enumerate(sessions, start=1):
            preview = s.first_user_text or "（无内容）"
            size = _fmt_size(s.file_size) if s.file_size else ""
            size_part = f" · {size}" if size else ""
            lines.append(f"**{i}.** {_humanize(s.mtime)}{size_part} · {preview}")
        lines.append("\n输入编号选择，其他内容取消")
        await manager._notify(chat_id, "\n".join(lines))

    elif st.state == "awaiting_session":
        proj = st.selected_project
        st.state = "idle"
        cwd = proj.cwd  # project_scanner 已记录真实 cwd，不做 git_root 上移
        if num == 0:
            await manager.start_session(open_id, chat_id, cwd=cwd)
        else:
            idx = num - 1
            if not (0 <= idx < len(st.sessions)):
                await manager._notify(chat_id, f"❌ 编号 `{num}` 超出范围，重新发 `projects` 开始")
                return
            sess = st.sessions[idx]
            if sess.jsonl_path:
                history = project_scanner.read_recent_turns(sess.jsonl_path, n_turns=3)
                if history:
                    await manager._notify(chat_id, history)
            await manager.start_session(
                open_id, chat_id,
                cwd=cwd,
                resume_claude_session=sess.claude_session_id,
            )


# ===== 命令解析 =====

def _parse(text: str) -> tuple[Optional[str], List[str]]:
    parts = _safe_split(text)
    if not parts:
        return None, []
    head = parts[0].lower()
    if head == "/cc":
        return (parts[1].lower(), parts[2:]) if len(parts) > 1 else ("help", [])
    if head.startswith("/") and len(head) > 1:
        slash_cmd = head[1:]
        if slash_cmd in _ROUTES:
            return slash_cmd, parts[1:]
        return None, []
    if head in _ROUTES:
        return head, parts[1:]
    return None, []


def _safe_split(text: str) -> List[str]:
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


# ===== 子命令实现 =====

async def _cmd_projects(m: CCBridgeManager, open_id: str, chat_id: str, _: List[str]) -> None:
    st = _state(open_id)
    projects = project_scanner.list_projects()
    if not projects:
        await m._notify(chat_id, "（本机还没有 claude 项目）")
        return
    st.projects = projects
    st.state = "awaiting_project"
    lines = ["📂 本机 claude 项目（按最近活跃排序）："]
    for i, p in enumerate(projects[:20], start=1):
        lines.append(
            f"**{i}.** **{p.name}** · {p.session_count} 会话 · {_humanize(p.latest_mtime)}\n"
            f"   `{p.cwd}`"
        )
    lines.append("\n输入编号选择项目")
    await m._notify(chat_id, "\n".join(lines))


async def _cmd_start(m: CCBridgeManager, open_id: str, chat_id: str, args: List[str]) -> None:
    if args:
        cwd = _git_root(os.path.expanduser(args[0]))
    else:
        recent = project_scanner.most_recent_project()
        cwd = recent.cwd if recent else os.path.expanduser("~")
    await m.start_session(open_id, chat_id, cwd=cwd)


async def _cmd_list(m: CCBridgeManager, open_id: str, chat_id: str, _: List[str]) -> None:
    sessions = m.list_sessions(open_id)
    if not sessions:
        await m._notify(chat_id, "（没有活动会话）")
        return
    lines = ["🗂 你的活跃会话："]
    for s in sessions:
        lines.append(f"- `{s.session_id}` cwd=`{s.cwd}`")
    await m._notify(chat_id, "\n".join(lines))


async def _cmd_use(m: CCBridgeManager, open_id: str, chat_id: str, args: List[str]) -> None:
    if not args:
        await m._notify(chat_id, "用法：`use <session_id>`")
        return
    await m.use_session(open_id, chat_id, args[0])


async def _cmd_stop(m: CCBridgeManager, open_id: str, chat_id: str, args: List[str]) -> None:
    sid = args[0] if args else None
    await m.stop_session(open_id, chat_id, session_id=sid)


async def _cmd_help(m: CCBridgeManager, _open_id: str, chat_id: str, _: List[str]) -> None:
    await m._notify(chat_id, HELP_TEXT)


_ROUTES = {
    "projects": _cmd_projects,
    "start":    _cmd_start,
    "list":     _cmd_list,
    "ls":       _cmd_list,
    "use":      _cmd_use,
    "stop":     _cmd_stop,
    "kill":     _cmd_stop,
    "help":     _cmd_help,
}
