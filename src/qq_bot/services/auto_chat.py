"""Autonomous group chat (S7-AUTO): prefilter, sampling, LLM gate decision,
casual persona generation and in-memory throttling.

Design contract (spec 2026-09-16-auto-chat-design.md): decisions fail
closed; guardrails (cooldown/limits/backoff) apply to the nicknamed
fast-path too; state is process-local and lost on restart by design."""

from __future__ import annotations

import asyncio
import enum
import json
import random
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

from qq_bot.config import BotSettings
from qq_bot.observability import metrics, record_error
from qq_bot.observability.logging import current_request_id, hash_id
from qq_bot.observability.tracing import get_tracer
from qq_bot.services.chat_memory import ChatMemoryRow
from qq_bot.services.memory_prompt import build_user_aliases
from qq_bot.services.memory_prompt import render_rows_with_aliases as _render_rows
from qq_bot.services.memory_prompt import substitute_reference_tokens
from qq_bot.services.persona import (
    Persona,
    casual_system_prompt,
    load_persona,
    mentions_persona,
)
from qq_bot.services.reliability import classify_exception
from qq_bot.services.typo_injector import maybe_apply_typo


def _default_bucket_clock() -> datetime:
    return datetime.now(UTC)


# 群兴趣度（S10-HUMAN-P2 H7）：每条计分消息的 boost 默认值。作为模块常量而非
# 构造参数——AutoChatState 不依赖 BotSettings（热路径保持配置无关），部署者通过
# `auto_chat_interest_boost` 调值，由 ai_chat handler 在调用处注入。
_INTEREST_BOOST_FIXED = 0.02

# 兴趣度惰性衰减的清理阈值：折算后低于此值视为归零并清键（状态字典不随安静群增长）
_INTEREST_DECAY_FLOOR = 1e-6


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
        self._hot_until: dict[int, float] = {}
        self._hot_streak: dict[int, int] = {}
        self._cold_daily: dict[tuple[int, str], int] = {}
        # 群兴趣度（S10-HUMAN-P2 H7）：每群 [0,1] 浮点 + 上次 boost 的单调时刻
        self._interest: dict[int, float] = {}
        self._interest_at: dict[int, float] = {}

    def lock(self, group_id: int) -> asyncio.Lock:
        return self._locks.setdefault(group_id, asyncio.Lock())

    def in_backoff(self, group_id: int) -> bool:
        deadline = self._backoff_until.get(group_id)
        return deadline is not None and self._clock() < deadline

    def set_backoff(self, group_id: int, seconds: float) -> None:
        self._backoff_until[group_id] = self._clock() + seconds
        # 负反馈强制退出热聊（spec 第四节）
        self._hot_until.pop(group_id, None)
        self._hot_streak[group_id] = 0

    def recently_spoke(self, group_id: int, window_seconds: float) -> bool:
        last = self._last_sent.get(group_id)
        return last is not None and self._clock() - last < window_seconds

    def limit_reason(
        self,
        group_id: int,
        *,
        settings: BotSettings,
        cooldown_seconds: float | None = None,
        skip_hourly: bool = False,
    ) -> str:
        """`""` when within limits, else `cooldown`|`hourly_limit`|`daily_limit`.

        `cooldown_seconds` overrides `settings.auto_chat_cooldown_seconds`
        (hot mode passes 30s); `skip_hourly=True` exempts the hourly cap
        (hot mode only)."""
        cooldown = (
            cooldown_seconds
            if cooldown_seconds is not None
            else settings.auto_chat_cooldown_seconds
        )
        now = self._clock()
        last = self._last_sent.get(group_id)
        if last is not None and now - last < cooldown:
            return "cooldown"
        bucket = self._bucket_clock()
        hour_key = bucket.strftime("%Y-%m-%dT%H")
        day_key = bucket.strftime("%Y-%m-%d")
        if (
            not skip_hourly
            and self._hourly.get((group_id, hour_key), 0) >= settings.auto_chat_hourly_limit
        ):
            return "hourly_limit"
        if self._daily.get((group_id, day_key), 0) >= settings.auto_chat_daily_limit:
            return "daily_limit"
        return ""

    def acquire(
        self,
        group_id: int,
        *,
        settings: BotSettings,
        cooldown_seconds: float | None = None,
        skip_hourly: bool = False,
    ) -> str:
        """Re-check limits and occupy the slot atomically (call under the
        group lock in the orchestration path). Returns `""` on success."""
        reason = self.limit_reason(
            group_id,
            settings=settings,
            cooldown_seconds=cooldown_seconds,
            skip_hourly=skip_hourly,
        )
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

    def hot_active(self, group_id: int, *, settings: BotSettings) -> bool:
        until = self._hot_until.get(group_id)
        return (
            until is not None
            and self._clock() < until
            and self._hot_streak.get(group_id, 0) < settings.auto_chat_hot_streak_limit
        )

    def note_reply(self, group_id: int, *, settings: BotSettings) -> str:
        """登记一次机器人发言：刷新热聊窗口并累计 streak。返回 `""` 或
        `"hot_exit_limit"`（streak 触及兜底上限并退出热聊）。"""
        self._hot_until[group_id] = self._clock() + settings.auto_chat_hot_window_seconds
        self._hot_streak[group_id] = self._hot_streak.get(group_id, 0) + 1
        if self._hot_streak[group_id] >= settings.auto_chat_hot_streak_limit:
            self._hot_until.pop(group_id, None)
            self._hot_streak[group_id] = 0
            return "hot_exit_limit"
        return ""

    def cold_limit_reached(self, group_id: int, *, settings: BotSettings) -> bool:
        day_key = self._bucket_clock().strftime("%Y-%m-%d")
        return self._cold_daily.get((group_id, day_key), 0) >= settings.auto_chat_cold_daily_limit

    def note_cold_reply(self, group_id: int, *, settings: BotSettings) -> None:
        day_key = self._bucket_clock().strftime("%Y-%m-%d")
        self._cold_daily[(group_id, day_key)] = self._cold_daily.get((group_id, day_key), 0) + 1

    # ---- 群兴趣度（S10-HUMAN-P2 H7）----

    def note_activity(
        self, group_id: int, *, weight: float = 1.0, boost: float | None = None
    ) -> None:
        """记一条群聊热度。先按半衰期折算存量，再加 `boost × weight`，钳 [0,1]。

        `boost` 省略时用模块常量 `_INTEREST_BOOST_FIXED`；调用方（ai_chat
        handler）显式传 `settings.auto_chat_interest_boost` 使配置项生效，
        同时 state 本身不依赖 BotSettings。负 weight 被夹为 0（绝不降温）。"""
        current = self.interest_raw(group_id)
        applied = _INTEREST_BOOST_FIXED if boost is None else boost
        self._interest[group_id] = max(0.0, min(1.0, current + applied * max(weight, 0.0)))
        self._interest_at[group_id] = self._clock()

    def interest_of(self, group_id: int, *, settings: BotSettings) -> float:
        return self.interest_raw(group_id, settings=settings)

    def interest_raw(self, group_id: int, *, settings: BotSettings | None = None) -> float:
        """惰性半衰衰减后的兴趣度。读路径会把折算值写回 `_interest`（有意为之：
        保持字典只存当前值，安静群自动清键），因此这是幂等但非纯读的操作。

        `settings is None` 时（note 路径的存量折算）不做时间衰减——那里只需要
        把存量按半衰期折小后再叠 boost，配置由读取方（interest_of）提供。衰减
        数学不随 `auto_chat_interest_enabled` 开关变化：开关只门控管道消费
        （采样乘数/冷却乘数/高热进热聊/gauge），状态本身始终如实记账。"""
        current = self._interest.get(group_id, 0.0)
        last = self._interest_at.get(group_id)
        if current <= 0.0 or last is None:
            return current
        if settings is None:
            return current
        elapsed = self._clock() - last
        if elapsed <= 0:
            return current
        half_life = settings.auto_chat_interest_half_life
        if half_life <= 0:
            return current
        decayed = current * (0.5 ** (elapsed / half_life))
        if decayed < _INTEREST_DECAY_FLOOR:
            self._interest.pop(group_id, None)
            self._interest_at.pop(group_id, None)
            return 0.0
        self._interest[group_id] = decayed
        self._interest_at[group_id] = self._clock()
        return decayed


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
    "消息列表中的“机器人：”行表示机器人参与过该话题；"
    "若最新消息里的“你”可能指机器人（尤其紧跟机器人发言之后），视为 addressed，倾向回复。"
    "不要输出任何其他字段。"
    "“连发N条”表示同一人的连续短消息，视为一段完整发言。"
)

