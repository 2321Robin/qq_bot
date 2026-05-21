from pathlib import Path

import pytest

import bot  # noqa: F401  # Initialize NoneBot before importing command plugins.
from qq_bot.config import BotSettings
from qq_bot.plugins import roco_counter as roco_counter_plugin


class FakeArgs:
    def __init__(self, text: str):
        self.text = text

    def extract_plain_text(self) -> str:
        return self.text


class FakeEvent:
    group_id = 1001
    user_id = 2002


class FinishCalled(Exception):
    def __init__(self, message: object):
        self.message = message


@pytest.mark.asyncio
async def test_counter_command_shows_empty_summary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(
        roco_counter_plugin,
        "get_settings",
        lambda: BotSettings(
            allowed_group_ids="1001",
            roco_counter_path=str(tmp_path / "counter.sqlite3"),
            roco_counter_season="S2",
        ),
    )
    monkeypatch.setattr(roco_counter_plugin.roco_counter_command, "finish", fake_finish)

    with pytest.raises(FinishCalled) as exc_info:
        await roco_counter_plugin.handle_roco_counter(FakeEvent(), FakeArgs(""))  # type: ignore[arg-type]

    assert exc_info.value.message == "S2 捕捉计数器\n暂无记录。发送 /计数 迪莫 开始记录。"


@pytest.mark.asyncio
async def test_counter_command_records_normal_capture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(
        roco_counter_plugin,
        "get_settings",
        lambda: BotSettings(
            allowed_group_ids="1001",
            roco_counter_path=str(tmp_path / "counter.sqlite3"),
            roco_counter_season="S2",
        ),
    )
    monkeypatch.setattr(roco_counter_plugin.roco_counter_command, "finish", fake_finish)

    with pytest.raises(FinishCalled) as exc_info:
        await roco_counter_plugin.handle_roco_counter(FakeEvent(), FakeArgs("迪莫"))  # type: ignore[arg-type]

    message = str(exc_info.value.message)
    assert "S2 迪莫 +1" in message
    assert "当前：1 | 异色：0" in message
    assert "总捕捉：1 | 总异色：0" in message


@pytest.mark.asyncio
async def test_counter_command_records_shiny_capture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(
        roco_counter_plugin,
        "get_settings",
        lambda: BotSettings(
            allowed_group_ids="1001",
            roco_counter_path=str(tmp_path / "counter.sqlite3"),
            roco_counter_season="S2",
        ),
    )
    monkeypatch.setattr(roco_counter_plugin.roco_counter_command, "finish", fake_finish)

    with pytest.raises(FinishCalled) as exc_info:
        await roco_counter_plugin.handle_roco_counter(FakeEvent(), FakeArgs("异色 迪莫"))  # type: ignore[arg-type]

    message = str(exc_info.value.message)
    assert "S2 异色 迪莫 +1（第 1 只是异色）" in message
    assert "当前：1 | 异色：1" in message
    assert "总捕捉：1 | 总异色：1" in message


@pytest.mark.asyncio
async def test_counter_command_records_later_shiny_index(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    messages: list[object] = []

    async def fake_finish(message: object) -> None:
        messages.append(message)
        raise FinishCalled(message)

    monkeypatch.setattr(
        roco_counter_plugin,
        "get_settings",
        lambda: BotSettings(
            allowed_group_ids="1001",
            roco_counter_path=str(tmp_path / "counter.sqlite3"),
            roco_counter_season="S2",
        ),
    )
    monkeypatch.setattr(roco_counter_plugin.roco_counter_command, "finish", fake_finish)

    for args in ["迪莫", "迪莫", "异色 迪莫"]:
        with pytest.raises(FinishCalled):
            await roco_counter_plugin.handle_roco_counter(FakeEvent(), FakeArgs(args))  # type: ignore[arg-type]

    assert "S2 异色 迪莫 +1（第 3 只是异色）" in str(messages[-1])


@pytest.mark.asyncio
async def test_counter_command_summary_shows_shiny_indexes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    messages: list[object] = []

    async def fake_finish(message: object) -> None:
        messages.append(message)
        raise FinishCalled(message)

    monkeypatch.setattr(
        roco_counter_plugin,
        "get_settings",
        lambda: BotSettings(
            allowed_group_ids="1001",
            roco_counter_path=str(tmp_path / "counter.sqlite3"),
            roco_counter_season="S2",
        ),
    )
    monkeypatch.setattr(roco_counter_plugin.roco_counter_command, "finish", fake_finish)

    for args in ["迪莫", "迪莫", "异色 迪莫", "迪莫", "异色 迪莫", ""]:
        with pytest.raises(FinishCalled):
            await roco_counter_plugin.handle_roco_counter(FakeEvent(), FakeArgs(args))  # type: ignore[arg-type]

    assert "迪莫：5（异色 2：第 3、5 只）" in str(messages[-1])


@pytest.mark.asyncio
async def test_counter_command_returns_usage_for_missing_shiny_pet(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(
        roco_counter_plugin,
        "get_settings",
        lambda: BotSettings(
            allowed_group_ids="1001",
            roco_counter_path=str(tmp_path / "counter.sqlite3"),
            roco_counter_season="S2",
        ),
    )
    monkeypatch.setattr(roco_counter_plugin.roco_counter_command, "finish", fake_finish)

    with pytest.raises(FinishCalled) as exc_info:
        await roco_counter_plugin.handle_roco_counter(FakeEvent(), FakeArgs("异色"))  # type: ignore[arg-type]

    assert str(exc_info.value.message).startswith("用法：/计数 迪莫")


@pytest.mark.asyncio
async def test_counter_command_returns_for_disallowed_group(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fake_finish(message: object) -> None:
        raise AssertionError("finish should not be called")

    monkeypatch.setattr(
        roco_counter_plugin,
        "get_settings",
        lambda: BotSettings(
            allowed_group_ids="2002",
            roco_counter_path=str(tmp_path / "counter.sqlite3"),
            roco_counter_season="S2",
        ),
    )
    monkeypatch.setattr(roco_counter_plugin.roco_counter_command, "finish", fake_finish)

    await roco_counter_plugin.handle_roco_counter(FakeEvent(), FakeArgs("迪莫"))  # type: ignore[arg-type]
