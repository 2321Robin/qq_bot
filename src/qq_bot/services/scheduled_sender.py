from __future__ import annotations

from typing import Protocol

from nonebot import logger

from qq_bot.config import BotSettings
from qq_bot.services.message_formatting import replace_named_mentions


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
) -> list[int]:
    failed_group_ids: list[int] = []
    formatted_message = replace_named_mentions(message)
    for group_id in group_ids:
        try:
            await bot.send_group_msg(group_id=group_id, message=formatted_message)
        except Exception:
            logger.exception(f"Scheduled message send failed for group {group_id}.")
            failed_group_ids.append(group_id)
    return failed_group_ids
