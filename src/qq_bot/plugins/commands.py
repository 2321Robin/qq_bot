from nonebot import on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent

from qq_bot.config import get_settings
from qq_bot.services.help_text import build_help_text
from qq_bot.services.onebot_send import finish_with_send_errors_logged
from qq_bot.version import get_version


help_command = on_command("help", aliases={"帮助"}, priority=5, block=True)
version_command = on_command("version", aliases={"版本"}, priority=5, block=True)


@help_command.handle()
async def handle_help(event: GroupMessageEvent) -> None:
    settings = get_settings()
    if not settings.group_allowed(event.group_id):
        return

    await finish_with_send_errors_logged(help_command, build_help_text(settings.ai_prefix))


@version_command.handle()
async def handle_version(event: GroupMessageEvent) -> None:
    settings = get_settings()
    if not settings.group_allowed(event.group_id):
        return

    await finish_with_send_errors_logged(
        version_command,
        f"当前版本：{get_version()}",
    )

