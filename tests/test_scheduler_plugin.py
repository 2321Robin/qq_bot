"""Scheduler plugin dispatch tests (S6-SCHED-03).

``run_scheduled_job`` is dependency-injected (bot + settings passed by the
caller) so the generalized path is testable without a live NoneBot driver.
"""

from __future__ import annotations

import bot  # noqa: F401  (initializes NoneBot before plugin imports)
import pytest
from prometheus_client import REGISTRY

from qq_bot.config import BotSettings
from qq_bot.plugins import scheduler as scheduler_plugin
from qq_bot.plugins.scheduler import run_scheduled_job
from qq_bot.services import scheduler_jobs as scheduler_jobs_module
from qq_bot.services import scheduled_sender as scheduled_sender_module
from qq_bot.services.reliability import CircuitBreaker
from qq_bot.services.scheduler_jobs import ScheduledJob
from qq_bot.services.scheduled_sender import build_scheduler_jobs_kwargs


@pytest.fixture(autouse=True)
def _fresh_onebot_breaker(monkeypatch):
    """Isolate each test from the shared module-level OneBot breaker."""
    breaker = CircuitBreaker(name="onebot", failure_threshold=3, recovery_seconds=30)
    monkeypatch.setattr(scheduled_sender_module, "_onebot_breaker", lambda: breaker)
    return breaker


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, object]] = []

    async def send_group_msg(self, *, group_id: int, message: object) -> None:
        self.sent.append((group_id, message))


def _job_metric(job: str, result: str) -> float:
    return (
        REGISTRY.get_sample_value("qq_bot_scheduled_jobs_total", {"job": job, "result": result})
        or 0.0
    )


def _configured_settings(**overrides: str) -> BotSettings:
    values = {
        "scheduled_jobs": "static@09:00",
        "scheduled_group_ids": "111",
        "allowed_group_ids": "111",
        "scheduled_message": "早上好",
    }
    values.update(overrides)
    return BotSettings(**values)  # type: ignore[arg-type]


async def test_static_job_sends_configured_message() -> None:
    fake_bot = FakeBot()
    before = _job_metric("static", "ok")

    await run_scheduled_job(
        ScheduledJob(job_type="static", hour=9, minute=0),
        fake_bot,
        settings=_configured_settings(),
    )

    assert [(gid, msg.extract_plain_text()) for gid, msg in fake_bot.sent] == [(111, "早上好")]
    assert _job_metric("static", "ok") == before + 1


async def test_empty_builder_result_skips_sending(monkeypatch: pytest.MonkeyPatch) -> None:
    async def empty_builder(settings: BotSettings) -> str | None:
        return ""

    monkeypatch.setitem(scheduler_jobs_module._CONTENT_BUILDERS, "life_morning", empty_builder)
    fake_bot = FakeBot()
    before = _job_metric("life_morning", "skipped_empty")

    await run_scheduled_job(
        ScheduledJob(job_type="life_morning", hour=7, minute=30),
        fake_bot,
        settings=_configured_settings(),
    )

    assert fake_bot.sent == []
    assert _job_metric("life_morning", "skipped_empty") == before + 1


async def test_missing_builder_skips_with_metric() -> None:
    # game_morning 未注册是 Stage B 阶段常态（Stage C 落地时注册）
    fake_bot = FakeBot()
    before = _job_metric("game_morning", "skipped_no_builder")

    await run_scheduled_job(
        ScheduledJob(job_type="game_morning", hour=7, minute=30),
        fake_bot,
        settings=_configured_settings(),
    )

    assert fake_bot.sent == []
    assert _job_metric("game_morning", "skipped_no_builder") == before + 1


async def test_no_bot_skips_with_metric() -> None:
    before = _job_metric("static", "skipped_no_bot")

    await run_scheduled_job(
        ScheduledJob(job_type="static", hour=9, minute=0),
        None,
        settings=_configured_settings(),
    )

    assert _job_metric("static", "skipped_no_bot") == before + 1


async def test_no_allowed_groups_skips_with_metric() -> None:
    fake_bot = FakeBot()
    before = _job_metric("static", "skipped_no_groups")

    await run_scheduled_job(
        ScheduledJob(job_type="static", hour=9, minute=0),
        fake_bot,
        settings=_configured_settings(scheduled_group_ids=""),
    )

    assert fake_bot.sent == []
    assert _job_metric("static", "skipped_no_groups") == before + 1


async def test_legacy_path_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _configured_settings(
        scheduled_jobs="",
        scheduled_cron_times="11:00,16:10",
    )

    assert [kwargs["id"] for kwargs in build_scheduler_jobs_kwargs(settings)] == [
        "daily_group_message_1100",
        "daily_group_message_1610",
    ]

    fake_bot = FakeBot()
    recorded: list[tuple[object, list[int], str]] = []

    async def fake_send_group_messages(bot_arg, group_ids, message, **kwargs):
        recorded.append((bot_arg, group_ids, message))
        return []

    monkeypatch.setattr(scheduler_plugin, "get_settings", lambda: settings)
    monkeypatch.setattr(scheduler_plugin, "OneBotV11Bot", FakeBot)
    monkeypatch.setattr(scheduler_plugin, "get_bots", lambda: {"conn1": fake_bot})
    monkeypatch.setattr(scheduler_plugin, "send_group_messages", fake_send_group_messages)

    await scheduler_plugin.send_daily_messages()

    assert recorded == [(fake_bot, [111], "早上好")]