_GATE_REASONS = frozenset({"addressed", "question_answerable", "banter", "none"})


def _row_created_at(row: ChatMemoryRow) -> datetime | None:
    """行时间解析：ISO 字符串，naive 视为 UTC；解析失败返回 None。"""
    try:
        parsed = datetime.fromisoformat(row.created_at)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


@dataclass(frozen=True)
class GateDecision:
    should_reply: bool
    reason: str
    confidence: float


@dataclass(frozen=True)
class BatchItem:
    """收集器批次项（S10-HUMAN-P0 H1）。`row_id` 是 chat_memory 行 id，
    供上下文去重（避免批次文本双渲染）；单消息旁路路径可为 None。"""

    raw_text: str
    sender_user_id: int
    created_at: datetime
    row_id: int | None = None


def _batch_as_rows(items: Sequence[BatchItem], group_id: int) -> list[ChatMemoryRow]:
    """批次项转合成行（负 id），供负反馈检测/调优上下文/批次渲染复用。"""
    return [
        ChatMemoryRow(
            id=-(index + 1),
            group_id=group_id,
            user_id=item.sender_user_id,
            message_text=item.raw_text,
            created_at=item.created_at.isoformat(),
            is_ai_prompt=False,
            ai_reply=None,
        )
        for index, item in enumerate(items)
    ]


def batch_plusone_text(items: Sequence[BatchItem], rows: Sequence[ChatMemoryRow]) -> str | None:
    """+1（批次版）：触发文本在批次∪近期行中存在不同发送者的同文消息。"""
    trigger = items[-1].raw_text.strip()
    if not trigger:
        return None
    senders = {items[-1].sender_user_id}
    for item in items[:-1]:
        if item.raw_text.strip() == trigger:
            senders.add(item.sender_user_id)
    for row in rows:
        if row.message_text.strip() == trigger:
            senders.add(row.user_id)
    if any(uid != items[-1].sender_user_id for uid in senders):
        return trigger
    return None


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
    return any(any(w in t for w in negative_words) and mentions_persona(t, persona) for t in texts)


class TriggerCategory(enum.Enum):
    """触发分类（S10-HUMAN-P0）：收集器旁路判断与管道内采样决策共用。"""

    NICKNAMED = "nicknamed"
    QUOTED = "quoted"
    YOU_PLURAL = "you_plural"
    YOU_TO_GATE = "you_to_gate"
    ORDINARY = "ordinary"


