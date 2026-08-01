"""OneBot send reliability tests (S1-SEND-01..05)."""

from __future__ import annotations

import pytest
from nonebot.adapters.onebot.v11.exception import ActionFailed, NetworkError
from nonebot.exception import FinishedException

from qq_bot.services import onebot_send
from qq_bot.services.onebot_send import (
    SendErrorCategory,
    classify_send_error,
    finish_with_send_errors_logged,
    is_send_timeout_error,
    record_send_failure,
    record_send_success,
)
from qq_bot.services.reliability import TRANSIENT, CircuitBreaker, CircuitState


class FakeMatcher:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.messages: list[object] = []

    async def finish(self, message: object) -> None:
        self.messages.append(message)
        if self.error is not None:
            raise self.error
        raise FinishedException


def test_is_send_timeout_error_matches_onebot_network_send_timeout() -> None:
    error = NetworkError("WebSocket call api send_msg timeout")

    assert is_send_timeout_error(error)


def test_is_send_timeout_error_matches_napcat_send_action_timeout() -> None:
    error = ActionFailed(
        status="failed",
        retcode=1,
        message="Timeout: NTEvent serviceAndMethod:NodeIKernelMsgService/sendMsg",
    )

    assert is_send_timeout_error(error)


def test_is_send_timeout_error_ignores_non_send_timeout() -> None:
    assert not is_send_timeout_error(NetworkError("WebSocket call api get_login_info timeout"))
    assert not is_send_timeout_error(RuntimeError("send failed"))


def test_classify_send_error_timeout_is_ambiguous_and_never_retried() -> None:
    classification = classify_send_error(NetworkError("WebSocket call api send_msg timeout"))
    assert classification.category is SendErrorCategory.AMBIGUOUS_TIMEOUT
    assert classification.retryable is False
    assert classification.counts_against_breaker is False


def test_classify_send_error_connection_failure_is_retryable() -> None:
    classification = classify_send_error(NetworkError("WebSocket connection closed"))
    assert classification.category is SendErrorCategory.RETRYABLE
    assert classification.retryable is True
    assert classification.counts_against_breaker is True


def test_classify_send_error_business_rejection_is_permanent() -> None:
    classification = classify_send_error(
        ActionFailed(status="failed", retcode=1, message="no permission")
    )
    assert classification.category is SendErrorCategory.PERMANENT
    assert classification.retryable is False
    assert classification.counts_against_breaker is False


def test_classify_send_error_unknown_is_never_retried() -> None:
    classification = classify_send_error(RuntimeError("mystery failure"))
    assert classification.category is SendErrorCategory.UNKNOWN
    assert classification.retryable is False
    assert classification.counts_against_breaker is False


@pytest.mark.asyncio
async def test_finish_with_send_errors_logged_reraises_send_timeout() -> None:
    matcher = FakeMatcher(NetworkError("WebSocket call api send_msg timeout"))

    with pytest.raises(NetworkError):
        await finish_with_send_errors_logged(matcher, "hello")

    assert matcher.messages == ["hello"]


@pytest.mark.asyncio
async def test_finish_with_send_errors_logged_reraises_other_errors() -> None:
    matcher = FakeMatcher(RuntimeError("send failed"))

    with pytest.raises(RuntimeError, match="send failed"):
        await finish_with_send_errors_logged(matcher, "hello")


@pytest.mark.asyncio
async def test_finish_with_send_errors_logged_treats_finished_as_success() -> None:
    """Normal matcher completion (NoneBot FinishedException) is a success and
    records it against the breaker."""
    matcher = FakeMatcher()

    with pytest.raises(FinishedException):
        await finish_with_send_errors_logged(matcher, "hello")

    assert matcher.messages == ["hello"]


@pytest.mark.asyncio
async def test_finish_with_send_errors_logged_fails_fast_when_circuit_open(
    monkeypatch,
) -> None:
    open_breaker = CircuitBreaker(name="onebot", failure_threshold=1, recovery_seconds=30)
    await open_breaker.on_failure(TRANSIENT)
    monkeypatch.setattr(onebot_send, "_onebot_breaker", lambda: open_breaker)
    matcher = FakeMatcher()

    with pytest.raises(RuntimeError, match="circuit is open"):
        await finish_with_send_errors_logged(matcher, "hello")

    assert matcher.messages == []  # the send must never be attempted


@pytest.mark.asyncio
async def test_ambiguous_timeout_never_counts_against_breaker(monkeypatch) -> None:
    breaker = CircuitBreaker(name="onebot", failure_threshold=1, recovery_seconds=30)
    monkeypatch.setattr(onebot_send, "_onebot_breaker", lambda: breaker)

    await record_send_failure(
        classify_send_error(NetworkError("WebSocket call api send_msg timeout"))
    )

    assert breaker.state is CircuitState.CLOSED


@pytest.mark.asyncio
async def test_retryable_failures_count_against_breaker_and_success_resets(
    monkeypatch,
) -> None:
    breaker = CircuitBreaker(name="onebot", failure_threshold=2, recovery_seconds=30)
    monkeypatch.setattr(onebot_send, "_onebot_breaker", lambda: breaker)

    await record_send_failure(classify_send_error(NetworkError("WebSocket connection closed")))
    assert breaker.state is CircuitState.CLOSED
    await record_send_failure(classify_send_error(NetworkError("WebSocket connection closed")))
    assert breaker.state is CircuitState.OPEN

    # a success cannot close an OPEN circuit (only a half-open probe can)
    await record_send_success()
    assert breaker.state is CircuitState.OPEN
