"""会话执行器抽象。

把"具体怎么跑 claude"和 manager 的状态机解耦。当前只实现 LocalExecutor
（直接在本机 spawn claude）。如果以后要加远程模式（通过 ws + bridge 调
远端机器），再写一个 RemoteExecutor 同样实现这个协议即可。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional, Protocol

from server.runner import ClaudeRunner, StartOptions

logger = logging.getLogger(__name__)

# 事件回调：fn(session_id, event_dict)
ExecutorEventCb = Callable[[str, dict], Awaitable[None]]

# 退出回调：fn(session_id, exit_code, reason)
ExecutorExitCb = Callable[[str, Optional[int], str], Awaitable[None]]

# 权限回调：fn(session_id, request_id, request_body) -> allow
ExecutorPermissionCb = Callable[[str, str, dict], Awaitable[bool]]


@dataclass
class ExecutorSessionInfo:
    """对外暴露的一个会话状态。"""
    session_id: str
    cwd: str
    executor_id: str


class Executor(Protocol):
    """会话执行器协议。"""
    executor_id: str

    async def start_session(self, cwd: str, resume_claude_session: Optional[str] = None,
                            continue_last: bool = False) -> str: ...

    async def send_prompt(self, session_id: str, prompt: str) -> None: ...

    async def stop_session(self, session_id: str) -> None: ...

    def list_sessions(self) -> List[ExecutorSessionInfo]: ...

    async def set_permission_mode(self, session_id: str, mode: str) -> None: ...


class LocalExecutor:
    """直接在本进程内 spawn claude 子进程的执行器。"""

    def __init__(self, claude_bin: str, on_event: ExecutorEventCb, on_exit: ExecutorExitCb,
                 on_permission: Optional[ExecutorPermissionCb] = None) -> None:
        self.executor_id = "local"
        self._claude_bin = claude_bin
        self._on_event = on_event
        self._on_exit = on_exit
        self._on_permission = on_permission
        self._runners: Dict[str, ClaudeRunner] = {}
        self._meta: Dict[str, ExecutorSessionInfo] = {}
        self._counter = 0
        self._lock = asyncio.Lock()

    # 生成简短可读的 session_id（不依赖 claude 内部 id）
    def _new_session_id(self) -> str:
        self._counter += 1
        return f"sess{self._counter:03d}"

    # 起一个新会话，返回 session_id
    async def start_session(self, cwd: str, resume_claude_session: Optional[str] = None,
                            continue_last: bool = False) -> str:
        async with self._lock:
            session_id = self._new_session_id()

        async def _on_event(event: dict) -> None:
            await self._on_event(session_id, event)

        async def _on_exit(code: Optional[int], reason: str) -> None:
            self._runners.pop(session_id, None)
            self._meta.pop(session_id, None)
            await self._on_exit(session_id, code, reason)

        async def _on_permission(request_id: str, request: dict) -> bool:
            if self._on_permission:
                return await self._on_permission(session_id, request_id, request)
            return False

        runner = ClaudeRunner(
            self._claude_bin,
            on_event=_on_event,
            on_exit=_on_exit,
            on_permission=_on_permission if self._on_permission else None,
        )
        await runner.start(StartOptions(
            cwd=cwd,
            resume_claude_session=resume_claude_session,
            continue_last=continue_last,
            extra_args=[],
        ))
        self._runners[session_id] = runner
        self._meta[session_id] = ExecutorSessionInfo(
            session_id=session_id, cwd=cwd, executor_id=self.executor_id,
        )
        return session_id

    # 把 prompt 写到指定会话的 stdin
    async def send_prompt(self, session_id: str, prompt: str) -> None:
        runner = self._runners.get(session_id)
        if not runner:
            raise KeyError(f"未知 session_id: {session_id}")
        await runner.send_prompt(prompt)

    # 优雅关闭一个会话
    async def stop_session(self, session_id: str) -> None:
        runner = self._runners.get(session_id)
        if runner:
            await runner.stop()

    # 列出当前所有活跃会话
    def list_sessions(self) -> List[ExecutorSessionInfo]:
        return list(self._meta.values())

    # 在运行中的会话里切换全局权限模式（acceptEdits / default / bypassPermissions）
    async def set_permission_mode(self, session_id: str, mode: str) -> None:
        runner = self._runners.get(session_id)
        if runner:
            await runner._set_permission_mode(mode)

    # 进程退出前批量关闭
    async def shutdown(self) -> None:
        for sid in list(self._runners.keys()):
            try:
                await self.stop_session(sid)
            except Exception:
                logger.exception("关闭会话失败 sid=%s", sid)
