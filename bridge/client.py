"""v2 远程模式：bridge 主流程，连 server、起 claude runner、双向转发。

会话表 ``self._runners`` 在内存即可；bridge 进程崩了用户会感知到。
"""

from __future__ import annotations

import asyncio
import logging
import platform
from dataclasses import dataclass
from typing import Dict, Optional

import aiohttp

from bridge.config import BridgeConfig
from bridge.runner import ClaudeRunner, StartOptions

logger = logging.getLogger(__name__)

RECONNECT_DELAYS = [1, 2, 5, 10, 20, 30]
BRIDGE_VERSION = "0.1.0"


# bridge → server 帧构造
def _hello(cfg: BridgeConfig) -> dict:
    return {
        "type": "hello",
        "bridge_id": cfg.bridge_id,
        "pair_token": cfg.pair_token,
        "host": platform.node(),
        "version": BRIDGE_VERSION,
    }


def _session_started(request_id: str, session_id: str, cwd: str) -> dict:
    return {"type": "session_started", "request_id": request_id, "session_id": session_id, "cwd": cwd}


def _session_event(session_id: str, event: dict) -> dict:
    return {"type": "session_event", "session_id": session_id, "event": event}


def _session_ended(session_id: str, reason: str, exit_code: Optional[int]) -> dict:
    return {"type": "session_ended", "session_id": session_id, "reason": reason, "exit_code": exit_code}


@dataclass
class _Runner:
    runner: ClaudeRunner
    cwd: str


class BridgeClient:
    """WebSocket 长连 + 会话管理。"""

    def __init__(self, config: BridgeConfig) -> None:
        self._config = config
        self._runners: Dict[str, _Runner] = {}
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._send_lock = asyncio.Lock()

    # 入口：阻塞运行，永不返回
    async def run_forever(self) -> None:
        attempt = 0
        while True:
            try:
                await self._connect_and_serve()
                attempt = 0
            except Exception:
                logger.exception("ws 连接异常")
            delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
            attempt += 1
            logger.info("将在 %ds 后重连", delay)
            await asyncio.sleep(delay)

    # 单次连接生命周期
    async def _connect_and_serve(self) -> None:
        async with aiohttp.ClientSession() as session:
            logger.info("连接 %s", self._config.server_ws_url)
            async with session.ws_connect(self._config.server_ws_url, heartbeat=30) as ws:
                self._ws = ws
                await self._safe_send(_hello(self._config))
                logger.info("已发送 hello，bridge_id=%s", self._config.bridge_id)
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self._on_downlink(msg.json())
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        logger.warning("ws 错误: %s", ws.exception())
                        break
        self._ws = None
        await self._stop_all_runners("ws disconnected")

    # 串行发送，避免并发写同一条 ws
    async def _safe_send(self, payload: dict) -> None:
        if self._ws is None or self._ws.closed:
            return
        async with self._send_lock:
            await self._ws.send_json(payload)

    # 下行命令分发
    async def _on_downlink(self, msg: dict) -> None:
        mtype = msg.get("type")
        if mtype == "welcome":
            logger.info("server welcome: %s", msg)
        elif mtype == "start_session":
            await self._handle_start_session(msg)
        elif mtype == "send_prompt":
            await self._handle_send_prompt(msg)
        elif mtype == "stop_session":
            await self._handle_stop_session(msg)
        elif mtype == "ping":
            await self._safe_send({"type": "pong"})
        else:
            logger.debug("忽略未知 downlink: %s", mtype)

    # 启动新 runner
    async def _handle_start_session(self, msg: dict) -> None:
        request_id = msg.get("request_id") or ""
        cwd = msg.get("cwd") or self._config.default_cwd
        session_id = f"sess_{request_id[:8] or 'x'}"

        async def on_event(event: dict) -> None:
            await self._safe_send(_session_event(session_id, event))

        async def on_exit(code: Optional[int], reason: str) -> None:
            self._runners.pop(session_id, None)
            await self._safe_send(_session_ended(session_id, reason, code))

        runner = ClaudeRunner(self._config.claude_bin, on_event=on_event, on_exit=on_exit)
        try:
            await runner.start(StartOptions(
                cwd=cwd,
                resume_claude_session=msg.get("resume_claude_session"),
                continue_last=bool(msg.get("continue_last")),
                extra_args=list(msg.get("extra_args") or []),
            ))
        except Exception as e:
            logger.exception("启动 claude 失败")
            await self._safe_send({"type": "error", "detail": f"启动失败: {e}", "related_id": request_id})
            return

        self._runners[session_id] = _Runner(runner=runner, cwd=cwd)
        await self._safe_send(_session_started(request_id, session_id, cwd))

    # 转发 prompt
    async def _handle_send_prompt(self, msg: dict) -> None:
        sid = msg.get("session_id") or ""
        prompt = msg.get("prompt") or ""
        entry = self._runners.get(sid)
        if not entry:
            await self._safe_send({"type": "error", "detail": f"未知 session_id: {sid}"})
            return
        try:
            await entry.runner.send_prompt(prompt)
        except Exception as e:
            await self._safe_send({"type": "error", "detail": f"prompt 写入失败: {e}"})

    # 关闭单个 runner
    async def _handle_stop_session(self, msg: dict) -> None:
        sid = msg.get("session_id") or ""
        entry = self._runners.get(sid)
        if entry:
            await entry.runner.stop()

    # 重连前清理
    async def _stop_all_runners(self, reason: str) -> None:
        for sid in list(self._runners.keys()):
            entry = self._runners.pop(sid, None)
            if entry:
                try:
                    await entry.runner.stop()
                except Exception:
                    logger.exception("清理 runner 失败 sid=%s", sid)
