"""Life report content pipeline (S6-REPORT).

Sections degrade independently: a single source failure renders '—' and never
blocks the report (S6-REPORT-02). Fetched items are the only facts the polish
step (S6-REPORT-04) may present; no message bodies or identifiers ever enter
logs or metrics.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from qq_bot.config import BotSettings
from qq_bot.observability import metrics
from qq_bot.services.ai_client import request_ai_reply
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


# ---- LLM 润色（S6-REPORT-04）----
# 角色边界：润色与排序，不是生产事实。输入标题是唯一事实来源；确定性校验
# 失败/超时/异常一律回退纯模板渲染，LLM 永不阻塞报告发送。
_POLISH_SYSTEM = (
    "你是群聊新闻编辑。下面给你若干条新闻标题。规则："
    "1) 输出以 · 开头的条目列表，条数必须与输入一致，顺序可以调整；"
    "2) 每条必须完整保留原标题原文，标题后可以追加一句不超过 15 字的说明；"
    "3) 不得新增、删除或改写任何标题，不得编造事实；"
    "4) 条目之后空一行，输出一行以【寄语】开头的一句话寄语。"
)


@dataclass(frozen=True)
class PolishOutcome:
    ok: bool
    text: str
    reason: str  # ok | check_failed | error | disabled | capped


def _quota_service() -> Any | None:
    """Quota service for the report scope; None when runtime is not ready."""
    try:
        from qq_bot.runtime import get_runtime

        return get_runtime().get_quota_service()
    except Exception:
        return None


def _normalize(text: str) -> str:
    return re.sub(r"[\s，。：:、,.\-—|｜·！!？?【】（）()\"'“”]+", "", text)


def _split_jiyu(text: str) -> str:
    """Strip the trailing 寄语 section; it is generation, not curated facts."""
    if "【寄语】" in text:
        return text.partition("【寄语】")[0]
    return text


def verify_polished(titles: tuple[str, ...], text: str) -> bool:
    """Deterministic grounding check: every input title must survive into the
    body (normalized containment; annotations after a title are allowed) and
    the body must not gain extra bullet entries."""
    body = _normalize(_split_jiyu(text))
    if any(_normalize(title) not in body for title in titles):
        return False
    bullets = len(re.findall(r"^\s*[·•]\s*", _split_jiyu(text), flags=re.MULTILINE))
    return bullets <= len(titles)


def format_news_template(items: tuple[NewsItem, ...]) -> str:
    return "\n".join(["📰 新闻", *(f"· {item.title}" for item in items)])


async def polish_news(
    items: tuple[NewsItem, ...],
    settings: BotSettings,
    client: Any | None = None,
) -> PolishOutcome:
    """Rewrite the news section through the main/fallback AI chain, verified
    against the fetched titles. Every failure path returns the plain template
    so the report still goes out on time."""
    titles = tuple(item.title for item in items)
    template = format_news_template(items)
    if not settings.report_ai_enabled or settings.report_ai_daily_max == 0 or not items:
        return PolishOutcome(ok=False, text=template, reason="disabled")
    quota = _quota_service()
    if quota is not None:
        summary = await quota.summary(scope_type="report", scope_id=0)
        if int(summary.get("requests", 0)) >= settings.report_ai_daily_max:
            return PolishOutcome(ok=False, text=template, reason="capped")
    # REPORT_AI_MODEL 生效方式：换模型名，复用主备链路与其余配置
    effective = settings.model_copy(update={"ai_model": settings.report_llm_model})
    prompt = _POLISH_SYSTEM + "\n\n" + "\n".join(f"- {title}" for title in titles)
    try:
        reply = await request_ai_reply(
            prompt,
            settings=effective,
            client=client,
            search_context="",
            chat_context="",
            roco_context="",
        )
    except Exception:
        return PolishOutcome(ok=False, text=template, reason="error")
    if not verify_polished(titles, reply):
        return PolishOutcome(ok=False, text=template, reason="check_failed")
    if quota is not None:
        # 订阅套餐无按量账单：tokens/cost 如实记 0/None，requests 计数由表自增
        await quota.record_usage(scope_type="report", scope_id=0, tokens=0, cost=None)
    return PolishOutcome(ok=True, text=reply, reason="ok")
