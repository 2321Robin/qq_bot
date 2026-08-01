from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any, NoReturn, Protocol

from nonebot import logger
from nonebot.adapters.onebot.v11.exception import ActionFailed, NetworkError
from nonebot.exception import FinishedException

from qq_bot.config import get_settings
from qq_bot.runtime import BREAKER_ONEBOT, RuntimeStateError, get_runtime
from qq_bot.services.reliability import (
    CircuitBreaker,
    CircuitOpenError,
    ErrorCategory,
    ErrorClassification,
)


class SendErrorCategory(enum.Enum):
    AMBIGUOUS_TIMEOUT = "ambiguous_timeout"
    RETRYABLE = "retryable"
    PERMANENT = "permanent"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SendErrorClassification:
    category: SendErrorCategory
    retryable: bool
    counts_against_breaker: bool


class FinishableMatcher(Protocol):
    async def finish(self, message: object, **kwargs: Any) -> NoReturn:
        raise NotImplementedError


def is_send_timeout_error(error: Exception) -> bool:
    text = _error_text(error)
    if "timeout" not in text.lower():
        return False
    if isinstance(error, NetworkError):
        return "send_msg" in text or "send_group_msg" in text
    if isinstance(error, ActionFailed):
        return "sendmsg" in text.replace("_", "").lower()
    return False


def classify_send_error(error: Exception) -> SendErrorClassification:
    """Unified OneBot send error classification (S1-SEND-01..03).

    - ambiguous timeout: the message may already have been accepted — never
      retry (S1-SEND-02), never counted against the breaker;
    - retryable: a connection-level failure that proves the server did not
      accept the message;
    - permanent: an explicit ``ActionFailed`` business rejection;
    - unknown: anything else — no retry, not counted.
    """
    if is_send_timeout_error(error):
        return SendErrorClassification(
            SendErrorCategory.AMBIGUOUS_TIMEOUT, retryable=False, counts_against_breaker=False
        )
    if isinstance(error, NetworkError):
        return SendErrorClassification(
            SendErrorCategory.RETRYABLE, retryable=True, counts_against_breaker=True
        )
    if isinstance(error, ActionFailed):
        return SendErrorClassification(
            SendErrorCategory.PERMANENT, retryable=False, counts_against_breaker=False
        )
    return SendErrorClassification(
        SendErrorCategory.UNKNOWN, retryable=False, counts_against_breaker=False
    )


_default_breaker: CircuitBreaker | None = None


def _onebot_breaker() -> CircuitBreaker:
    """Resolve the shared OneBot breaker from the runtime; fall back to a
    process-local default when no runtime is installed (unit tests)."""
    global _default_breaker
    try:
        return get_runtime().get_breaker(BREAKER_ONEBOT)
    except RuntimeStateError:
        if _default_breaker is None:
            settings = get_settings()
            _default_breaker = CircuitBreaker(
                name=BREAKER_ONEBOT,
                failure_threshold=settings.breaker_failure_threshold,
                recovery_seconds=settings.breaker_recovery_seconds,
            )
        return _default_breaker


async def check_onebot_breaker() -> None:
    """Fail fast when the OneBot circuit is open (raises ``CircuitOpenError``)."""
    await _onebot_breaker().check()


async def record_send_success() -> None:
    await _onebot_breaker().on_success()


async def record_send_failure(classification: SendErrorClassification) -> None:
    await _onebot_breaker().on_failure(_to_error_classification(classification))


def _to_error_classification(classification: SendErrorClassification) -> ErrorClassification:
    if classification.category is SendErrorCategory.RETRYABLE:
        category = ErrorCategory.TRANSIENT
    elif classification.category is SendErrorCategory.PERMANENT:
        category = ErrorCategory.PERMANENT
    else:
        category = ErrorCategory.AMBIGUOUS
    return ErrorClassification(
        category=category,
        retryable=classification.retryable,
        counts_against_breaker=classification.counts_against_breaker,
    )


async def finish_with_send_errors_logged(
    matcher: FinishableMatcher,
    message: object,
    **kwargs: Any,
) -> NoReturn:
    try:
        await check_onebot_breaker()
    except CircuitOpenError as exc:
        logger.warning("OneBot circuit is open; skipping interactive send")
        raise RuntimeError("QQ send skipped: OneBot circuit is open") from exc

    try:
        await matcher.finish(message, **kwargs)
    except FinishedException:
        # Normal matcher completion (NoneBot raises FinishedException); a
        # successful finish is not a retryable failure.
        await record_send_success()
        raise
    except Exception as exc:
        classification = classify_send_error(exc)
        await record_send_failure(classification)
        if classification.category is SendErrorCategory.AMBIGUOUS_TIMEOUT:
            logger.warning(f"Message send timed out and may not be visible in QQ: {exc!r}")
        raise

    # The matcher protocol says finish never returns; reaching this line is a
    # contract violation of the fake/matcher, not a send failure.
    await record_send_success()
    raise RuntimeError("matcher.finish returned unexpectedly")


def _error_text(error: Exception) -> str:
    parts = [str(error), repr(error)]
    info = getattr(error, "info", None)
    if isinstance(info, dict):
        parts.extend(str(value) for value in info.values())
    return " ".join(parts)
