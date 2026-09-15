"""Autonomous group chat (S7-AUTO): prefilter, sampling, LLM gate decision,
casual persona generation and in-memory throttling.

Design contract (spec 2026-09-16-auto-chat-design.md): decisions fail
closed; guardrails (cooldown/limits/backoff) apply to the nicknamed
fast-path too; state is process-local and lost on restart by design."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, Sequence

from qq_bot.config import BotSettings
from qq_bot.services.chat_memory import ChatMemoryRow
from qq_bot.services.persona import Persona, mentions_persona


def _default_bucket_clock() -> datetime:
    return datetime.now(UTC)


class AutoChatState:
    """Per-group in-memory throttle state: cooldown timestamp, hourly/daily
    reply counters and negative-feedback backoff deadline."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        bucket_clock: Callable[[], datetime] = _default_bucket_clock,
    ) -> None:
        self._clock = clock
        self._bucket_clock = bucket_clock
        self._last_sent: dict[int, float] = {}
        self._hourly: dict[tuple[int, str], int] = {}
        self._daily: dict[tuple[int, str], int] = {}
        self._backoff_until: dict[int, float] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def lock(self, group_id: int) -> asyncio.Lock:
        return self._locks.setdefault(group_id, asyncio.Lock())

    def in_backoff(self, group_id: int) -> bool:
        deadline = self._backoff_until.get(group_id)
        return deadline is not None and self._clock() < deadline

    def set_backoff(self, group_id: int, seconds: float) -> None:
        self._backoff_until[group_id] = self._clock() + seconds

    def recently_spoke(self, group_id: int, window_seconds: float) -> bool:
        last = self._last_sent.get(group_id)
        return last is not None and self._clock() - last < window_seconds

    def limit_reason(self, group_id: int, *, settings: BotSettings) -> str:
        """`""` when within limits, else `cooldown`|`hourly_limit`|`daily_limit`."""
        now = self._clock()
        last = self._last_sent.get(group_id)
        if last is not None and now - last < settings.auto_chat_cooldown_seconds:
            return "cooldown"
        bucket = self._bucket_clock()
        hour_key = bucket.strftime("%Y-%m-%dT%H")
        day_key = bucket.strftime("%Y-%m-%d")
        if self._hourly.get((group_id, hour_key), 0) >= settings.auto_chat_hourly_limit:
            return "hourly_limit"
        if self._daily.get((group_id, day_key), 0) >= settings.auto_chat_daily_limit:
            return "daily_limit"
        return ""

    def acquire(self, group_id: int, *, settings: BotSettings) -> str:
        """Re-check limits and occupy the slot atomically (call under the
        group lock in the orchestration path). Returns `""` on success."""
        reason = self.limit_reason(group_id, settings=settings)
        if reason:
            return reason
        bucket = self._bucket_clock()
        hour_key = bucket.strftime("%Y-%m-%dT%H")
        day_key = bucket.strftime("%Y-%m-%d")
        self._prune(hour_key, day_key)
        self._last_sent[group_id] = self._clock()
        self._hourly[(group_id, hour_key)] = self._hourly.get((group_id, hour_key), 0) + 1
        self._daily[(group_id, day_key)] = self._daily.get((group_id, day_key), 0) + 1
        return ""

    def _prune(self, current_hour: str, current_day: str) -> None:
        """Drop stale bucket keys so the counters cannot grow unbounded."""
        for key in [k for k in self._hourly if k[1] != current_hour]:
            del self._hourly[key]
        for key in [k for k in self._daily if k[1] != current_day]:
            del self._daily[key]


# ---- 纯函数层：预筛 / 负反馈 / prompt 构建 / 决策门解析（S7-AUTO-05）----

GATE_SYSTEM_PROMPT = (
    "你是QQ群聊天决策器。根据最近群消息判断机器人此刻是否应该以普通群友身份发言。"
    '输出严格 JSON：{"should_reply": true或false, '
    '"reason": "addressed|question_answerable|banter|none", '
    '"confidence": 0.0到1.0的小数}。'
    "addressed=消息在喊机器人名字或明显对它说；question_answerable=有人提问且机器人能可靠回答；"
    "banter=闲聊接梗且机器人有实质可说；none=没有实质可说。"
    "只在有实质可说时才 should_reply=true；政治/色情/赌博/暴力/求医问药/投资建议等"
    "敏感或高风险话题必须 false；纯表情包、灌水、上下文接不上也必须 false。"
    "不要输出任何其他字段。"
)

_GATE_REASONS = frozenset({"addressed", "question_answerable", "banter", "none"})


@dataclass(frozen=True)
class GateDecision:
    should_reply: bool
    reason: str
    confidence: float


def static_prefilter(raw_text: str, *, settings: BotSettings) -> str:
    """Zero-cost static text rules. Returns "pass" or a rejection reason:
    "command" | "too_short" | "sensitive"."""
    text = raw_text.strip()
    prefix = settings.ai_prefix
    if text.startswith("/") or text == prefix or text.startswith(prefix + " "):
        return "command"
    # \\W 对 CJK 是字母类（保留中文），剥掉表情、纯标点与空白
    core = re.sub(r"[\W_]+", "", text, flags=re.UNICODE)
    if len(core) < 2:
        return "too_short"
    if any(word in text for word in settings.auto_chat_sensitive_word_list):
        return "sensitive"
    return "pass"


def detect_negative_feedback(
    rows: Sequence[ChatMemoryRow],
    *,
    persona: Persona,
    settings: BotSettings,
    bot_recently_spoke: bool,
) -> bool:
    """spec 第六节语义：负反馈词与点名特征同现，或该群冷却窗口内刚插过话。"""
    texts = [row.message_text for row in rows]
    negative_words = settings.auto_chat_negative_word_list
    if bot_recently_spoke and any(any(w in t for w in negative_words) for t in texts):
        return True
    return any(
        any(w in t for w in negative_words) and mentions_persona(t, persona) for t in texts
    )


def build_gate_user_prompt(rows: Sequence[ChatMemoryRow]) -> str:
    lines = ["最近群消息（最后一条是最新消息）："]
    lines.extend(f"用户{row.user_id}：{row.message_text}" for row in rows)
    return "\n".join(lines)


def build_casual_user_prompt(rows: Sequence[ChatMemoryRow]) -> str:
    lines = ["最近群消息（最后一条是最新消息）："]
    lines.extend(f"用户{row.user_id}：{row.message_text}" for row in rows)
    lines.append("请以群友身份对最新消息自然地接一句话。")
    return "\n".join(lines)


def parse_gate_output(content: str | None) -> GateDecision | None:
    """Strict parse; anything unexpected returns None (fail closed)."""
    if not content or not content.strip():
        return None
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    should_reply = payload.get("should_reply")
    reason = payload.get("reason", "none")
    try:
        confidence = float(payload.get("confidence", -1.0))
    except (TypeError, ValueError):
        return None
    if not isinstance(should_reply, bool):
        return None
    if reason not in _GATE_REASONS:
        return None
    if not 0.0 <= confidence <= 1.0:
        return None
    return GateDecision(should_reply=should_reply, reason=reason, confidence=confidence)
