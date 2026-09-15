"""Autonomous group chat (S7-AUTO): prefilter, sampling, LLM gate decision,
casual persona generation and in-memory throttling.

Design contract (spec 2026-09-16-auto-chat-design.md): decisions fail
closed; guardrails (cooldown/limits/backoff) apply to the nicknamed
fast-path too; state is process-local and lost on restart by design."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Callable

from qq_bot.config import BotSettings


def _default_bucket_clock() -> datetime:
    return datetime.now(UTC)


class AutoChatState:
    """Per-group in-memory throttle state: cooldown timestamp, hourly/daily
    reply counters and negative-feedback backoff deadline."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        bucket_clock: Callable[[], datetime] = _default_bucket_clock,
    ) -> None:
        self._clock = clock
        self._bucket_clock = bucket_clock
        self._last_sent: dict[int, float] = {}
        self._hourly: dict[tuple[int, str], int] = {}
        self._daily: dict[tuple[int, str], int] = {}
        self._backoff_until: dict[int, float] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def lock(self, group_id: int) -> asyncio.Lock:
        return self._locks.setdefault(group_id, asyncio.Lock())

    def in_backoff(self, group_id: int) -> bool:
        deadline = self._backoff_until.get(group_id)
        return deadline is not None and self._clock() < deadline

    def set_backoff(self, group_id: int, seconds: float) -> None:
        self._backoff_until[group_id] = self._clock() + seconds

    def recently_spoke(self, group_id: int, window_seconds: float) -> bool:
        last = self._last_sent.get(group_id)
        return last is not None and self._clock() - last < window_seconds

    def limit_reason(self, group_id: int, *, settings: BotSettings) -> str:
        """`""` when within limits, else `cooldown`|`hourly_limit`|`daily_limit`."""
        now = self._clock()
        last = self._last_sent.get(group_id)
        if last is not None and now - last < settings.auto_chat_cooldown_seconds:
            return "cooldown"
        bucket = self._bucket_clock()
        hour_key = bucket.strftime("%Y-%m-%dT%H")
        day_key = bucket.strftime("%Y-%m-%d")
        if self._hourly.get((group_id, hour_key), 0) >= settings.auto_chat_hourly_limit:
            return "hourly_limit"
        if self._daily.get((group_id, day_key), 0) >= settings.auto_chat_daily_limit:
            return "daily_limit"
        return ""

    def acquire(self, group_id: int, *, settings: BotSettings) -> str:
        """Re-check limits and occupy the slot atomically (call under the
        group lock in the orchestration path). Returns `""` on success."""
        reason = self.limit_reason(group_id, settings=settings)
        if reason:
            return reason
        bucket = self._bucket_clock()
        hour_key = bucket.strftime("%Y-%m-%dT%H")
        day_key = bucket.strftime("%Y-%m-%d")
        self._prune(hour_key, day_key)
        self._last_sent[group_id] = self._clock()
        self._hourly[(group_id, hour_key)] = self._hourly.get((group_id, hour_key), 0) + 1
        self._daily[(group_id, day_key)] = self._daily.get((group_id, day_key), 0) + 1
        return ""

    def _prune(self, current_hour: str, current_day: str) -> None:
        """Drop stale bucket keys so the counters cannot grow unbounded."""
        for key in [k for k in self._hourly if k[1] != current_hour]:
            del self._hourly[key]
        for key in [k for k in self._daily if k[1] != current_day]:
            del self._daily[key]
