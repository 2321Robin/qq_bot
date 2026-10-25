from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from qq_bot.config import BotSettings, get_settings
from qq_bot.observability import metrics, record_error
from qq_bot.observability.logging import current_request_id
from qq_bot.observability.tracing import get_tracer
from qq_bot.runtime import BREAKER_TAVILY, RuntimeStateError, get_runtime
from qq_bot.services.ai_client import AsyncPostClient
from qq_bot.services.reliability import (
    CircuitBreaker,
    CircuitOpenError,
    PermanentDependencyError,
    TransientDependencyError,
    build_retry_policy,
    classify_exception,
    wrap_http_error,
)


class SearchError(RuntimeError):
    """Raised when web search cannot return usable results."""


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    content: str


def prompt_needs_search(prompt: str) -> bool:
    triggers = (
        "搜索",
        "联网",
        "今天",
        "现在",
        "新闻",
        "查一下",
        "查下",
        "最新",
        "价格",
        "天气",
        "官网",
        "search",
        "latest",
        "today",
        "news",
    )
    return any(trigger in prompt for trigger in triggers)


# 联网判别门（S11-SEARCH-01）：关键词启发式覆盖不住"评价一下 kimi k3.1pro"
# 这类不含触发词、但答案可能过时的问法——由模型判断是否需要搜并产出搜索词。
SEARCH_GATE_SYSTEM_PROMPT = (
    "你是QQ群助手的联网判断器。判断用户的问题是否需要联网搜索才能可靠回答——"
    "涉及最新产品/版本/发布信息、新闻时事、价格、赛程、天气，或任何你的知识"
    "可能已经过时的具体事实，都需要搜；闲聊、问候、纯情绪、对人的看法不需要。"
    '只输出 JSON：{"search": true或false, "query": "中文搜索词"}。'
    "search=true 时 query 给具体的搜索词（补全产品/事件全名，不要照抄原话）；"
    "search=false 时 query 给空字符串。"
)


def build_search_gate_user_prompt(prompt: str) -> str:
    return f"用户问题：{prompt}\n\n需要联网搜索吗？只输出 JSON。"


def parse_search_intent(content: str) -> tuple[bool, str] | None:
    """解析判别门输出；任何不合法形态返回 None（调用方按"不搜"处理）。
    search=true 必须带非空 query（截到 80 字符），否则同样视为无效。"""
    data: Any = None
    try:
        data = json.loads(content)
    except ValueError:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except ValueError:
                return None
    if not isinstance(data, dict):
        return None
    search = data.get("search")
    if not isinstance(search, bool):
        return None
    if not search:
        return False, ""
    query = data.get("query")
    if not isinstance(query, str) or not query.strip():
        return None
    return True, query.strip()[:80]


def _breaker_for(name: str) -> CircuitBreaker:
    try:
        return get_runtime().get_breaker(name)
    except RuntimeStateError:
        settings = get_settings()
        return CircuitBreaker(
            name=name,
            failure_threshold=settings.breaker_failure_threshold,
            recovery_seconds=settings.breaker_recovery_seconds,
        )


def _resolve_client(client: AsyncPostClient | None, settings: BotSettings) -> AsyncPostClient:
    if client is not None:
        return client
    try:
        return get_runtime().get_http_client()
    except RuntimeStateError as exc:
        raise SearchError("Tavily client is not available (runtime not ready)") from exc


async def search_web(
    query: str,
    *,
    settings: BotSettings,
    client: AsyncPostClient | None = None,
) -> list[SearchResult]:
    if not settings.has_search_config():
        raise SearchError("TAVILY_API_KEY is not configured")

    active_client = _resolve_client(client, settings)
    breaker = _breaker_for(BREAKER_TAVILY)
    tracer = get_tracer()
    span = tracer.start_span("search.call", trace_id=current_request_id())
    started_at = time.perf_counter()
    try:
        await breaker.check()
    except CircuitOpenError:
        metrics.SEARCH_REQUESTS.labels("circuit_open").inc()
        tracer.end_span(span, status="error")
        raise SearchError("Tavily search is temporarily unavailable") from None

    policy = build_retry_policy(
        max_attempts=settings.search_max_attempts,
        base_delay_seconds=settings.search_retry_base_delay_seconds,
        max_delay_seconds=settings.search_retry_max_delay_seconds,
        jitter_ratio=settings.retry_jitter_ratio,
    )
    try:
        async for attempt in policy:
            with attempt:
                if attempt.retry_state.attempt_number >= 2:
                    metrics.RETRIES.labels("search").inc()
                try:
                    results = await _search_once(query, settings=settings, client=active_client)
                except SearchError:
                    # Invalid response: permanent, not retried, not counted.
                    raise
                except Exception as exc:
                    await breaker.on_failure(classify_exception(exc))
                    raise
                await breaker.on_success()
                metrics.SEARCH_DURATION.observe(time.perf_counter() - started_at)
                result = "retried" if attempt.retry_state.attempt_number >= 2 else "ok"
                metrics.SEARCH_REQUESTS.labels(result).inc()
                tracer.end_span(span)
                return results
    except (TransientDependencyError, PermanentDependencyError) as exc:
        record_error("search", classify_exception(exc).category.value)
        metrics.SEARCH_REQUESTS.labels("error").inc()
        tracer.end_span(span, status="error", category=classify_exception(exc).category.value)
        raise SearchError("Tavily search request failed") from exc
    tracer.end_span(span, status="error")
    raise SearchError("Tavily search request failed")


async def _search_once(
    query: str,
    *,
    settings: BotSettings,
    client: AsyncPostClient,
) -> list[SearchResult]:
    try:
        response = await client.post(
            "https://api.tavily.com/search",
            headers={
                "Authorization": f"Bearer {settings.tavily_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "query": query.strip(),
                "search_depth": "basic",
                "max_results": settings.search_max_results,
                "include_answer": False,
                "include_raw_content": False,
            },
            timeout=settings.search_timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        raw_results = data["results"]
        if not isinstance(raw_results, list):
            raise TypeError("results must be a list")
        return [_normalize_result(item) for item in raw_results if _is_valid_result(item)]
    except (TransientDependencyError, PermanentDependencyError):
        raise
    except httpx.HTTPStatusError as exc:
        raise wrap_http_error(exc) from exc
    except httpx.HTTPError as exc:
        raise wrap_http_error(exc) from exc
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise SearchError("Tavily search returned an invalid response") from exc


def _is_valid_result(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    return all(
        isinstance(item.get(key), str) and item[key].strip() for key in ("title", "url", "content")
    )


def _normalize_result(item: dict[str, Any]) -> SearchResult:
    return SearchResult(
        title=item["title"].strip(),
        url=item["url"].strip(),
        content=item["content"].strip(),
    )


def format_search_context(results: list[SearchResult]) -> str:
    # Cap at three sources: the reply prompt budget (S1-AI-01) and the
    # "来源：" footer both limit to three entries.
    blocks = [
        f"[{index}] {result.title} - {result.url}\n{result.content}"
        for index, result in enumerate(results[:3], start=1)
    ]
    return "\n\n".join(blocks)