def classify_trigger(
    raw_text: str, *, settings: BotSettings, quoted_self: bool = False
) -> TriggerCategory:
    """纯函数：点名 > 引用必回 > "你们" > 含"你" > 普通。热聊依赖进程状态，
    不在此判定（管道内处理）。"""
    persona = load_persona(settings)
    if mentions_persona(raw_text, persona):
        return TriggerCategory.NICKNAMED
    if quoted_self:
        return TriggerCategory.QUOTED
    if settings.auto_chat_you_plural_reply and "你们" in raw_text:
        return TriggerCategory.YOU_PLURAL
    if "你" in raw_text:
        return TriggerCategory.YOU_TO_GATE
    return TriggerCategory.ORDINARY


_ACTIVITY_WEIGHT_CAP = 4.0


def activity_weight(raw_text: str, *, settings: BotSettings) -> float:
    """群聊热度权重（S10-HUMAN-P2 H7）：点名对象/第二人称/疑问加权。

    含"你/你们" ×2、疑问性质（`_is_questionish`）×2，可叠加（"你…吗" ×4），
    封顶 `_ACTIVITY_WEIGHT_CAP`。纯函数：同文本同设置恒定，无状态读取。
    `settings` 入参为与 `classify_trigger` 等同族签名保持一致（当前不使用）。"""
    weight = 1.0
    if "你" in raw_text:
        weight += 2.0
    if _is_questionish(raw_text):
        weight += 2.0
    return min(weight, _ACTIVITY_WEIGHT_CAP)


# 上下文渲染的匿名别名（用户A/B/C…，防止原始 QQ 号进入模型 prompt，
# 实测泄露问题）已上收到 memory_prompt；保留 `_render_rows` 名字以兼容
# 既有调用点与测试。别名按每次调用重建，无跨请求状态。

_BATCH_STREAK_GAP_SECONDS = 3.0


def render_batch_rows(batch_rows: Sequence[ChatMemoryRow], alias: dict[int, str]) -> list[str]:
    """批次段落渲染：同人相邻（间隔 < 3s）合并为连发行，其余逐行。"""
    lines: list[str] = []
    i = 0
    while i < len(batch_rows):
        group = [batch_rows[i]]
        j = i + 1
        while j < len(batch_rows):
            prev_at = _row_created_at(batch_rows[j - 1])
            cur_at = _row_created_at(batch_rows[j])
            if batch_rows[j].user_id != batch_rows[i].user_id or prev_at is None or cur_at is None:
                break
            if (cur_at - prev_at).total_seconds() >= _BATCH_STREAK_GAP_SECONDS:
                break
            group.append(batch_rows[j])
            j += 1
        name = alias.get(batch_rows[i].user_id, "用户?")
        # 引用标记里的 u{id} 同样匿名化（防裸 QQ 号经批次段落进模型 prompt）
        if len(group) > 1:
            lines.append(
                f"{name}（连发{len(group)}条）："
                + " / ".join(substitute_reference_tokens(r.message_text, alias) for r in group)
            )
        else:
            lines.append(f"{name}：{substitute_reference_tokens(group[0].message_text, alias)}")
        i = j
    return lines


_BATCH_SECTION_HEADER = (
    "以下消息是刚刚连续发生的对话（同一个人的相邻消息为连发），主要回应最后一条："
)

# 学习参考（S10-HUMAN-P1-04）：表达句式仅供语气参考，黑话仅供理解。
# 内容仍须回应最新消息；禁止照抄原句，不确定含义就不要用黑话。
_EXPRESSION_SECTION_HEADER = "本群表达参考（仅供语气参考，禁止照抄原句，内容仍须回应最新消息）："
_JARGON_SECTION_HEADER = "本群黑话参考（理解用，不确定含义就不要用）："


def build_gate_user_prompt(
    rows: Sequence[ChatMemoryRow],
    *,
    batch_rows: Sequence[ChatMemoryRow] = (),
    jargon_refs: Sequence[str] = (),
) -> str:
    lines = ["最近群消息（最后一条是最新消息）："]
    lines.extend(_render_rows(rows))
    if batch_rows:
        lines.append(_BATCH_SECTION_HEADER)
        lines.extend(render_batch_rows(batch_rows, build_user_aliases([*rows, *batch_rows])))
    if jargon_refs:
        lines.append(_JARGON_SECTION_HEADER)
        lines.extend(f"- {text}" for text in jargon_refs)
    return "\n".join(lines)


def build_casual_user_prompt(
    rows: Sequence[ChatMemoryRow],
    recent_replies: Sequence[str] = (),
    *,
    batch_rows: Sequence[ChatMemoryRow] = (),
    expression_refs: Sequence[str] = (),
    jargon_refs: Sequence[str] = (),
) -> str:
    lines = ["最近群消息（最后一条是最新消息）："]
    lines.extend(_render_rows(rows))
    if batch_rows:
        lines.append(_BATCH_SECTION_HEADER)
        lines.extend(render_batch_rows(batch_rows, build_user_aliases([*rows, *batch_rows])))
    if expression_refs:
        lines.append(_EXPRESSION_SECTION_HEADER)
        lines.extend(f"- {text}" for text in expression_refs)
    if jargon_refs:
        lines.append(_JARGON_SECTION_HEADER)
        lines.extend(f"- {text}" for text in jargon_refs)
    if recent_replies:
        lines.append("你最近发过的消息（换着花样说，禁止重复这些句式和用词）：")
        lines.extend(f"- {text}" for text in recent_replies)
    lines.append(
        "请以群友身份先回应最新消息本身：打招呼就回应问候，提问就回应问题，"
        "可以顺势接梗；不要跑题到更早的话题。"
    )
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


# ---- 编排：run_auto_chat（S7-AUTO-06）----

_SHARED_STATE = AutoChatState()

