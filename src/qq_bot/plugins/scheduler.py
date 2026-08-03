import logging

from nonebot import get_bots, require
from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot

from qq_bot.config import get_settings
from qq_bot.observability.logging import get_logger, record_event
from qq_bot.services.scheduled_sender import (
    build_scheduler_jobs_kwargs,
    describe_scheduler_job,
    filter_allowed_group_ids,
    send_group_messages,
)

require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler  # noqa: E402

logger = get_logger("qq_bot.scheduler")


async def send_daily_messages() -> None:
    settings = get_settings()
    record_event(
        logger, logging.INFO, "scheduled_job_triggered", message="Scheduled message job triggered."
    )
    if not settings.scheduled_enabled():
        record_event(
            logger,
            logging.INFO,
            "scheduled_disabled",
            message="Scheduled messages are disabled because no target groups are configured.",
        )
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
        record_event(
            logger,
            logging.WARNING,
            "scheduled_no_bot_connected",
            message="No OneBot v11 bot is connected; scheduled message skipped.",
        )
        return

    group_ids = filter_allowed_group_ids(settings.scheduled_group_id_list, settings)
    if not group_ids:
        record_event(
            logger,
            logging.INFO,
            "scheduled_no_target_groups",
            message="Scheduled messages skipped because no configured target groups are allowed.",
        )
        return

    record_event(
        logger,
        logging.INFO,
        "scheduled_sending",
        message=f"Sending scheduled message to {len(group_ids)} group(s).",
    )

    failures = await send_group_messages(
        bot,
        group_ids,
        settings.scheduled_message,
        named_mention_replacements=settings.named_mention_replacement_map,
    )
    successful_count = len(group_ids) - len(failures)
    record_event(
        logger,
        logging.INFO,
        "scheduled_job_finished",
        message=(
            f"Scheduled message job finished: {successful_count} succeeded, {len(failures)} failed."
        ),
    )
    if failures:
        record_event(
            logger,
            logging.WARNING,
            "scheduled_partial_failure",
            message=f"Scheduled message failed for {len(failures)} group(s).",
        )


settings = get_settings()
if settings.scheduled_enabled():
    for job_kwargs in build_scheduler_jobs_kwargs(settings):
        scheduler.add_job(send_daily_messages, **job_kwargs)
        record_event(
            logger,
            logging.INFO,
            "scheduled_job_registered",
            message=f"Registered scheduled message job: {describe_scheduler_job(job_kwargs)}.",
        )
else:
    record_event(
        logger,
        logging.INFO,
        "scheduled_jobs_not_registered",
        message=(
            "Scheduled message jobs were not registered because scheduled messages are disabled."
        ),
    )
