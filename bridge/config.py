"""bridge 端配置：环境变量 + 命令行覆盖（v2 远程模式）。"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class BridgeConfig:
    server_ws_url: str
    pair_token: str
    bridge_id: str
    claude_bin: str
    default_cwd: str


# 取本机主机名作为默认 bridge_id 后缀，便于识别
def _default_bridge_id() -> str:
    host = socket.gethostname().replace(".", "-")
    return f"bridge-{host}"


# 从环境变量装配；调用方可在 cli 里再行覆盖
def load(overrides: Optional[dict] = None) -> BridgeConfig:
    overrides = overrides or {}
    server_ws_url = overrides.get("server_ws_url") or os.getenv(
        "FEISHU_CC_SERVER_WS", "ws://127.0.0.1:8765/ws/bridge"
    )
    pair_token = overrides.get("pair_token") or os.getenv("FEISHU_CC_PAIR_TOKEN", "").strip()
    if not pair_token:
        raise RuntimeError("FEISHU_CC_PAIR_TOKEN 未配置（或用 --pair-token 传入）")
    bridge_id = overrides.get("bridge_id") or os.getenv("FEISHU_CC_BRIDGE_ID") or _default_bridge_id()
    claude_bin = overrides.get("claude_bin") or os.getenv("FEISHU_CC_CLAUDE_BIN", "claude")
    default_cwd = overrides.get("default_cwd") or os.getenv("FEISHU_CC_DEFAULT_CWD") or os.path.expanduser("~")
    return BridgeConfig(
        server_ws_url=server_ws_url,
        pair_token=pair_token,
        bridge_id=bridge_id,
        claude_bin=claude_bin,
        default_cwd=default_cwd,
    )
