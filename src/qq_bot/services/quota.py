"""Quota & budget: sliding-window rate limit and daily cost caps (S4-QUOTA).

The service reuses the runtime's repository connection (S4-QUOTA-04): daily
counters live in ``quota_usage`` and every denial/estimate note in
``quota_events`` (migration 3). The rate window itself is an in-memory
sliding window whose transient state may reset on restart — the SQLite rows
are the durable source of truth for the day, matching the stage-1 in-memory
boundary convention (S1-CB-04).
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from collections import deque
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from qq_bot.config import BotSettings
from qq_bot.observability.cost import CostEstimate
from qq_bot.observability.logging import get_logger, hash_id, record_event

logger = get_logger("qq_bot.quota")

_WINDOW_SECONDS = 60.0


class QuotaRepository(Protocol):
    """The minimal repository surface the service needs (the runtime's
    ChatMemoryRepository satisfies it via ``execute``)."""

    async def execute(self, sql: str, parameters: Sequence[Any] = ()) -> Any: ...


@dataclass(frozen=True)
class QuotaDecision:
    allowed: bool
    reason: str = ""  # "" | "rate" | "cost"


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class QuotaService:
    def __init__(
        self,
        settings: BotSettings,
        repository: QuotaRepository,
        clock=time.monotonic,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._clock = clock
        self._window: deque[tuple[float, str, int]] = deque()
        self._window_lock = asyncio.Lock()

    async def check_admission(self, *, scope_type: str, scope_id: int) -> QuotaDecision:
        """Sliding-window rate limit + today's actual-cost caps (S4-QUOTA-02/03).

        Denials are persisted to ``quota_events`` and logged with hashed
        scope ids; only the caller increments the metric and answers the user.
        Explicit commands never pass through here (they do not route through
        ai_chat), so they are never blocked by the rate limit.
        """
        if not self._settings.quota_enabled:
            return QuotaDecision(allowed=True)

        limit = self._settings.quota_rate_limit_per_minute
        if limit > 0:
            now = self._clock()
            async with self._window_lock:
                cutoff = now - _WINDOW_SECONDS
                while self._window and self._window[0][0] < cutoff:
                    self._window.popleft()
                recent = sum(
                    1
                    for _, scope, scope_id_ in self._window
                    if scope == scope_type and scope_id_ == scope_id
                )
                if recent >= limit:
                    await self._deny(
                        kind="rate_denied",
                        scope_type=scope_type,
                        scope_id=scope_id,
                        reason="rate",
                        detail=f"rate limit {limit}/minute reached",
                    )
                    return QuotaDecision(allowed=False, reason="rate")
                self._window.append((now, scope_type, scope_id))

        day = _today()
        if self._settings.quota_daily_cost_limit_usd > 0:
            spent = await self._spent_cost(day)
            if spent >= self._settings.quota_daily_cost_limit_usd:
                await self._deny(
                    kind="cost_denied",
                    scope_type=scope_type,
                    scope_id=scope_id,
                    reason="cost",
                    detail=(
                        f"daily global cost limit "
                        f"{self._settings.quota_daily_cost_limit_usd} USD reached"
                    ),
                )
                return QuotaDecision(allowed=False, reason="cost")
        if self._settings.quota_group_daily_cost_limit_usd > 0 and scope_type == "group":
            spent = await self._spent_cost(day, group_id=scope_id)
            if spent >= self._settings.quota_group_daily_cost_limit_usd:
                await self._deny(
                    kind="cost_denied",
                    scope_type=scope_type,
                    scope_id=scope_id,
                    reason="cost",
                    detail=(
                        f"daily per-group cost limit "
                        f"{self._settings.quota_group_daily_cost_limit_usd} USD reached"
                    ),
                )
                return QuotaDecision(allowed=False, reason="cost")
        return QuotaDecision(allowed=True)

    async def record_usage(
        self,
        *,
        scope_type: str,
        scope_id: int,
        tokens: int,
        cost: CostEstimate | None,
    ) -> None:
        """Count one admitted request (S4-QUOTA-03/04): tokens always, actual
        cost into the daily budget row. Estimated/unknown costs never enforce
        — they are recorded as events for the admin view only."""
        if not self._settings.quota_enabled:
            return
        day = _today()
        actual_cost = cost.cost if cost is not None and cost.status == "actual" else 0.0
        await self._repository.execute(
            """
            INSERT INTO quota_usage (scope_type, scope_id, day, requests, tokens, cost_usd, updated_at)
            VALUES (?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT (scope_type, scope_id, day)
            DO UPDATE SET
                requests = requests + 1,
                tokens = tokens + excluded.tokens,
                cost_usd = cost_usd + excluded.cost_usd,
                updated_at = excluded.updated_at
            """,
            (scope_type, scope_id, day, int(tokens), float(actual_cost), _now_iso()),
        )
        if cost is not None and cost.status != "actual":
            await self.record_event(
                "cost_estimated",
                scope_type=scope_type,
                scope_id=scope_id,
                reason=cost.status,
                detail="cost recorded without enforcement (estimated/unknown)",
            )

    async def record_event(
        self,
        kind: str,
        *,
        scope_type: str,
        scope_id: int,
        reason: str,
        detail: str = "",
    ) -> None:
        await self._repository.execute(
            "INSERT INTO quota_events (at, scope_type, scope_id, kind, reason, detail) VALUES (?, ?, ?, ?, ?, ?)",
            (_now_iso(), scope_type, scope_id, kind, reason, detail),
        )

    async def summary(self, *, scope_type: str, scope_id: int) -> dict[str, Any]:
        """Today's usage plus the configured caps, for the admin view."""
        day = _today()
        cursor = await self._repository.execute(
            "SELECT requests, tokens, cost_usd FROM quota_usage "
            "WHERE scope_type = ? AND scope_id = ? AND day = ?",
            (scope_type, scope_id, day),
        )
        row = await cursor.fetchone()
        return {
            "scope_type": scope_type,
            "scope_id": scope_id,
            "day": day,
            "requests": int(row[0]) if row is not None else 0,
            "tokens": int(row[1]) if row is not None else 0,
            "cost_usd": float(row[2]) if row is not None else 0.0,
            "rate_limit_per_minute": self._settings.quota_rate_limit_per_minute,
            "daily_cost_limit_usd": self._settings.quota_daily_cost_limit_usd,
            "group_daily_cost_limit_usd": self._settings.quota_group_daily_cost_limit_usd,
        }

    async def recent_failures(self, limit: int = 10) -> list[dict[str, Any]]:
        """Most recent quota events (denials, estimates, failures)."""
        cursor = await self._repository.execute(
            "SELECT at, scope_type, scope_id, kind, reason, detail "
            "FROM quota_events ORDER BY id DESC LIMIT ?",
            (int(limit),),
        )
        rows = await cursor.fetchall()
        return [
            {
                "at": row[0],
                "scope_type": row[1],
                "scope_id": row[2],
                "kind": row[3],
                "reason": row[4],
                "detail": row[5],
            }
            for row in rows
        ]

    async def _spent_cost(self, day: str, *, group_id: int | None = None) -> float:
        if group_id is None:
            sql = "SELECT COALESCE(SUM(cost_usd), 0.0) FROM quota_usage WHERE day = ?"
            parameters: tuple[Any, ...] = (day,)
        else:
            sql = (
                "SELECT COALESCE(SUM(cost_usd), 0.0) FROM quota_usage "
                "WHERE day = ? AND scope_type = 'group' AND scope_id = ?"
            )
            parameters = (day, group_id)
        cursor = await self._repository.execute(sql, parameters)
        row = await cursor.fetchone()
        return float(row[0]) if row is not None else 0.0

    async def _deny(
        self,
        *,
        kind: str,
        scope_type: str,
        scope_id: int,
        reason: str,
        detail: str,
    ) -> None:
        await self.record_event(
            kind, scope_type=scope_type, scope_id=scope_id, reason=reason, detail=detail
        )
        record_event(
            logger,
            logging.INFO,
            "quota_denied",
            message=f"quota denied: scope={scope_type}, reason={reason}",
            category=reason,
            scope_hash=hash_id(scope_id, kind=scope_type),
        )


_quota_scope: contextvars.ContextVar[tuple[str, int] | None] = contextvars.ContextVar(
    "qq_bot_quota_scope", default=None
)


@contextmanager
def quota_scope(scope_type: str, scope_id: int) -> Iterator[None]:
    """Bind the request scope (group/user) for the duration of one AI reply
    so the accounting point inside ai_client records into the right row."""
    token = _quota_scope.set((scope_type, scope_id))
    try:
        yield
    finally:
        _quota_scope.reset(token)


def active_quota_scope() -> tuple[str, int] | None:
    return _quota_scope.get()
