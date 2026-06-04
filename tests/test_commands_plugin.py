import bot  # noqa: F401

import pytest

from qq_bot.config import BotSettings

from qq_bot.plugins import commands


class FakeEvent:
    group_id = 1001


class FinishCalled(Exception):
    def __init__(self, message: object):
        self.message = message


def _command_names(matcher: object) -> set[str]:
    names: set[str] = set()
    for checker in matcher.rule.checkers:
        command_rule = getattr(checker, "call", None)
        for command in getattr(command_rule, "cmds", ()):
            names.add("/" + " ".join(command))
    return names


def test_commands_plugin_does_not_register_ping_command() -> None:
    assert not hasattr(commands, "ping_command")
    assert not hasattr(commands, "handle_ping")


def test_version_command_registers_english_and_chinese_names() -> None:
    assert _command_names(commands.version_command) == {"/version", "/版本"}


@pytest.mark.asyncio
async def test_version_command_replies_with_current_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(commands, "get_settings", lambda: BotSettings(allowed_group_ids="1001"))
    monkeypatch.setattr(commands, "get_version", lambda: "9.8.7")
    monkeypatch.setattr(commands.version_command, "finish", fake_finish)

    with pytest.raises(FinishCalled) as exc_info:
        await commands.handle_version(FakeEvent())  # type: ignore[arg-type]

    assert exc_info.value.message == "当前版本：9.8.7"
