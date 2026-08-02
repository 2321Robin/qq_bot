"""Web search tool (S2-TOOL-01, S2-SEC-01..06, S2-AGENT-07).

Reuses the stage-1 Tavily search service (shared client, retry policy,
independent breaker). Output is sanitized and carried as untrusted Web
evidence; the bot never fetches search result URLs itself (S2-SEC-05).
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from qq_bot.agent.guardrails import sanitize_search_text, validate_web_url
from qq_bot.agent.models import Evidence, RouteKind, ToolResult
from qq_bot.agent.registry import StrictInputModel, ToolContext, ToolRegistry, ToolSpec
from qq_bot.config import BotSettings
from qq_bot.services import search as search_service

MAX_QUERY_LEN = 200
MAX_TITLE_CHARS = 200
MAX_SNIPPET_CHARS = 500
MAX_RESULTS = 5
MAX_EVIDENCE_RESULTS = 5

WEB_RESULT_TIMEOUT_SECONDS = 15.0


class SearchWebInput(StrictInputModel):
    query: Annotated[str, Field(min_length=1, max_length=MAX_QUERY_LEN)]
    max_results: Annotated[int, Field(ge=1, le=MAX_RESULTS)] = 3


def create_web_tool(
    *,
    settings: BotSettings,
    client: object | None = None,
) -> ToolSpec:
    """Build the ``search_web`` tool. ``client`` is injected in tests; in
    production the shared runtime client is used (S2-AGENT-07 — the tool
    never creates its own global client)."""

    async def search_web(arguments: dict[str, object], context: ToolContext) -> ToolResult:
        query = str(arguments["query"])
        max_results = int(arguments["max_results"])
        try:
            results = await search_service.search_web(
                query,
                settings=settings,
                client=client,  # type: ignore[arg-type]
            )
        except search_service.SearchError:
            return ToolResult(
                tool="search_web", status="unavailable", warnings=("搜索服务暂不可用",)
            )

        entries: list[Evidence] = []
        index = context.evidence_index + 1
        for result in results[:max_results]:
            url = validate_web_url(result.url)
            if url is None:
                continue  # an unsafe URL never becomes evidence (S2-SEC-01/02)
            entries.append(
                Evidence(
                    id=f"W{index}",
                    source_type="web",
                    title=sanitize_search_text(result.title, max_chars=MAX_TITLE_CHARS),
                    facts={
                        "snippet": sanitize_search_text(
                            result.content, max_chars=MAX_SNIPPET_CHARS
                        ),
                        "query": sanitize_search_text(query, max_chars=MAX_QUERY_LEN),
                    },
                    url=url,
                )
            )
            index += 1
        if not entries:
            return ToolResult(
                tool="search_web", status="not_found", warnings=("没有可用的搜索结果",)
            )
        return ToolResult(
            tool="search_web",
            status="ok",
            evidence=tuple(entries),
            truncated=len(results) > max_results,
        )

    return ToolSpec(
        name="search_web",
        description="联网搜索最新的洛克王国公告、活动与资料（结果来自第三方搜索，不可信）。",
        input_model=SearchWebInput,
        allowed_routes=frozenset({RouteKind.WEB_SEARCH}),
        max_results=MAX_RESULTS,
        contains_untrusted=True,
        timeout_seconds=WEB_RESULT_TIMEOUT_SECONDS,
        executor=search_web,
    )


def register_web_tool(
    registry: ToolRegistry,
    *,
    settings: BotSettings,
    client: object | None = None,
) -> None:
    registry.register(create_web_tool(settings=settings, client=client))
