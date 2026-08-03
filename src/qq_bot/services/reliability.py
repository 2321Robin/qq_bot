"""Retry and circuit-breaker primitives for external dependencies (S1-RET, S1-CB).

Classification rules:
- transient (retryable, counts against the breaker): connect/read/write/pool
  timeouts, temporary connection errors, HTTP 408/429/5xx, and explicit
  ``TransientDependencyError`` markers carrying an optional ``Retry-After``.
- permanent (never retried, never counted): deterministic HTTP 4xx (except
  408/429), invalid response shapes, config/input errors, business rejections.
- ambiguous (never retried, never counted): anything unrecognized.

The breaker keeps state in memory only (S1-CB-04) and uses an async lock so
that at most one half-open probe passes after the recovery window.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt

from qq_bot.observability.logging import get_logger, record_event

logger = get_logger("qq_bot.reliability")

RETRYABLE_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


class ErrorCategory(enum.Enum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class ErrorClassification:
    category: ErrorCategory
    retryable: bool
    counts_against_breaker: bool


TRANSIENT = ErrorClassification(ErrorCategory.TRANSIENT, True, True)
PERMANENT = ErrorClassification(ErrorCategory.PERMANENT, False, False)
AMBIGUOUS = ErrorClassification(ErrorCategory.AMBIGUOUS, False, False)


class TransientDependencyError(RuntimeError):
    """A dependency failed temporarily; the call may be retried safely."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PermanentDependencyError(RuntimeError):
    """A dependency rejected the request deterministically; never retry."""


class CircuitOpenError(RuntimeError):
    """The dependency circuit is open; fail fast without calling it."""


def classify_http_status(status_code: int) -> ErrorClassification:
    if status_code in RETRYABLE_HTTP_STATUSES:
        return TRANSIENT
    if 400 <= status_code < 500:
        return PERMANENT
    if status_code >= 500:
        return TRANSIENT
    return AMBIGUOUS


def _retry_after_from_headers(headers: Any) -> float | None:
    value = getattr(headers, "get", lambda _key: None)("retry-after")
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def classify_exception(exc: BaseException) -> ErrorClassification:
    """Classify an exception for retry and breaker accounting.

    Invalid-response parsing errors (``ValueError``/``TypeError``/``KeyError``/
    ``AttributeError``) are treated as permanent: they are deterministic local
    failures, never transient network conditions.
    """
    if isinstance(exc, TransientDependencyError):
        return TRANSIENT
    if isinstance(exc, PermanentDependencyError):
        return PERMANENT
    if isinstance(exc, httpx.HTTPStatusError):
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        if status_code is None:
            return AMBIGUOUS
        return classify_http_status(status_code)
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return TRANSIENT
    if isinstance(exc, (ValueError, TypeError, KeyError, AttributeError)):
        return PERMANENT
    return AMBIGUOUS


def is_retryable(exc: BaseException) -> bool:
    return classify_exception(exc).retryable


def wrap_http_error(exc: httpx.HTTPError) -> TransientDependencyError | PermanentDependencyError:
    """Convert an httpx error into a reliability error carrying its category.

    The returned error keeps the retry/breaker semantics of the original; the
    original exception text is only used for the message, never as a state key.
    """
    classification = classify_exception(exc)
    if classification.retryable:
        retry_after = None
        if isinstance(exc, httpx.HTTPStatusError):
            retry_after = _retry_after_from_headers(exc.response.headers)
        return TransientDependencyError(f"transient HTTP failure: {exc!r}", retry_after=retry_after)
    return PermanentDependencyError(f"permanent HTTP failure: {exc!r}")


def build_retry_policy(
    *,
    max_attempts: int,
    base_delay_seconds: float,
    max_delay_seconds: float,
    jitter_ratio: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    random_source: Callable[[], float] = random.random,
    retryable: Callable[[BaseException], bool] = is_retryable,
) -> AsyncRetrying:
    """Assemble a tenacity async retry policy (S1-RET-02/03).

    Waiting is capped exponential backoff with proportional jitter; a valid
    ``Retry-After`` on a ``TransientDependencyError`` is adopted but capped at
    ``max_delay_seconds``. Attempts include the first call.
    """

    def _wait(retry_state: Any) -> float:
        outcome = getattr(retry_state, "outcome", None)
        exception = outcome.exception() if outcome is not None else None
        if isinstance(exception, TransientDependencyError) and exception.retry_after is not None:
            base = min(exception.retry_after, max_delay_seconds)
        else:
            attempt = retry_state.attempt_number - 1
            base = min(base_delay_seconds * (2**attempt), max_delay_seconds)
        jitter = (random_source() * 2 - 1) * jitter_ratio * base
        return max(0.0, base + jitter)

    return AsyncRetrying(
        retry=retry_if_exception(retryable),
        wait=_wait,
        stop=stop_after_attempt(max_attempts),
        sleep=sleep,
        reraise=True,
    )


