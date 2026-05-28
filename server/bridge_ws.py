"""v2 远程模式预留：aiohttp WebSocket 服务，接受本地 bridge 客户端连接。

⚠️ v1 不使用本文件。当前 main.py 走 LocalExecutor 单进程，远程模式要等
v2 把这里改造为 RemoteExecutor 的服务端，并提供配套的 manager API。
保留是为了 v2 复用握手 / 重连 / 协议帧这套已经写好的逻辑。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Awaitable, Callable

from aiohttp import WSMsgType, web

from server.config import ServerConfig

logger = logging.getLogger(__name__)

HELLO_TIMEOUT_SEC = 10
PING_INTERVAL_SEC = 30


# 协议帧类型常量（v1 protocol.py 已删，这里就近内联避免外部依赖）
class _Up:
    HELLO = "hello"
    ERROR = "error"


def _new_id(prefix: str = "") -> str:
    raw = uuid.uuid4().hex[:10]
    return f"{prefix}{raw}" if prefix else raw


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _loads(raw: str) -> dict:
    data = json.loads(raw)
    if not isinstance(data, dict) or "type" not in data:
        raise ValueError("invalid frame: must be JSON object with 'type'")
    return data


# 创建 aiohttp app；v2 需要 manager 实现 register_remote_bridge / unregister / handle_uplink
def build_app(config: ServerConfig, manager) -> web.Application:
    app = web.Application()
    app["config"] = config
    app["manager"] = manager
    app.router.add_get("/ws/bridge", _ws_handler)
    app.router.add_get("/health", _health)
    return app


async def _health(_: web.Request) -> web.Response:
    return web.Response(text="ok")


# 单条 WebSocket 连接的生命周期
async def _ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=PING_INTERVAL_SEC, max_msg_size=4 * 1024 * 1024)
    await ws.prepare(request)

    config: ServerConfig = request.app["config"]
    manager = request.app["manager"]

    bridge_id = ""
    try:
        bridge_id = await _do_handshake(ws, config)
        if not bridge_id:
            return ws

        send: Callable[[str], Awaitable[None]] = ws.send_str
        # v2 TODO: manager.register_remote_bridge(bridge_id, host, send)
        await ws.send_str(_dumps({"type": "welcome", "server_version": "0.1.0"}))
        logger.info("bridge %s 接入成功 from=%s", bridge_id, request.remote)

        await _pump_inbound(ws, manager, bridge_id)
    except asyncio.TimeoutError:
        logger.warning("bridge hello 超时，断开 from=%s", request.remote)
    except Exception:
        logger.exception("ws handler 异常")
    finally:
        if bridge_id:
            # v2 TODO: await manager.unregister_remote_bridge(bridge_id)
            logger.info("bridge %s 已断开", bridge_id)
    return ws


# 等待并校验 hello 帧
async def _do_handshake(ws: web.WebSocketResponse, config: ServerConfig) -> str:
    msg = await asyncio.wait_for(ws.receive(), timeout=HELLO_TIMEOUT_SEC)
    if msg.type != WSMsgType.TEXT:
        await _close_with_error(ws, "首帧必须是 TEXT")
        return ""
    try:
        data = _loads(msg.data)
    except ValueError as e:
        await _close_with_error(ws, f"hello 解析失败：{e}")
        return ""
    if data.get("type") != _Up.HELLO:
        await _close_with_error(ws, "首帧 type 必须是 hello")
        return ""
    if not config.is_pair_token_valid(data.get("pair_token", "")):
        await _close_with_error(ws, "pair_token 不通过")
        return ""
    bridge_id = (data.get("bridge_id") or "").strip()
    if not bridge_id:
        await _close_with_error(ws, "缺少 bridge_id")
        return ""
    return bridge_id


# 主循环：v2 由 manager.handle_remote_uplink 消费
async def _pump_inbound(ws: web.WebSocketResponse, manager, bridge_id: str) -> None:
    async for msg in ws:
        if msg.type == WSMsgType.TEXT:
            try:
                data = _loads(msg.data)
            except ValueError as e:
                logger.warning("bridge %s 发来无效帧：%s", bridge_id, e)
                continue
            # v2 TODO: await manager.handle_remote_uplink(bridge_id, data)
            logger.debug("uplink ignored (v2 not wired): %s", data)
        elif msg.type == WSMsgType.ERROR:
            logger.warning("ws 出错 bridge=%s: %s", bridge_id, ws.exception())
            break


async def _close_with_error(ws: web.WebSocketResponse, detail: str) -> None:
    try:
        await ws.send_str(_dumps({"type": "error", "detail": detail}))
    finally:
        await ws.close()
