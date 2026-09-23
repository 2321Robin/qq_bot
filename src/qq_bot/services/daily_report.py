"""Life report content pipeline (S6-REPORT).

Sections degrade independently: a single source failure omits that section
and never blocks the report (2026-09-16 用户裁决). Fetched items are the only
facts the polish step (S6-REPORT-04) may present; hot-list intros must quote
fetched search snippets; no message bodies or identifiers ever enter logs or
metrics. Lists render as ``1.`` numbering (QQ plain text does not style
``-``/``·`` bullets).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

import httpx

from qq_bot.config import BotSettings
from qq_bot.observability import metrics, record_error
from qq_bot.observability.logging import current_request_id, new_request_id
from qq_bot.observability.tracing import get_tracer
from qq_bot.services.ai_client import request_ai_reply
from qq_bot.services.countdown import entries_from_settings, format_countdown_section
from qq_bot.services.reliability import (
    CircuitBreaker,
    CircuitOpenError,
    TransientDependencyError,
    build_retry_policy,
    classify_exception,
    wrap_http_error,
)

_REPORT_MAX_ATTEMPTS = 3
_REPORT_BREAKER_NAMES = {
    "news": "report_news",
    "hot": "report_hot",
    "heh": "report_heh",
    "toutiao": "report_toutiao",
}
# 新闻板块标题按端点区分：晚报默认换头条热榜源，避免与早报 60s 新闻重复
_NEWS_SECTION_TITLES = {"news": "📰 新闻", "toutiao": "📰 头条热榜"}


class SourceError(RuntimeError):
    """A report data source is unavailable; its section is omitted."""


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
    ``SourceError`` so the caller can omit that section."""
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
# 2026-09-19 修订（用户裁决）：QQ 纯文本不渲染 -/·，列表统一数字编号；
# 润色模型看不到正文，无事实基础的【寄语】删除；热搜板块改为结合联网搜索
# 资料逐条介绍，资料缺失时退回纯标题模板。
_POLISH_SYSTEM = (
    "你是群聊新闻编辑。下面给你若干条新闻标题。规则："
    "1) 输出条目列表，每条以输入序号加句点开头（如「1. 」），条数必须与输入一致，顺序可以调整；"
    "2) 每条必须完整保留原标题原文，标题后可以追加一句不超过 15 字的说明；"
    "3) 不得新增、删除或改写任何标题，不得编造事实；"
    "4) 只输出条目列表本身，不要输出任何总结、寄语或其他附加行。"
)

_HOT_POLISH_SYSTEM = (
    "你是群聊热搜编辑。下面给你若干条微博热搜标题，部分标题附有联网搜索到的资料。规则："
    "1) 输出条目列表，每条以输入序号加句点开头（如「3. 」），条数与顺序必须与输入完全一致；"
    "2) 每条必须完整保留原标题原文；"
    "3) 标题后可以用一句不超过 30 字的话介绍该条热搜的具体内容，介绍只能依据该条给出的资料，"
    "资料缺失或不足时只保留原标题，不要猜测编造；"
    "4) 最后可以另起一行，以【今日小结】开头用一句话依据资料整体点评今天的热搜，此行可省略；"
    "5) 不得编造事实；除条目列表和可选的小结行外，不要输出任何其他内容。"
)

# 模型自由发挥的"元行"（寄语/小结）：参与校验前一律剥离，是否保留由板块决定
_META_MARKERS = ("【寄语】", "【今日小结】")


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


def _strip_jiyu(text: str) -> str:
    """Remove 寄语 lines from the final output (kept out of every section:
    the polish model never sees article bodies, so it has no grounding)."""
    return "\n".join(line for line in text.splitlines() if "【寄语】" not in line)


def _strip_meta_lines(text: str) -> str:
    """Drop model-generated meta lines (寄语/小结) for grounding comparison;
    they are generation, not curated facts."""
    return "\n".join(
        line for line in text.splitlines() if not any(marker in line for marker in _META_MARKERS)
    )


def _numbered(titles: Sequence[str]) -> list[str]:
    return [f"{index}. {title}" for index, title in enumerate(titles, start=1)]


def _render_list(title: str, titles: Sequence[str]) -> str:
    return "\n".join([title, *_numbered(titles)])


def _split_leading_number(line: str) -> str:
    return re.sub(r"^\s*\d{1,2}\s*[.、)）]?\s*", "", line, count=1)


def _dedupe_items(items: tuple[NewsItem, ...]) -> tuple[NewsItem, ...]:
    """Drop repeated entries (热搜/热榜同题重复很常见), keeping the first
    occurrence; comparison ignores whitespace, case and punctuation."""
    seen: set[str] = set()
    unique: list[NewsItem] = []
    for item in items:
        key = _normalize(item.title).casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return tuple(unique)


