"""AI 早报内容管线（S8-BRIEF）。

消息源为「橘鸦AI早报」的公开 RSS（daily.juya.uk/rss.xml，一天一期）：每期
「概览」给出当日全部要闻的标题、分类与原文链接，正文部分给每条新闻的详细
内容。本服务拉取最新一期并校验时效，再用 LLM 把每条「标题+资料」改写成
一行摘要——与生活早报润色同一契约：标题必须原样保留，说明只能依据该条
资料；确定性校验失败/超时/关闭一律回退纯标题模板，LLM 永不阻塞发送。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from xml.etree import ElementTree as ET

import httpx

from qq_bot.config import BotSettings
from qq_bot.observability import metrics
from qq_bot.services.ai_client import request_ai_reply
from qq_bot.services.daily_report import (
    AsyncGetClient,
    PolishOutcome,
    _repair_polished,
    _strip_meta_lines,
    build_date_lines,
    verify_polished,
)
from qq_bot.services.reliability import (
    CircuitBreaker,
    CircuitOpenError,
    TransientDependencyError,
    build_retry_policy,
    classify_exception,
    wrap_http_error,
)

_MAX_ATTEMPTS = 3
_CONTENT_ENCODED_TAG = "{http://purl.org/rss/1.0/modules/content/}encoded"
_ITEM_NUMBER_RE = re.compile(r"#(\d+)")


class SourceError(RuntimeError):
    """The briefing feed is unavailable, unusable or stale; the run is skipped."""


@dataclass(frozen=True)
class BriefingItem:
    number: int  # 源内的 #N 序号（0 = 源未标注）
    category: str  # 概览分类：要闻/开发生态/产品应用/技术与洞察/行业动态
    headline: str
    url: str = ""
    detail: str = ""


# ---- 源 HTML 解析 ----
# 每期 content:encoded 是机器生成的固定结构：
#   <h2>概览</h2> 下按分类 <h3> 分组，每个 <li> = 标题文本 + <a href>原文链接
#   + <code>#N</code>；之后正文区每个 <h3>（内含 #N）开启一条新闻的详细
#   <p> 段落。HTML 非严格 XML（<img ...> 不闭合），用流式 HTMLParser 解析。
class _IssueHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.overview: list[tuple[str, str, str, int]] = []  # (分类, 标题, 链接, 序号)
        self.details: dict[int, list[str]] = {}  # 序号 -> 段落列表
        self._in_heading = False
        self._heading_buf: list[str] = []
        self._h2 = ""
        self._h3 = ""
        self._li: dict[str, Any] | None = None
        self._detail_no: int | None = None
        self._para: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("h1", "h2", "h3"):
            self._in_heading = True
            self._heading_buf = []
        elif tag == "li" and self._h2 == "概览":
            self._li = {"text": [], "url": "", "number": 0, "in_a": False, "in_code": False}
        elif tag == "a":
            if self._li is not None:
                self._li["in_a"] = True
                href = dict(attrs).get("href") or ""
                if href.startswith("http") and not self._li["url"]:
                    self._li["url"] = href
        elif tag == "code" and self._li is not None:
            self._li["in_code"] = True
        elif tag == "p" and self._detail_no is not None:
            self._para = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("h1", "h2", "h3"):
            text = "".join(self._heading_buf).strip()
            self._in_heading = False
            if tag == "h2":
                self._h2 = text
                self._h3 = ""
                self._detail_no = None
            elif tag == "h3":
                if self._h2 == "概览":
                    self._h3 = text
                else:
                    self._detail_no = None
                    match = _ITEM_NUMBER_RE.search(text)
                    if match:
                        self._detail_no = int(match.group(1))
            else:
                self._h2 = ""
                self._detail_no = None
        elif tag == "li" and self._li is not None:
            li = self._li
            self._li = None
            headline = "".join(li["text"]).strip()
            if headline:
                self.overview.append((self._h3, headline, li["url"], int(li["number"])))
        elif tag == "a" and self._li is not None:
            self._li["in_a"] = False
        elif tag == "code" and self._li is not None:
            self._li["in_code"] = False
        elif tag == "p" and self._para is not None:
            text = "".join(self._para).strip()
            if text and self._detail_no is not None:
                self.details.setdefault(self._detail_no, []).append(text)
            self._para = None

    def handle_data(self, data: str) -> None:
        if self._li is not None:
            if self._li["in_code"]:
                match = _ITEM_NUMBER_RE.search(data)
                if match:
                    self._li["number"] = int(match.group(1))
            elif not self._li["in_a"]:
                self._li["text"].append(data)
        elif self._in_heading:
            self._heading_buf.append(data)
        elif self._para is not None:
            self._para.append(data)


def _parse_pub_date(text: str) -> datetime | None:
    text = text.strip()
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _extract_issue_content(xml_text: str) -> tuple[datetime, str]:
    """Return the freshest dated issue's (pubDate, content HTML)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise SourceError("ai briefing feed is not valid xml") from exc
    channel = root.find("channel")
    entries = channel.findall("item") if channel is not None else []
    best_pub: datetime | None = None
    best_content = ""
    for item in entries:
        content = (item.findtext(_CONTENT_ENCODED_TAG) or "").strip()
        if not content:
            continue
        pub = _parse_pub_date(item.findtext("pubDate") or "")
        if pub is None:
            continue
        if best_pub is None or pub > best_pub:
            best_pub, best_content = pub, content
    if best_pub is None or not best_content:
        raise SourceError("ai briefing feed has no dated issue")
    return best_pub, best_content


