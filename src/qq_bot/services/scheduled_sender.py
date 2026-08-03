from __future__ import annotations

import asyncio
import logging
import random

from collections.abc import Awaitable, Callable
from typing import Protocol

from qq_bot.config import BotSettings
from qq_bot.observability import metrics
from qq_bot.observability.logging import get_logger, record_event
from qq_bot.services.message_formatting import replace_named_mentions
from qq_bot.services.onebot_send import (
    SendErrorCategory,
    _onebot_breaker,
    _to_error_classification,
    classify_send_error,
)
from qq_bot.services.reliability import (
    CircuitBreaker,
    CircuitOpenError,
    build_retry_policy,
)


class GroupMessageBot(Protocol):
    async def send_group_msg(self, *, group_id: int, message: str) -> object:
        raise NotImplementedError


def build_scheduler_job_kwargs(settings: BotSettings) -> dict[str, object]:
    return {
        "trigger": "cron",
        "hour": settings.scheduled_cron_hour,
        "minute": settings.scheduled_cron_minute,
        "id": "daily_group_message",
        "replace_existing": True,
    }


def build_scheduler_jobs_kwargs(settings: BotSettings) -> list[dict[str, object]]:
    return [
        {
            "trigger": "cron",
            "hour": hour,
            "minute": minute,
            "id": f"daily_group_message_{hour:02d}{minute:02d}",
            "replace_existing": True,
        }
        for hour, minute in settings.scheduled_cron_time_list
    ]


def describe_scheduler_job(job_kwargs: dict[str, object]) -> str:
    return f"{job_kwargs['id']} at {job_kwargs['hour']:02d}:{job_kwargs['minute']:02d}"


def filter_allowed_group_ids(group_ids: list[int], settings: BotSettings) -> list[int]:
    return [group_id for group_id in group_ids if settings.group_allowed(group_id)]


async def send_group_messages(
    bot: GroupMessageBot,
    group_ids: list[int],
    message: str,
    *,
    max_attempts: int = 2,
    base_delay_seconds: float = 0.5,
    max_delay_seconds: float = 3.0,
    jitter_ratio: float = 0.1,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    random_source: Callable[[], float] = random.random,
    breaker: CircuitBreaker | None = None,
    named_mention_replacements: dict[str, str] | None = None,
) -> list[int]:
    """Send one scheduled message per group with capped exponential backoff,
    jitter and the shared OneBot breaker (S1-SEND-01..03).

    Ambiguous send timeouts are never retried (the message may already have
    been accepted); only connection-level failures proven before acceptance
    are retried. Named mentions resolve through the configured replacement
    map, never through hardcoded accounts. Logs contain no message bodies and
    no raw group ids (S1-SEND-05).
    """
    failed_group_ids: list[int] = []
    formatted_message = replace_named_mentions(message, named_mention_replacements)
    active_breaker = breaker if breaker is not None else _onebot_breaker()
    send_logger = get_logger("qq_bot.scheduled_sender")

    for group_id in group_ids:
        try:
            await active_breaker.check()
        except CircuitOpenError:
            record_event(
                send_logger,
                logging.WARNING,
                "scheduled_send_skipped_circuit_open",
                message="Scheduled message send skipped: OneBot circuit is open",
                circuit_state="open",
            )
            metrics.SCHEDULER_SENDS.labels("circuit_open").inc()
            failed_group_ids.append(group_id)
            continue

        policy = build_retry_policy(
            max_attempts=max_attempts,
            base_delay_seconds=base_delay_seconds,
            max_delay_seconds=max_delay_seconds,
            jitter_ratio=jitter_ratio,
            sleep=sleep,
            random_source=random_source,
            retryable=_send_retryable,
        )
        result = "error"
        try:
            async for attempt in policy:
                with attempt:
                    if attempt.retry_state.attempt_number >= 2:
                        metrics.RETRIES.labels("send").inc()
                    try:
                        await bot.send_group_msg(group_id=group_id, message=formatted_message)
                    except Exception as exc:
                        classification = classify_send_error(exc)
                        await active_breaker.on_failure(_to_error_classification(classification))
                        if classification.category is SendErrorCategory.AMBIGUOUS_TIMEOUT:
                            result = "ambiguous_timeout"
                            record_event(
                                send_logger,
                                logging.WARNING,
                                "scheduled_send_ambiguous_timeout",
                                message=(
                                    "Scheduled message send timed out and may not be "
                                    f"visible in QQ (attempt {attempt.retry_state.attempt_number}/"
                                    f"{max_attempts})"
                                ),
                                attempt=attempt.retry_state.attempt_number,
                                max_attempts=max_attempts,
                                category="ambiguous_timeout",
                            )
                        elif classification.category is SendErrorCategory.RETRYABLE:
                            result = "retryable"
                            record_event(
                                send_logger,
                                logging.WARNING,
                                "scheduled_send_retrying",
                                message=(
                                    "Scheduled message send failed before acceptance; "
                                    f"retrying (attempt {attempt.retry_state.attempt_number}/"
                                    f"{max_attempts})"
                                ),
                                attempt=attempt.retry_state.attempt_number,
                                max_attempts=max_attempts,
                                category="retryable",
                            )
                        else:
                            result = "rejected"
                            record_event(
                                send_logger,
                                logging.WARNING,
                                "scheduled_send_rejected",
                                message=(
                                    "Scheduled message send rejected "
                                    f"(attempt {attempt.retry_state.attempt_number}/"
                                    f"{max_attempts}, category {classification.category.value})"
                                ),
                                attempt=attempt.retry_state.attempt_number,
                                max_attempts=max_attempts,
                                category=classification.category.value,
                            )
                        raise
                    await active_breaker.on_success()
                    result = "retried" if attempt.retry_state.attempt_number >= 2 else "ok"
                    break
        except Exception:
            pass
        metrics.SCHEDULER_SENDS.labels(result).inc()
        if result not in ("ok", "retried"):
            failed_group_ids.append(group_id)
    return failed_group_ids


def _send_retryable(exc: BaseException) -> bool:
    return classify_send_error(exc).retryable
