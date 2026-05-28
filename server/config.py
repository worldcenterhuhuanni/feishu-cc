"""集中读取环境变量，避免到处 os.getenv。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Set

from dotenv import load_dotenv

load_dotenv()


# 把逗号分隔字符串切成 set，自动去空白
def _split_csv(value: str) -> Set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


@dataclass(frozen=True)
class ServerConfig:
    """运行时配置；由 ``load()`` 从环境变量装配。"""
    feishu_app_id: str
    feishu_app_secret: str
    feishu_connection_mode: str  # websocket | webhook
    feishu_domain: str           # feishu | lark

    bridge_ws_host: str
    bridge_ws_port: int
    bridge_pair_tokens: Set[str]

    feishu_allowed_users: Set[str] = field(default_factory=set)
    log_level: str = "INFO"
    # 权限请求历史记录模式：True=决策后保留权限区，False=决策后消失（默认）
    perm_keep_history: bool = False

    # 是否允许这个 open_id 使用 /cc 命令
    def is_user_allowed(self, open_id: str) -> bool:
        if not self.feishu_allowed_users:
            return True
        return open_id in self.feishu_allowed_users

    # 配对 token 是否有效
    def is_pair_token_valid(self, token: str) -> bool:
        return token in self.bridge_pair_tokens


# 从环境变量装配配置；缺关键项直接抛错
def load() -> ServerConfig:
    app_id = os.getenv("FEISHU_APP_ID", "").strip()
    app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        raise RuntimeError("FEISHU_APP_ID / FEISHU_APP_SECRET 未配置")

    pair_tokens = _split_csv(os.getenv("BRIDGE_PAIR_TOKENS", ""))
    if not pair_tokens:
        raise RuntimeError("BRIDGE_PAIR_TOKENS 不能为空，否则 bridge 无法配对")

    return ServerConfig(
        feishu_app_id=app_id,
        feishu_app_secret=app_secret,
        feishu_connection_mode=os.getenv("FEISHU_CONNECTION_MODE", "websocket").strip(),
        feishu_domain=os.getenv("FEISHU_DOMAIN", "feishu").strip(),
        bridge_ws_host=os.getenv("BRIDGE_WS_HOST", "0.0.0.0").strip(),
        bridge_ws_port=int(os.getenv("BRIDGE_WS_PORT", "8765")),
        bridge_pair_tokens=pair_tokens,
        feishu_allowed_users=_split_csv(os.getenv("FEISHU_ALLOWED_USERS", "")),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        perm_keep_history=os.getenv("FEISHU_CC_PERM_KEEP_HISTORY", "false").strip().lower() == "true",
    )
