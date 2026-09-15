"""Scheduled job model and content-builder registry (S6-SCHED-02).

``SCHEDULED_JOBS`` entries become :class:`ScheduledJob` objects dispatched
through registered content builders. Stage B registers only ``static``;
``game_*`` (Stage C) and ``life_*`` (Stage D) builders register through
``register_builder`` when they land. Unregistered types are skipped at
runtime as ``skipped_no_builder``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from qq_bot.config import SCHEDULED_JOB_TYPES, BotSettings

ContentBuilder = Callable[[BotSettings], Awaitable[str | None]]

_CONTENT_BUILDERS: dict[str, ContentBuilder] = {}


@dataclass(frozen=True)
class ScheduledJob:
    job_type: str
    hour: int
    minute: int

    @property
    def job_id(self) -> str:
        return f"{self.job_type}_{self.hour:02d}{self.minute:02d}"


def jobs_from_settings(settings: BotSettings) -> list[ScheduledJob]:
    return [
        ScheduledJob(job_type=job_type, hour=hour, minute=minute)
        for job_type, hour, minute in settings.scheduled_job_list
    ]


def register_builder(job_type: str, builder: ContentBuilder) -> None:
    if job_type not in SCHEDULED_JOB_TYPES:
        raise ValueError(f"register_builder: unknown job type: {job_type}")
    _CONTENT_BUILDERS[job_type] = builder


async def build_static_message(settings: BotSettings) -> str | None:
    return settings.scheduled_message


register_builder("static", build_static_message)


def get_builder(job_type: str) -> ContentBuilder | None:
    return _CONTENT_BUILDERS.get(job_type)
