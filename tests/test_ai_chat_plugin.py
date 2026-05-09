import pytest
from nonebot.adapters.onebot.v11 import Message

from qq_bot.config import BotSettings
from qq_bot.plugins import ai_chat as ai_chat_plugin


class FakeEvent:
    group_id = 1001

    def __init__(self, text: str):
        self.text = text

    def get_message(self) -> "FakeEvent":
        return self

    def extract_plain_text(self) -> str:
        return self.text

    def is_tome(self) -> bool:
        return False


class FinishCalled(Exception):
    def __init__(self, message: object):
        self.message = message


@pytest.mark.asyncio
async def test_ai_chat_formats_named_mentions_in_final_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_request_ai_reply(prompt: str, *, settings: BotSettings) -> str:
        assert prompt == "提醒我"
        assert settings.ai_api_key == "secret"
        return "好的，@小呱呱 会收到提醒"

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", ai_api_key="secret"),
    )
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled) as exc_info:
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 提醒我"))  # type: ignore[arg-type]

    message = exc_info.value.message
    assert isinstance(message, Message)
    assert message[0].type == "text"
    assert message[0].data["text"] == "好的，"
    assert message[1].type == "at"
    assert message[1].data["qq"] == "2854203313"
    assert message[2].type == "text"
    assert message[2].data["text"] == " 会收到提醒"
