from nonebot import on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent

from qq_bot.config import get_settings
from qq_bot.services.help_text import build_help_text


help_command = on_command("help", aliases={"帮助"}, priority=5, block=True)
ping_command = on_command("ping", aliases={"状态"}, priority=5, block=True)


@help_command.handle()
async def handle_help(event: GroupMessageEvent) -> None:
    settings = get_settings()
    if not settings.group_allowed(event.group_id):
        return

    await help_command.finish(build_help_text(settings.ai_prefix))


@ping_command.handle()
async def handle_ping(event: GroupMessageEvent) -> None:
    settings = get_settings()
    if not settings.group_allowed(event.group_id):
        return

    await ping_command.finish("pong")
