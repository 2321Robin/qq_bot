from __future__ import annotations

import asyncio

from collections.abc import Awaitable, Callable
from typing import Protocol

from nonebot import logger

from qq_bot.config import BotSettings
from qq_bot.services.message_formatting import replace_named_mentions
from qq_bot.services.onebot_send import is_send_timeout_error


class GroupMessageBot(Protocol):
    async def send_group_msg(self, *, group_id: int, message: str) -> object:
        raise NotImplementedError


def build_scheduler_job_kwargs(settings: BotSettings) -> dict[str, object]:
    return {
        "trigger": "cron",
        "hour": settings.scheduled_cron_hour,
        "minute": settings.scheduled_cron_minute,
        "id": "daily_group_message",
        "replace_existing": True,
    }


def build_scheduler_jobs_kwargs(settings: BotSettings) -> list[dict[str, object]]:
    return [
        {
            "trigger": "cron",
            "hour": hour,
            "minute": minute,
            "id": f"daily_group_message_{hour:02d}{minute:02d}",
            "replace_existing": True,
        }
        for hour, minute in settings.scheduled_cron_time_list
    ]


def describe_scheduler_job(job_kwargs: dict[str, object]) -> str:
    return f"{job_kwargs['id']} at {job_kwargs['hour']:02d}:{job_kwargs['minute']:02d}"


def filter_allowed_group_ids(group_ids: list[int], settings: BotSettings) -> list[int]:
    return [group_id for group_id in group_ids if settings.group_allowed(group_id)]


async def send_group_messages(
    bot: GroupMessageBot,
    group_ids: list[int],
    message: str,
    *,
    max_attempts: int = 3,
    retry_delay_seconds: float = 30.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> list[int]:
    failed_group_ids: list[int] = []
    formatted_message = replace_named_mentions(message)
    for group_id in group_ids:
        for attempt in range(1, max_attempts + 1):
            try:
                await bot.send_group_msg(group_id=group_id, message=formatted_message)
                break
            except Exception as exc:
                if is_send_timeout_error(exc):
                    logger.warning(
                        f"Scheduled message send timed out and may not be visible in QQ for group {group_id} "
                        f"(attempt {attempt}/{max_attempts})."
                    )
                    failed_group_ids.append(group_id)
                    break
                final_attempt = attempt >= max_attempts
                if final_attempt:
                    logger.exception(
                        f"Scheduled message send failed for group {group_id} "
                        f"(attempt {attempt}/{max_attempts})."
                    )
                    failed_group_ids.append(group_id)
                    break
                logger.warning(
                    f"Scheduled message send failed for group {group_id}; retrying in "
                    f"{retry_delay_seconds:g}s (attempt {attempt}/{max_attempts}): {exc!r}"
                )
                await sleep(retry_delay_seconds)
    return failed_group_ids
