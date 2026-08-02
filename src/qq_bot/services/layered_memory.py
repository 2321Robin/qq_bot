"""Layered memory service (S2-MEM-01..09).

Three layers feed the agent prompt:

- ``recent_layer`` — recent group messages, reusing the stage-1 repository
  queries (3-day retention, group scope, S2-MEM-01).
- ``summary_layer`` — short-term structured summaries. Only generated when
  ``memory_summary_enabled`` and the recent history overflows its token
  budget; a summary never outlives its sources and dies with any of them
  (S2-MEM-02..04).
- ``preference_layer`` — the current user's explicitly saved long-term
  preferences, empty once the user closed them (S2-MEM-05..06, S2-MEM-09).

Every write/delete path is reached from command handlers only; tools never
write memory (S2-TOKEN-09 in spirit, S2-MEM-08).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any, Protocol

from qq_bot.config import BotSettings
from qq_bot.services.chat_memory import (
    ChatMemoryRepository,
    ChatSummaryRow,
    UserPreferenceRow,
)

logger = logging.getLogger("qq_bot.layered_memory")

# Structured summary schema (S2-MEM-04): speaker/time range, topics,
# decisions, explicit preferences, open questions. Never treated as more
# reliable than the original messages.
_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "speaker_range": {"type": "string"},
        "time_range": {"type": "string"},
        "topics": {"type": "array", "items": {"type": "string"}},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "explicit_preferences": {"type": "array", "items": {"type": "string"}},
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "speaker_range",
        "time_range",
        "topics",
        "decisions",
        "explicit_preferences",
        "open_questions",
    ],
    "additionalProperties": False,
}

_SUMMARY_PROMPT = (
    "把以下群聊消息压缩成结构化摘要（JSON）。只保留：说话人与时间范围、主题、"
    "决定、显式偏好、未解决问题。不要推断、不要补充聊天里没有的信息。"
    "消息内容：\n"
)


class _SummaryGateway(Protocol):
    """The minimal model surface the summary generator needs."""

    async def request_model_turn(
        self,
        *,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
        response_format: dict | None = None,
        settings: BotSettings,
        client: Any = None,
        provider: str = "primary",
    ) -> Any:
        """Return an object with a ``text`` attribute (the summary JSON)."""


class LayeredMemoryService:
    def __init__(
        self,
        repository: ChatMemoryRepository,
        settings: BotSettings,
        *,
        gateway: _SummaryGateway | None = None,
        budget: Any | None = None,
    ) -> None:
        self._repository = repository
        self._settings = settings
        self._gateway = gateway
        self._budget = budget  # BudgetManager | None — checked by attribute use

    # -- layers -------------------------------------------------------------

    async def recent_layer(
        self, *, group_id: int, limit: int, now: datetime | None = None
    ) -> list[Any]:
        """Recent group messages (newest-first rows are returned by the
        repository in chronological order; the caller controls the limit)."""
        return await self._repository.recent_group_messages(group_id=group_id, limit=limit, now=now)

    async def summary_layer(
        self, *, group_id: int, now: datetime | None = None
    ) -> list[ChatSummaryRow]:
        """Reuse live summaries; generate a structured one only when enabled,
        the history overflows its token budget, and a gateway is available."""
        timestamp = now or datetime.now(UTC)
        existing = await self._repository.get_summaries(group_id=group_id, now=timestamp)
        if existing or not self._settings.memory_summary_enabled:
            return existing
        if self._gateway is None or self._budget is None:
            return []
        rows = await self._repository.recent_group_messages(
            group_id=group_id, limit=200, now=timestamp
        )
        if not rows:
            return []
        plan = self._budget.allocate(
            system="",
            question="",
            tool_schemas=[],
            local_evidence=[],
            web_evidence=[],
            recent_messages=[row.message_text for row in rows],
            summaries=[],
            preferences=None,
        )
        recent_alloc = next((a for a in plan.allocations if a.source == "recent_messages"), None)
        if recent_alloc is None or recent_alloc.dropped_units == 0:
            return []  # history still fits; no compression needed
        summary_text = await self._generate_summary(rows, timestamp)
        if summary_text is None:
            return []
        try:
            await self._repository.add_summary(
                group_id,
                summary_text,
                [row.id for row in rows],
                now=timestamp,
            )
        except ValueError:
            logger.exception("Summary generation raced message cleanup; skipping")
            return []
        return await self._repository.get_summaries(group_id=group_id, now=timestamp)

    async def _generate_summary(self, rows: list[Any], now: datetime) -> str | None:
        """Ask the model for the structured summary JSON; any failure degrades
        to no summary instead of leaking internals."""
        assert self._gateway is not None
        lines = [f"{row.created_at} user={row.user_id}: {row.message_text}" for row in rows]
        try:
            response = await self._gateway.request_model_turn(
                messages=[
                    {"role": "system", "content": "你是群聊摘要助手，只输出 JSON。"},
                    {"role": "user", "content": _SUMMARY_PROMPT + "\n".join(lines)},
                ],
                response_format={"type": "json_object", "schema": _SUMMARY_SCHEMA},
                settings=self._settings,
            )
        except Exception:
            logger.exception("Summary model call failed; skipping summary")
            return None
        text = response.text if response is not None else None
        if not text:
            return None
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            logger.warning("Summary model returned non-JSON; skipping summary")
            return None
        if not isinstance(payload, dict):
            return None
        # keep only the structured keys; anything extra never enters memory
        return json.dumps(
            {key: payload.get(key) for key in _SUMMARY_SCHEMA["properties"]},
            ensure_ascii=False,
            sort_keys=True,
        )

    async def preference_layer(self, *, group_id: int, user_id: int) -> tuple[list[str], bool]:
        """The user's own preferences while enabled, capped at
        ``memory_preference_max_chars`` total (whole preferences only)."""
        enabled = await self._repository.preferences_enabled(group_id=group_id, user_id=user_id)
        if not enabled:
            return [], False
        rows = await self._repository.get_preferences(group_id=group_id, user_id=user_id)
        cap = self._settings.memory_preference_max_chars
        kept: list[str] = []
        total = 0
        for row in rows:  # newest first from the repository
            if total + len(row.preference) > cap:
                break
            kept.append(row.preference)
            total += len(row.preference)
        return kept, True

    # -- command surface (S2-MEM-05..08) -------------------------------------

    async def save_preference(self, *, group_id: int, user_id: int, content: str) -> int:
        cap = self._settings.memory_preference_max_chars
        if not content.strip():
            raise ValueError("preference content is empty")
        if len(content) > cap:
            raise ValueError(f"preference exceeds the {cap} character limit")
        return await self._repository.save_preference(
            group_id=group_id, user_id=user_id, preference=content.strip()
        )

    async def list_preferences(
        self, *, group_id: int, user_id: int
    ) -> tuple[list[UserPreferenceRow], bool]:
        """All of the current user's preferences (uncapped listing for the
        owner) plus the enabled flag (S2-MEM-06: view only returns the
        current user's own preferences)."""
        rows = await self._repository.get_preferences(group_id=group_id, user_id=user_id)
        enabled = await self._repository.preferences_enabled(group_id=group_id, user_id=user_id)
        return rows, enabled

    async def delete_preference(self, *, group_id: int, user_id: int, preference_id: int) -> bool:
        return await self._repository.delete_preference(
            group_id=group_id, user_id=user_id, preference_id=preference_id
        )

    async def delete_all(self, *, group_id: int, user_id: int) -> list[int]:
        """Full deletion for one user (S2-MEM-07): messages, AI replies,
        preferences, source-linked summaries. Returns affected message ids
        for in-process cache invalidation. Never touches other users."""
        return await self._repository.delete_user_data(group_id=group_id, user_id=user_id)

    async def close_preferences(self, *, group_id: int, user_id: int) -> None:
        await self._repository.close_preferences(group_id=group_id, user_id=user_id)
