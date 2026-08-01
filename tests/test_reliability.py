"""Retry policy, error classification and circuit breaker tests (S1-RET, S1-CB)."""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from qq_bot.services.reliability import (
    AMBIGUOUS,
    PERMANENT,
    TRANSIENT,
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    PermanentDependencyError,
    TransientDependencyError,
    build_retry_policy,
    classify_exception,
    classify_http_status,
    is_retryable,
    log_reliability_event,
    wrap_http_error,
)


def _http_status_error(status: int, headers: dict[str, str] | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.com/api")
    response = httpx.Response(status, request=request, headers=headers or {})
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


def _noop_sleep(_seconds: float) -> asyncio.Future[None]:
    future: asyncio.Future[None] = asyncio.Future()
    future.set_result(None)
    return future


def _recorded_sleeps(sleeps: list[float]) -> object:
    async def _sleep(seconds: float) -> None:
        sleeps.append(seconds)

    return _sleep


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


def test_classify_http_status_matrix() -> None:
    for status in (408, 429, 500, 502, 503, 504):
        classification = classify_http_status(status)
        assert classification.retryable is True
        assert classification.counts_against_breaker is True
    for status in (400, 401, 403, 404, 422):
        classification = classify_http_status(status)
        assert classification.retryable is False
        assert classification.counts_against_breaker is False


def test_classify_exception_matrix() -> None:
    assert classify_exception(httpx.ConnectTimeout("connect timeout")) is TRANSIENT
    assert classify_exception(httpx.ReadTimeout("read timeout")) is TRANSIENT
    assert classify_exception(httpx.ConnectError("connection refused")) is TRANSIENT
    assert classify_exception(_http_status_error(503)) is TRANSIENT
    assert classify_exception(_http_status_error(429)) is TRANSIENT
    assert classify_exception(_http_status_error(401)) is PERMANENT
    assert classify_exception(TransientDependencyError("upstream hiccup")) is TRANSIENT
    assert classify_exception(PermanentDependencyError("bad request")) is PERMANENT
    assert classify_exception(ValueError("invalid json")) is PERMANENT
    assert classify_exception(TypeError("bad shape")) is PERMANENT
    assert classify_exception(KeyError("missing field")) is PERMANENT
    assert classify_exception(AttributeError("no attr")) is PERMANENT
    assert classify_exception(RuntimeError("unrecognized")) is AMBIGUOUS
    # an HTTPStatusError without a response is ambiguous, never retried
    orphan = httpx.HTTPStatusError(
        "no response", request=httpx.Request("GET", "https://x"), response=None
    )  # type: ignore[arg-type]
    assert classify_exception(orphan) is AMBIGUOUS


def test_is_retryable_helpers() -> None:
    assert is_retryable(httpx.ReadTimeout("t"))
    assert not is_retryable(ValueError("bad"))


def test_wrap_http_error_carries_retry_after() -> None:
    wrapped = wrap_http_error(_http_status_error(429, {"retry-after": "2"}))
    assert isinstance(wrapped, TransientDependencyError)
    assert wrapped.retry_after == 2.0


def test_wrap_http_error_ignores_invalid_retry_after() -> None:
    wrapped = wrap_http_error(_http_status_error(503, {"retry-after": "later"}))
    assert isinstance(wrapped, TransientDependencyError)
    assert wrapped.retry_after is None


def test_wrap_http_error_permanent() -> None:
    wrapped = wrap_http_error(_http_status_error(401))
    assert isinstance(wrapped, PermanentDependencyError)


# --------------------------------------------------------------------------
# Retry policy (deterministic sleep/random injection)
# --------------------------------------------------------------------------


async def _run_policy(policy: object, fn: object) -> object:
    async for attempt in policy:  # type: ignore[union-attr]
        with attempt:
            return await fn()  # type: ignore[misc]
    raise AssertionError("policy exhausted without result")


def _policy(
    max_attempts: int,
    sleeps: list[float],
    *,
    base: float = 0.5,
    max_delay: float = 4.0,
    jitter: float = 0.1,
) -> object:
    return build_retry_policy(
        max_attempts=max_attempts,
        base_delay_seconds=base,
        max_delay_seconds=max_delay,
        jitter_ratio=jitter,
        sleep=_recorded_sleeps(sleeps),  # type: ignore[arg-type]
        random_source=lambda: 0.5,  # midpoint -> zero jitter
    )


@pytest.mark.asyncio
async def test_retry_policy_retries_transient_then_succeeds() -> None:
    sleeps: list[float] = []
    calls = {"count": 0}

    async def attempt() -> str:
        calls["count"] += 1
        if calls["count"] < 3:
            raise TransientDependencyError("temporary failure")
        return "ok"

    result = await _run_policy(_policy(3, sleeps), attempt)
    assert result == "ok"
    assert calls["count"] == 3
    # capped exponential backoff: 0.5 then 1.0, zero jitter at random=0.5
    assert sleeps == [0.5, 1.0]


@pytest.mark.asyncio
async def test_retry_policy_exhausts_attempts_and_reraises() -> None:
    sleeps: list[float] = []
    calls = {"count": 0}

    async def attempt() -> None:
        calls["count"] += 1
        raise TransientDependencyError("always failing")

    with pytest.raises(TransientDependencyError):
        await _run_policy(_policy(2, sleeps), attempt)
    assert calls["count"] == 2
    assert sleeps == [0.5]


@pytest.mark.asyncio
async def test_retry_policy_never_retries_permanent() -> None:
    sleeps: list[float] = []
    calls = {"count": 0}

    async def attempt() -> None:
        calls["count"] += 1
        raise PermanentDependencyError("deterministic")

    with pytest.raises(PermanentDependencyError):
        await _run_policy(_policy(3, sleeps), attempt)
    assert calls["count"] == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_retry_policy_caps_exponential_backoff() -> None:
    sleeps: list[float] = []
    calls = {"count": 0}

    async def attempt() -> None:
        calls["count"] += 1
        raise TransientDependencyError("temporary")

    with pytest.raises(TransientDependencyError):
        await _run_policy(_policy(4, sleeps, base=0.5, max_delay=1.0), attempt)
    # waits: min(0.5*2^0,1)=0.5, min(0.5*2^1,1)=1.0, min(0.5*2^2,1)=1.0
    assert sleeps == [0.5, 1.0, 1.0]


@pytest.mark.asyncio
async def test_retry_policy_adopts_retry_after_capped() -> None:
    sleeps: list[float] = []
    calls = {"count": 0}

    async def attempt() -> None:
        calls["count"] += 1
        raise TransientDependencyError("rate limited", retry_after=30.0)

    with pytest.raises(TransientDependencyError):
        await _run_policy(_policy(2, sleeps, max_delay=4.0), attempt)
    assert sleeps == [4.0]  # Retry-After adopted but capped at max_delay


@pytest.mark.asyncio
async def test_retry_policy_jitter_bounded_by_ratio() -> None:
    sleeps: list[float] = []
    calls = {"count": 0}

    async def attempt() -> None:
        calls["count"] += 1
        raise TransientDependencyError("temporary")

    policy = build_retry_policy(
        max_attempts=2,
        base_delay_seconds=1.0,
        max_delay_seconds=4.0,
        jitter_ratio=0.1,
        sleep=_recorded_sleeps(sleeps),  # type: ignore[arg-type]
        random_source=lambda: 1.0,  # maximum positive jitter
    )
    with pytest.raises(TransientDependencyError):
        await _run_policy(policy, attempt)
    assert sleeps == [1.1]

    sleeps.clear()
    policy = build_retry_policy(
        max_attempts=2,
        base_delay_seconds=1.0,
        max_delay_seconds=4.0,
        jitter_ratio=0.1,
        sleep=_recorded_sleeps(sleeps),  # type: ignore[arg-type]
        random_source=lambda: 0.0,  # maximum negative jitter
    )
    with pytest.raises(TransientDependencyError):
        await _run_policy(policy, attempt)
    assert sleeps == [0.9]


@pytest.mark.asyncio
async def test_retry_policy_does_not_retry_ambiguous() -> None:
    sleeps: list[float] = []
    calls = {"count": 0}

    async def attempt() -> None:
        calls["count"] += 1
        raise RuntimeError("unrecognized failure")

    with pytest.raises(RuntimeError):
        await _run_policy(_policy(3, sleeps), attempt)
    assert calls["count"] == 1
    assert sleeps == []


# --------------------------------------------------------------------------
# Circuit breaker
# --------------------------------------------------------------------------


def _clock_source(values: list[float]) -> object:
    return lambda: values[0]


@pytest.mark.asyncio
async def test_breaker_opens_after_threshold_of_transient_failures() -> None:
    breaker = CircuitBreaker(name="dep", failure_threshold=2, recovery_seconds=30)
    assert breaker.state is CircuitState.CLOSED
    await breaker.check()  # closed admits
    await breaker.on_failure(TRANSIENT)
    assert breaker.state is CircuitState.CLOSED
    await breaker.on_failure(TRANSIENT)
    assert breaker.state is CircuitState.OPEN
    with pytest.raises(CircuitOpenError):
        await breaker.check()


@pytest.mark.asyncio
async def test_breaker_success_resets_consecutive_failures() -> None:
    breaker = CircuitBreaker(name="dep", failure_threshold=3, recovery_seconds=30)
    await breaker.on_failure(TRANSIENT)
    await breaker.on_failure(TRANSIENT)
    await breaker.on_success()
    await breaker.on_failure(TRANSIENT)
    await breaker.on_failure(TRANSIENT)
    assert breaker.state is CircuitState.CLOSED  # threshold not reached in a row


@pytest.mark.asyncio
async def test_breaker_ignores_permanent_and_ambiguous_failures() -> None:
    breaker = CircuitBreaker(name="dep", failure_threshold=2, recovery_seconds=30)
    await breaker.on_failure(PERMANENT)
    await breaker.on_failure(AMBIGUOUS)
    assert breaker.state is CircuitState.CLOSED


@pytest.mark.asyncio
async def test_breaker_opens_only_after_recovery_window_with_single_probe() -> None:
    clock = [0.0]
    breaker = CircuitBreaker(
        name="dep",
        failure_threshold=1,
        recovery_seconds=10,
        clock=_clock_source(clock),  # type: ignore[arg-type]
    )
    await breaker.on_failure(TRANSIENT)
    assert breaker.state is CircuitState.OPEN
    with pytest.raises(CircuitOpenError):
        await breaker.check()  # still inside the recovery window

    clock[0] = 10.0
    await breaker.check()  # recovery window elapsed: one probe admitted
    assert breaker.state is CircuitState.HALF_OPEN
    with pytest.raises(CircuitOpenError):
        await breaker.check()  # second concurrent caller must be rejected

    await breaker.on_success()
    assert breaker.state is CircuitState.CLOSED
    await breaker.check()  # closed again


@pytest.mark.asyncio
async def test_breaker_probe_failure_reopens_immediately() -> None:
    clock = [0.0]
    breaker = CircuitBreaker(
        name="dep",
        failure_threshold=1,
        recovery_seconds=10,
        clock=_clock_source(clock),  # type: ignore[arg-type]
    )
    await breaker.on_failure(TRANSIENT)
    clock[0] = 100.0
    await breaker.check()
    assert breaker.state is CircuitState.HALF_OPEN
    await breaker.on_failure(TRANSIENT)
    assert breaker.state is CircuitState.OPEN
    with pytest.raises(CircuitOpenError):
        await breaker.check()  # new recovery window starts


@pytest.mark.asyncio
async def test_breaker_concurrent_probes_admit_exactly_one() -> None:
    clock = [0.0]
    breaker = CircuitBreaker(
        name="dep",
        failure_threshold=1,
        recovery_seconds=10,
        clock=_clock_source(clock),  # type: ignore[arg-type]
    )
    await breaker.on_failure(TRANSIENT)
    clock[0] = 100.0

    results = await asyncio.gather(*(breaker.check() for _ in range(10)), return_exceptions=True)
    admitted = [result for result in results if result is None]
    rejected = [result for result in results if isinstance(result, CircuitOpenError)]
    assert len(admitted) == 1
    assert len(rejected) == 9


@pytest.mark.asyncio
async def test_breaker_rejects_invalid_parameters() -> None:
    with pytest.raises(ValueError):
        CircuitBreaker(name="x", failure_threshold=0, recovery_seconds=10)
    with pytest.raises(ValueError):
        CircuitBreaker(name="x", failure_threshold=1, recovery_seconds=0)


def test_log_reliability_event_contains_only_sanitized_fields(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="qq_bot.reliability"):
        log_reliability_event(
            operation="send",
            dependency="onebot",
            attempt=2,
            max_attempts=2,
            error_category="ambiguous_timeout",
            delay_seconds=0.5,
            circuit_state="closed",
        )
    message = caplog.text
    assert "operation=send" in message
    assert "dependency=onebot" in message
    assert "attempt=2" in message
    assert "max_attempts=2" in message
    assert "error_category=ambiguous_timeout" in message
    assert "delay_seconds=0.5" in message
    assert "circuit_state=closed" in message
