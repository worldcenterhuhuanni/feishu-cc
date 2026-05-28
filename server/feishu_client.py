"""飞书事件接入 + 出站发送。

只依赖 ``lark-oapi``。对外暴露：
- :class:`FeishuClient`：start() / stop() / send_text() 三个动作；
- 注入回调 ``on_user_text(open_id, chat_id, text)`` 处理入站文本。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from typing import Any, Awaitable, Callable, Optional

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    P2ImChatAccessEventBotP2pChatEnteredV1,
    P2ImMessageMessageReadV1,
    P2ImMessageReceiveV1,
    PatchMessageRequest,
    PatchMessageRequestBody,
)
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

from server.config import ServerConfig

logger = logging.getLogger(__name__)

# 覆盖飞书服务端握手后推送的重连参数
_WS_RECONNECT_NONCE = 0       # 断线后立刻重连，不随机等待
_WS_RECONNECT_INTERVAL = 5    # 重连失败后 5s 重试
# websockets 协议级 ping 参数：每 30s 一次，10s 内无 pong 则判定连接死亡
_WS_PING_INTERVAL = 30
_WS_PING_TIMEOUT = 10

# 文本里 @机器人 后留下的占位符样式 "@_user_1"
_MENTION_PATTERN = re.compile(r"@_user_\d+\s*")

# 入站文本回调类型
OnUserText = Callable[[str, str, str], Awaitable[None]]

# 卡片按钮点击回调：fn(open_id, chat_id, message_id, value_dict)
OnCardAction = Callable[[str, str, str, dict], Awaitable[None]]


class FeishuClient:
    """飞书 SDK 的薄封装：订阅消息 + 发消息。"""

    def __init__(self, config: ServerConfig, on_user_text: OnUserText,
                 on_card_action: Optional[OnCardAction] = None) -> None:
        self._config = config
        self._on_user_text = on_user_text
        self._on_card_action = on_card_action
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._client = self._build_api_client()
        self._ws_client: Optional[lark.ws.Client] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._ws_thread_loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws_reconnecting_since: Optional[float] = None
        self._ws_handler: Optional[Any] = None

    # 构建用于「主动发消息」的 API client
    def _build_api_client(self) -> lark.Client:
        domain = lark.FEISHU_DOMAIN if self._config.feishu_domain == "feishu" else lark.LARK_DOMAIN
        return (
            lark.Client.builder()
            .app_id(self._config.feishu_app_id)
            .app_secret(self._config.feishu_app_secret)
            .domain(domain)
            .log_level(lark.LogLevel.INFO)  # 调高便于看连接状态
            .build()
        )

    # 启动 websocket 长连，绑定事件 handler
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._ws_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message_receive)
            .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(self._on_p2p_chat_entered)
            .register_p2_im_message_message_read_v1(self._on_message_read)
            .register_p2_card_action_trigger(self._on_card_action_trigger)
            .build()
        )
        self._ws_client = lark.ws.Client(
            app_id=self._config.feishu_app_id,
            app_secret=self._config.feishu_app_secret,
            event_handler=self._ws_handler,
            log_level=lark.LogLevel.INFO,
        )
        self._patch_ws_disconnect()
        self._ws_client.on_reconnecting = self._on_ws_reconnecting
        self._ws_client.on_reconnected = self._on_ws_reconnected
        self._start_ws_thread()
        asyncio.create_task(self._ws_watchdog())

    def _patch_ws_disconnect(self) -> None:
        """给 ws SDK 的 _disconnect 加 5s 超时，防止 conn.close() 无限阻塞。"""
        ws = self._ws_client

        async def _disconnect_with_timeout() -> None:
            acquired = False
            try:
                await ws._lock.acquire()
                acquired = True
                if ws._conn is None:
                    return
                conn_url = ws._conn_url
                try:
                    await asyncio.wait_for(ws._conn.close(), timeout=5.0)
                    logger.info("[ws] 已断开 %s", conn_url)
                except asyncio.TimeoutError:
                    logger.warning("[ws] conn.close() 超时（5s），强制断开 %s", conn_url)
                except Exception as e:
                    logger.debug("[ws] conn.close() 异常: %s", e)
            finally:
                ws._conn = None
                ws._conn_url = ""
                ws._conn_id = ""
                ws._service_id = ""
                if acquired:
                    ws._lock.release()

        ws._disconnect = _disconnect_with_timeout

    def _start_ws_thread(self) -> None:
        self._ws_thread = threading.Thread(
            target=self._run_ws_in_dedicated_loop,
            name="feishu-ws",
            daemon=True,
        )
        self._ws_thread.start()
        logger.info("飞书 websocket 客户端线程已启动（等待 lark 连接日志…）")

    def _run_ws_in_dedicated_loop(self) -> None:
        """在专属 event loop 中运行 WS 客户端，覆盖服务端推送的重连参数。

        关键：替换 lark_oapi.ws.client 的模块级 loop 单例，使每次重连都在
        同一个独立 loop 里完成，避免与主 loop 冲突导致新线程无法启动。
        """
        import lark_oapi.ws.client as _ws_module

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _ws_module.loop = loop          # 替换模块级单例
        self._ws_thread_loop = loop

        ws = self._ws_client

        # --- patch 1：覆盖服务端推送的重连参数 ---
        original_configure = getattr(ws, "_configure", None)
        if original_configure is not None:
            def _configure_override(conf: Any) -> Any:
                original_configure(conf)
                ws._reconnect_nonce = _WS_RECONNECT_NONCE
                ws._reconnect_interval = _WS_RECONNECT_INTERVAL
            ws._configure = _configure_override
        ws._reconnect_nonce = _WS_RECONNECT_NONCE
        ws._reconnect_interval = _WS_RECONNECT_INTERVAL

        # --- patch 2：注入 websockets 协议级 ping 参数，加速死连接检测 ---
        original_ws_connect = _ws_module.websockets.connect

        def _connect_with_ping(*args: Any, **kwargs: Any) -> Any:
            kwargs.setdefault("ping_interval", _WS_PING_INTERVAL)
            kwargs.setdefault("ping_timeout", _WS_PING_TIMEOUT)
            return original_ws_connect(*args, **kwargs)

        _ws_module.websockets.connect = _connect_with_ping

        try:
            ws.start()
        except Exception:
            pass
        finally:
            _ws_module.websockets.connect = original_ws_connect
            if original_configure is not None:
                try:
                    ws._configure = original_configure
                except Exception:
                    pass
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            try:
                loop.close()
            except Exception:
                pass
            self._ws_thread_loop = None

    def _on_ws_reconnecting(self) -> None:
        self._ws_reconnecting_since = time.monotonic()
        logger.info("[ws] 开始重连，记录时刻")

    def _on_ws_reconnected(self) -> None:
        self._ws_reconnecting_since = None
        logger.info("[ws] 重连成功")

    async def _ws_watchdog(self) -> None:
        """每 30s 检查重连是否卡住；超过 60s 则停掉旧 loop，重建客户端和线程。

        阈值设为 60s（> _WS_RECONNECT_NONCE=10s），只有真正卡住才触发。
        停旧 loop 而不是丢弃线程：_run_ws_in_dedicated_loop 的 finally 块
        会自动清理任务并设 _ws_thread_loop=None。
        """
        while True:
            await asyncio.sleep(30)
            since = self._ws_reconnecting_since
            if since is None:
                continue
            elapsed = time.monotonic() - since
            if elapsed > 60:
                logger.warning("[ws] 重连卡住 %.0fs，强制停止旧 ws loop 并重建", elapsed)
                self._ws_reconnecting_since = None
                ws_loop = self._ws_thread_loop
                if ws_loop is not None and not ws_loop.is_closed():
                    ws_loop.call_soon_threadsafe(ws_loop.stop)
                await asyncio.sleep(1.0)   # 等旧线程 finally 清理完
                self._ws_client = lark.ws.Client(
                    app_id=self._config.feishu_app_id,
                    app_secret=self._config.feishu_app_secret,
                    event_handler=self._ws_handler,
                    log_level=lark.LogLevel.INFO,
                )
                self._patch_ws_disconnect()
                self._ws_client.on_reconnecting = self._on_ws_reconnecting
                self._ws_client.on_reconnected = self._on_ws_reconnected
                self._start_ws_thread()

    # 停止 ws 长连
    async def stop(self) -> None:
        ws = self._ws_client
        if ws is not None:
            try:
                ws._auto_reconnect = False  # 阻止断开后自动重连
            except Exception:
                pass
            self._ws_client = None
        ws_loop = self._ws_thread_loop
        if ws_loop is not None and not ws_loop.is_closed():
            ws_loop.call_soon_threadsafe(ws_loop.stop)
        ws_thread = self._ws_thread
        if ws_thread is not None and ws_thread.is_alive():
            ws_thread.join(timeout=3.0)

    # ===== 入站：lark SDK 在自己线程回调，转回 asyncio =====

    def _on_p2p_chat_entered(self, data: P2ImChatAccessEventBotP2pChatEnteredV1) -> None:
        pass  # 仅注册以避免 SDK 报 "processor not found"

    def _on_message_read(self, data: P2ImMessageMessageReadV1) -> None:
        pass  # 仅注册以避免 SDK 报 "processor not found"

    def _on_card_action_trigger(self, data: Any) -> P2CardActionTriggerResponse:
        """卡片按钮点击回调（SDK 在自己线程触发）。"""
        loop = self._loop
        if loop is not None:
            asyncio.run_coroutine_threadsafe(self._on_card_action_event(data), loop)
        return P2CardActionTriggerResponse()

    async def _on_card_action_event(self, data: Any) -> None:
        event = getattr(data, "event", None)
        if not event:
            return
        operator = getattr(event, "operator", None)
        context = getattr(event, "context", None)
        action = getattr(event, "action", None)
        if not operator or not context or not action:
            return
        open_id = getattr(operator, "open_id", "") or ""
        chat_id = getattr(context, "open_chat_id", "") or ""
        message_id = getattr(context, "open_message_id", "") or ""
        value = getattr(action, "value", {}) or {}
        if not open_id or not chat_id or not self._on_card_action:
            return
        try:
            await self._on_card_action(open_id, chat_id, message_id, value)
        except Exception:
            logger.exception("on_card_action 回调出错")

    def _on_message_receive(self, data: P2ImMessageReceiveV1) -> None:
        # 入口日志：只要 lark 推过来事件，无论后续怎样这一条一定打印
        logger.info("[feishu] 收到 message_receive 事件")
        try:
            text, open_id, chat_id = self._extract(data)
        except Exception:
            logger.exception("解析飞书消息失败")
            return
        logger.info("[feishu] open_id=%s chat_id=%s text=%r", open_id, chat_id, text[:80])
        if not text or not open_id or not chat_id:
            return
        loop = self._loop
        if loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._on_user_text(open_id, chat_id, text), loop)

    # 从飞书事件里把（文本、open_id、chat_id）拆出来
    def _extract(self, data: P2ImMessageReceiveV1) -> tuple[str, str, str]:
        event = data.event
        message = event.message
        sender = event.sender
        if message.message_type != "text":
            return "", "", ""
        try:
            content = json.loads(message.content or "{}")
        except json.JSONDecodeError:
            content = {}
        text = (content.get("text") or "").strip()
        # 去掉 @机器人 占位
        text = _MENTION_PATTERN.sub("", text).strip()
        open_id = sender.sender_id.open_id if sender and sender.sender_id else ""
        chat_id = message.chat_id or ""
        return text, open_id, chat_id

    # ===== 出站 =====

    @staticmethod
    def _card_content(markdown: str) -> str:
        """把 markdown 文本包成飞书 interactive card JSON 字符串。"""
        return json.dumps(
            {
                "config": {"wide_screen_mode": True},
                "elements": [
                    {"tag": "div", "text": {"content": markdown, "tag": "lark_md"}}
                ],
            },
            ensure_ascii=False,
        )

    async def send_card(self, chat_id: str, markdown: str) -> Optional[str]:
        """发一条 markdown 卡片消息，返回 message_id（失败返回 None）。"""
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("interactive")
            .content(self._card_content(markdown))
            .build()
        )
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(body)
            .build()
        )
        response = await asyncio.to_thread(self._client.im.v1.message.create, request)
        if not response.success():
            logger.warning("发飞书卡片失败 chat=%s code=%s msg=%s", chat_id, response.code, response.msg)
            return None
        return response.data.message_id if response.data else None

    async def update_card(self, message_id: str, markdown: str) -> None:
        """更新已有的卡片消息内容。"""
        body = PatchMessageRequestBody.builder().content(self._card_content(markdown)).build()
        request = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(body)
            .build()
        )
        response = await asyncio.to_thread(self._client.im.v1.message.patch, request)
        if not response.success():
            logger.warning("更新飞书卡片失败 msg_id=%s code=%s msg=%s", message_id, response.code, response.msg)

    async def send_interactive_card(self, chat_id: str, card: dict) -> Optional[str]:
        """发完整 card JSON（含按钮等交互元素），返回 message_id。"""
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("interactive")
            .content(json.dumps(card, ensure_ascii=False))
            .build()
        )
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(body)
            .build()
        )
        response = await asyncio.to_thread(self._client.im.v1.message.create, request)
        if not response.success():
            logger.warning("发交互卡片失败 chat=%s code=%s msg=%s",
                           chat_id, response.code, response.msg)
            return None
        return response.data.message_id if response.data else None

    async def update_interactive_card(self, message_id: str, card: dict) -> None:
        """全量替换交互式卡片内容。"""
        body = PatchMessageRequestBody.builder().content(
            json.dumps(card, ensure_ascii=False)
        ).build()
        request = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(body)
            .build()
        )
        response = await asyncio.to_thread(self._client.im.v1.message.patch, request)
        if not response.success():
            logger.warning("更新交互卡片失败 msg_id=%s code=%s msg=%s",
                           message_id, response.code, response.msg)

    async def send_text(self, chat_id: str, text: str, reply_to: Optional[str] = None) -> None:
        """发纯文本消息（用于系统提示/状态通知）。"""
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .build()
        )
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(body)
            .build()
        )
        response = await asyncio.to_thread(self._client.im.v1.message.create, request)
        if not response.success():
            logger.warning(
                "发飞书失败 chat=%s code=%s msg=%s", chat_id, response.code, response.msg
            )
