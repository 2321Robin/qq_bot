"""Chat memory retrieval tool (S2-TOOL-06, S2-MEM-01).

``search_chat_memory`` lets the model query the recent-message layer. Group
and user scope are server-injected from ``AgentScope`` at execution time —
the input schema contains no group_id/user_id the model could forge. The
tool is read-only: persistent memory writes only ever run through explicit
user commands (S2-TOOL-09, S2-MEM-08).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from qq_bot.agent.models import Evidence, RouteKind, ToolResult
from qq_bot.agent.registry import (
    StrictInputModel,
    ToolContext,
    ToolRegistry,
    ToolSpec,
)
from qq_bot.services.chat_memory import ChatMemoryRepository

MAX_KEYWORD_LEN = 100
MAX_MEMORY_ROWS = 10
DEFAULT_MEMORY_ROWS = 5


class SearchChatMemoryInput(StrictInputModel):
    query_type: Literal["recent", "keyword", "user"]
    keyword: Annotated[str | None, Field(default=None, max_length=MAX_KEYWORD_LEN)] = None
    limit: Annotated[int, Field(ge=1, le=MAX_MEMORY_ROWS)] = DEFAULT_MEMORY_ROWS


def create_memory_tool(
    repository: ChatMemoryRepository,
) -> ToolSpec:
    """Build the chat-memory tool over the given repository. Scope and the
    memory permission come from the request's ``AgentScope`` at execution
    time, never from model arguments (S2-TOOL-06)."""

    async def _executor(arguments: dict, context: ToolContext) -> ToolResult:
        scope = context.scope
        if not scope.can_use_chat_memory or scope.group_id is None:
            return ToolResult(
                tool="search_chat_memory",
                status="denied",
                warnings=("聊天记忆不可用",),
            )
        group_id = int(scope.group_id)
        user_id = int(scope.user_id)
        query_type = arguments["query_type"]
        limit = arguments["limit"]
        if query_type == "recent":
            rows = await repository.recent_user_turns(
                group_id=group_id, user_id=user_id, limit=limit
            )
        elif query_type == "keyword":
            rows = await repository.search_messages(
                group_id=group_id, keyword=arguments.get("keyword"), limit=limit
            )
        else:  # user
            rows = await repository.search_messages(group_id=group_id, user_id=user_id, limit=limit)
        if not rows:
            return ToolResult(tool="search_chat_memory", status="not_found")
        evidence = Evidence(
            id=f"M{context.evidence_index + 1}",
            source_type="memory",
            title="聊天记忆",
            facts={
                "query_type": query_type,
                "messages": [
                    {
                        "id": row.id,
                        "user_id": row.user_id,
                        "text": row.message_text,
                        "created_at": row.created_at,
                    }
                    for row in rows
                ],
            },
            url=None,
        )
        return ToolResult(
            tool="search_chat_memory",
            status="ok",
            evidence=(evidence,),
            truncated=len(rows) >= limit,
        )

    return ToolSpec(
        name="search_chat_memory",
        description=(
            "查询近期群聊记录：recent 返回当前用户最近的提问与回复，"
            "keyword 按关键词搜索群内消息，user 返回当前用户的全部消息。"
        ),
        input_model=SearchChatMemoryInput,
        allowed_routes=frozenset({RouteKind.CHAT_MEMORY}),
        max_results=MAX_MEMORY_ROWS,
        timeout_seconds=10.0,
        executor=_executor,
    )


def register_memory_tools(registry: ToolRegistry, repository: ChatMemoryRepository) -> None:
    """Register the memory tool over the given repository (S2-TOOL-06)."""
    registry.register(create_memory_tool(repository))
