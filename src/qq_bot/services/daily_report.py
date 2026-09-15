"""Life report content pipeline (S6-REPORT).

Sections degrade independently: a single source failure renders '—' and never
blocks the report (S6-REPORT-02). Fetched items are the only facts the polish
step (S6-REPORT-04) may present; no message bodies or identifiers ever enter
logs or metrics.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from qq_bot.config import BotSettings
from qq_bot.observability import metrics
from qq_bot.services.reliability import (
    CircuitBreaker,
    CircuitOpenError,
    TransientDependencyError,
    build_retry_policy,
    classify_exception,
    wrap_http_error,
)

_REPORT_MAX_ATTEMPTS = 3
_REPORT_BREAKER_NAMES = {"news": "report_news", "hot": "report_hot", "heh": "report_heh"}


class SourceError(RuntimeError):
    """A report data source is unavailable; its section degrades to '—'."""


@dataclass(frozen=True)
class NewsItem:
    title: str


class AsyncGetClient(Protocol):
    """httpx.AsyncClient satisfies this; tests supply canned stand-ins."""

    async def get(self, url: str, *, timeout: float) -> Any: ...


def _breaker_for(name: str, settings: BotSettings) -> CircuitBreaker:
    try:
        from qq_bot.runtime import get_runtime

        return get_runtime().get_breaker(name)
    except Exception:
        return CircuitBreaker(
            name=name,
            failure_threshold=settings.breaker_failure_threshold,
            recovery_seconds=settings.breaker_recovery_seconds,
        )


def _resolve_client(client: AsyncGetClient | None) -> AsyncGetClient:
    if client is not None:
        return client
    try:
        from qq_bot.runtime import get_http_client

        return get_http_client()
    except Exception as exc:
        raise SourceError("report http client is not available") from exc


def _normalize_items(payload: Any) -> tuple[NewsItem, ...]:
    entries = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise SourceError("report payload missing items list")
    items: list[NewsItem] = []
    for entry in entries:
        title = entry.get("title") if isinstance(entry, dict) else None
        if isinstance(title, str) and title.strip():
            items.append(NewsItem(title=title.strip()))
    if not items:
        raise SourceError("report payload has no usable items")
    return tuple(items)


async def fetch_section_items(
    endpoint: str,
    settings: BotSettings,
    client: AsyncGetClient | None = None,
    *,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> tuple[NewsItem, ...]:
    """Fetch one report section: transient failures retry with backoff, the
    per-source breaker short-circuits, and every failure surfaces as
    ``SourceError`` so the caller can degrade that section to '—'."""
    if endpoint not in _REPORT_BREAKER_NAMES:
        raise ValueError(f"unknown report endpoint: {endpoint}")
    if not settings.has_report_source_config():
        raise SourceError("report source base url is not configured")
    breaker = _breaker_for(_REPORT_BREAKER_NAMES[endpoint], settings)
    try:
        await breaker.check()
    except CircuitOpenError as exc:
        raise SourceError("report source circuit open") from exc
    http = _resolve_client(client)
    url = f"{settings.normalized_report_60s_base_url}/{endpoint}"
    policy = build_retry_policy(
        max_attempts=_REPORT_MAX_ATTEMPTS,
        base_delay_seconds=0.2,
        max_delay_seconds=1.0,
        jitter_ratio=settings.retry_jitter_ratio,
        sleep=sleep,
        retryable=lambda exc: isinstance(exc, TransientDependencyError),
    )
    try:
        async for attempt in policy:
            with attempt:
                if attempt.retry_state.attempt_number >= 2:
                    metrics.RETRIES.labels(f"report_{endpoint}").inc()
                try:
                    response = await http.get(url, timeout=settings.report_60s_timeout_seconds)
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    await breaker.on_failure(classify_exception(exc))
                    raise wrap_http_error(exc) from exc
                await breaker.on_success()
                return _normalize_items(response.json())
    except SourceError:
        raise
    except Exception as exc:
        raise SourceError(f"report source {endpoint} unavailable") from exc
    raise SourceError(f"report source {endpoint} exhausted retries")


def truncate_items(items: tuple[NewsItem, ...], limit: int) -> tuple[NewsItem, ...]:
    return items[: max(limit, 0)]


# ---- 离线日期板块（S6-REPORT-03）----
# 两个可选库都允许缺失/异常：缺谁就降级掉谁的文本，日期行永远在场。
_WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _lunar_text(today: Any) -> str:
    try:
        from datetime import datetime as _datetime

        from cnlunar import Lunar

        lunar = Lunar(_datetime(today.year, today.month, today.day))
        month_cn = lunar.lunarMonthCn
        for size in ("大", "小"):
            if month_cn.endswith(size):
                month_cn = month_cn[: -len(size)]
        text = f"农历{month_cn}{lunar.lunarDayCn}"
        term = lunar.get_todaySolarTerms()
        if term and term != "无":
            text = f"{text}·{term}"
        return text
    except Exception:
        return ""


def _holiday_text(today: Any) -> str:
    try:
        import chinese_calendar as _cn_holiday

        is_holiday, name = _cn_holiday.get_holiday_detail(today)
    except Exception:
        return ""
    if not is_holiday:
        return ""
    suffix = f"（{name}）" if name else ""
    return f"🎉 法定节假日{suffix}"


def build_date_lines(today: Any, *, kind: str = "早报") -> tuple[str, ...]:
    header = f"【{kind}】{today.month}月{today.day}日 {_WEEKDAY_CN[today.weekday()]}"
    extras = [text for text in (_lunar_text(today), _holiday_text(today)) if text]
    line = f"{header} {''.join(extras)}" if extras else header
    return (line,)
