"""会话注册中心：飞书用户 ↔ 当前会话 的映射 + 事件路由。

具体怎么跑 claude 已经被 :class:`Executor` 封装走了，manager 只负责：
- 维护 open_id → 当前活动 session_id 的绑定；
- 把 executor 流出的事件渲染成飞书消息；
- 把 /cc 命令解析后的动作转发给 executor。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional

from server import render
from server.executor import Executor

logger = logging.getLogger(__name__)

# 飞书发送回调：fn(chat_id, text, reply_to)
FeishuSender = Callable[[str, str, Optional[str]], Awaitable[None]]
# 飞书卡片发送回调：fn(chat_id, markdown) → message_id
FeishuCardSender = Callable[[str, str], Awaitable[Optional[str]]]
# 飞书卡片更新回调：fn(message_id, markdown)
FeishuCardUpdater = Callable[[str, str], Awaitable[None]]
# 飞书交互卡片发送回调（完整 JSON）：fn(chat_id, card_dict) → message_id
FeishuInteractiveSender = Callable[[str, dict], Awaitable[Optional[str]]]
# 飞书交互卡片更新回调（完整 JSON）：fn(message_id, card_dict)
FeishuInteractiveUpdater = Callable[[str, dict], Awaitable[None]]

# 最小卡片更新间隔（秒），避免触发飞书频率限制
_CARD_UPDATE_INTERVAL = 0.2


@dataclass
class _ToolEntry:
    """单次工具调用的状态，用于渲染进度行。"""
    tool_id: str
    name: str
    hint: str
    start_time: float
    done: bool = False
    duration: float = 0.0

    def render_line(self) -> str:
        if self.done:
            return f"✅ {self.name}{self.hint}  ({self.duration:.1f}s)"
        return f"⏳ {self.name}{self.hint}"


@dataclass
class _TurnState:
    """记录一次 claude 回合正在输出的交互卡片状态。"""
    message_id: Optional[str]
    text_parts: List[str] = field(default_factory=list)
    tool_entries: List[_ToolEntry] = field(default_factory=list)  # 工具调用进度
    file_buttons: List[dict] = field(default_factory=list)  # 懒加载按钮（只存路径/url）
    expanded_file: Optional[str] = None      # 当前展开的文件路径
    expanded_content: Optional[str] = None  # 展开文件的内容
    perm_request: Optional[dict] = None     # 当前内嵌权限请求（tool_name/input_preview/decided…）
    done: bool = False                       # result 事件后标记，保留以支持点击
    tick: int = 0                            # 更新计数
    last_update: float = field(default_factory=time.monotonic)


@dataclass
class Session:
    """一个活跃会话的对外视图（manager 这一层维护的元数据）。"""
    session_id: str
    owner_open_id: str
    owner_chat_id: str
    cwd: str
    claude_session_id: Optional[str] = None  # init 事件里给的真实 claude session
    task_subjects: Dict[str, str] = field(default_factory=dict)    # task_id → subject
    _pending_task_creates: Dict[str, str] = field(default_factory=dict)  # tool_id → subject


@dataclass
class UserBinding:
    """飞书用户当前默认会话指针。"""
    session_id: Optional[str] = None


class CCBridgeManager:
    """单例。executor 在 set_executor() 时注入；feishu_sender 构造时注入。"""

    def __init__(
        self,
        feishu_sender: FeishuSender,
        feishu_card_sender: Optional[FeishuCardSender] = None,
        feishu_card_updater: Optional[FeishuCardUpdater] = None,
        feishu_interactive_sender: Optional[FeishuInteractiveSender] = None,
        feishu_interactive_updater: Optional[FeishuInteractiveUpdater] = None,
        perm_keep_history: bool = False,
    ) -> None:
        self._feishu_sender = feishu_sender
        self._feishu_card_sender = feishu_card_sender
        self._feishu_card_updater = feishu_card_updater
        self._feishu_interactive_sender = feishu_interactive_sender
        self._feishu_interactive_updater = feishu_interactive_updater
        self._perm_keep_history = perm_keep_history
        self._executor: Optional[Executor] = None
        self._sessions: Dict[str, Session] = {}
        self._bindings: Dict[str, UserBinding] = {}
        self._turn_states: Dict[str, _TurnState] = {}
        self._pending_permissions: Dict[str, dict] = {}
        self._lock = asyncio.Lock()

    # 注入 executor。executor 上的 on_event/on_exit 必须指向本对象的方法
    def set_executor(self, executor: Executor) -> None:
        self._executor = executor

    # ============ 用户绑定 ============

    def _binding(self, open_id: str) -> UserBinding:
        return self._bindings.setdefault(open_id, UserBinding())

    def _current_session(self, open_id: str) -> Optional[Session]:
        sid = self._binding(open_id).session_id
        return self._sessions.get(sid) if sid else None

    # ============ 命令实现 ============

    # 启动会话；executor 同步返回 session_id 后立刻通知用户
    async def start_session(self, open_id: str, chat_id: str, cwd: str,
                            resume_claude_session: Optional[str] = None,
                            continue_last: bool = False) -> Optional[str]:
        if self._executor is None:
            await self._notify(chat_id, "❌ executor 未就绪")
            return None
        await self._notify(chat_id, f"⏳ 正在 `{cwd}` 启动会话…")
        try:
            session_id = await self._executor.start_session(
                cwd=cwd,
                resume_claude_session=resume_claude_session,
                continue_last=continue_last,
            )
        except Exception as e:
            logger.exception("启动会话失败")
            await self._notify(chat_id, f"❌ 启动失败：{e}")
            return None
        self._sessions[session_id] = Session(
            session_id=session_id,
            owner_open_id=open_id,
            owner_chat_id=chat_id,
            cwd=cwd,
        )
        self._binding(open_id).session_id = session_id
        hint = "继续上次" if continue_last else (f"恢复 {resume_claude_session}" if resume_claude_session else "新会话")
        await self._notify(chat_id, f"✅ 会话已启动 `{session_id}`（{hint}），直接发消息即可对话。")
        return session_id

    # 把 prompt 送到用户当前会话
    async def send_prompt(self, open_id: str, chat_id: str, prompt: str) -> bool:
        sess = self._current_session(open_id)
        if not sess:
            await self._notify(chat_id, "❌ 没有活动会话。先 `/cc start`")
            return False
        if self._executor is None:
            return False
        try:
            await self._executor.send_prompt(sess.session_id, prompt)
            return True
        except KeyError:
            await self._notify(chat_id, "❌ 会话已失效，请重新 `/cc start`")
            self._binding(open_id).session_id = None
            return False

    # 关闭会话；不传 session_id 关当前
    async def stop_session(self, open_id: str, chat_id: str, session_id: Optional[str] = None) -> bool:
        target = self._sessions.get(session_id) if session_id else self._current_session(open_id)
        if not target:
            await self._notify(chat_id, "❌ 没有可关闭的会话")
            return False
        if self._executor is not None:
            try:
                # 最长等 10s；进程僵死时 runner.stop() 可能卡锁，必须有超时兜底
                await asyncio.wait_for(
                    self._executor.stop_session(target.session_id),
                    timeout=10.0,
                )
            except asyncio.TimeoutError:
                logger.warning("stop_session 超时 session_id=%s，强制清理", target.session_id)
            except Exception:
                logger.exception("stop_session 异常 session_id=%s", target.session_id)
        # 无论 executor 是否成功，都立即清理 session 状态
        # on_executor_exit 若之后触发，会因 _sessions 已无此 key 而静默跳过
        self._sessions.pop(target.session_id, None)
        for b in self._bindings.values():
            if b.session_id == target.session_id:
                b.session_id = None
        await self._notify(chat_id, f"🛑 已关闭会话 `{target.session_id}`")
        return True

    # 切换当前默认会话
    async def use_session(self, open_id: str, chat_id: str, session_id: str) -> bool:
        if session_id not in self._sessions:
            await self._notify(chat_id, f"❌ 会话 `{session_id}` 不存在")
            return False
        self._binding(open_id).session_id = session_id
        await self._notify(chat_id, f"✅ 当前会话切换为 `{session_id}`")
        return True

    # 列出该用户名下所有活跃会话
    def list_sessions(self, open_id: str) -> List[Session]:
        return [s for s in self._sessions.values() if s.owner_open_id == open_id]

    # ============ Executor 事件入口 ============

    # executor 每条 stream-json 事件都走这里
    async def on_executor_event(self, session_id: str, event: dict) -> None:
        sess = self._sessions.get(session_id)
        if not sess:
            return

        etype = event.get("type")

        # 抓取真实 claude session id
        if etype == "system" and event.get("subtype") == "init":
            sess.claude_session_id = event.get("session_id") or sess.claude_session_id
            return

        if etype == "assistant":
            text = render.extract_text(event)
            new_tools = render.extract_tool_uses(event)
            new_btns = render.extract_file_buttons(event)
            if not text and not new_tools:
                return
            # 记录 TaskCreate 的 subject，供后续 TaskUpdate 查找
            for tid, n, _h, raw_inp in new_tools:
                if n == "TaskCreate" and "subject" in raw_inp:
                    sess._pending_task_creates[tid] = str(raw_inp["subject"])
            state = self._turn_states.get(session_id)
            # 上一回合已完成，新回合开始 → 重置 state
            if state is not None and state.done:
                self._turn_states.pop(session_id)
                state = None
            now = time.monotonic()
            if state is None:
                entries = [
                    self._make_tool_entry(tid, n, h, raw_inp, now, sess)
                    for tid, n, h, raw_inp in new_tools
                ]
                card = self._build_turn_card(
                    [text] if text else [], entries, new_btns, session_id=session_id
                )
                msg_id = await self._send_turn_card(sess.owner_chat_id, card)
                self._turn_states[session_id] = _TurnState(
                    message_id=msg_id,
                    text_parts=[text] if text else [],
                    tool_entries=entries,
                    file_buttons=new_btns,
                )
            else:
                if text:
                    state.text_parts.append(text)
                for tid, n, h, raw_inp in new_tools:
                    state.tool_entries.append(
                        self._make_tool_entry(tid, n, h, raw_inp, now, sess)
                    )
                # 只保留最近 10 条（保证 undone 优先可见）
                if len(state.tool_entries) > 10:
                    state.tool_entries = state.tool_entries[-10:]
                if new_btns:
                    seen = {b.get("file_path") or b.get("url") for b in state.file_buttons}
                    for btn in new_btns:
                        key = btn.get("file_path") or btn.get("url")
                        if key not in seen:
                            state.file_buttons.append(btn)
                            seen.add(key)
                if state.message_id and now - state.last_update >= _CARD_UPDATE_INTERVAL:
                    state.tick += 1
                    card = self._build_turn_card(
                        state.text_parts, state.tool_entries, state.file_buttons,
                        session_id=session_id, tick=state.tick,
                        expanded_file=state.expanded_file, expanded_content=state.expanded_content,
                        perm_request=state.perm_request,
                    )
                    await self._update_turn_card(state.message_id, card)
                    state.last_update = now

        elif etype == "user":
            done_ids = render.extract_tool_result_ids(event)
            if done_ids:
                state = self._turn_states.get(session_id)
                if state:
                    now = time.monotonic()
                    done_set = set(done_ids)
                    for entry in state.tool_entries:
                        if entry.tool_id in done_set and not entry.done:
                            entry.done = True
                            entry.duration = now - entry.start_time
            # 解析 TaskCreate 结果，建立 task_id → subject 映射
            for tool_id, result_text in render.extract_tool_results(event):
                if tool_id in sess._pending_task_creates:
                    subject = sess._pending_task_creates.pop(tool_id)
                    task_id = _parse_task_id_from_result(result_text)
                    if task_id:
                        sess.task_subjects[task_id] = subject

        elif etype == "result":
            # 不 pop：保留 state 以支持回合结束后仍可点文件按钮
            state = self._turn_states.get(session_id)
            summary = render.extract_result_summary(event)
            if state:
                state.done = True
                # 把所有未完成的工具标为完成
                now = time.monotonic()
                for entry in state.tool_entries:
                    if not entry.done:
                        entry.done = True
                        entry.duration = now - entry.start_time
            if state and state.message_id:
                card = self._build_turn_card(
                    state.text_parts, state.tool_entries, state.file_buttons,
                    done=True, summary=summary, session_id=session_id,
                    expanded_file=state.expanded_file,
                    expanded_content=state.expanded_content,
                    perm_request=state.perm_request,
                )
                await self._update_turn_card(state.message_id, card)
            if not (state and state.message_id):
                await self._notify(sess.owner_chat_id, summary)
            # 额外发一条纯文本消息触发手机推送（card update 不产生推送通知）
            await self._feishu_sender(sess.owner_chat_id, summary, None)

    # claude 请求权限：内嵌到当前回合卡片，阻塞等待用户决策
    async def on_executor_permission(self, session_id: str, request_id: str, request: dict) -> bool:
        sess = self._sessions.get(session_id)
        if not sess:
            return False
        tool_name = request.get("tool_name", "unknown")
        input_data = request.get("input", {})
        description = request.get("description") or request.get("title") or ""
        input_preview = _format_permission_input(tool_name, input_data)
        desc_line = f"\n**说明：** {description}" if description else ""
        card_content = f"**工具：** `{tool_name}`{desc_line}\n{input_preview}"

        perm_info = {
            "tool_name": tool_name,
            "input_preview": card_content,
            "session_id": session_id,
            "decided": False,
            "decision_label": "",
            "decision_template": "grey",
        }

        state = self._turn_states.get(session_id)
        if state is None:
            card = self._build_turn_card([], [], perm_request=perm_info, session_id=session_id)
            msg_id = await self._send_turn_card(sess.owner_chat_id, card)
            state = _TurnState(message_id=msg_id, perm_request=perm_info)
            self._turn_states[session_id] = state
        else:
            state.perm_request = perm_info
            card = self._build_turn_card(
                state.text_parts, state.tool_entries, state.file_buttons,
                session_id=session_id, tick=state.tick,
                expanded_file=state.expanded_file, expanded_content=state.expanded_content,
                perm_request=perm_info,
            )
            if state.message_id:
                await self._update_turn_card(state.message_id, card)

        future: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
        self._pending_permissions[session_id] = {
            "request_id": request_id,
            "future": future,
            "card_id": state.message_id,
            "content": card_content,
        }
        return await future

    # 从飞书收到文字"允许"/"拒绝"后，解析 pending permission（按钮点击走 on_card_action）
    async def resolve_permission(self, open_id: str, allow: bool, always: bool = False) -> bool:
        sess = self._current_session(open_id)
        if not sess:
            return False
        pending = self._pending_permissions.pop(sess.session_id, None)
        if not pending:
            return False
        future = pending["future"]
        card_id = pending.get("card_id")
        await self._after_permission_resolved(
            session_id=sess.session_id, card_id=card_id, allow=allow, always=always,
            card_content=pending.get("content", ""),
        )
        if not future.done():
            future.set_result(allow)
        return True

    # 卡片按钮点击回调：由 FeishuClient._on_card_action_event 调用
    async def on_card_action(self, open_id: str, chat_id: str, message_id: str, value: dict) -> None:
        action = value.get("cc_action")
        if not action:
            return

        # 文件折叠框：在原卡片内展开/收起，无需新卡片
        if action in ("show_file", "hide_file"):
            sid = value.get("session_id", "")
            state = self._turn_states.get(sid)
            if state and state.message_id:
                if action == "hide_file" or state.expanded_file == value.get("file_path"):
                    # 再次点击同一文件 → 收起
                    state.expanded_file = None
                    state.expanded_content = None
                else:
                    file_path = value.get("file_path", "")
                    try:
                        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                            content = f.read()
                        MAX = 7000
                        total = len(content)
                        if total > MAX:
                            content = content[:MAX] + f"\n\n…（仅显示前 {MAX} 字符，共 {total} 字符）"
                        state.expanded_file = file_path
                        state.expanded_content = content
                    except FileNotFoundError:
                        await self._notify(chat_id, f"❌ 文件不存在：`{file_path}`")
                        return
                    except Exception as e:
                        await self._notify(chat_id, f"❌ 读取失败：{e}")
                        return
                card = self._build_turn_card(
                    state.text_parts, state.tool_entries, state.file_buttons,
                    done=state.done, session_id=sid,
                    expanded_file=state.expanded_file,
                    expanded_content=state.expanded_content,
                    perm_request=state.perm_request,
                )
                await self._update_turn_card(state.message_id, card)
            else:
                # 找不到 state（极端情况）→ 降级发新卡片
                if action == "show_file":
                    await self._send_file_detail_card(chat_id, value.get("file_path", ""))
            return
        if action == "show_url":
            await self._notify(chat_id, f"🌐 URL 查看暂不支持，请直接访问：{value.get('url', '')}")
            return

        session_id = value.get("session_id")
        if not session_id:
            return
        pending = self._pending_permissions.pop(session_id, None)
        if not pending:
            logger.debug("[permission] 按钮回调但无 pending permission session_id=%s", session_id)
            if message_id and self._feishu_interactive_updater:
                expired_card = {
                    "config": {"wide_screen_mode": True},
                    "header": {
                        "title": {"content": "🔐 Claude 权限 · 已过期", "tag": "plain_text"},
                        "template": "grey",
                    },
                    "elements": [{"tag": "markdown", "content": "此权限请求已过期或已被处理，无需操作。"}],
                }
                try:
                    await self._feishu_interactive_updater(message_id, expired_card)
                except Exception:
                    logger.debug("更新过期权限卡片失败 message_id=%s", message_id)
            return
        future = pending["future"]
        card_id = pending.get("card_id")
        allow = action in ("allow_once", "allow_always")
        always = action == "allow_always"
        await self._after_permission_resolved(
            session_id=session_id, card_id=card_id, allow=allow, always=always,
            card_content=pending.get("content", ""),
        )
        if not future.done():
            future.set_result(allow)

    # 权限决策后的公共后处理
    async def _after_permission_resolved(
        self, session_id: str, card_id: Optional[str],
        allow: bool, always: bool, card_content: str = "",
    ) -> None:
        if allow:
            label = "✅ 始终允许（本次会话）" if always else "✅ 已允许"
            template = "green"
        else:
            label = "🚫 已拒绝"
            template = "red"

        state = self._turn_states.get(session_id)
        logger.debug(
            "[perm] _after_permission_resolved session_id=%s card_id=%s state=%s "
            "msg_id=%s perm_req=%s keep_history=%s",
            session_id, card_id, "found" if state else "None",
            state.message_id if state else None,
            bool(state.perm_request) if state else None,
            self._perm_keep_history,
        )
        if state and state.perm_request:
            if self._perm_keep_history:
                # 开关开启：保留权限区，改为展示决策结果（移除按钮）
                state.perm_request["decided"] = True
                state.perm_request["decision_label"] = label
                state.perm_request["decision_template"] = template
            else:
                # 默认：决策后权限区消失
                state.perm_request = None
            card = self._build_turn_card(
                state.text_parts, state.tool_entries, state.file_buttons,
                session_id=session_id, tick=state.tick,
                expanded_file=state.expanded_file, expanded_content=state.expanded_content,
                perm_request=state.perm_request,
            )
            logger.debug("[perm] updating card msg_id=%s", state.message_id)
            if state.message_id:
                await self._update_turn_card(state.message_id, card)
            else:
                logger.warning("[perm] state found but message_id is None, cannot update card")
        elif card_id:
            # 找不到 state（极端情况）→ 降级更新独立权限卡片
            await self._update_perm_card_resolved(card_id, label, template=template, content=card_content)

        # 始终允许 → 切换 acceptEdits 模式
        if allow and always and self._executor:
            try:
                await self._executor.set_permission_mode(session_id, "acceptEdits")
                logger.info("[permission] 已切换 acceptEdits 模式 session_id=%s", session_id)
            except Exception:
                logger.exception("set_permission_mode 失败 session_id=%s", session_id)

    # claude 进程退出
    async def on_executor_exit(self, session_id: str, exit_code: Optional[int], reason: str) -> None:
        sess = self._sessions.pop(session_id, None)
        if not sess:
            return
        for b in self._bindings.values():
            if b.session_id == session_id:
                b.session_id = None
        await self._notify(sess.owner_chat_id, f"⏹️ 会话 `{session_id}` 已结束（{reason}）")

    # ============ 工具 ============

    @staticmethod
    def _make_tool_entry(
        tid: str, name: str, hint: str, raw_inp: dict,
        start_time: float, sess: Session,
    ) -> _ToolEntry:
        """创建 _ToolEntry，TaskUpdate 时用 task_subjects 替换 hint 以显示任务名。"""
        if name == "TaskUpdate":
            task_id = str(raw_inp.get("taskId", ""))
            subject = sess.task_subjects.get(task_id, "")
            if subject:
                status = raw_inp.get("status", "")
                emoji = {"in_progress": "⏳", "completed": "✅", "deleted": "🗑️"}.get(status, "→")
                hint = f"  {emoji} {subject[:35]}"
        return _ToolEntry(tool_id=tid, name=name, hint=hint, start_time=start_time)

    async def _notify(self, chat_id: str, text: str) -> None:
        try:
            if self._feishu_card_sender:
                await self._feishu_card_sender(chat_id, text)
            else:
                await self._feishu_sender(chat_id, text, None)
        except Exception:
            logger.exception("发飞书失败 chat_id=%s", chat_id)

    async def _send_card(self, chat_id: str, markdown: str) -> Optional[str]:
        if self._feishu_card_sender:
            try:
                return await self._feishu_card_sender(chat_id, markdown)
            except Exception:
                logger.exception("发飞书卡片失败 chat_id=%s", chat_id)
        else:
            await self._notify(chat_id, markdown)
        return None

    async def _update_card(self, message_id: str, markdown: str) -> None:
        if self._feishu_card_updater:
            try:
                await self._feishu_card_updater(message_id, markdown)
            except Exception:
                logger.exception("更新飞书卡片失败 message_id=%s", message_id)

    @staticmethod
    def _build_turn_card(
        text_parts: List[str],
        tool_entries: List[_ToolEntry],
        file_buttons: Optional[List[dict]] = None,
        done: bool = False,
        summary: str = "",
        session_id: str = "",
        expanded_file: Optional[str] = None,
        expanded_content: Optional[str] = None,
        perm_request: Optional[dict] = None,
        tick: int = 0,
    ) -> dict:
        header_title = summary if done else "⚡ 正在思考…"
        header_template = "green" if done else "carmine"
        elements: list = []
        text = "\n\n".join(text_parts)
        if text:
            # 活跃中在正文末尾追加流式光标
            display_text = text if done else text + " ▉"
            elements.append({"tag": "markdown", "content": display_text})
        if tool_entries:
            note_text = "\n".join(e.render_line() for e in tool_entries)
            elements.append({
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": note_text}],
            })
        if not elements:
            elements.append({"tag": "markdown", "content": "…"})
        # 折叠框：展开文件内容 + 收起按钮
        if expanded_file and expanded_content is not None:
            lang = render.file_lang(expanded_file)
            fname = expanded_file.split("/")[-1]
            elements.append({"tag": "hr"})
            # .md 文件转为飞书原生 elements（支持标题/加粗/表格）；其他文件用 code block
            if lang == "markdown":
                elements.append({"tag": "markdown", "content": f"**📄 {fname}**"})
                elements.extend(render.markdown_to_feishu_elements(expanded_content))
            else:
                elements.append({"tag": "markdown", "content": f"**📄 {fname}**\n```{lang}\n{expanded_content}\n```"})
            elements.append({
                "tag": "action",
                "actions": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "▲ 收起"},
                    "type": "default",
                    "value": {"cc_action": "hide_file", "session_id": session_id},
                }],
            })
        # 内嵌权限请求区
        if perm_request:
            sid = perm_request.get("session_id", "")
            elements.append({"tag": "hr"})
            elements.append({"tag": "markdown", "content": perm_request.get("input_preview", "")})
            if not perm_request.get("decided"):
                elements.append({
                    "tag": "action",
                    "actions": [
                        {"tag": "button", "text": {"tag": "plain_text", "content": "✅ 允许一次"}, "type": "primary", "value": {"cc_action": "allow_once", "session_id": sid}},
                        {"tag": "button", "text": {"tag": "plain_text", "content": "🔒 始终允许"}, "type": "default", "value": {"cc_action": "allow_always", "session_id": sid}},
                        {"tag": "button", "text": {"tag": "plain_text", "content": "❌ 拒绝"}, "type": "danger", "value": {"cc_action": "deny", "session_id": sid}},
                    ],
                })
            else:
                label = perm_request.get("decision_label", "")
                elements.append({
                    "tag": "note",
                    "elements": [{"tag": "plain_text", "content": label}],
                })

        # 文件/URL 懒加载按钮（最多 6 个），当前展开的高亮显示
        if file_buttons:
            actions = []
            for btn in file_buttons[:6]:
                action_val = {k: v for k, v in btn.items() if k != "label"}
                action_val["session_id"] = session_id
                is_expanded = (btn.get("file_path") == expanded_file)
                actions.append({
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": btn["label"]},
                    "type": "primary" if is_expanded else "default",
                    "value": action_val,
                })
            elements.append({"tag": "action", "actions": actions})
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"content": header_title, "tag": "plain_text"},
                "template": header_template,
            },
            "elements": elements,
        }

    async def _send_turn_card(self, chat_id: str, card: dict) -> Optional[str]:
        if self._feishu_interactive_sender:
            try:
                return await self._feishu_interactive_sender(chat_id, card)
            except Exception:
                logger.exception("发交互卡片失败 chat_id=%s", chat_id)
        return None

    async def _update_turn_card(self, message_id: str, card: dict) -> None:
        if self._feishu_interactive_updater:
            try:
                await self._feishu_interactive_updater(message_id, card)
                logger.debug("更新交互卡片成功 message_id=%s", message_id)
            except Exception:
                logger.exception("更新交互卡片失败 message_id=%s", message_id)
        else:
            logger.warning("_feishu_interactive_updater 未设置，无法更新卡片 message_id=%s", message_id)

    async def _send_file_detail_card(self, chat_id: str, file_path: str) -> None:
        """懒加载：实时读取文件内容并发送展示卡片。"""
        if not file_path:
            await self._notify(chat_id, "❌ 文件路径为空")
            return
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except FileNotFoundError:
            await self._notify(chat_id, f"❌ 文件不存在：`{file_path}`")
            return
        except Exception as e:
            await self._notify(chat_id, f"❌ 读取失败：{e}")
            return

        fname = file_path.split("/")[-1]
        lang = render.file_lang(file_path)
        total = len(content)
        MAX = 7000
        truncated = total > MAX
        snippet = content[:MAX] if truncated else content
        code_block = f"```{lang}\n{snippet}\n```"
        if truncated:
            code_block += f"\n\n*（仅显示前 {MAX} 字符，文件共 {total} 字符）*"

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"content": f"📄 {fname}", "tag": "plain_text"},
                "template": "grey",
            },
            "elements": [{"tag": "markdown", "content": code_block}],
        }
        await self._send_turn_card(chat_id, card)

    async def _send_permission_card(
        self, chat_id: str, session_id: str,
        tool_name: str, input_preview: str, description: str
    ) -> Optional[str]:
        """发带三个按钮的权限请求卡片，返回 message_id。"""
        desc_line = f"\n**说明：** {description}" if description else ""
        content = f"**工具：** `{tool_name}`{desc_line}\n{input_preview}"

        def _btn(label: str, action: str, btn_type: str = "default") -> dict:
            return {
                "tag": "button",
                "text": {"tag": "plain_text", "content": label},
                "type": btn_type,
                "value": {"cc_action": action, "session_id": session_id},
            }

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"content": "🔐 Claude 请求权限", "tag": "plain_text"},
                "template": "orange",
            },
            "elements": [
                {"tag": "markdown", "content": content},
                {
                    "tag": "action",
                    "actions": [
                        _btn("✅ 允许一次", "allow_once", "primary"),
                        _btn("🔒 始终允许（本次会话）", "allow_always"),
                        _btn("❌ 拒绝", "deny", "danger"),
                    ],
                },
            ],
        }
        if self._feishu_interactive_sender:
            try:
                return await self._feishu_interactive_sender(chat_id, card)
            except Exception:
                logger.exception("发权限卡片失败 chat_id=%s", chat_id)
        # 降级：markdown 卡片
        msg = f"🔐 **Claude 请求权限**\n工具：`{tool_name}`\n{input_preview}"
        if description:
            msg += f"\n说明：{description}"
        msg += "\n\n回复：**1** 允许一次 | **2** 始终允许 | **3** 拒绝"
        return await self._send_card(chat_id, msg)

    async def _update_perm_card_resolved(
        self, card_id: str, label: str, template: str = "green", content: str = ""
    ) -> None:
        """权限决策后更新卡片（去掉按钮，保留工具详情）。"""
        if self._feishu_interactive_updater:
            elements: list = []
            if content:
                elements.append({"tag": "markdown", "content": content})
            elements.append({
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": "执行中…"}],
            })
            card = {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"content": f"🔐 Claude 权限 · {label}", "tag": "plain_text"},
                    "template": template,
                },
                "elements": elements,
            }
            try:
                await self._feishu_interactive_updater(card_id, card)
                return
            except Exception:
                logger.exception("更新权限卡片失败 card_id=%s", card_id)
        await self._update_card(card_id, f"{label}\n\n执行中…")

    async def _update_perm_card_done(self, card_id: str, summary: str) -> None:
        """执行完成后更新权限卡片，显示结果摘要。"""
        if self._feishu_interactive_updater:
            card = {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"content": "🔐 Claude 权限 · ✅ 完成", "tag": "plain_text"},
                    "template": "green",
                },
                "elements": [{"tag": "markdown", "content": summary}],
            }
            try:
                await self._feishu_interactive_updater(card_id, card)
                return
            except Exception:
                logger.exception("更新权限完成卡片失败 card_id=%s", card_id)
        await self._update_card(card_id, f"✅ {summary}")


def _parse_task_id_from_result(text: str) -> Optional[str]:
    """从 TaskCreate 工具结果中提取 task_id，支持 '#3'、'id: 3' 等格式。"""
    for pattern in [r'#(\d+)', r'\bID[:\s]+(\d+)', r'\btask[_\s]+(\d+)', r'"id"\s*:\s*(\d+)']:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def _format_permission_input(tool_name: str, input_data: dict) -> str:
    if tool_name in ("Bash", "PowerShell") and "command" in input_data:
        cmd = str(input_data["command"])
        if len(cmd) > 300:
            cmd = cmd[:300] + "…"
        return f"```\n{cmd}\n```"
    parts = []
    for k, v in list(input_data.items())[:3]:
        v_str = str(v)
        if len(v_str) > 120:
            v_str = v_str[:120] + "…"
        parts.append(f"`{k}`: {v_str}")
    return "\n".join(parts)


# ====== 模块级单例 ======

_manager: Optional[CCBridgeManager] = None


def get_manager(
    feishu_sender: Optional[FeishuSender] = None,
    feishu_card_sender: Optional[FeishuCardSender] = None,
    feishu_card_updater: Optional[FeishuCardUpdater] = None,
    feishu_interactive_sender: Optional[FeishuInteractiveSender] = None,
    feishu_interactive_updater: Optional[FeishuInteractiveUpdater] = None,
    perm_keep_history: bool = False,
) -> CCBridgeManager:
    """获取/初始化单例。第一次调用必须传 sender。"""
    global _manager
    if _manager is None:
        if feishu_sender is None:
            raise RuntimeError("首次获取 manager 必须提供 feishu_sender")
        _manager = CCBridgeManager(
            feishu_sender, feishu_card_sender, feishu_card_updater,
            feishu_interactive_sender, feishu_interactive_updater,
            perm_keep_history=perm_keep_history,
        )
    return _manager
