"""Quota & budget tests (S4-QUOTA-01..08)."""

from __future__ import annotations

import pytest

from qq_bot.config import BotSettings
from qq_bot.observability.cost import CostEstimate
from qq_bot.services.chat_memory import ChatMemoryRepository
from qq_bot.services.quota import QuotaService, active_quota_scope, quota_scope

ACTUAL = CostEstimate(0.02, "USD", "actual")
ESTIMATED = CostEstimate(0.02, "USD", "estimated")
UNKNOWN = CostEstimate(None, None, "unknown")


@pytest.fixture
async def repository(tmp_path):
    repo = ChatMemoryRepository(tmp_path / "quota.sqlite3", retention_days=30)
    await repo.open()
    try:
        yield repo
    finally:
        await repo.close()


def _settings(**overrides) -> BotSettings:
    return BotSettings(**overrides)


async def test_window_rate_limit_rejects_inside_and_allows_after_window(
    repository,
) -> None:
    clock = [1000.0]
    service = QuotaService(
        _settings(quota_enabled=True, quota_rate_limit_per_minute=2),
        repository,
        clock=lambda: clock[0],
    )
    assert (await service.check_admission(scope_type="group", scope_id=1001)).allowed
    assert (await service.check_admission(scope_type="group", scope_id=1001)).allowed
    decision = await service.check_admission(scope_type="group", scope_id=1001)
    assert not decision.allowed
    assert decision.reason == "rate"
    # different scope is not affected by the group window
    assert (await service.check_admission(scope_type="group", scope_id=1002)).allowed
    # window slides: after 60s the same scope is admitted again
    clock[0] += 61.0
    assert (await service.check_admission(scope_type="group", scope_id=1001)).allowed


async def test_rate_denial_is_persisted(repository) -> None:
    service = QuotaService(
        _settings(quota_enabled=True, quota_rate_limit_per_minute=1),
        repository,
    )
    assert (await service.check_admission(scope_type="group", scope_id=1001)).allowed
    decision = await service.check_admission(scope_type="group", scope_id=1001)
    assert decision.reason == "rate"
    events = await service.recent_failures()
    assert events[0]["kind"] == "rate_denied"
    assert events[0]["reason"] == "rate"
    assert events[0]["scope_id"] == 1001


async def test_actual_cost_over_global_daily_limit_is_rejected(repository) -> None:
    service = QuotaService(
        _settings(quota_enabled=True, quota_daily_cost_limit_usd=0.01),
        repository,
    )
    assert (await service.check_admission(scope_type="group", scope_id=1001)).allowed
    await service.record_usage(scope_type="group", scope_id=1001, tokens=10, cost=ACTUAL)
    decision = await service.check_admission(scope_type="group", scope_id=1002)
    assert not decision.allowed
    assert decision.reason == "cost"


async def test_actual_cost_over_group_daily_limit_is_rejected(repository) -> None:
    service = QuotaService(
        _settings(
            quota_enabled=True,
            quota_daily_cost_limit_usd=0.0,
            quota_group_daily_cost_limit_usd=0.01,
        ),
        repository,
    )
    assert (await service.check_admission(scope_type="group", scope_id=1001)).allowed
    await service.record_usage(scope_type="group", scope_id=1001, tokens=10, cost=ACTUAL)
    # the spending group is now over its own cap; the other group is not
    assert not (await service.check_admission(scope_type="group", scope_id=1001)).allowed
    assert (await service.check_admission(scope_type="group", scope_id=1002)).allowed


async def test_estimated_and_unknown_costs_never_reject(repository) -> None:
    service = QuotaService(
        _settings(
            quota_enabled=True,
            quota_daily_cost_limit_usd=0.01,
            quota_group_daily_cost_limit_usd=0.01,
        ),
        repository,
    )
    await service.record_usage(scope_type="group", scope_id=1001, tokens=10, cost=ESTIMATED)
    await service.record_usage(scope_type="group", scope_id=1001, tokens=10, cost=UNKNOWN)
    assert (await service.check_admission(scope_type="group", scope_id=1001)).allowed
    # estimated/unknown costs are visible to the admin as events
    kinds = {entry["kind"] for entry in await service.recent_failures()}
    assert kinds == {"cost_estimated"}
    # the budget row only counted actual cost
    summary = await service.summary(scope_type="group", scope_id=1001)
    assert summary["cost_usd"] == 0.0
    assert summary["tokens"] == 20


async def test_usage_survives_restart_via_sqlite(repository) -> None:
    first = QuotaService(
        _settings(quota_enabled=True, quota_daily_cost_limit_usd=0.01),
        repository,
    )
    await first.record_usage(scope_type="group", scope_id=1001, tokens=5, cost=ACTUAL)
    # a fresh service over the same database still sees the spent cost
    second = QuotaService(
        _settings(quota_enabled=True, quota_daily_cost_limit_usd=0.01),
        repository,
    )
    decision = await second.check_admission(scope_type="group", scope_id=1002)
    assert not decision.allowed
    assert decision.reason == "cost"
    summary = await second.summary(scope_type="group", scope_id=1001)
    assert summary["cost_usd"] == 0.02
    assert summary["requests"] == 1


async def test_quota_disabled_allows_everything(repository) -> None:
    service = QuotaService(
        _settings(
            quota_enabled=False,
            quota_rate_limit_per_minute=1,
            quota_daily_cost_limit_usd=0.0,
        ),
        repository,
    )
    for _ in range(5):
        assert (await service.check_admission(scope_type="group", scope_id=1001)).allowed
    await service.record_usage(scope_type="group", scope_id=1001, tokens=1, cost=ACTUAL)
    assert await service.recent_failures() == []


async def test_summary_reports_usage_and_caps(repository) -> None:
    service = QuotaService(
        _settings(
            quota_enabled=True,
            quota_rate_limit_per_minute=30,
            quota_daily_cost_limit_usd=2.0,
            quota_group_daily_cost_limit_usd=0.5,
        ),
        repository,
    )
    await service.record_usage(scope_type="group", scope_id=1001, tokens=150, cost=ACTUAL)
    summary = await service.summary(scope_type="group", scope_id=1001)
    assert summary["requests"] == 1
    assert summary["tokens"] == 150
    assert summary["cost_usd"] == 0.02
    assert summary["rate_limit_per_minute"] == 30
    assert summary["daily_cost_limit_usd"] == 2.0
    assert summary["group_daily_cost_limit_usd"] == 0.5


def test_quota_scope_contextvar_binds_and_restores() -> None:
    assert active_quota_scope() is None
    with quota_scope("group", 1001):
        assert active_quota_scope() == ("group", 1001)
    assert active_quota_scope() is None
