import asyncio

from qq_bot.config import BotSettings
from qq_bot.services.auto_chat import AutoChatState


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _settings(**overrides) -> BotSettings:
    defaults = {
        "auto_chat_cooldown_seconds": 300.0,
        "auto_chat_hourly_limit": 2,
        "auto_chat_daily_limit": 3,
    }
    defaults.update(overrides)
    return BotSettings(**defaults)


def test_limit_reason_empty_then_cooldown_after_acquire() -> None:
    clock = FakeClock()
    state = AutoChatState(clock=clock)
    settings = _settings()
    assert state.limit_reason(1, settings=settings) == ""
    assert state.acquire(1, settings=settings) == ""
    assert state.limit_reason(1, settings=settings) == "cooldown"
    clock.advance(301)
    assert state.limit_reason(1, settings=settings) == ""


def test_acquire_is_atomic_recheck_under_cooldown() -> None:
    clock = FakeClock()
    state = AutoChatState(clock=clock)
    settings = _settings()
    assert state.acquire(1, settings=settings) == ""
    assert state.acquire(1, settings=settings) == "cooldown"


def test_hourly_limit_trips_then_resets_next_hour() -> None:
    from datetime import UTC, datetime, timedelta

    clock = FakeClock()

    def bucket() -> datetime:
        hours = int((clock.now - 1000.0) // 3600)
        return datetime(2026, 9, 16, 10, 0, tzinfo=UTC) + timedelta(hours=hours)

    state = AutoChatState(clock=clock, bucket_clock=bucket)
    settings = _settings(auto_chat_cooldown_seconds=0.0)
    assert state.acquire(1, settings=settings) == ""
    clock.advance(1)
    assert state.acquire(1, settings=settings) == ""
    assert state.acquire(1, settings=settings) == "hourly_limit"
    clock.advance(3600)
    assert state.acquire(1, settings=settings) == ""


def test_daily_limit_trips() -> None:
    from datetime import UTC, datetime

    def fixed_bucket() -> datetime:
        return datetime(2026, 9, 16, tzinfo=UTC)

    state = AutoChatState(bucket_clock=fixed_bucket)
    settings = _settings(auto_chat_hourly_limit=10, auto_chat_cooldown_seconds=0.0)
    assert state.acquire(1, settings=settings) == ""
    assert state.acquire(1, settings=settings) == ""
    assert state.acquire(1, settings=settings) == ""
    assert state.acquire(1, settings=settings) == "daily_limit"


def test_backoff_window_and_expiry() -> None:
    clock = FakeClock()
    state = AutoChatState(clock=clock)
    assert state.in_backoff(1) is False
    state.set_backoff(1, 1800.0)
    assert state.in_backoff(1) is True
    clock.advance(1801)
    assert state.in_backoff(1) is False


def test_recently_spoke_within_window() -> None:
    clock = FakeClock()
    state = AutoChatState(clock=clock)
    assert state.recently_spoke(1, 300.0) is False
    state.acquire(1, settings=_settings())
    assert state.recently_spoke(1, 300.0) is True
    clock.advance(301)
    assert state.recently_spoke(1, 300.0) is False


def test_lock_is_per_group() -> None:
    state = AutoChatState()
    assert state.lock(1) is state.lock(1)
    assert state.lock(1) is not state.lock(2)
    asyncio.run(state.lock(1).acquire())
    assert state.lock(1).locked()
