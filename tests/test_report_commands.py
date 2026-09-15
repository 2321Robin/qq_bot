"""Manual life-report command tests (S6-REPORT-07)."""

import bot  # noqa: F401  # Initialize NoneBot before importing command plugins.
import pytest
from qq_bot.config import BotSettings
from qq_bot.plugins import report_commands as report_commands_plugin


class FinishCalled(Exception):
    def __init__(self, message: object):
        self.message = message


class FakeEvent:
    group_id = 1001


def _install_common_patches(
    monkeypatch: pytest.MonkeyPatch, *, allowed_group_ids: str = "1001"
) -> None:
    monkeypatch.setattr(
        report_commands_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids=allowed_group_ids),
    )
    monkeypatch.setattr(report_commands_plugin, "_resolve_report_client", lambda: "fake-client")


async def test_morning_command_sends_report(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_common_patches(monkeypatch)

    async def fake_build(settings, *, client, kind=None):
        assert client == "fake-client"
        return "【早报】内容"

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(
        report_commands_plugin,
        "build_life_morning_message",
        lambda settings, *, client=None: fake_build(settings, client=client),
    )
    monkeypatch.setattr(report_commands_plugin.life_morning_command, "finish", fake_finish)

    with pytest.raises(FinishCalled) as exc_info:
        await report_commands_plugin.handle_life_morning(FakeEvent())  # type: ignore[arg-type]

    assert "【早报】" in str(exc_info.value.message)


async def test_evening_command_sends_report(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_common_patches(monkeypatch)

    async def fake_build(settings, *, client, kind=None):
        return "【晚报】内容"

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(
        report_commands_plugin,
        "build_life_evening_message",
        lambda settings, *, client=None: fake_build(settings, client=client),
    )
    monkeypatch.setattr(report_commands_plugin.life_evening_command, "finish", fake_finish)

    with pytest.raises(FinishCalled) as exc_info:
        await report_commands_plugin.handle_life_evening(FakeEvent())  # type: ignore[arg-type]

    assert "【晚报】" in str(exc_info.value.message)


async def test_command_ignored_in_disallowed_group(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_common_patches(monkeypatch, allowed_group_ids="9999")

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(report_commands_plugin.life_morning_command, "finish", fake_finish)

    await report_commands_plugin.handle_life_morning(FakeEvent())  # type: ignore[arg-type]
    # 未抛 FinishCalled 即通过：非允许群不回复
