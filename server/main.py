"""server 启动入口：单进程模式。

装配关系：
    FeishuClient ──→ commands.dispatch ──→ CCBridgeManager ──→ LocalExecutor ──→ claude
                                                  ▲                  │
                                                  └── on_event ──────┘
"""

from __future__ import annotations

import asyncio
import atexit
import datetime
import logging
import logging.handlers
import os
import signal
from pathlib import Path
from typing import Optional

from server import commands
from server.config import ServerConfig, load
from server.executor import LocalExecutor
from server.feishu_client import FeishuClient
from server.manager import CCBridgeManager, get_manager

logger = logging.getLogger("feishu_cc.server")

_PID_FILE = Path(__file__).parent.parent / "feishu-cc.pid"


class _PidFileLock:
    """启动时写 PID 文件，退出时删除；已有存活实例则拒绝启动。"""

    def __init__(self, path: Path) -> None:
        self._path = path

    def acquire(self) -> None:
        if self._path.exists():
            try:
                pid = int(self._path.read_text().strip())
                os.kill(pid, 0)  # 只检查进程是否存在，不发真实信号
                print(
                    f"[feishu-cc] 已有实例在运行（PID {pid}），拒绝重复启动。\n"
                    f"           如需强制重启，请先执行：kill {pid}"
                )
                raise SystemExit(1)
            except (ValueError, ProcessLookupError):
                pass  # PID 文件残留但进程已死，覆盖即可
            except PermissionError:
                pass  # 无权 kill(0)，保守地继续（罕见情况）

        self._path.write_text(str(os.getpid()))
        atexit.register(self._release)

    def _release(self) -> None:
        try:
            self._path.unlink(missing_ok=True)
        except Exception:
            pass


# claude 可执行文件路径：环境变量优先，否则用 PATH 上的 claude
def _resolve_claude_bin() -> str:
    return os.getenv("FEISHU_CC_CLAUDE_BIN", "claude")


# 入站文本：白名单过滤 + 命令分发
def _build_inbound_handler(config: ServerConfig, manager: CCBridgeManager):
    async def on_user_text(open_id: str, chat_id: str, text: str) -> None:
        if not config.is_user_allowed(open_id):
            logger.info("拒绝非白名单用户 open_id=%s", open_id)
            return
        await commands.dispatch(manager, open_id, chat_id, text)
    return on_user_text


# 异步主流程
async def _run() -> None:
    config = load()
    log_fmt = "%(asctime)s %(levelname)s %(name)s :: %(message)s"
    logging.basicConfig(level=config.log_level, format=log_fmt)

    # 文件日志：logs/session-YYYYMMDD-HHMMSS.log
    logs_dir = Path(__file__).parent.parent / "logs"
    logs_dir.mkdir(exist_ok=True)
    session_ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_file = logs_dir / f"session-{session_ts}.log"
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(log_fmt))
    logging.getLogger().addHandler(file_handler)
    logging.getLogger().setLevel(logging.DEBUG)
    # 屏蔽第三方库的 DEBUG 噪音（websockets 二进制帧 / urllib3 连接日志）
    for _noisy in ("websockets", "websocket", "urllib3", "httpx", "httpcore"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)
    logger.info("日志写入 %s", log_file)

    # 两步装配：先建 manager（持有 feishu sender），再注入 executor
    feishu_holder: dict[str, Optional[FeishuClient]] = {"client": None}

    async def feishu_sender(chat_id: str, text: str, reply_to: Optional[str]) -> None:
        client = feishu_holder["client"]
        if client is None:
            return
        await client.send_text(chat_id, text, reply_to=reply_to)

    async def feishu_card_sender(chat_id: str, markdown: str) -> Optional[str]:
        client = feishu_holder["client"]
        if client is None:
            return None
        return await client.send_card(chat_id, markdown)

    async def feishu_card_updater(message_id: str, markdown: str) -> None:
        client = feishu_holder["client"]
        if client is None:
            return
        await client.update_card(message_id, markdown)

    async def feishu_interactive_sender(chat_id: str, card: dict) -> Optional[str]:
        client = feishu_holder["client"]
        if client is None:
            return None
        return await client.send_interactive_card(chat_id, card)

    async def feishu_interactive_updater(message_id: str, card: dict) -> None:
        client = feishu_holder["client"]
        if client is None:
            return
        await client.update_interactive_card(message_id, card)

    manager = get_manager(
        feishu_sender, feishu_card_sender, feishu_card_updater,
        feishu_interactive_sender, feishu_interactive_updater,
        perm_keep_history=config.perm_keep_history,
    )

    executor = LocalExecutor(
        claude_bin=_resolve_claude_bin(),
        on_event=manager.on_executor_event,
        on_exit=manager.on_executor_exit,
        on_permission=manager.on_executor_permission,
    )
    manager.set_executor(executor)

    feishu = FeishuClient(
        config,
        on_user_text=_build_inbound_handler(config, manager),
        on_card_action=manager.on_card_action,
    )
    feishu_holder["client"] = feishu
    await feishu.start()

    logger.info("feishu-cc server 就绪（单进程模式，claude=%s）", _resolve_claude_bin())

    # 等终止信号
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass  # Windows
    await stop_event.wait()

    logger.info("正在关闭…")
    await executor.shutdown()
    await feishu.stop()


# console_scripts 入口
def main() -> None:
    _PidFileLock(_PID_FILE).acquire()

    # macOS：阻止系统休眠（不影响屏幕熄屏），服务退出后 caffeinate 自动结束
    import platform
    import subprocess
    _caffeinate: Optional[subprocess.Popen] = None
    if platform.system() == "Darwin":
        try:
            _caffeinate = subprocess.Popen(
                ["caffeinate", "-si", "-w", str(os.getpid())],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            pass  # 非 macOS 或 caffeinate 不存在时静默跳过

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    finally:
        if _caffeinate is not None:
            _caffeinate.terminate()


if __name__ == "__main__":
    main()
