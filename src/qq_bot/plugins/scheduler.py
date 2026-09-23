import asyncio
import inspect
import logging

from pathlib import Path

from nonebot import get_bots, require
from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot

from qq_bot.config import BotSettings, get_settings
from qq_bot.observability import metrics
from qq_bot.observability.logging import LogContext, get_logger, new_request_id, record_event
from qq_bot.services import game_digest
from qq_bot.services.ai_briefing import build_ai_briefing_message
from qq_bot.services.daily_report import build_life_evening_message, build_life_morning_message
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

# 发送失败后的延迟重投递任务保留引用，防止被垃圾回收中途丢弃
_REDELIVER_TASKS: set[asyncio.Task] = set()


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
    # 兜底可观测（S6-SCHED-03）：未预期异常不能只进 APScheduler 自己的日志。
    # 发送失败已由内部 failures 列表处理（含 skipped_* 提前返回），不会走到
    # 这个 except，因此不存在双重计数。
    try:
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
            _schedule_redelivery(
                job=job,
                bot=bot,
                group_ids=failures,
                message=message,
                settings=effective,
            )
    except Exception:
        metrics.SCHEDULED_JOBS_TOTAL.labels(job.job_type, "failed").inc()
        record_event(
            logger,
            logging.ERROR,
            "scheduled_job_failed",
            message="Scheduled job raised an unexpected exception.",
            job=job.job_id,
        )
        raise


def _schedule_redelivery(
    *,
    job: ScheduledJob,
    bot: GroupMessageBot,
    group_ids: list[int],
    message: str,
    settings: BotSettings,
) -> None:
    """失败群延迟重投递（S6-SCHED-04）。

    模糊超时（NTQQ sendMsg 挂起）在 send 层有意不立即重试——消息可能已被
    QQ 接受，立即重发有重复风险；但对日报类内容，丢失比延迟重复危害大，
    故数分钟后用同一份内容对失败群补投。宁可重复，不可丢失。
    """
    max_rounds = settings.scheduled_redeliver_max
    if max_rounds <= 0 or not group_ids:
        return
    delay = settings.scheduled_redeliver_delay_seconds

    async def _redeliver() -> None:
        remaining = list(group_ids)
        for _round in range(1, max_rounds + 1):
            await asyncio.sleep(delay)
            remaining = await send_group_messages(
                bot,
                remaining,
                message,
                named_mention_replacements=settings.named_mention_replacement_map,
            )
            if not remaining:
                metrics.SCHEDULED_REDELIVER.labels(job.job_type, "ok").inc()
                record_event(
                    logger,
                    logging.INFO,
                    "scheduled_redeliver_succeeded",
                    message="Scheduled message redelivered to failed group(s).",
                    job=job.job_id,
                )
                return
        metrics.SCHEDULED_REDELIVER.labels(job.job_type, "failed").inc()
        record_event(
            logger,
            logging.WARNING,
            "scheduled_redeliver_exhausted",
            message="Scheduled message redelivery exhausted; message not delivered.",
            job=job.job_id,
        )

    task = asyncio.ensure_future(_redeliver())
    _REDELIVER_TASKS.add(task)
    task.add_done_callback(_REDELIVER_TASKS.discard)


def _connected_onebot_bot() -> OneBotV11Bot | None:
    return next(
        (bot for bot in get_bots().values() if isinstance(bot, OneBotV11Bot)),
        None,
    )


def _make_typed_job_runner(job: ScheduledJob):
    async def _run_typed_job() -> None:
        # 定时任务同样有贯穿日志/指标/span 的合成 request_id（S6-OBS-01）
        with LogContext(request_id=new_request_id()):
            await run_scheduled_job(job, _connected_onebot_bot())

    return _run_typed_job


def _register_typed_jobs(jobs: list[ScheduledJob]) -> None:
    """为每个 typed 任务注册 cron 触发器。

    misfire_grace_time=300 + coalesce=True：APScheduler 默认宽限仅 1 秒，
    事件循环被同步 IO 卡住超过 1 秒当日任务即被静默跳过；5 分钟宽限内补跑，
    且多次错过的触发合并为一次。
    """
    for job in jobs:
        scheduler.add_job(
            _make_typed_job_runner(job),
            "cron",
            hour=job.hour,
            minute=job.minute,
            id=job.job_id,
            replace_existing=True,
            misfire_grace_time=300,
            coalesce=True,
        )
        record_event(
            logger,
            logging.INFO,
            "scheduled_job_registered",
            message=f"Registered scheduled job: {job.job_id} at {job.hour:02d}:{job.minute:02d}.",
        )


def _register_game_builders(settings: BotSettings) -> None:
    """Load and validate the game calendar, then register game_* builders.

    坏文件在这里抛 GameCalendarError → 插件加载失败 → 启动失败（S6-GAME-04：
    带病日历宁可拒绝启动，也不静默漏提醒）。
    """
    jobs = jobs_from_settings(settings)
    if not any(job.job_type in ("game_morning", "game_evening") for job in jobs):
        return
    game_digest.set_calendar(
        load_game_calendar(Path(settings.game_calendar_path)),
        path=Path(settings.game_calendar_path),
    )
    register_builder("game_morning", game_digest.build_game_morning_message)
    register_builder("game_evening", game_digest.build_game_evening_message)


def _register_life_builders(settings: BotSettings) -> None:
    """生活早/晚报生成器。板块级降级意味着配置不全是降级而非错误（与
    SEARCH_ENABLED 的宽松语义一致），因此这里没有可失败的外部加载。"""
    jobs = jobs_from_settings(settings)
    if not any(job.job_type in ("life_morning", "life_evening") for job in jobs):
        return

    async def _morning(effective: BotSettings) -> str | None:
        return await build_life_morning_message(effective)

    async def _evening(effective: BotSettings) -> str | None:
        return await build_life_evening_message(effective)

    register_builder("life_morning", _morning)
    register_builder("life_evening", _evening)


def _register_ai_briefing_builder(settings: BotSettings) -> None:
    """AI 早报生成器（S8-BRIEF）。源未配置/过期/失败在运行期返回空内容跳过，
    与生活早报一样属于降级而非加载错误，这里没有可失败的外部加载。"""
    jobs = jobs_from_settings(settings)
    if not any(job.job_type == "ai_morning" for job in jobs):
        return

    async def _briefing(effective: BotSettings) -> str | None:
        return await build_ai_briefing_message(effective)

    register_builder("ai_morning", _briefing)


settings = get_settings()
if settings.scheduled_job_list:
    # 泛化路径为唯一权威；旧 SCHEDULED_CRON_* 变量不再参与注册
    _register_game_builders(settings)
    _register_life_builders(settings)
    _register_ai_briefing_builder(settings)
    _register_typed_jobs(jobs_from_settings(settings))
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
