from nonebot import get_bots, logger, require
from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot

from qq_bot.config import get_settings
from qq_bot.services.scheduled_sender import build_scheduler_job_kwargs, send_group_messages

require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler  # noqa: E402


async def send_daily_messages() -> None:
    settings = get_settings()
    if not settings.scheduled_enabled():
        logger.info("Scheduled messages are disabled because no target groups are configured.")
        return

    bot = next(
        (
            connected_bot
            for connected_bot in get_bots().values()
            if isinstance(connected_bot, OneBotV11Bot)
        ),
        None,
    )
    if bot is None:
        logger.warning("No OneBot v11 bot is connected; scheduled message skipped.")
        return

    failures = await send_group_messages(
        bot,
        settings.scheduled_group_id_list,
        settings.scheduled_message,
    )
    for group_id in failures:
        logger.warning(f"Scheduled message failed for group {group_id}.")


settings = get_settings()
if settings.scheduled_enabled():
    scheduler.add_job(send_daily_messages, **build_scheduler_job_kwargs(settings))
