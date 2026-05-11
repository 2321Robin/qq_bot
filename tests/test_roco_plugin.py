import pytest
from pathlib import Path

import bot  # noqa: F401  # Initialize NoneBot before importing command plugins.
from qq_bot.config import BotSettings
from qq_bot.plugins import roco as roco_plugin
from qq_bot.services.roco_pets import PetRecord


def test_roco_mention_matcher_does_not_block_ai_chat() -> None:
    assert not roco_plugin.roco_mention_pet.block


class FakeArgs:
    def __init__(self, text: str):
        self.text = text

    def extract_plain_text(self) -> str:
        return self.text


class FinishCalled(Exception):
    def __init__(self, message: object):
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
async def test_roco_pet_command_replies_with_local_pet(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    def fake_card_path(record: PetRecord) -> object:
        assert record.name == "迪莫"
        path = tmp_path / "001.png"
        path.write_bytes(b"png")
        return path

    monkeypatch.setattr(roco_plugin, "get_settings", lambda: BotSettings(allowed_group_ids="1001"))
    monkeypatch.setattr(roco_plugin, "pet_card_path", fake_card_path)
    monkeypatch.setattr(roco_plugin.roco_pet_command, "finish", fake_finish)

    with pytest.raises(FinishCalled) as exc_info:
        await roco_plugin.handle_roco_pet(FakeEvent(), FakeArgs("迪莫"))  # type: ignore[arg-type]

    message = exc_info.value.message
    assert "image" in str(message)
    assert (tmp_path / "001.png").resolve().as_posix() in str(message)


@pytest.mark.asyncio
async def test_roco_mention_lookup_replies_when_pet_exists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    def fake_card_path(record: PetRecord) -> object:
        assert record.name == "迪莫"
        path = tmp_path / "001.png"
        path.write_bytes(b"png")
        return path

    monkeypatch.setattr(roco_plugin, "get_settings", lambda: BotSettings(allowed_group_ids="1001"))
    monkeypatch.setattr(roco_plugin, "pet_card_path", fake_card_path)
    monkeypatch.setattr(roco_plugin.roco_mention_pet, "finish", fake_finish)

    with pytest.raises(FinishCalled) as exc_info:
        await roco_plugin.handle_roco_mention_pet(FakeEvent("迪莫"))  # type: ignore[arg-type]

    message = exc_info.value.message
    assert "image" in str(message)
    assert (tmp_path / "001.png").resolve().as_posix() in str(message)


@pytest.mark.asyncio
async def test_roco_pet_command_falls_back_to_text_when_card_file_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    def fake_card_path(record: PetRecord) -> object:
        assert record.name == "迪莫"
        return type("FakePath", (), {"exists": lambda self: False})()

    monkeypatch.setattr(roco_plugin, "get_settings", lambda: BotSettings(allowed_group_ids="1001"))
    monkeypatch.setattr(roco_plugin, "pet_card_path", fake_card_path)
    monkeypatch.setattr(roco_plugin.roco_pet_command, "finish", fake_finish)

    with pytest.raises(FinishCalled) as exc_info:
        await roco_plugin.handle_roco_pet(FakeEvent(), FakeArgs("迪莫"))  # type: ignore[arg-type]

    assert "迪莫" in str(exc_info.value.message)
    assert "进化条件" in str(exc_info.value.message)


@pytest.mark.asyncio
async def test_roco_mention_lookup_returns_when_pet_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(roco_plugin, "get_settings", lambda: BotSettings(allowed_group_ids="1001"))
    monkeypatch.setattr(roco_plugin.roco_mention_pet, "finish", fake_finish)

    await roco_plugin.handle_roco_mention_pet(FakeEvent("今天有什么新闻"))  # type: ignore[arg-type]
