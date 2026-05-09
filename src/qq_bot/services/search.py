from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from qq_bot.config import BotSettings
from qq_bot.services.ai_client import AsyncPostClient


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


async def search_web(
    query: str,
    *,
    settings: BotSettings,
    client: AsyncPostClient | None = None,
) -> list[SearchResult]:
    if not settings.has_search_config():
        raise SearchError("TAVILY_API_KEY is not configured")

    owns_client = client is None
    active_client: AsyncPostClient
    if client is None:
        active_client = httpx.AsyncClient(timeout=settings.search_timeout_seconds)
    else:
        active_client = client

    try:
        response = await active_client.post(
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
        )
        response.raise_for_status()
        data = response.json()
        raw_results = data["results"]
        if not isinstance(raw_results, list):
            raise TypeError("results must be a list")
        return [_normalize_result(item) for item in raw_results if _is_valid_result(item)]
    except httpx.HTTPError as exc:
        raise SearchError("Tavily search request failed") from exc
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise SearchError("Tavily search returned an invalid response") from exc
    finally:
        if owns_client and isinstance(active_client, httpx.AsyncClient):
            await active_client.aclose()


def _is_valid_result(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    return all(isinstance(item.get(key), str) and item[key].strip() for key in ("title", "url", "content"))


def _normalize_result(item: dict[str, Any]) -> SearchResult:
    return SearchResult(
        title=item["title"].strip(),
        url=item["url"].strip(),
        content=item["content"].strip(),
    )


def format_search_context(results: list[SearchResult]) -> str:
    blocks = [
        f"[{index}] {result.title}\nURL: {result.url}\nSummary: {result.content}"
        for index, result in enumerate(results[:3], start=1)
    ]
    return "\n\n".join(blocks)