def verify_polished(titles: tuple[str, ...], text: str) -> bool:
    """Deterministic grounding check (2026-09-19 revision, format-robust):
    every input title must survive into the body, and every non-empty body
    line must carry at least one original title (nothing fabricated in
    between). Bullet style, numbering and meta lines (寄语/小结) are free."""
    body_lines = [line for line in _strip_meta_lines(text).splitlines() if line.strip()]
    body_norm = _normalize("\n".join(body_lines))
    title_norms = [_normalize(title) for title in titles]
    if any(norm not in body_norm for norm in title_norms):
        return False
    for line in body_lines:
        line_norm = _normalize(line)
        if not any(norm in line_norm for norm in title_norms):
            return False
    return True


def _repair_polished(titles: tuple[str, ...], text: str) -> str | None:
    """Deterministic repair for truncated-but-honest polish output: keep the
    model's annotated lines, append missing titles as plain template lines.
    Returns None when the output contains a line carrying no original title
    (fabrication) — fall back to template."""
    lines = [line for line in _strip_meta_lines(text).splitlines() if line.strip()]
    title_norms = [_normalize(title) for title in titles]
    valid: list[str] = []
    for line in lines:
        line_norm = _normalize(line)
        if not any(norm in line_norm for norm in title_norms):
            return None
        valid.append(line)
    body_norm = _normalize("\n".join(valid))
    missing = [t for t, n in zip(titles, title_norms) if n not in body_norm]
    if not missing:
        return None
    out = "\n".join(valid)
    out += "\n" + "\n".join(_numbered_tail(valid, missing))
    return out


def _numbered_tail(valid: Sequence[str], missing: Sequence[str]) -> list[str]:
    """Number repair-appended lines continuing after the kept body lines."""
    start = len(valid) + 1
    return [f"{start + offset}. {title}" for offset, title in enumerate(missing)]


def verify_hot_polished(titles: tuple[str, ...], text: str) -> bool:
    """Positional grounding check for the hot list: exactly one body line per
    input title, same order (rank order is meaningful), each line carrying
    its own title; per-line intros and the 小结 line are free."""
    body_lines = [line for line in _strip_meta_lines(text).splitlines() if line.strip()]
    if len(body_lines) != len(titles):
        return False
    for title, line in zip(titles, body_lines):
        if _normalize(title) not in _normalize(line):
            return False
    return True


def _repair_hot_polished(titles: tuple[str, ...], text: str) -> str | None:
    """Repair honest-but-misaligned hot output: every kept line must carry an
    original title (else None → template); lines are reassigned to their
    titles and re-emitted in input rank order with fresh numbers; missing
    titles appended as plain numbered lines."""
    lines = [line for line in _strip_meta_lines(text).splitlines() if line.strip()]
    title_norms = [_normalize(title) for title in titles]
    kept: list[str] = []
    for line in lines:
        line_norm = _normalize(line)
        if not any(norm in line_norm for norm in title_norms):
            return None
        kept.append(line)
    used: set[int] = set()
    out_lines: list[str] = []
    for index, (title, norm) in enumerate(zip(titles, title_norms)):
        match = next(
            (
                position
                for position, line in enumerate(kept)
                if position not in used and norm in _normalize(line)
            ),
            None,
        )
        if match is None:
            out_lines.append(f"{index + 1}. {title}")
            continue
        used.add(match)
        # 模型可能编错序号：剥掉行首数字后再按正确排名重编号；剥坏标题则原样保留
        candidate = _split_leading_number(kept[match]).strip()
        body = candidate if norm in _normalize(candidate) else kept[match].strip()
        out_lines.append(f"{index + 1}. {body}")
    return "\n".join(out_lines)


def format_news_template(items: tuple[NewsItem, ...]) -> str:
    return _render_list("📰 新闻", [item.title for item in items])


