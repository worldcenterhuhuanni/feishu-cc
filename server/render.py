"""Claude stream-json 事件 → 飞书消息文本。

职责单一：格式化。不负责发送，不负责会话状态。
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple

_LANG_MAP: Dict[str, str] = {
    "py": "python", "js": "javascript", "ts": "typescript",
    "tsx": "typescript", "jsx": "javascript",
    "md": "markdown", "json": "json",
    "yaml": "yaml", "yml": "yaml", "toml": "toml",
    "sh": "bash", "bash": "bash",
    "go": "go", "rs": "rust", "java": "java",
    "cpp": "cpp", "c": "c", "html": "html", "css": "css",
    "sql": "sql", "xml": "xml",
}

# Bash 常用命令 → 中文简称（便于飞书显示，无中文时原样保留）
_BASH_CMD_LABELS: Dict[str, str] = {
    "grep": "搜索", "find": "查找", "ls": "列目录", "cat": "读取",
    "git": "git", "python": "运行", "python3": "运行",
    "node": "运行", "npm": "npm", "npx": "npx",
    "uv": "uv", "pip": "pip", "pip3": "pip",
    "curl": "请求", "wget": "下载",
    "mkdir": "建目录", "rm": "删除", "mv": "移动", "cp": "复制",
    "chmod": "权限", "sed": "编辑", "awk": "处理",
    "echo": "输出", "which": "查路径", "head": "读头", "tail": "读尾",
    "wc": "统计", "sort": "排序", "uniq": "去重",
    "ps": "进程", "kill": "终止", "pkill": "终止",
}


# 从 assistant 事件提取纯文本块（合并多段文本）
def extract_text(event: Dict[str, Any]) -> Optional[str]:
    content = (event.get("message") or {}).get("content") or []
    if not isinstance(content, list):
        return None
    parts = [
        (b.get("text") or "").strip()
        for b in content
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    parts = [p for p in parts if p]
    return "\n\n".join(parts) if parts else None


def _tool_input_hint(name: str, input_data: dict) -> str:
    """返回工具调用的简短输入摘要（纯文本）。"""
    if name in ("Bash", "PowerShell") and "command" in input_data:
        cmd = str(input_data["command"]).split("\n")[0].strip()
        # 取第一个单词作为子命令
        first_word = cmd.split()[0] if cmd.split() else cmd
        # 去掉路径前缀（/usr/bin/grep → grep）
        bin_name = first_word.split("/")[-1]
        label = _BASH_CMD_LABELS.get(bin_name, bin_name)
        # 尝试找最后一个看起来像路径或模式的参数（不以 - 开头）
        args = cmd.split()
        target = ""
        for a in reversed(args[1:]):
            if not a.startswith("-"):
                # 缩短路径，只保留最后两段
                parts = a.replace("\\", "/").split("/")
                target = "/".join(parts[-2:]) if len(parts) > 2 else a
                target = target[:30]
                break
        return f"  {label}" + (f"  {target}" if target else "")
    if name in ("Read", "Write", "Edit", "NotebookEdit") and "file_path" in input_data:
        fpath = str(input_data["file_path"])
        parts = fpath.split("/")
        # 显示最后两段路径（父目录/文件名）
        fname = "/".join(parts[-2:]) if len(parts) > 2 else parts[-1]
        return f"  {fname}"
    if name == "Glob" and "pattern" in input_data:
        return f"  {str(input_data['pattern'])[:35]}"
    if name == "Grep" and "pattern" in input_data:
        pat = str(input_data['pattern'])[:25]
        path = str(input_data.get('path', '') or input_data.get('include', ''))[:20]
        return f"  {pat}" + (f"  {path}" if path else "")
    if name == "WebSearch" and "query" in input_data:
        return f"  {str(input_data['query'])[:35]}"
    if name == "WebFetch" and "url" in input_data:
        url = str(input_data["url"]).replace("https://", "").replace("http://", "")[:40]
        return f"  {url}"
    if name == "Agent" and "description" in input_data:
        return f"  {str(input_data['description'])[:35]}"
    if name == "TaskCreate" and "subject" in input_data:
        return f"  {str(input_data['subject'])[:40]}"
    if name == "TaskUpdate":
        task_id = str(input_data.get("taskId", ""))
        status = input_data.get("status", "")
        emoji = {"in_progress": "⏳", "completed": "✅", "deleted": "🗑️"}.get(status, "→")
        return f"  {emoji} #{task_id}" if task_id else ""
    return ""


def extract_tool_uses(event: Dict[str, Any]) -> List[Tuple[str, str, str, dict]]:
    """从 assistant 事件提取工具调用列表。返回 [(tool_id, name, hint, raw_input), ...]。"""
    content = (event.get("message") or {}).get("content") or []
    if not isinstance(content, list):
        return []
    results = []
    for b in content:
        if isinstance(b, dict) and b.get("type") == "tool_use":
            tool_id = b.get("id") or f"unknown_{time.monotonic()}"
            name = b.get("name") or "tool"
            raw_input = b.get("input") or {}
            hint = _tool_input_hint(name, raw_input)
            results.append((tool_id, name, hint, raw_input))
    return results


def extract_tool_result_ids(event: Dict[str, Any]) -> List[str]:
    """从 user 事件提取已完成的 tool_use_id 列表（顺序对应）。"""
    content = (event.get("message") or {}).get("content") or []
    if not isinstance(content, list):
        return []
    return [
        b["tool_use_id"]
        for b in content
        if isinstance(b, dict) and b.get("type") == "tool_result"
        and b.get("tool_use_id")
    ]


def extract_tool_results(event: Dict[str, Any]) -> List[Tuple[str, str]]:
    """从 user 事件提取 tool_result 内容。返回 [(tool_use_id, text), ...]。"""
    content = (event.get("message") or {}).get("content") or []
    if not isinstance(content, list):
        return []
    results = []
    for b in content:
        if not (isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id")):
            continue
        rc = b.get("content") or ""
        if isinstance(rc, list):
            text_parts = [
                x.get("text", "") for x in rc
                if isinstance(x, dict) and x.get("type") == "text"
            ]
            rc = " ".join(text_parts)
        results.append((b["tool_use_id"], str(rc)))
    return results


# 回合终结事件 → 简短统计
def extract_result_summary(event: Dict[str, Any]) -> str:
    subtype = event.get("subtype") or "done"
    cost = event.get("total_cost_usd")
    duration_ms = event.get("duration_ms")
    parts = ["✅ 完成"]
    if subtype not in ("success", "done"):
        parts[0] = f"✅ 完成（{subtype}）"
    if duration_ms is not None:
        parts.append(f"{duration_ms / 1000:.1f}s")
    if cost is not None:
        parts.append(f"${cost:.4f}")
    return " · ".join(parts)


def file_lang(file_path: str) -> str:
    ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""
    return _LANG_MAP.get(ext, "")


def extract_file_buttons(event: Dict[str, Any]) -> List[Dict[str, str]]:
    """从 assistant 事件提取可懒加载查看的文件/URL 按钮元数据（不含内容）。"""
    content = (event.get("message") or {}).get("content") or []
    if not isinstance(content, list):
        return []
    buttons: List[Dict[str, str]] = []
    for b in content:
        if not (isinstance(b, dict) and b.get("type") == "tool_use"):
            continue
        name = b.get("name") or ""
        inp = b.get("input") or {}
        if name in ("Read", "Write", "Edit", "NotebookEdit") and "file_path" in inp:
            fpath = str(inp["file_path"])
            fname = fpath.split("/")[-1]
            buttons.append({"label": f"📄 {fname}", "cc_action": "show_file", "file_path": fpath})
        elif name == "WebFetch" and "url" in inp:
            url = str(inp["url"])
            short = url.replace("https://", "").replace("http://", "")[:35]
            buttons.append({"label": f"🌐 {short}", "cc_action": "show_url", "url": url})
    return buttons


# ─── Markdown → 飞书卡片 elements 转换 ───────────────────────────────────────

def _parse_pipe_table(lines: List[str], start: int) -> Tuple[Optional[dict], int]:
    """从 lines[start] 开始尝试解析一个 pipe table，返回 (feishu_table_element, next_line_idx)。
    如果不是合法 table 返回 (None, start)。
    """
    if start >= len(lines):
        return None, start

    def split_row(line: str) -> List[str]:
        cells = [c.strip() for c in line.strip().split("|")]
        if cells and cells[0] == "":
            cells = cells[1:]
        if cells and cells[-1] == "":
            cells = cells[:-1]
        return cells

    def is_separator(line: str) -> bool:
        return bool(re.match(r"^\|[\s\-|:]+\|?\s*$", line.strip()))

    # 先找 header 行（非分隔行）和紧随其后的分隔行
    if is_separator(lines[start]):
        return None, start

    header_cells = split_row(lines[start])
    if len(header_cells) < 1 or not lines[start].strip().startswith("|"):
        return None, start

    # 下一行必须是分隔行
    sep_idx = start + 1
    if sep_idx >= len(lines) or not is_separator(lines[sep_idx]):
        return None, start

    # 收集数据行
    data_rows = []
    idx = sep_idx + 1
    while idx < len(lines) and lines[idx].strip().startswith("|"):
        row_cells = split_row(lines[idx])
        if row_cells:
            data_rows.append(row_cells)
        idx += 1

    if not data_rows:
        return None, start

    columns = [
        {
            "name": f"col{i}",
            "display_name": h,
            "data_type": "lark_md",
            "width": "auto",
        }
        for i, h in enumerate(header_cells)
    ]
    rows = []
    for cells in data_rows:
        row: Dict[str, str] = {}
        for i, cell in enumerate(cells):
            if i < len(header_cells):
                row[f"col{i}"] = cell
        # 补全空缺列
        for i in range(len(header_cells)):
            if f"col{i}" not in row:
                row[f"col{i}"] = ""
        if row:
            rows.append(row)

    table_el = {
        "tag": "table",
        "page_size": 99,
        "header_style": {"background_style": "grey", "bold": True, "lines": 1},
        "columns": columns,
        "rows": rows,
    }
    return table_el, idx


def markdown_to_feishu_elements(text: str) -> List[dict]:
    """把 Markdown 文本转换为飞书卡片 elements 列表。
    pipe table 转为原生 table element，其余段落按 markdown element 渲染。
    """
    lines = text.split("\n")
    elements: List[dict] = []
    buf: List[str] = []
    i = 0

    def flush_buf():
        chunk = "\n".join(buf).strip()
        if chunk:
            elements.append({"tag": "markdown", "content": chunk})
        buf.clear()

    while i < len(lines):
        line = lines[i]
        # 尝试解析 table（当前行以 | 开头且下一行是分隔行）
        if line.strip().startswith("|"):
            table_el, next_i = _parse_pipe_table(lines, i)
            if table_el is not None:
                flush_buf()
                elements.append(table_el)
                i = next_i
                continue
        buf.append(line)
        i += 1

    flush_buf()
    return elements if elements else [{"tag": "markdown", "content": text}]