# 回复多样性（S7-AUTO-P2-09）：每次回复随机注入一条风格指令，机械打散
# 小模型的模式坍缩（语气词开头+😂收尾那种模板腔）
_STYLE_DIRECTIVES: tuple[str, ...] = (
    "这条回复不要使用任何表情",
    "这条回复不要使用任何标点符号",
    "这条回复控制在 12 个字以内",
    "这条回复不要用任何语气词开头",
    "这条回复只发一个 3 到 8 个字的短语",
    "这条回复控制在 12 个字以内，并且不要任何标点",
    "这条回复就像随口嘟囔的一句碎碎念",
    "这条回复只回两个字到五个字",
)

# 每群最近自主发言（不含被 @ 问答），注入 prompt 让模型避开自己的旧句式
_RECENT_REPLIES: dict[int, deque[str]] = {}
_RECENT_REPLIES_MAX = 8

# 调优记录（S7-AUTO-P2-11）：自主回复的上文与回复内容落 JSONL 供部署者复盘
_TUNE_LOG_PATH = "data/auto_chat_tune.jsonl"


def _recent_replies(group_id: int) -> tuple[str, ...]:
    return tuple(_RECENT_REPLIES.get(group_id, ()))


def _remember_reply(group_id: int, text: str) -> None:
    if text:
        _RECENT_REPLIES.setdefault(group_id, deque(maxlen=_RECENT_REPLIES_MAX)).append(text)


def style_directive(rng: Callable[[], float]) -> str:
    return _STYLE_DIRECTIVES[int(rng() * len(_STYLE_DIRECTIVES)) % len(_STYLE_DIRECTIVES)]


def split_casual_reply(text: str, *, max_parts: int = 3) -> list[str]:
    """逗号长句 → 多条短消息（S7-AUTO-P2-10）：真人群聊连发几条短句，
    而不是挤一句逗号长句。生成端约束对经济型模型不可靠，因此确定性拆分；
    超出条数的尾段用逗号并回最后一条（用户接受逗号，反对的是长逗号链）。"""
    parts = [part.strip() for part in re.split(r"[，,、；;。]+", text.strip()) if part.strip()]
    if len(parts) <= max_parts:
        return parts
    merged = parts[: max_parts - 1]
    merged.append("，".join(parts[max_parts - 1 :]))
    return merged


def typing_intervals(
    parts: Sequence[str],
    *,
    base: float,
    per_char: float,
    max_seconds: float,
    rng: Callable[[], float],
) -> list[float]:
    """碎片间打字间隔（S10-HUMAN-P0 H2，Tulpa 协议公式）：
    min(base + per_char×上一碎片去空白字数, max) × 抖动(0.8~1.2)。"""
    intervals: list[float] = []
    for prev in parts[:-1]:
        visible = len(re.sub(r"\s+", "", prev))
        delay = min(base + per_char * visible, max_seconds) * (0.8 + 0.4 * rng())
        intervals.append(max(delay, 0.0))
    return intervals


_PUNCT_NORM_RE = re.compile(r"[\s，,、；;。.!！?？~～…\"'「」『』（）()·]+")


# 引用令牌 u{id}（S10-HUMAN-P0-06）也算伪影：万一漏进回复文本，重写兜底
_ARTIFACT_RE = re.compile(r"用户\d{4,}|u\d{5,}|(?<!\d)\d{5,11}(?!\d)")
_EMOJI_RE = "[🀀-🫿☀-➿⬀-⯿️]"


def _has_id_artifact(text: str) -> bool:
    """回复里出现"用户+数字"或裸长数字串 = 把 prompt 上下文格式当内容
    抄出来了（实测问题），整条放弃。"""
    return bool(_ARTIFACT_RE.search(text))


def _norm_text(text: str) -> str:
    return _PUNCT_NORM_RE.sub("", text).lower()


def _reply_issue(reply: str, trigger: str, recent: Sequence[str]) -> str | None:
    """自主回复质量闸（S7-AUTO-P2-12）：返回拒绝原因，None 表示通过。
    echo = 几乎复述触发句；repeat = 与自己最近发言高度雷同。"""
    norm_reply = _norm_text(reply)
    if not norm_reply:
        return "empty"
    norm_trigger = _norm_text(trigger)
    if len(norm_trigger) >= 6 and norm_trigger in norm_reply:
        return "echo"
    for prev in recent:
        norm_prev = _norm_text(prev)
        if norm_prev and SequenceMatcher(None, norm_reply, norm_prev).ratio() >= 0.8:
            return "repeat"
    return None


def _strip_excl_and_emoji_punct(text: str) -> str:
    """自主回复禁感叹号（拆分语义由句号承担）；表情与文字之间不夹标点。"""
    cleaned = text.replace("！", "。").replace("!", "。")
    cleaned = re.sub(r"[\s，,、；。：]*(" + _EMOJI_RE + r")", r"\1", cleaned)
    cleaned = re.sub(r"(" + _EMOJI_RE + r")\s*[，,、；。：]", r"\1", cleaned)
    return cleaned


def _is_questionish(text: str) -> bool:
    stripped = text.strip()
    if re.search(r"[?？]", stripped):
        return True
    return bool(stripped) and stripped[-1] in "吗呢吧嘛"


def plusone_triggered(rows: Sequence[ChatMemoryRow], raw_text: str, *, sender_user_id: int) -> bool:
    """≥2 人发过一模一样的消息（含当前发送者）→ 跟发一条一样的（+1 文化）。"""
    text = raw_text.strip()
    if not text:
        return False
    others = {
        row.user_id
        for row in rows
        if row.message_text.strip() == text and row.user_id != sender_user_id
    }
    return bool(others)


