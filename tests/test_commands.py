import asyncio

from server import commands


class FakeManager:
    def __init__(self) -> None:
        self.prompts: list[tuple[str, str, str]] = []
        self.notifications: list[tuple[str, str]] = []

    async def resolve_permission(self, *_args, **_kwargs) -> bool:
        return False

    async def send_prompt(self, open_id: str, chat_id: str, text: str) -> bool:
        self.prompts.append((open_id, chat_id, text))
        return True

    async def _notify(self, chat_id: str, text: str) -> None:
        self.notifications.append((chat_id, text))


def test_parse_forwards_unknown_slash_commands_to_claude() -> None:
    assert commands._parse("/compact") == (None, [])
    assert commands._parse("/resume abc") == (None, [])


def test_parse_keeps_known_feishu_commands() -> None:
    assert commands._parse("/help") == ("help", [])
    assert commands._parse("help") == ("help", [])
    assert commands._parse("/cc help") == ("help", [])


def test_dispatch_sends_compact_to_current_session() -> None:
    manager = FakeManager()

    asyncio.run(commands.dispatch(manager, "open-id", "chat-id", "/compact"))

    assert manager.prompts == [("open-id", "chat-id", "/compact")]
    assert manager.notifications == []