async def _polish_via_ai(
    prompt: str,
    *,
    titles: tuple[str, ...],
    template: str,
    verify: Callable[[tuple[str, ...], str], bool],
    repair: Callable[[tuple[str, ...], str], str | None],
    settings: BotSettings,
) -> PolishOutcome:
    """Shared polish pipeline: quota gate → main/fallback AI chain →
    deterministic grounding check with repair. Every failure path returns the
    plain template so the report still goes out on time."""
    quota = _quota_service()
    if quota is not None:
        # 配额子系统故障（如 DB locked）按「未启用配额」降级：不封顶、不记账、
        # 不抛出——LLM 永不阻塞报告
        try:
            summary = await quota.summary(scope_type="report", scope_id=0)
            capped = int(summary.get("requests", 0)) >= settings.report_ai_daily_max
        except Exception:
            record_error("quota", "unknown")
            capped = False
        if capped:
            return PolishOutcome(ok=False, text=template, reason="capped")
    # REPORT_AI_MODEL 生效方式：换模型名。REPORT_AI_PROVIDER=fallback 时整个
    # 首选链路切到备用 Provider（模型与主链路不同源的场景，如主 DeepSeek + 备 GLM）
    update = {"ai_model": settings.report_llm_model}
    if settings.report_ai_provider == "fallback":
        update["ai_base_url"] = settings.normalized_ai_fallback_base_url
        update["ai_api_key"] = settings.ai_fallback_api_key
    effective = settings.model_copy(update=update)
    # 润色专用客户端：免费档生成约 30s+，共享客户端固化了主链路 30s 超时，
    # 这里用独立超时（REPORT_AI_TIMEOUT_SECONDS）自建，每次调用即用即关
    timeout_client = httpx.AsyncClient(timeout=httpx.Timeout(settings.report_ai_timeout_seconds))
    try:
        reply = await request_ai_reply(
            prompt,
            settings=effective,
            client=timeout_client,
            search_context="",
            chat_context="",
            roco_context="",
        )
    except Exception:
        return PolishOutcome(ok=False, text=template, reason="error")
    finally:
        await timeout_client.aclose()
    reply = _strip_jiyu(reply)
    if not verify(titles, reply):
        repaired = repair(titles, reply)
        if repaired is None:
            return PolishOutcome(ok=False, text=template, reason="check_failed")
        reply = repaired
    if quota is not None:
        # 订阅套餐无按量账单：tokens/cost 如实记 0/None，requests 计数由表自增
        try:
            await quota.record_usage(scope_type="report", scope_id=0, tokens=0, cost=None)
        except Exception:
            record_error("quota", "unknown")
    return PolishOutcome(ok=True, text=reply, reason="ok")


def _polish_allowed(settings: BotSettings) -> bool:
    return settings.report_ai_enabled and settings.report_ai_daily_max != 0


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
    if not _polish_allowed(settings) or not items:
        return PolishOutcome(ok=False, text=template, reason="disabled")
    prompt = _POLISH_SYSTEM + "\n\n" + "\n".join(_numbered(titles))
    outcome = await _polish_via_ai(
        prompt,
        titles=titles,
        template=template,
        verify=verify_polished,
        repair=_repair_polished,
        settings=settings,
    )
    if not outcome.ok:
        return outcome
    # 新闻模型只看得到标题：小结/寄语一律不出现在成品里
    return PolishOutcome(ok=True, text=_strip_meta_lines(outcome.text), reason="ok")


def _hot_prompt_block(index: int, title: str, note: str) -> str:
    block = f"{index}. {title}"
    if note:
        block += f"\n资料：{note}"
    return block


async def _gather_hot_material(titles: tuple[str, ...], settings: BotSettings) -> tuple[str, ...]:
    """Fetch search snippets per hot title (concurrency-capped). Search is
    optional context: any failure degrades that item's intro to title-only,
    never the section."""
    empty = tuple("" for _ in titles)
    if not titles:
        return empty
    try:
        from qq_bot.services.search import search_web
    except Exception:
        return empty
    if not settings.has_search_config():
        return empty
    semaphore = asyncio.Semaphore(4)

    async def _snippets(title: str) -> str:
        try:
            async with semaphore:
                results = await search_web(title, settings=settings)
        except Exception:
            return ""
        blocks = [f"{result.title}：{result.content[:160]}" for result in results[:2]]
        return " ".join(blocks).strip()

    return tuple(await asyncio.gather(*(_snippets(title) for title in titles)))


async def polish_hot(
    items: tuple[NewsItem, ...],
    settings: BotSettings,
    *,
    material: Sequence[str] = (),
) -> PolishOutcome:
    """Render the weibo hot list with per-item intros grounded in fetched
    search snippets; same failure contract as ``polish_news``."""
    titles = tuple(item.title for item in items)
    template = _render_list(_HOT_TITLE, titles)
    if not _polish_allowed(settings) or not items:
        return PolishOutcome(ok=False, text=template, reason="disabled")
    prompt = (
        _HOT_POLISH_SYSTEM
        + "\n\n"
        + "\n".join(
            _hot_prompt_block(index, title, note)
            for index, (title, note) in enumerate(zip(titles, material), start=1)
        )
    )
    return await _polish_via_ai(
        prompt,
        titles=titles,
        template=template,
        verify=verify_hot_polished,
        repair=_repair_hot_polished,
        settings=settings,
    )