def parse_briefing_issue(
    xml_text: str,
    *,
    now: datetime,
    max_age_hours: float,
) -> tuple[BriefingItem, ...]:
    """Parse the freshest RSS issue into briefing items; stale or unusable
    issues raise :class:`SourceError` so the run is skipped entirely."""
    pub, content = _extract_issue_content(xml_text)
    age_hours = (now - pub).total_seconds() / 3600.0
    if age_hours > max_age_hours:
        raise SourceError(f"ai briefing issue is stale ({age_hours:.0f}h old)")
    parser = _IssueHTMLParser()
    parser.feed(content)
    parser.close()
    items: list[BriefingItem] = []
    for index, (category, headline, url, number) in enumerate(parser.overview, start=1):
        detail = "\n".join(parser.details.get(number, []))
        items.append(
            BriefingItem(
                number=number or index,
                category=category,
                headline=headline,
                url=url,
                detail=detail,
            )
        )
    if not items:
        raise SourceError("ai briefing issue has no overview items")
    return tuple(items)


# ---- 拉取（与生活早报同款：重试 + 熔断，失败 SourceError）----
def _breaker_for(settings: BotSettings) -> CircuitBreaker:
    try:
        from qq_bot.runtime import get_runtime

        return get_runtime().get_breaker("ai_briefing")
    except Exception:
        return CircuitBreaker(
            name="ai_briefing",
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
        raise SourceError("ai briefing http client is not available") from exc


async def fetch_briefing_xml(
    settings: BotSettings,
    client: AsyncGetClient | None = None,
    *,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> str:
    breaker = _breaker_for(settings)
    try:
        await breaker.check()
    except CircuitOpenError as exc:
        raise SourceError("ai briefing feed circuit open") from exc
    http = _resolve_client(client)
    policy = build_retry_policy(
        max_attempts=_MAX_ATTEMPTS,
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
                    metrics.RETRIES.labels("ai_briefing_feed").inc()
                try:
                    response = await http.get(
                        settings.normalized_ai_briefing_feed_url,
                        timeout=settings.ai_briefing_timeout_seconds,
                    )
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    await breaker.on_failure(classify_exception(exc))
                    raise wrap_http_error(exc) from exc
                await breaker.on_success()
                return response.text
    except SourceError:
        raise
    except Exception as exc:
        raise SourceError("ai briefing feed unavailable") from exc
    raise SourceError("ai briefing feed exhausted retries")


# ---- LLM 编辑（与生活早报润色同一契约，scope=ai_briefing）----
# 角色边界：摘要与排序，不是生产事实。标题是唯一必须原样保留的事实锚点；
# 每条说明只能依据该条给出的资料；确定性校验失败/超时/异常一律回退纯标题
# 模板，LLM 永不阻塞发送。
_BRIEFING_SYSTEM = (
    "你是群聊AI新闻早报编辑。下面给你若干条过去24小时的AI新闻，每条含编号、"
    "标题和资料。规则："
    "1) 输出条目列表，每条以输入序号加句点开头（如「1. 」），条数必须与输入"
    "一致，越重要的排越前，顺序可以调整；"
    "2) 每条必须完整保留原标题原文，标题后可以依据该条给出的资料追加一句"
    "不超过 25 字的说明；"
    "3) 说明只能依据该条给出的资料，资料缺失或不足时只保留原标题，不要猜测"
    "编造；"
    "4) 不得新增、删除或改写任何标题，不得编造事实；"
    "5) 只输出条目列表本身，不要输出任何总结、点评或其他附加行。"
)


def _quota_service() -> Any | None:
    try:
        from qq_bot.runtime import get_runtime

        return get_runtime().get_quota_service()
    except Exception:
        return None


def render_briefing_template(items: tuple[BriefingItem, ...]) -> str:
    """Deterministic fallback: the issue's own overview, numbered for QQ."""
    return "\n".join(f"{index}. {item.headline}" for index, item in enumerate(items, start=1))


def build_briefing_prompt(items: tuple[BriefingItem, ...], *, detail_chars: int) -> str:
    blocks: list[str] = []
    for index, item in enumerate(items, start=1):
        block = f"{index}. {item.headline}"
        detail = item.detail.strip()
        if detail_chars > 0 and detail:
            block += f"\n资料：{detail[:detail_chars]}"
        blocks.append(block)
    return _BRIEFING_SYSTEM + "\n\n" + "\n".join(blocks)


async def compose_briefing(
    items: tuple[BriefingItem, ...],
    settings: BotSettings,
) -> PolishOutcome:
    """Rewrite every item into a one-line summary through the main/fallback
    AI chain, verified against the fetched headlines. Every failure path
    returns the plain headline template so the briefing still goes out."""
    titles = tuple(item.headline for item in items)
    template = render_briefing_template(items)
    if not items or not settings.ai_briefing_ai_enabled or settings.ai_briefing_ai_daily_max == 0:
        return PolishOutcome(ok=False, text=template, reason="disabled")
    quota = _quota_service()
    if quota is not None:
        summary = await quota.summary(scope_type="ai_briefing", scope_id=0)
        if int(summary.get("requests", 0)) >= settings.ai_briefing_ai_daily_max:
            return PolishOutcome(ok=False, text=template, reason="capped")
    # 模型/链路选择与 REPORT_AI_* 同语义：model 空 = 复用 ai_model；
    # provider=fallback 时整个首选链路切到备用 Provider。
    update = {"ai_model": settings.ai_briefing_llm_model}
    if settings.ai_briefing_ai_provider == "fallback":
        update["ai_base_url"] = settings.normalized_ai_fallback_base_url
        update["ai_api_key"] = settings.ai_fallback_api_key
    effective = settings.model_copy(update=update)
    # 摘要专用客户端：免费档生成慢，独立超时（AI_BRIEFING_AI_TIMEOUT_SECONDS）
    # 自建，每次调用即用即关
    timeout_client = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.ai_briefing_ai_timeout_seconds)
    )
    try:
        reply = await request_ai_reply(
            build_briefing_prompt(items, detail_chars=settings.ai_briefing_detail_chars),
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
    reply = _strip_meta_lines(reply)
    if not verify_polished(titles, reply):
        repaired = _repair_polished(titles, reply)
        if repaired is None:
            return PolishOutcome(ok=False, text=template, reason="check_failed")
        reply = repaired
    if quota is not None:
        # 订阅套餐无按量账单：tokens/cost 如实记 0/None，requests 计数由表自增
        await quota.record_usage(scope_type="ai_briefing", scope_id=0, tokens=0, cost=None)
    return PolishOutcome(ok=True, text=reply, reason="ok")


# ---- 组装 ----
async def build_ai_briefing_message(
    settings: BotSettings,
    *,
    client: AsyncGetClient | None = None,
    now: datetime | None = None,
) -> str | None:
    """Build one AI briefing message; ``None`` means nothing to send this
    round (feed unconfigured/unavailable/stale) and the run is skipped."""
    if not settings.has_ai_briefing_feed_config():
        return None
    # 本地时区（日报头部的日期/农历按本地日算）；注入 aware datetime 供测试
    effective_now = now if now is not None else datetime.now().astimezone()
    try:
        xml_text = await fetch_briefing_xml(settings, client)
        items = parse_briefing_issue(
            xml_text,
            now=effective_now,
            max_age_hours=settings.ai_briefing_max_age_hours,
        )
    except SourceError:
        metrics.BRIEFING_FEED_TOTAL.labels("unavailable").inc()
        return None
    metrics.BRIEFING_FEED_TOTAL.labels("ok").inc()
    items = items[: settings.ai_briefing_max_items]
    if not items:
        return None
    outcome = await compose_briefing(items, settings)
    metrics.BRIEFING_LLM_TOTAL.labels(outcome.reason).inc()
    body = outcome.text if outcome.ok else render_briefing_template(items)
    parts = [*build_date_lines(effective_now.date(), kind="AI早报"), body]
    credit = settings.ai_briefing_credit.strip()
    if credit:
        parts.append(credit)
    return "\n".join(parts)
