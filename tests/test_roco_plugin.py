import pytest

import bot  # noqa: F401  # Initialize NoneBot before importing command plugins.
from qq_bot.config import BotSettings
from qq_bot.plugins import roco as roco_plugin


class FakeArgs:
    def __init__(self, text: str):
        self.text = text

    def extract_plain_text(self) -> str:
        return self.text


class FinishCalled(Exception):
    def __init__(self, message: str):
        self.message = message


class FakeEvent:
    group_id = 1001

    def __init__(self, text: str = ""):
        self.text = text

    def get_message(self) -> "FakeEvent":
        return self

    def extract_plain_text(self) -> str:
        return self.text

    def is_tome(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_roco_pet_command_replies_with_local_pet(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_finish(message: str) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(roco_plugin, "get_settings", lambda: BotSettings(allowed_group_ids="1001"))
    monkeypatch.setattr(roco_plugin.roco_pet_command, "finish", fake_finish)

    with pytest.raises(FinishCalled) as exc_info:
        await roco_plugin.handle_roco_pet(FakeEvent(), FakeArgs("迪莫"))  # type: ignore[arg-type]

    assert "迪莫" in exc_info.value.message
    assert "进化条件" in exc_info.value.message


@pytest.mark.asyncio
async def test_roco_mention_lookup_replies_when_pet_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_finish(message: str) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(roco_plugin, "get_settings", lambda: BotSettings(allowed_group_ids="1001"))
    monkeypatch.setattr(roco_plugin.roco_mention_pet, "finish", fake_finish)

    with pytest.raises(FinishCalled) as exc_info:
        await roco_plugin.handle_roco_mention_pet(FakeEvent("迪莫"))  # type: ignore[arg-type]

    assert "迪莫" in exc_info.value.message
    assert "进化条件" in exc_info.value.message


@pytest.mark.asyncio
async def test_roco_mention_lookup_returns_when_pet_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_finish(message: str) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(roco_plugin, "get_settings", lambda: BotSettings(allowed_group_ids="1001"))
    monkeypatch.setattr(roco_plugin.roco_mention_pet, "finish", fake_finish)

    await roco_plugin.handle_roco_mention_pet(FakeEvent("今天有什么新闻"))  # type: ignore[arg-type]