# ---- 早/晚报组装器（S6-REPORT-05）----
# 板块级独立降级：任何单一来源失败都整块省略，整报照发（2026-09-16 用户裁决：
# 失败板块直接省略，不渲染占位）。
_NEWS_TITLE = "📰 新闻"
_HOT_TITLE = "🔥 热搜"
_HEH_TITLE = "🎮 小黑盒热帖"


async def _news_section(
    settings: BotSettings,
    client: AsyncGetClient | None,
    *,
    kind: str,
) -> str:
    endpoint = "news" if kind == "早报" else settings.report_evening_news_endpoint
    title = _NEWS_SECTION_TITLES.get(endpoint, "📰 新闻")
    try:
        items = await fetch_section_items(endpoint, settings, client)
    except SourceError:
        metrics.REPORT_SECTIONS_TOTAL.labels(endpoint, "unavailable").inc()
        return ""
    # 关键词屏蔽（2026-09-16 用户需求）：标题含任一屏蔽词的条目直接剔除
    blocklist = settings.report_news_blocklist_list
    if blocklist:
        lowered = [word.casefold() for word in blocklist]
        items = tuple(
            item for item in items if not any(word in item.title.casefold() for word in lowered)
        )
    items = truncate_items(_dedupe_items(items), settings.report_news_max_items)
    if not items:
        return ""
    outcome = await polish_news(items, settings, client=client)
    metrics.REPORT_LLM_TOTAL.labels("news", outcome.reason).inc()
    if outcome.ok:
        # 润色输出只有条目行，板块标题行由这里统一补上
        return f"{title}\n{outcome.text}"
    return _render_list(title, [item.title for item in items])


async def _list_section(
    endpoint: str,
    title: str,
    settings: BotSettings,
    client: AsyncGetClient | None,
) -> str:
    try:
        items = truncate_items(
            _dedupe_items(await fetch_section_items(endpoint, settings, client)),
            settings.report_heh_max_items,
        )
    except SourceError:
        metrics.REPORT_SECTIONS_TOTAL.labels(endpoint, "unavailable").inc()
        return ""
    metrics.REPORT_SECTIONS_TOTAL.labels(endpoint, "ok").inc()
    return _render_list(title, [item.title for item in items])


async def _hot_section(settings: BotSettings, client: AsyncGetClient | None) -> str:
    """微博热搜：默认 10 条、同题去重；润色开启且搜到资料时由 LLM 结合
    资料逐条介绍，无资料或任何失败都退回纯标题模板。"""
    try:
        items = await fetch_section_items("hot", settings, client)
    except SourceError:
        metrics.REPORT_SECTIONS_TOTAL.labels("hot", "unavailable").inc()
        return ""
    metrics.REPORT_SECTIONS_TOTAL.labels("hot", "ok").inc()
    items = truncate_items(_dedupe_items(items), settings.report_hot_max_items)
    if not items:
        return ""
    titles = tuple(item.title for item in items)
    template = _render_list(_HOT_TITLE, titles)
    if not _polish_allowed(settings):
        return template
    material = await _gather_hot_material(titles, settings)
    if not any(material):
        # 无资料可依据：不做无事实基础的介绍，直接用标题模板
        return template
    outcome = await polish_hot(items, settings, material=material)
    metrics.REPORT_LLM_TOTAL.labels("hot", outcome.reason).inc()
    if outcome.ok:
        return f"{_HOT_TITLE}\n{outcome.text}"
    return template


async def _build_life_message(
    settings: BotSettings,
    *,
    kind: str,
    client: AsyncGetClient | None = None,
    today: date | None = None,
) -> str:
    effective_today = today if today is not None else date.today()
    trace_id = current_request_id() or new_request_id()
    tracer = get_tracer()
    span = tracer.start_span("report.build", trace_id=trace_id)
    try:
        news_text, hot_text, heh_text = await asyncio.gather(
            _news_section(settings, client, kind=kind),
            _hot_section(settings, client),
            _list_section("heh", _HEH_TITLE, settings, client),
        )
    finally:
        tracer.end_span(span)
    parts: list[str] = [*build_date_lines(effective_today, kind=kind)]
    countdown = format_countdown_section(entries_from_settings(settings), effective_today)
    if countdown:
        parts.append(countdown)
    parts.extend(part for part in (news_text, hot_text, heh_text) if part)
    return "\n\n".join(parts)


async def build_life_morning_message(
    settings: BotSettings,
    *,
    client: AsyncGetClient | None = None,
    today: date | None = None,
) -> str | None:
    return await _build_life_message(settings, kind="早报", client=client, today=today)


async def build_life_evening_message(
    settings: BotSettings,
    *,
    client: AsyncGetClient | None = None,
    today: date | None = None,
) -> str | None:
    return await _build_life_message(settings, kind="晚报", client=client, today=today)
