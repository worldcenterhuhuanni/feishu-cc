"""一个 ClaudeRunner == 一个 claude 子进程会话。

约定使用 stream-json 双向 IO：
- 进程不会自动退出，直到我们关闭 stdin 或调用 stop()；
- 每次 send_prompt 写一行 JSON 到 stdin，触发新一轮回合；
- stdout 每行一条事件，回调式投递给上层。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable, List, Optional

logger = logging.getLogger(__name__)

# 事件回调：fn(event_dict)
EventCallback = Callable[[dict], Awaitable[None]]

# 退出回调：fn(exit_code, reason)
ExitCallback = Callable[[Optional[int], str], Awaitable[None]]

# 权限回调：fn(request_id, request_body) -> allow
PermissionCallback = Callable[[str, dict], Awaitable[bool]]


@dataclass
class StartOptions:
    """单次会话启动参数。"""
    cwd: str
    resume_claude_session: Optional[str] = None
    continue_last: bool = False
    extra_args: List[str] = None  # type: ignore[assignment]


class ClaudeRunner:
    """封装一个 claude stream-json 子进程。"""

    def __init__(
        self,
        claude_bin: str,
        on_event: EventCallback,
        on_exit: ExitCallback,
        on_permission: Optional[PermissionCallback] = None,
    ) -> None:
        self._claude_bin = claude_bin
        self._on_event = on_event
        self._on_exit = on_exit
        self._on_permission = on_permission
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._stdout_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._wait_task: Optional[asyncio.Task] = None
        self._stop_lock = asyncio.Lock()

    # 拼参数；让 cwd 体现在 subprocess 自己的工作目录而不是 --add-dir
    def _build_argv(self, opts: StartOptions) -> List[str]:
        argv: List[str] = [
            self._claude_bin,
            "--print",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--permission-prompt-tool", "stdio",
        ]
        if opts.resume_claude_session:
            argv += ["--resume", opts.resume_claude_session]
        elif opts.continue_last:
            argv += ["--continue"]
        if opts.extra_args:
            argv += list(opts.extra_args)
        return argv

    # 启动进程；返回时进程已 spawn 但 stdin 还在等待第一帧
    async def start(self, opts: StartOptions) -> None:
        if self._proc is not None:
            raise RuntimeError("ClaudeRunner 已在运行")
        argv = self._build_argv(opts)
        logger.info("spawn: %s (cwd=%s)", " ".join(shlex.quote(a) for a in argv), opts.cwd)
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=opts.cwd if os.path.isdir(opts.cwd) else None,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._stdout_task = asyncio.create_task(self._pump_stdout())
        self._stderr_task = asyncio.create_task(self._pump_stderr())
        self._wait_task = asyncio.create_task(self._wait_exit())

    # 把一段 prompt 包成 stream-json user 消息写到 stdin
    async def send_prompt(self, prompt: str) -> None:
        if not self._proc or not self._proc.stdin:
            raise RuntimeError("ClaudeRunner 未运行")
        payload = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": prompt}],
            },
        }
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        self._proc.stdin.write(line.encode("utf-8"))
        await self._proc.stdin.drain()

    # 优雅停止：先关 stdin，等 3 秒，仍活着则 kill；kill 后再等 3 秒
    async def stop(self, kill_timeout: float = 3.0) -> None:
        async with self._stop_lock:
            if not self._proc:
                return
            try:
                if self._proc.stdin and not self._proc.stdin.is_closing():
                    self._proc.stdin.close()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=kill_timeout)
                except asyncio.TimeoutError:
                    logger.warning("claude 未在 %ss 内退出，强制 kill", kill_timeout)
                    self._proc.kill()
                    # kill 后再给一次 timeout，防止僵尸进程导致永久 hang
                    try:
                        await asyncio.wait_for(self._proc.wait(), timeout=kill_timeout)
                    except asyncio.TimeoutError:
                        logger.error("claude 进程 SIGKILL 后仍未退出（可能是僵尸），跳过 wait")
            except ProcessLookupError:
                pass

    # ===== 内部循环 =====

    # 逐行读 stdout，每行一条 stream-json 事件
    async def _pump_stdout(self) -> None:
        assert self._proc and self._proc.stdout
        while True:
            raw = await self._proc.stdout.readline()
            if not raw:
                return
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("非 JSON stdout 行被忽略: %s", line[:120])
                continue
            # claude 就绪时强制切回 default 权限模式（覆盖 skipAutoPermissionPrompt: true）
            if event.get("type") == "system" and event.get("subtype") == "init":
                asyncio.create_task(self._set_permission_mode("default"))
            logger.info("eventType: %s", event.get("type"))
            # 拦截权限请求，等待用户决策后再写响应
            if event.get("type") == "control_request":
                req = event.get("request", {})
                if req.get("subtype") == "can_use_tool":
                    logger.info("[permission] 收到权限请求 tool=%s request_id=%s",
                                req.get("tool_name"), event.get("request_id"))
                    await self._handle_permission(event)
                    continue
                logger.debug("[permission] 未处理的 control_request subtype=%s", req.get("subtype"))
            try:
                await self._on_event(event)
            except Exception:
                logger.exception("on_event 回调出错")

    async def _handle_permission(self, event: dict) -> None:
        request_id = event.get("request_id", "")
        request = event.get("request", {})
        allow = False
        if self._on_permission:
            try:
                allow = await self._on_permission(request_id, request)
            except Exception:
                logger.exception("on_permission 回调出错")
        else:
            logger.warning("[permission] on_permission 未注册，默认拒绝 request_id=%s", request_id)
        logger.info("[permission] 决策 allow=%s request_id=%s", allow, request_id)
        await self._send_permission_response(request_id, allow, request.get("input", {}))

    async def _set_permission_mode(self, mode: str) -> None:
        """覆盖全局权限模式（如 skipAutoPermissionPrompt: true → default）。"""
        if not self._proc or not self._proc.stdin:
            return
        payload = {
            "type": "control_request",
            "request_id": str(uuid.uuid4()),
            "request": {"subtype": "set_permission_mode", "mode": mode},
        }
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        self._proc.stdin.write(line.encode("utf-8"))
        await self._proc.stdin.drain()
        logger.info("[permission] 已发送 set_permission_mode mode=%s", mode)

    async def _send_permission_response(self, request_id: str, allow: bool, original_input: dict) -> None:
        if not self._proc or not self._proc.stdin:
            return
        inner: dict = (
            {"behavior": "allow", "updatedInput": original_input}
            if allow
            else {"behavior": "deny", "message": "用户在飞书拒绝了此操作"}
        )
        response = {
            "type": "control_response",
            "response": {"subtype": "success", "request_id": request_id, "response": inner},
        }
        line = json.dumps(response, ensure_ascii=False) + "\n"
        self._proc.stdin.write(line.encode("utf-8"))
        await self._proc.stdin.drain()

    # stderr 仅日志化，不当作事件
    async def _pump_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        while True:
            raw = await self._proc.stderr.readline()
            if not raw:
                return
            logger.warning("[claude stderr] %s", raw.decode("utf-8", errors="replace").rstrip())

    # 等待进程结束并触发 on_exit
    async def _wait_exit(self) -> None:
        assert self._proc
        code = await self._proc.wait()
        # 等 stdout 任务彻底冲掉残留行
        if self._stdout_task:
            await asyncio.gather(self._stdout_task, return_exceptions=True)
        reason = "exited" if code == 0 else f"exit_code={code}"
        try:
            await self._on_exit(code, reason)
        except Exception:
            logger.exception("on_exit 回调出错")