def _record_tune_entry(
    settings: BotSettings,
    *,
    path: str,
    trigger: str,
    context_rows: Sequence[ChatMemoryRow],
    reply_parts: Sequence[str],
    group_id: int,
    typo_applied: bool = False,
) -> None:
    """调优记录：上文与回复内容落 JSONL（仅本地 data/，不进日志/指标），
    供部署者复盘哪些回复好、哪些差。失败绝不影响主流程。"""
    if not settings.auto_chat_tune_log_enabled:
        return
    try:
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "group_id": group_id,
            "path": path,
            "trigger": trigger,
            "context": [
                {
                    "user": row.user_id,
                    "text": row.message_text,
                    "bot_reply": row.ai_reply or None,
                }
                for row in context_rows
            ],
            "reply": list(reply_parts),
            "typo_applied": typo_applied,
        }
        log_path = Path(_TUNE_LOG_PATH)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


COLD_FALLBACK_MESSAGES = (
    "怎么没人理我…鱼都晒干了",
    "就当我说的是空气吧",
    "冷场了？行吧 我躺回去了",
)

_COLD_TASKS: set[asyncio.Task] = set()


def shared_state() -> AutoChatState:
    """公开的模块级状态入口（插件被 @ 路径使用）。"""
    return _SHARED_STATE


def build_cold_user_prompt(
    rows: Sequence[ChatMemoryRow], bot_reply_text: str, recent_replies: Sequence[str] = ()
) -> str:
    lines = ["最近群消息（最后一条是最新消息）："]
    lines.extend(_render_rows(rows))
    if recent_replies:
        lines.append("你最近发过的消息（换着花样说，禁止重复这些句式和用词）：")
        lines.extend(f"- {text}" for text in recent_replies)
    lines.append(f"你刚才说了「{bot_reply_text}」，但没有人回应。")
    lines.append("用一句话自嘲或吐槽冷场，可以呼应你刚才说的内容。")
    return "\n".join(lines)


