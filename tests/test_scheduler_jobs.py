"""Scheduled job registry and content builders (S6-SCHED-02)."""

from __future__ import annotations

import pytest

from qq_bot.config import BotSettings
from qq_bot.services.scheduler_jobs import (
    ScheduledJob,
    build_static_message,
    get_builder,
    jobs_from_settings,
    register_builder,
)


def test_jobs_from_settings_parses_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCHEDULED_JOBS", "life_morning@07:30,game_morning@07:30")
    jobs = jobs_from_settings(BotSettings())
    assert [job.job_id for job in jobs] == ["life_morning_0730", "game_morning_0730"]


def test_scheduled_job_id_pads_hour_and_minute() -> None:
    assert ScheduledJob(job_type="static", hour=9, minute=5).job_id == "static_0905"


def test_register_builder_rejects_unknown_type() -> None:
    async def builder(settings: BotSettings) -> str | None:
        return None

    with pytest.raises(ValueError, match="unknown job type"):
        register_builder("nonsense", builder)


async def test_static_builder_returns_configured_message() -> None:
    message = await build_static_message(BotSettings(scheduled_message="早上好"))
    assert message == "早上好"


def test_static_builder_registered_at_import() -> None:
    builder = get_builder("static")
    assert builder is build_static_message


def test_unregistered_type_has_no_builder() -> None:
    # Stage B常态：game_*/life_* 生成器由 Stage C/D 注册
    assert get_builder("game_morning") is None
    assert get_builder("life_morning") is None