class CircuitState(enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """In-memory async circuit breaker (S1-CB-01..03)."""

    def __init__(
        self,
        *,
        name: str,
        failure_threshold: int,
        recovery_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        on_state_change: Callable[[CircuitState, CircuitState], None] | None = None,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be a positive integer")
        if recovery_seconds <= 0:
            raise ValueError("recovery_seconds must be greater than 0")
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self._clock = clock
        self.on_state_change = on_state_change
        self._lock = asyncio.Lock()
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False

    def _notify_state_change(self, old_state: CircuitState) -> None:
        """Fire the optional state-change callback (S4-METRIC-03).

        Callback failures never propagate into the breaker call chain.
        """
        if self.on_state_change is None:
            return
        try:
            self.on_state_change(old_state, self._state)
        except Exception:
            logger.exception("circuit state change callback failed")

    @property
    def state(self) -> CircuitState:
        return self._state

    async def check(self) -> None:
        """Admit or reject the next call; raises ``CircuitOpenError`` when the
        call must fail fast. At most one half-open probe passes at a time."""
        async with self._lock:
            if self._state is CircuitState.CLOSED:
                return
            if self._state is CircuitState.OPEN:
                if self._opened_at is not None and (
                    self._clock() - self._opened_at >= self.recovery_seconds
                ):
                    old_state = self._state
                    self._state = CircuitState.HALF_OPEN
                    self._probe_in_flight = True
                    self._notify_state_change(old_state)
                    return
                raise CircuitOpenError(f"circuit {self.name} is open")
            # HALF_OPEN: only the admitted probe may pass.
            if self._probe_in_flight:
                raise CircuitOpenError(f"circuit {self.name} is half-open")
            self._probe_in_flight = True

    async def on_success(self) -> None:
        async with self._lock:
            if self._state is CircuitState.HALF_OPEN:
                old_state = self._state
                self._state = CircuitState.CLOSED
                self._probe_in_flight = False
                self._notify_state_change(old_state)
            self._consecutive_failures = 0

    async def on_failure(self, classification: ErrorClassification) -> None:
        if not classification.counts_against_breaker:
            return
        async with self._lock:
            if self._state is CircuitState.HALF_OPEN:
                old_state = self._state
                self._state = CircuitState.OPEN
                self._opened_at = self._clock()
                self._probe_in_flight = False
                self._notify_state_change(old_state)
                return
            if self._state is CircuitState.OPEN:
                return
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.failure_threshold:
                old_state = self._state
                self._state = CircuitState.OPEN
                self._opened_at = self._clock()
                self._notify_state_change(old_state)


def log_reliability_event(
    *,
    operation: str,
    dependency: str,
    attempt: int,
    max_attempts: int,
    error_category: str,
    delay_seconds: float | None = None,
    circuit_state: str | None = None,
) -> None:
    """Log a sanitized reliability event via the observability facade
    (S1-SEND-05, S4-LOG-05).

    Accepts only sanitized scalar fields — never payloads, headers or message
    bodies. The message text keeps the historical flat format; the whitelisted
    structured fields carry the machine-readable parts.
    """
    parts = [
        f"operation={operation}",
        f"dependency={dependency}",
        f"attempt={attempt}",
        f"max_attempts={max_attempts}",
        f"error_category={error_category}",
    ]
    if delay_seconds is not None:
        parts.append(f"delay_seconds={delay_seconds:g}")
    if circuit_state is not None:
        parts.append(f"circuit_state={circuit_state}")
    fields: dict[str, object] = {
        "attempt": attempt,
        "max_attempts": max_attempts,
        "error_category": error_category,
    }
    if circuit_state is not None:
        fields["circuit_state"] = circuit_state
    record_event(
        logger,
        logging.WARNING,
        "reliability_event",
        message="reliability event: " + " ".join(parts),
        **fields,
    )
