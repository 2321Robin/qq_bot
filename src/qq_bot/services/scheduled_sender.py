from __future__ import annotations

from typing import Protocol

from qq_bot.config import BotSettings


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


def filter_allowed_group_ids(group_ids: list[int], settings: BotSettings) -> list[int]:
    return [group_id for group_id in group_ids if settings.group_allowed(group_id)]


async def send_group_messages(
    bot: GroupMessageBot,
    group_ids: list[int],
    message: str,
) -> list[int]:
    failed_group_ids: list[int] = []
    for group_id in group_ids:
        try:
            await bot.send_group_msg(group_id=group_id, message=message)
        except Exception:
            failed_group_ids.append(group_id)
    return failed_group_ids
