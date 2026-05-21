import bot  # noqa: F401

from qq_bot.plugins import commands


def test_commands_plugin_does_not_register_ping_command() -> None:
    assert not hasattr(commands, "ping_command")
    assert not hasattr(commands, "handle_ping")