def schedule_cold_check(
    *,
    group_id: int,
    bot_reply_text: str,
    bot_reply_time: datetime,
    settings: BotSettings,
    memory_store: Any,
    send: Callable[[Sequence[str]], Awaitable[None]],
    quota_check: Callable[[], Awaitable[bool]] | None = None,
    client: Any | None = None,
    state: AutoChatState | None = None,
    rng: Callable[[], float] = random.random,
    _request_completion: Any = None,
    _sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """发言后调度一次冷场检查（spec 第五节）。进程内 task，重启丢失可接受。
    仅当上一条机器人发言具有提问性质时才视为冷场——陈述句没人接话是常态。"""
    if not _is_questionish(bot_reply_text):
        return

    async def _check() -> None:
        tracer = get_tracer()
        live_state = state or _SHARED_STATE

        def _cold(result: str) -> None:
            metrics.AUTO_CHAT.labels("cold", result).inc()

        try:
            await _sleep(settings.auto_chat_cold_followup_seconds)
            if live_state.in_backoff(group_id):
                _cold("skip")
                return
            if live_state.cold_limit_reached(group_id, settings=settings):
                _cold("skip")
                return
            try:
                rows = await memory_store.recent_group_messages(
                    group_id=group_id, limit=settings.auto_chat_context_messages
                )
            except Exception:
                _cold("error")
                return
            latest = rows[-1] if rows else None
            if latest is not None:
                latest_at = _row_created_at(latest)
                if latest_at is not None and latest_at >= bot_reply_time:
                    _cold("skip")
                    return
            persona = load_persona(settings)
            from qq_bot.services.ai_client import (
                request_completion as _real_completion,
            )

            complete = _request_completion or _real_completion
            span = tracer.start_span("auto.generate", trace_id=current_request_id())
            try:
                reply = (
                    await complete(
                        system_prompt=casual_system_prompt(persona),
                        user_prompt=(
                            build_cold_user_prompt(
                                rows,
                                bot_reply_text,
                                recent_replies=_recent_replies(group_id),
                            )
                            + f"\n风格要求：{style_directive(random.random)}"
                        ),
                        settings=settings,
                        client=client,
                        model=settings.auto_chat_reply_model or settings.ai_model,
                        max_tokens=100,
                        temperature=settings.auto_chat_reply_temperature,
                    )
                ).strip()
            except Exception as exc:
                tracer.end_span(
                    span, status="error", category=classify_exception(exc).category.value
                )
                reply = ""
            else:
                tracer.end_span(span)
            if not reply:
                reply = random.choice(COLD_FALLBACK_MESSAGES)
            reply = _strip_excl_and_emoji_punct(reply).rstrip("。 ")
            if _has_id_artifact(reply):
                reply = random.choice(COLD_FALLBACK_MESSAGES)
            if any(word in reply for word in settings.auto_chat_sensitive_word_list):
                _cold("filtered")
                return
            if quota_check is not None and not await quota_check():
                _cold("quota_denied")
                return
            live_state.note_reply(
                group_id, settings=settings
            )  # 冷场补话也是机器人发言：刷新热聊窗口（spec 第四节）；不级联约束不受影响
            live_state.note_cold_reply(group_id, settings=settings)
            _remember_reply(group_id, reply)
            _record_tune_entry(
                settings,
                path="cold",
                trigger=bot_reply_text,
                context_rows=rows,
                reply_parts=[reply],
                group_id=group_id,
            )
            await send([reply])
            _cold("ok")
        except Exception:
            _cold("error")

    task = asyncio.ensure_future(_check())
    _COLD_TASKS.add(task)
    task.add_done_callback(_COLD_TASKS.discard)


async def _jargon_refs(
    memory_store: Any, group_id: int, *, settings: BotSettings
) -> tuple[str, ...]:
    """已确认黑话的「术语：释义」引用串（S10-HUMAN-P1-04）。关闭或任何失败
    （含存储未实现该方法）都返回空——注入是护栏增强，绝不拖累主流程。"""
    if not settings.expression_learn_enabled:
        return ()
    try:
        rows = await memory_store.list_jargon(
            group_id=group_id, status="confirmed", limit=settings.jargon_inject_max
        )
        return tuple(f"{r.term}：{r.definition}" for r in rows)
    except Exception:
        return ()


async def _expression_sample(
    memory_store: Any, group_id: int, *, settings: BotSettings
) -> tuple[Any, ...]:
    """学习到的活跃表达行样本（S10-HUMAN-P1-04）。关闭或任何失败（含存储
    未实现该方法）都返回空。调用方按 ``r.template`` 渲染参考、``r.id`` 回写。

    观测（spec 观测节）：``inject/ok``（样本非空 → 本轮生成有参考可用）、
    ``inject/skip_insufficient``（活跃池未过阈值 → 空样本）、
    ``inject/skip_disabled``（功能关闭）。三者都记在 helper 里而不是 run_auto_chat
    顶层：每次进入生成路径（含点名/引用必回/普通采样）且功能关闭时发射一次，
    每次生成尝试恰好一次（样本整轮只采一次，重试不重复采），指标是「每次
    生成尝试的注入结局」这一诚实信号；决策门、冷却/backoff 等前置退出在
    到达 helper 之前就返回，不会触发。
    存储异常（fail-soft）不贴任何 inject 标签——那不是规格里的 skip 理由。"""
    if not settings.expression_learn_enabled:
        metrics.EXPRESSION_TOTAL.labels("inject", "skip_disabled").inc()
        return ()
    try:
        sample = tuple(
            await memory_store.sample_expressions(
                group_id=group_id,
                limit=settings.expression_inject_max,
                threshold=settings.expression_min_active,
            )
        )
    except Exception:
        return ()
    metrics.EXPRESSION_TOTAL.labels("inject", "ok" if sample else "skip_insufficient").inc()
    return sample


async def run_auto_chat(
    *,
    event: Any,
    raw_text: str,
    settings: BotSettings,
    memory_store: Any,
    send: Callable[[Sequence[str]], Awaitable[None]],
    quota_check: Callable[[], Awaitable[bool]] | None = None,
    client: Any | None = None,
    state: AutoChatState | None = None,
    rng: Callable[[], float] = random.random,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    _cold_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    _request_completion: Any = None,
    batch: Sequence[BatchItem] | None = None,
    quoted_self: bool = False,
) -> None:
    """Full pipeline for one non-addressed group message (spec 一/二/三/四节).

    ``batch``（S10-HUMAN-P0）传入收集器预筛后的批次项时走批次路径：跳过
    static_prefilter（收集器已逐条预筛），批次文本作为最后一条触发上下文；
    ``batch=None`` 时为单消息路径，行为与历史版本逐行等价。
    ``quoted_self``（spec §5.3）标记引用机器人的消息：分类为 QUOTED，
    跳过采样与决策门（必回旁路）。

    ``_request_completion`` is a test seam defaulting to the real
    ``request_completion``; production callers never pass it. Send and quota
    are injected so this service stays matcher- and runtime-free."""
    from qq_bot.services.ai_client import request_completion as _real_completion

    complete = _request_completion or _real_completion
    state = state or _SHARED_STATE
    persona = load_persona(settings)
    group_id = event.group_id

    def _metric(stage: str, result: str) -> None:
        metrics.AUTO_CHAT.labels(stage, result).inc()

    is_batch = batch is not None
    items = (
        list(batch)
        if is_batch
        else [
            BatchItem(raw_text=raw_text, sender_user_id=event.user_id, created_at=datetime.now(UTC))
        ]
    )

    if is_batch:
        items = [
            it for it in items if it.sender_user_id not in settings.auto_chat_ignored_user_id_list
        ]
        if not items:
            _metric("prefilter", "ignored")
            return
    elif event.user_id in settings.auto_chat_ignored_user_id_list:
        _metric("prefilter", "ignored")
        return
    # 触发消息 = 过滤后批次最后一条（忽略名单可能移除末条，S10-HUMAN-P0-03 回归）；
    # 单消息路径恒等于 raw_text，与历史版本逐行等价。
    trigger_text = items[-1].raw_text if is_batch else raw_text
    static = "pass"
    if not is_batch:
        static = static_prefilter(raw_text, settings=settings)
        if static in ("command", "sensitive"):
            _metric("prefilter", static)
            return

    try:
        rows = await memory_store.recent_group_messages(
            group_id=group_id, limit=settings.auto_chat_context_messages
        )
    except Exception as exc:
        _metric("prefilter", "error")
        record_error("auto_chat", classify_exception(exc).category.value)
        return
    max_age_minutes = settings.auto_chat_context_max_age_minutes
    if max_age_minutes > 0:
        cutoff = datetime.now(UTC) - timedelta(minutes=max_age_minutes)
        rows = [
            row for row in rows if (parsed := _row_created_at(row)) is not None and parsed >= cutoff
        ]
    if is_batch:
        rows = [
            row
            for row in rows
            if row.id not in {it.row_id for it in items if it.row_id is not None}
        ]
    if not rows and not is_batch:
        _metric("prefilter", "no_context")
        return
    if is_batch:
        plusone_text = batch_plusone_text(items, rows)
        plusone = plusone_text is not None and plusone_text not in _recent_replies(group_id)
    else:
        plusone = plusone_triggered(rows, raw_text, sender_user_id=event.user_id) and (
            raw_text not in _recent_replies(group_id)
        )
        plusone_text = raw_text
    if static == "too_short" and not plusone:
        _metric("prefilter", "too_short")
        return

    bot_recently = state.recently_spoke(group_id, settings.auto_chat_cooldown_seconds)
    detection_rows = [*rows, *_batch_as_rows(items, group_id)] if is_batch else rows
    if detect_negative_feedback(
        detection_rows, persona=persona, settings=settings, bot_recently_spoke=bot_recently
    ):
        state.set_backoff(group_id, settings.auto_chat_negative_backoff_seconds)
        _metric("prefilter", "backoff")
        return
    if state.in_backoff(group_id):
        _metric("prefilter", "backoff")
        return
    hot = state.hot_active(group_id, settings=settings)
    # 兴趣度只在本轮算一次，供自动进热聊/冷却乘数/采样乘数/gauge 复用。
    interest = 0.0
    if settings.auto_chat_interest_enabled:
        interest = state.interest_of(group_id, settings=settings)
        # 兴趣度自动进热聊（S10-HUMAN-P2 H7）：高热群且当前不在热聊窗口时，按热聊
        # 路径处理本轮（跳过采样与门）并刷新窗口。note_reply 的 "hot_exit_limit"
        # 返回值此处有意忽略——新 streak=1 绝不可能触及 50 条兜底，streak 兜底由
        # 后续正常发言路径自然处理。
        if not hot and interest >= settings.auto_chat_interest_hot_threshold:
            state.note_reply(group_id, settings=settings)  # 刷新热聊窗口即进入
            hot = True
            _metric("prefilter", "interest_hot")
        # gauge 每轮更新一次（含 NICKNAMED/QUOTED/热聊等旁路路径），避免只走
        # 旁路的群长期保留过期值；关闭时与 HEAD 一致不写 gauge。
        metrics.AUTO_CHAT_INTEREST.labels(hash_id(group_id, kind="group")).set(interest)
    # 冷却乘数（H7）：非热聊期按热度缩放冷却，钳不低于热聊冷却；热聊期沿用
    # 热聊冷却（豁免语义不变）。
    cooldown_override: float | None = None
    if settings.auto_chat_interest_enabled and not hot:
        scaled = settings.auto_chat_cooldown_seconds * (1.2 - 0.7 * interest)
        cooldown_override = max(settings.auto_chat_hot_cooldown_seconds, scaled)
    limit = state.limit_reason(
        group_id,
        settings=settings,
        cooldown_seconds=(settings.auto_chat_hot_cooldown_seconds if hot else cooldown_override),
        skip_hourly=hot,
    )
    if limit == "cooldown":
        _metric("prefilter", "cooldown")
        return
    if limit:
        _metric("prefilter", "limit")
        return

    category = classify_trigger(trigger_text, settings=settings, quoted_self=quoted_self)
    tracer = get_tracer()
    reply = None  # plusone 分支直接采用原文；其余路径由生成产出
    # 学习参考（S10-HUMAN-P1-04）：黑话在决策门之前取一次，门与生成器共用；
    # 表达样本在生成时才取（生成路径专属），回写 id 也来自那里。
    jargon_refs = await _jargon_refs(memory_store, group_id, settings=settings)
    expression_ids: list[int] = []
    if category is TriggerCategory.NICKNAMED:
        _metric("prefilter", "nicknamed")
    elif category is TriggerCategory.QUOTED:
        _metric("prefilter", "quoted")
    elif category is TriggerCategory.YOU_PLURAL:
        _metric("prefilter", "you_plural")
    elif plusone:
        reply = plusone_text
        _metric("prefilter", "plusone")
    elif hot:
        _metric("prefilter", "hot_reply")
    else:
        # 采样乘数（S10-HUMAN-P2 H7）：热度越高有效采样率越高；开关关闭时与
        # 现状逐行等价（乘数 1、标签仍为 sampled_out）。
        effective_rate = settings.auto_chat_sample_rate
        if settings.auto_chat_interest_enabled:
            effective_rate = min(1.0, effective_rate * (0.5 + 2.5 * interest))
        if category is not TriggerCategory.YOU_TO_GATE and rng() >= effective_rate:
            _metric(
                "prefilter",
                "interest_low" if settings.auto_chat_interest_enabled else "sampled_out",
            )
            return
        if category is TriggerCategory.YOU_TO_GATE:
            _metric("prefilter", "you_to_gate")
        else:
            _metric("prefilter", "passed")
        gate_span = tracer.start_span("auto.gate", trace_id=current_request_id())
        try:
            content = await complete(
                system_prompt=GATE_SYSTEM_PROMPT,
                user_prompt=build_gate_user_prompt(
                    rows,
                    batch_rows=_batch_as_rows(items, group_id) if is_batch else (),
                    jargon_refs=jargon_refs,
                ),
                settings=settings,
                client=client,
                model=settings.router_model,
                max_tokens=120,
                json_mode=True,
            )
        except Exception as exc:
            tracer.end_span(
                gate_span, status="error", category=classify_exception(exc).category.value
            )
            record_error("auto_chat", classify_exception(exc).category.value)
            _metric("gate", "error")
            return
        decision = parse_gate_output(content)
        if decision is None:
            tracer.end_span(gate_span)
            _metric("gate", "error")
            return
        if not decision.should_reply:
            tracer.end_span(gate_span)
            _metric("gate", "skip")
            return
        if decision.confidence < settings.auto_chat_confidence_threshold:
            tracer.end_span(gate_span)
            _metric("gate", "low_confidence")
            return
        tracer.end_span(gate_span)
        _metric("gate", "reply")

    if quota_check is not None and not await quota_check():
        _metric("gate", "quota_denied")
        return
    async with state.lock(group_id):
        if state.acquire(
            group_id,
            settings=settings,
            cooldown_seconds=(
                settings.auto_chat_hot_cooldown_seconds if hot else cooldown_override
            ),
            skip_hourly=hot,
        ):
            _metric("prefilter", "limit")
            return
        if reply is None:
            generate_span = tracer.start_span("auto.generate", trace_id=current_request_id())
            # 表达样本每次 run 只取一次：重试 prompt 复用同一批参考（重试不是
            # 新的一轮群聊），id 也一并留待发送成功后回写。
            expression_rows = await _expression_sample(memory_store, group_id, settings=settings)
            expression_refs = tuple(r.template for r in expression_rows)
            expression_ids = [r.id for r in expression_rows]

            async def _generate() -> str:
                return await complete(
                    system_prompt=casual_system_prompt(persona),
                    user_prompt=(
                        build_casual_user_prompt(
                            rows,
                            recent_replies=_recent_replies(group_id),
                            batch_rows=_batch_as_rows(items, group_id) if is_batch else (),
                            expression_refs=expression_refs,
                            jargon_refs=jargon_refs,
                        )
                        + f"\n风格要求：{style_directive(rng)}"
                    ),
                    settings=settings,
                    client=client,
                    model=settings.auto_chat_reply_model or settings.ai_model,
                    max_tokens=100,
                    temperature=settings.auto_chat_reply_temperature,
                )

            gen = asyncio.ensure_future(_generate())
            try:
                delay_min = settings.auto_chat_delay_min_seconds
                delay_max = settings.auto_chat_delay_max_seconds
                await sleeper(delay_min + (delay_max - delay_min) * rng())
                reply = (await gen).strip()
            except Exception as exc:
                gen.cancel()
                tracer.end_span(
                    generate_span,
                    status="error",
                    category=classify_exception(exc).category.value,
                )
                record_error("auto_chat", classify_exception(exc).category.value)
                _metric("generate", "error")
                return  # 冷却已被占用：有意的保守行为
            tracer.end_span(generate_span)

    if not reply:
        _metric("generate", "error")
        return
    if not plusone:
        _metric("generate", "ok")
        reply = _strip_excl_and_emoji_punct(reply)
        if _has_id_artifact(reply):
            _metric("generate", "artifact")
            return
        issue = _reply_issue(reply, trigger_text, _recent_replies(group_id))
        if issue is not None:
            retry_model = settings.auto_chat_reply_model or settings.ai_model
            retry_prompt = (
                build_casual_user_prompt(
                    rows,
                    recent_replies=_recent_replies(group_id),
                    batch_rows=_batch_as_rows(items, group_id) if is_batch else (),
                    expression_refs=expression_refs,
                    jargon_refs=jargon_refs,
                )
                + f"\n风格要求：{style_directive(rng)}"
                + "\n重写要求：你刚才的回复被拒绝（"
                + ("几乎复述了别人的原话" if issue == "echo" else "和自己最近的话重复")
                + "）。必须换一个完全不同的角度，用不同的词语，简短回应最新消息。"
            )
            try:
                reply = (
                    await complete(
                        system_prompt=casual_system_prompt(persona),
                        user_prompt=retry_prompt,
                        settings=settings,
                        client=client,
                        model=retry_model,
                        max_tokens=100,
                        temperature=settings.auto_chat_reply_temperature,
                    )
                ).strip()
            except Exception:
                reply = ""
            issue = (
                _reply_issue(reply, trigger_text, _recent_replies(group_id)) if reply else "empty"
            )
            if issue is not None:
                _metric("generate", "repeat")
                return
    if any(word in reply for word in settings.auto_chat_sensitive_word_list):
        _metric("generate", "filtered")
        return
    # 错别字注入（S10-HUMAN-P0 H3）：敏感词过滤先于注入；+1 跟发保持原文不动。
    # 注入后再过一次敏感词检查：换位可拼出内置敏感词（"支枪"→"枪支"）。
    if not plusone:
        reply, typo_applied = maybe_apply_typo(reply, rate=settings.auto_chat_typo_rate, rng=rng)
        if typo_applied and any(word in reply for word in settings.auto_chat_sensitive_word_list):
            _metric("generate", "filtered")
            return
    else:
        typo_applied = False
    if quota_check is not None and not await quota_check():
        _metric("send", "quota_denied")
        return
    if state.in_backoff(group_id):
        _metric("send", "backoff")
        return
    exit_flag = state.note_reply(group_id, settings=settings)
    if exit_flag:
        _metric("prefilter", "hot_exit_limit")
    _remember_reply(group_id, reply)
    schedule_cold_check(
        group_id=group_id,
        bot_reply_text=reply,
        bot_reply_time=datetime.now(UTC),
        settings=settings,
        memory_store=memory_store,
        send=send,
        quota_check=quota_check,
        client=client,
        state=state,
        rng=rng,
        _request_completion=_request_completion,
        _sleep=_cold_sleep,
    )
    parts = [reply] if plusone else split_casual_reply(reply)
    path_label = (
        "nicknamed"
        if category is TriggerCategory.NICKNAMED
        else "quoted"
        if category is TriggerCategory.QUOTED
        else "you_plural"
        if category is TriggerCategory.YOU_PLURAL
        else "plusone"
        if plusone
        else "hot_reply"
        if hot
        else "gate_reply"
    )
    _record_tune_entry(
        settings,
        path=path_label,
        trigger=trigger_text,
        context_rows=[*rows, *_batch_as_rows(items, group_id)] if is_batch else rows,
        reply_parts=parts,
        group_id=group_id,
        typo_applied=typo_applied,
    )
    await send(parts)
    # 表达使用回写（S10-HUMAN-P1-04）：仅生成路径采样到 id 且真的发出去了才记；
    # 失败静默——计数统计不能拖累主流程。
    if expression_ids:
        try:
            await memory_store.touch_expressions(expression_ids)
        except Exception:
            pass
    _metric("send", "ok")  # 已移交发送器；发送失败由 onebot_send 的 SEND_RESULTS 计数
