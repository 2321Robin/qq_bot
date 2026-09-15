import inspect
import logging

from pathlib import Path

from nonebot import get_bots, require
from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot

from qq_bot.config import BotSettings, get_settings
from qq_bot.observability import metrics
from qq_bot.observability.logging import get_logger, record_event
from qq_bot.services import game_digest
from qq_bot.services.game_calendar import load_game_calendar
from qq_bot.services.scheduled_sender import (
    GroupMessageBot,
    build_scheduler_jobs_kwargs,
    describe_scheduler_job,
    filter_allowed_group_ids,
    send_group_messages,
)
from qq_bot.services.scheduler_jobs import (
    ScheduledJob,
    get_builder,
    jobs_from_settings,
    register_builder,
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


async def run_scheduled_job(
    job: ScheduledJob,
    bot: GroupMessageBot | None,
    *,
    settings: BotSettings | None = None,
) -> None:
    """Run one typed scheduled job through its registered content builder.

    The unified pipeline (S6-SCHED-03): trigger -> builder produces content
    (None/blank = nothing to send this round) -> the shared send path with
    retry, breaker and named-mention handling. Bot and settings are injected
    so the dispatch logic stays testable without a live NoneBot driver.
    """
    effective = settings if settings is not None else get_settings()
    record_event(
        logger,
        logging.INFO,
        "scheduled_job_triggered",
        message="Scheduled job triggered.",
        job=job.job_id,
    )
    builder = get_builder(job.job_type)
    if builder is None:
        metrics.SCHEDULED_JOBS_TOTAL.labels(job.job_type, "skipped_no_builder").inc()
        record_event(
            logger,
            logging.WARNING,
            "scheduled_job_no_builder",
            message="No content builder registered for job type.",
            job=job.job_id,
        )
        return
    # 生成器允许同步（确定性规则引擎，如 game_*）或异步（static/网络型）签名
    built = builder(effective)
    message = await built if inspect.isawaitable(built) else built
    if not message or not message.strip():
        metrics.SCHEDULED_JOBS_TOTAL.labels(job.job_type, "skipped_empty").inc()
        record_event(
            logger,
            logging.INFO,
            "scheduled_job_skipped_empty",
            message="Builder produced no content; skipping send.",
            job=job.job_id,
        )
        return
    if bot is None:
        metrics.SCHEDULED_JOBS_TOTAL.labels(job.job_type, "skipped_no_bot").inc()
        record_event(
            logger,
            logging.WARNING,
            "scheduled_no_bot_connected",
            message="No OneBot v11 bot is connected; scheduled job skipped.",
            job=job.job_id,
        )
        return
    group_ids = filter_allowed_group_ids(effective.scheduled_group_id_list, effective)
    if not group_ids:
        metrics.SCHEDULED_JOBS_TOTAL.labels(job.job_type, "skipped_no_groups").inc()
        return
    record_event(
        logger,
        logging.INFO,
        "scheduled_sending",
        message=f"Sending scheduled job content to {len(group_ids)} group(s).",
        job=job.job_id,
    )
    failures = await send_group_messages(
        bot,
        group_ids,
        message,
        named_mention_replacements=effective.named_mention_replacement_map,
    )
    result = "failed" if failures else "ok"
    metrics.SCHEDULED_JOBS_TOTAL.labels(job.job_type, result).inc()
    record_event(
        logger,
        logging.INFO,
        "scheduled_job_finished",
        message=(
            f"Scheduled job finished: {len(group_ids) - len(failures)} succeeded, "
            f"{len(failures)} failed."
        ),
        job=job.job_id,
    )
    if failures:
        record_event(
            logger,
            logging.WARNING,
            "scheduled_partial_failure",
            message=f"Scheduled message failed for {len(failures)} group(s).",
            job=job.job_id,
        )


def _connected_onebot_bot() -> OneBotV11Bot | None:
    return next(
        (bot for bot in get_bots().values() if isinstance(bot, OneBotV11Bot)),
        None,
    )


def _make_typed_job_runner(job: ScheduledJob):
    async def _run_typed_job() -> None:
        await run_scheduled_job(job, _connected_onebot_bot())

    return _run_typed_job


def _register_game_builders(settings: BotSettings) -> None:
    """Load and validate the game calendar, then register game_* builders.

    坏文件在这里抛 GameCalendarError → 插件加载失败 → 启动失败（S6-GAME-04：
    带病日历宁可拒绝启动，也不静默漏提醒）。
    """
    jobs = jobs_from_settings(settings)
    if not any(job.job_type in ("game_morning", "game_evening") for job in jobs):
        return
    game_digest.set_calendar(load_game_calendar(Path(settings.game_calendar_path)))
    register_builder("game_morning", game_digest.build_game_morning_message)
    register_builder("game_evening", game_digest.build_game_evening_message)


settings = get_settings()
if settings.scheduled_job_list:
    # 泛化路径为唯一权威；旧 SCHEDULED_CRON_* 变量不再参与注册
    _register_game_builders(settings)
    for job in jobs_from_settings(settings):
        scheduler.add_job(
            _make_typed_job_runner(job),
            "cron",
            hour=job.hour,
            minute=job.minute,
            id=job.job_id,
            replace_existing=True,
        )
        record_event(
            logger,
            logging.INFO,
            "scheduled_job_registered",
            message=f"Registered scheduled job: {job.job_id} at {job.hour:02d}:{job.minute:02d}.",
        )
elif settings.scheduled_enabled():
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
