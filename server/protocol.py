"""Bridge ↔ Server 之间走 WebSocket 的 JSON 消息协议。

所有消息为单层 JSON 对象，必须含 ``type`` 字段。为了避免引入 pydantic，
这里用 dataclasses + 显式 to_wire / from_wire 转换。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


# ========= 类型常量（防拼写错误）=========

class Up:
    """Bridge → Server 消息类型。"""
    HELLO = "hello"
    PONG = "pong"
    SESSION_STARTED = "session_started"
    SESSION_EVENT = "session_event"
    SESSION_ENDED = "session_ended"
    ERROR = "error"


class Down:
    """Server → Bridge 消息类型。"""
    WELCOME = "welcome"
    PING = "ping"
    START_SESSION = "start_session"
    SEND_PROMPT = "send_prompt"
    STOP_SESSION = "stop_session"
    SLASH = "slash"  # /resume /clear 等斜杠命令


# ========= 工具函数 =========

# 生成短随机 id，前缀方便日志识别
def new_id(prefix: str = "") -> str:
    raw = uuid.uuid4().hex[:10]
    return f"{prefix}{raw}" if prefix else raw


# 统一序列化：UTF-8 + 紧凑
def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# 解析 WebSocket 文本帧，失败抛 ValueError
def loads(raw: str) -> Dict[str, Any]:
    data = json.loads(raw)
    if not isinstance(data, dict) or "type" not in data:
        raise ValueError("invalid frame: must be JSON object with 'type'")
    return data


# 把 dataclass 编码为 WebSocket 帧字符串
def to_wire(msg: Any) -> str:
    return dumps(asdict(msg))


# ========= Bridge → Server =========

@dataclass
class HelloMsg:
    """本地 bridge 启动后第一帧，配对鉴权。"""
    bridge_id: str
    pair_token: str
    host: str
    version: str
    type: str = Up.HELLO


@dataclass
class SessionStartedMsg:
    """bridge 成功 spawn 一个 claude 进程后回执。"""
    request_id: str
    session_id: str
    cwd: str
    claude_session_id: Optional[str] = None
    type: str = Up.SESSION_STARTED


@dataclass
class SessionEventMsg:
    """转发自 claude stream-json 的单条事件。"""
    session_id: str
    event: Dict[str, Any]
    type: str = Up.SESSION_EVENT


@dataclass
class SessionEndedMsg:
    """claude 进程退出。"""
    session_id: str
    reason: str
    exit_code: Optional[int] = None
    type: str = Up.SESSION_ENDED


@dataclass
class ErrorMsg:
    """通用错误回报。"""
    detail: str
    related_id: Optional[str] = None
    type: str = Up.ERROR


# ========= Server → Bridge =========

@dataclass
class WelcomeMsg:
    server_version: str = "0.1.0"
    type: str = Down.WELCOME


@dataclass
class StartSessionCmd:
    request_id: str
    cwd: Optional[str] = None
    resume_claude_session: Optional[str] = None
    continue_last: bool = False
    extra_args: List[str] = field(default_factory=list)
    type: str = Down.START_SESSION


@dataclass
class SendPromptCmd:
    session_id: str
    prompt: str
    type: str = Down.SEND_PROMPT


@dataclass
class StopSessionCmd:
    session_id: str
    type: str = Down.STOP_SESSION


@dataclass
class SlashCmd:
    session_id: str
    command: str
    args: Dict[str, Any] = field(default_factory=dict)
    type: str = Down.SLASH


# 心跳起始时间（外部如果要算 latency 用得上）
def now_ts() -> float:
    return time.time()
