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
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

from qq_bot.config import BotSettings
from qq_bot.observability import metrics, record_error
from qq_bot.observability.logging import current_request_id, get_logger, hash_id
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
from qq_bot.services.sticker_library import Sticker, StickerLibrary

logger = get_logger("qq_bot.auto_chat")


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
        # 贴表情反应（S10-HUMAN-P2 H9）：独立冷却时刻 + 每日计数（进程内存态）
        self._last_reaction: dict[int, float] = {}
        self._reaction_daily: dict[tuple[int, str], int] = {}
        # 表情包（S10-HUMAN-P2 H10）：与文本/react 各自独立的冷却与每日计数
        self._last_sticker: dict[int, float] = {}
        self._sticker_daily: dict[tuple[int, str], int] = {}
        # 联网搜索（S11-AUTO-SEARCH）：每群每日独立计数（判别门 + Tavily 有真实成本）
        self._search_daily: dict[tuple[int, str], int] = {}

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

    # ---- 贴表情反应限额（S10-HUMAN-P2 H9）----

    def reaction_limit_reason(
        self, group_id: int, *, settings: BotSettings, cooldown_seconds: float | None = None
    ) -> str:
        """`""` 在限额内，否则 `cooldown`|`daily_limit`。与文本回复完全独立计数
        （react 不占 _last_sent/_hourly/_daily，反之亦然）。"""
        cooldown = (
            cooldown_seconds
            if cooldown_seconds is not None
            else settings.auto_chat_reaction_cooldown_seconds
        )
        last = self._last_reaction.get(group_id)
        if last is not None and self._clock() - last < cooldown:
            return "cooldown"
        day_key = self._bucket_clock().strftime("%Y-%m-%d")
        if (
            self._reaction_daily.get((group_id, day_key), 0)
            >= settings.auto_chat_reaction_daily_limit
        ):
            return "daily_limit"
        return ""

    def note_reaction(self, group_id: int) -> None:
        """登记一次成功贴表情：刷新 react 独立冷却并累计当日计数。"""
        self._last_reaction[group_id] = self._clock()
        day_key = self._bucket_clock().strftime("%Y-%m-%d")
        self._reaction_daily[(group_id, day_key)] = (
            self._reaction_daily.get((group_id, day_key), 0) + 1
        )

    # ---- 表情包限额（S10-HUMAN-P2 H10）----

    def sticker_limit_reason(self, group_id: int, *, settings: BotSettings) -> str:
        """`""` 在限额内，否则 `cooldown`|`daily_limit`。与文本、react 三者
        各自独立计数：发图不占文本冷却/小时/日额度，也不占 react 额度。"""
        last = self._last_sticker.get(group_id)
        if last is not None and self._clock() - last < settings.auto_chat_sticker_cooldown_seconds:
            return "cooldown"
        day_key = self._bucket_clock().strftime("%Y-%m-%d")
        if (
            self._sticker_daily.get((group_id, day_key), 0)
            >= settings.auto_chat_sticker_daily_limit
        ):
            return "daily_limit"
        return ""

    def note_sticker(self, group_id: int) -> None:
        """登记一次成功发表情包：刷新独立冷却并累计当日计数。"""
        self._last_sticker[group_id] = self._clock()
        day_key = self._bucket_clock().strftime("%Y-%m-%d")
        self._sticker_daily[(group_id, day_key)] = (
            self._sticker_daily.get((group_id, day_key), 0) + 1
        )

    # ---- 联网搜索限额（S11-AUTO-SEARCH）----

    def search_limit_reason(self, group_id: int, *, settings: BotSettings) -> str:
        """`""` 在限额内，否则 `daily_limit`。搜索独立于文本/表情/react 额度：
        每次搜索 = 一次判别门调用 + 一次 Tavily 调用，有真实成本。"""
        if settings.auto_chat_search_daily_limit <= 0:
            return ""
        day_key = self._bucket_clock().strftime("%Y-%m-%d")
        if self._search_daily.get((group_id, day_key), 0) >= settings.auto_chat_search_daily_limit:
            return "daily_limit"
        return ""

    def note_search(self, group_id: int) -> None:
        """登记一次实际执行的搜索（判别门说不需要不计数）。"""
        day_key = self._bucket_clock().strftime("%Y-%m-%d")
        self._search_daily[(group_id, day_key)] = self._search_daily.get((group_id, day_key), 0) + 1

    # ---- 群兴趣度（S10-HUMAN-P2 H7）----

    def note_activity(
        self,
        group_id: int,
        *,
        weight: float = 1.0,
        boost: float | None = None,
        half_life: float | None = None,
    ) -> None:
        """记一条群聊热度。先按半衰期折叠存量，再加 `boost × weight`，钳 [0,1]。

        `boost` 省略时用模块常量 `_INTEREST_BOOST_FIXED`；`half_life` 省略时
        不做存量折叠（默认 None，直接操作 state 的单测保持累加语义），调用方
        （ai_chat handler）显式传 `settings.auto_chat_interest_half_life` 与
        `settings.auto_chat_interest_boost` 使配置生效，state 本身不依赖
        BotSettings。负 weight 被夹为 0（绝不降温）。不折叠会导致活跃消息流中
        衰减永不生效（每次 note 都把 `_interest_at` 重置为现在），存量按纯
        累加器高估——因此生产调用方必须传 half_life。"""
        current = self.interest_raw(group_id, half_life=half_life)
        applied = _INTEREST_BOOST_FIXED if boost is None else boost
        self._interest[group_id] = max(0.0, min(1.0, current + applied * max(weight, 0.0)))
        self._interest_at[group_id] = self._clock()

    def interest_of(self, group_id: int, *, settings: BotSettings) -> float:
        return self.interest_raw(group_id, settings=settings)

    def interest_raw(
        self,
        group_id: int,
        *,
        settings: BotSettings | None = None,
        half_life: float | None = None,
    ) -> float:
        """惰性半衰衰减后的兴趣度。读路径会把折算值写回 `_interest`（有意为之：
        保持字典只存当前值，安静群自动清键），因此这是幂等但非纯读的操作。

        `half_life` 显式传入时优先生效；否则回落 `settings.auto_chat_interest_half_life`。
        两者都缺省时（默认 None 的 note 路径旧签名）不做时间衰减——衰减数学
        不随 `auto_chat_interest_enabled` 开关变化：开关只门控管道消费（采样
        乘数/冷却乘数/高热进热聊/gauge），状态本身始终如实记账。"""
        current = self._interest.get(group_id, 0.0)
        last = self._interest_at.get(group_id)
        if current <= 0.0 or last is None:
            return current
        if half_life is None:
            if settings is None:
                return current
            half_life = settings.auto_chat_interest_half_life
        elapsed = self._clock() - last
        if elapsed <= 0:
            return current
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
    "反过来，消息明显在喊别人或等别人回应（叫某个群友、叫群里其他机器人）时，"
    "那不是说给机器人的，不要当作 addressed；话题若开放、机器人接得上有实质可说，"
    "可按 banter 参与，否则 none。"
    "不要输出任何其他字段。"
    "“连发N条”表示同一人的连续短消息，视为一段完整发言。"
)


def gate_system_prompt(persona_name: str = "") -> str:
    """门系统提示词 = 静态规则 + 机器人名字提示（2026-10-25 复盘）：门此前
    不知道机器人叫什么，把“喊大肥鱼（群里另一个机器人）”误分类成叫自己。
    名字提示只修正 addressed 归类；叫别人的消息仍可按 banter 参与——部署者
    裁决（2026-10-25）：第三方对话可以是话题，禁的是不接上文的回复。"""
    name = persona_name.strip()
    if not name:
        return GATE_SYSTEM_PROMPT
    return GATE_SYSTEM_PROMPT + f"机器人的名字叫“{name}”，喊别的名字都不是在叫机器人。"


# react 指令（S10-HUMAN-P2 H9，Final Fix）：不进系统提示词——通道关闭/不可用
# 的部署不为永不出现的表情列表付 token；仅当本轮 reaction_available 时与
# 「可用表情 ID」列表合并成一个条件块拼进 user prompt（同进同出）。模型未被
# 告知 react 时不会自发输出 action=react，解析层对未知 action 也回落 reply
# （fail-closed 双保险）。
_GATE_REACT_INSTRUCTION = (
    "当回应只是轻量情绪反应（赞同、好笑、无语、安慰等）且不值得打字时，"
    '可输出 "action": "react" 并在 "emoji_id" 给出下方可用表情 ID 之一；'
    '其他情况一律 "action": "reply"。'
)

# 表情包指令（S10-HUMAN-P2 H10）：与 react 同一"同进同出"原则——只在库非空、
# 开关开、通道可用时与候选列表一起进 user prompt；关闭时模型不知道有这个动作，
# 解析层对未知 action 回落 reply。
_GATE_STICKER_INSTRUCTION = (
    "当回应更适合用一张图（大笑、无语、问号、安慰等）且打字反而多余时，"
    '可输出 "action": "sticker" 并在 "sticker_id" 给出下方候选 ID 之一；'
    '其他情况一律 "action": "reply"。'
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
    # S10-HUMAN-P2 H9：贴表情反应。action=react 时本轮只贴表情不生成文本；
    # emoji_id 必须是调用方白名单内的字符串（解析层只做类型归一）。
    # H10：action=sticker 时本轮只发表情包；sticker_id 必须是库中启用条目
    # （同样只在这里做类型归一，白名单校验在服务层）。
    action: str = "reply"
    emoji_id: str = ""
    sticker_id: str = ""


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

# 人设示例回复（2026-10-22 人设方案 B）：与表达参考段同级的语气参考。
_PERSONA_EXAMPLES_SECTION_HEADER = "你平时的说话感觉（学这个感觉，禁止照抄原句）："

_SEARCH_SECTION_HEADER = (
    "联网搜到的资料（最新消息里的具体事实以下面资料为准，"
    "可以随口提在哪看到的；没用到的资料不用提，别用播报腔）："
)

_PERSON_OBSERVE_SECTION_HEADER = "你对当前发言者的了解（可能过时，仅供参考，接个人梗时可用）："


def build_gate_user_prompt(
    rows: Sequence[ChatMemoryRow],
    *,
    batch_rows: Sequence[ChatMemoryRow] = (),
    jargon_refs: Sequence[str] = (),
    nicknames: Mapping[int, str] | None = None,
) -> str:
    lines = ["最近群消息（最后一条是最新消息）："]
    lines.extend(_render_rows(rows, nicknames=nicknames))
    if batch_rows:
        lines.append(_BATCH_SECTION_HEADER)
        lines.extend(
            render_batch_rows(
                batch_rows, build_user_aliases([*rows, *batch_rows], nicknames=nicknames)
            )
        )
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
    person_observe_refs: Sequence[str] = (),
    persona_examples: Sequence[str] = (),
    nicknames: Mapping[int, str] | None = None,
    search_refs: str = "",
) -> str:
    lines = ["最近群消息（最后一条是最新消息）："]
    lines.extend(_render_rows(rows, nicknames=nicknames))
    if batch_rows:
        lines.append(_BATCH_SECTION_HEADER)
        lines.extend(
            render_batch_rows(
                batch_rows, build_user_aliases([*rows, *batch_rows], nicknames=nicknames)
            )
        )
    if search_refs:
        lines.append(_SEARCH_SECTION_HEADER)
        lines.append(search_refs)
    if expression_refs:
        lines.append(_EXPRESSION_SECTION_HEADER)
        lines.extend(f"- {text}" for text in expression_refs)
    if jargon_refs:
        lines.append(_JARGON_SECTION_HEADER)
        lines.extend(f"- {text}" for text in jargon_refs)
    if persona_examples:
        lines.append(_PERSONA_EXAMPLES_SECTION_HEADER)
        lines.extend(f"- {text}" for text in persona_examples)
    if recent_replies:
        lines.append("你最近发过的消息（换着花样说，禁止重复这些句式和用词）：")
        lines.extend(f"- {text}" for text in recent_replies)
    if person_observe_refs:
        lines.append(_PERSON_OBSERVE_SECTION_HEADER)
        lines.extend(f"- {text}" for text in person_observe_refs)
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
    # H9：action 仅接受 reply/react（未知值回落 reply，fail-closed 不变）；
    # emoji_id 只接受字符串（模型给数字等一律视为不可用）。
    # H10：action 再接受 sticker；sticker_id 同样只接受字符串，白名单在服务层。
    action = payload.get("action", "reply")
    if action not in ("reply", "react", "sticker"):
        action = "reply"
    emoji_raw = payload.get("emoji_id", "")
    emoji_id = emoji_raw.strip() if isinstance(emoji_raw, str) else ""
    sticker_raw = payload.get("sticker_id", "")
    sticker_id = sticker_raw.strip() if isinstance(sticker_raw, str) else ""
    return GateDecision(
        should_reply=should_reply,
        reason=reason,
        confidence=confidence,
        action=action,
        emoji_id=emoji_id,
        sticker_id=sticker_id,
    )


# ---- 编排：run_auto_chat（S7-AUTO-06）----

_SHARED_STATE = AutoChatState()

# 回复多样性（S7-AUTO-P2-09）：每次回复随机注入一条风格指令，机械打散
# 小模型的模式坍缩（语气词开头+😂收尾那种模板腔）。修剪说明：原 8 条里
# 5 条强制极短，统计上把大部分回复压成统一的超短碎片——那本身就是机器人
# 特征。现为短/中/长混合，并补两条节奏类（疑问收尾/感叹收尾）。
_STYLE_DIRECTIVES: tuple[str, ...] = (
    "这条回复不要使用任何表情",
    "这条回复不要用任何语气词开头",
    "这条回复控制在 12 个字以内",
    "这条回复只回两个字到五个字",
    "这条回复就像随口嘟囔的一句碎碎念",
    "这条回复可以说长一点，两三个短句也行",
    "这条回复用一个疑问句收尾",
    "这条回复用一个感叹号收尾",
)

# 每群最近自主发言（不含被 @ 问答），注入 prompt 让模型避开自己的旧句式
_RECENT_REPLIES: dict[int, deque[str]] = {}
_RECENT_REPLIES_MAX = 8

# 贴表情反应降级（S10-HUMAN-P2 H9，spec Q2 方案 A）：同一 set_msg_emoji_like
# 动作在同一进程内连续失败 ≥3 次 → 进程级禁用通道（协议不支持的 NapCat/端）。
# 偶发单次失败只累计不触发；任何一次成功清零。禁用后 react 意图回落文本。
_REACTION_FAIL_STREAK = 0
_REACTION_UNSUPPORTED = False


def reaction_unsupported() -> bool:
    """贴表情通道是否已被判不支持（进程内，重启复位）。"""
    return _REACTION_UNSUPPORTED


def _reset_reaction_streak() -> None:
    """复位降级状态。仅测试隔离用（_REACTION_* 为模块级进程态）。"""
    global _REACTION_FAIL_STREAK, _REACTION_UNSUPPORTED
    _REACTION_FAIL_STREAK = 0
    _REACTION_UNSUPPORTED = False


def _note_reaction_result(ok: bool) -> str:
    """登记一次贴表情发送结局（H9 降级状态机），返回 AUTO_CHAT_REACTIONS 标签：
    成功清零计 `"ok"`；失败累计，第 3 次连续失败起置禁用并计 `"unsupported"`，
    其间计 `"failed"`（偶发单次失败不触发禁用，spec Q2）。翻转禁用那一刻发
    一条固定文案 warning（只报事实，不携带 message_id/消息内容），且只发一次。"""
    global _REACTION_FAIL_STREAK, _REACTION_UNSUPPORTED
    if ok:
        _REACTION_FAIL_STREAK = 0
        return "ok"
    _REACTION_FAIL_STREAK += 1
    if _REACTION_FAIL_STREAK >= 3:
        if not _REACTION_UNSUPPORTED:
            _REACTION_UNSUPPORTED = True
            logger.warning("auto chat reaction channel disabled after consecutive failures")
        return "unsupported"
    return "failed"


# 表情包发送降级（S10-HUMAN-P2 H10）：与 react 同一方案——同一发送动作连续
# 失败 ≥3 次 → 进程级禁用该通道（例如 NapCat 拒绝本地文件路径）；偶发单次
# 失败只累计，任何一次成功清零。禁用后 sticker 意图回落文本回复。
_STICKER_FAIL_STREAK = 0
_STICKER_UNSUPPORTED = False


def sticker_unsupported() -> bool:
    """表情包通道是否已被判不可用（进程内，重启复位）。"""
    return _STICKER_UNSUPPORTED


def _reset_sticker_streak() -> None:
    """复位降级状态。仅测试隔离用（_STICKER_* 为模块级进程态）。"""
    global _STICKER_FAIL_STREAK, _STICKER_UNSUPPORTED
    _STICKER_FAIL_STREAK = 0
    _STICKER_UNSUPPORTED = False


def _note_sticker_result(ok: bool) -> str:
    """登记一次表情包发送结局，返回 AUTO_CHAT_STICKERS 标签：
    成功清零计 `"ok"`；失败累计，第 3 次连续失败起置禁用并计 `"unsupported"`，
    其间计 `"failed"`。翻转禁用时发一条固定文案 warning（不携带路径/内容）。"""
    global _STICKER_FAIL_STREAK, _STICKER_UNSUPPORTED
    if ok:
        _STICKER_FAIL_STREAK = 0
        return "ok"
    _STICKER_FAIL_STREAK += 1
    if _STICKER_FAIL_STREAK >= 3:
        if not _STICKER_UNSUPPORTED:
            _STICKER_UNSUPPORTED = True
            logger.warning("auto chat sticker channel disabled after consecutive failures")
        return "unsupported"
    return "failed"


# 调优记录（S7-AUTO-P2-11）：自主回复的上文与回复内容落 JSONL 供部署者复盘
_TUNE_LOG_PATH = "data/auto_chat_tune.jsonl"


def _recent_replies(group_id: int) -> tuple[str, ...]:
    return tuple(_RECENT_REPLIES.get(group_id, ()))


def _remember_reply(group_id: int, text: str) -> None:
    if text:
        _RECENT_REPLIES.setdefault(group_id, deque(maxlen=_RECENT_REPLIES_MAX)).append(text)


def style_directive(rng: Callable[[], float]) -> str:
    return _STYLE_DIRECTIVES[int(rng() * len(_STYLE_DIRECTIVES)) % len(_STYLE_DIRECTIVES)]


def _visible_len(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def truncate_at_sentence(text: str, max_chars: int) -> str:
    """长度护栏兜底（S10-HUMAN-P3-01）：截到最后一个句末标点（含），
    窗口内没有句末标点则硬切。只做截断，不改写内容。"""
    window = text[:max_chars]
    cut = 0
    for index, char in enumerate(window):
        if char in "。！？!?…~～\n":
            cut = index + 1
    if cut > 0:
        return window[:cut].strip()
    return window.rstrip()


# 分段策略（S10-HUMAN-P3-01，重写 S7-AUTO-P2-10 的见逗号就切）：参考 MaiBot
# response_splitter 与 QQ-agent "普通对话默认 1 条最多 2 条" 的口径——只在
# 句末标点处切，逗号/顿号/分号留在条内；短句永不拆；三段是低概率而非常态。
# 实测旧规则把人设后的长回复稳定拆成 3 条（tune log 三段占比 58%）。
_SPLIT_NEVER_BELOW = 12  # 去空白字数 ≤ 此值：整条发送，永不拆
_SPLIT_TWO_BELOW = 25  # ≤ 此值：至多 2 条；更长：2 条常态、3 条低概率
_THREE_PART_RATE = 0.2
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[。！？!?…~～])\s*|\n+")


def split_casual_reply(text: str, *, rng: Callable[[], float], max_parts: int = 3) -> list[str]:
    """长回复 → 少量短消息。只在句末标点（。！？…~与换行）处切，一条
    带逗号的消息不再被拆成几条；去空白 ≤12 字整条发送；≤25 字至多 2 条；
    更长时 2 条常态、3 条低概率（rng < 0.2）；超出上限的尾段并回最后一条
    （各段自带句末标点，不再补逗号）。"""
    text = text.strip()
    if not text:
        return []
    visible = _visible_len(text)
    if visible <= _SPLIT_NEVER_BELOW:
        cleaned = text.strip("，,、")
        return [cleaned] if cleaned else []
    parts = [part.strip("，,、") for part in _SENTENCE_BOUNDARY_RE.split(text)]
    parts = [part for part in parts if part]
    if len(parts) <= 1:
        return parts
    cap = 2
    if visible > _SPLIT_TWO_BELOW and rng() < _THREE_PART_RATE:
        cap = 3
    cap = min(cap, max_parts)
    if len(parts) <= cap:
        return parts
    return [*parts[: cap - 1], "".join(parts[cap - 1 :])]


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

# 口头禅短语闸阈值（S10-HUMAN-P3-01）：去标点后共享 ≥5 连续字判 repeat。
# 低于 5 会误伤"不知道啊"这类常用短语的正常复用。
_PHRASE_REPEAT_MIN = 5


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
    echo = 几乎复述触发句；repeat = 与自己最近发言高度雷同，或共享
    ≥5 连续字（去标点后）的口头禅短语——整句相似度抓不住"这波操作
    有点意思"式复读（S10-HUMAN-P3-01，实测 9 条回复内出现 4 次）。"""
    norm_reply = _norm_text(reply)
    if not norm_reply:
        return "empty"
    norm_trigger = _norm_text(trigger)
    if len(norm_trigger) >= 6 and norm_trigger in norm_reply:
        return "echo"
    for prev in recent:
        norm_prev = _norm_text(prev)
        if not norm_prev:
            continue
        matcher = SequenceMatcher(None, norm_reply, norm_prev, autojunk=False)
        blocks = matcher.get_matching_blocks()
        if matcher.ratio() >= 0.8 or max(block.size for block in blocks) >= _PHRASE_REPEAT_MIN:
            return "repeat"
    return None


def _strip_excl_and_emoji_punct(text: str) -> str:
    """自主回复感叹号收口：整条至多保留一个感叹号——连续感叹折叠为一个，
    多处感叹只留最后一句的、其余转句号（真人会"！"但不会"！！！"，而从不
    感叹又是另一种机器人特征）；表情与文字之间不夹标点。"""
    parts = re.split(r"[!！]+", text)
    if len(parts) == 2:
        cleaned = parts[0] + "！" + parts[1]
    elif len(parts) > 2:
        cleaned = "。".join(parts[:-2] + [parts[-2]]) + "！" + parts[-1]
    else:
        cleaned = text
    cleaned = re.sub(r"。+！", "！", cleaned)
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
    model: str = "",
    nicknames: Mapping[int, str] | None = None,
) -> None:
    """调优记录：上文与回复内容落 JSONL（仅本地 data/，不进日志/指标），
    供部署者复盘哪些回复好、哪些差。`model` 记请求的回复模型（fallback 未必
    真用这个模型，结合 AI_FALLBACK 日志可判断）。失败绝不影响主流程。
    S11-AUTO-04：context 行带 `nick`（有群昵称时），字母代称复盘分不清人。"""
    if not settings.auto_chat_tune_log_enabled:
        return
    try:
        context_items = []
        for row in context_rows:
            item: dict[str, Any] = {
                "user": row.user_id,
                "text": row.message_text,
                "bot_reply": row.ai_reply or None,
            }
            nick = (nicknames or {}).get(row.user_id)
            if nick:
                item["nick"] = nick
            context_items.append(item)
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "group_id": group_id,
            "path": path,
            "model": model,
            "trigger": trigger,
            "context": context_items,
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
    rows: Sequence[ChatMemoryRow],
    bot_reply_text: str,
    recent_replies: Sequence[str] = (),
    *,
    persona_examples: Sequence[str] = (),
    nicknames: Mapping[int, str] | None = None,
) -> str:
    lines = ["最近群消息（最后一条是最新消息）："]
    lines.extend(_render_rows(rows, nicknames=nicknames))
    if persona_examples:
        lines.append(_PERSONA_EXAMPLES_SECTION_HEADER)
        lines.extend(f"- {text}" for text in persona_examples)
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
    nicknames: Mapping[int, str] | None = None,
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
                                persona_examples=settings.persona_example_list,
                                nicknames=nicknames,
                            )
                            + f"\n风格要求：{style_directive(random.random)}"
                        ),
                        settings=settings,
                        client=client,
                        model=settings.auto_chat_reply_model or settings.ai_model,
                        max_tokens=256,
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
                model=settings.auto_chat_reply_model or settings.ai_model,
                nicknames=nicknames,
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


async def _person_observe_refs(
    memory_store: Any, group_id: int, user_id: int, *, settings: BotSettings
) -> tuple[str, ...]:
    """当前发言者的观察卡引用（S10-HUMAN-P2 H8）。关闭、无卡、负卡或任何
    存储失败都返回空——注入只是语气增强，绝不拖累主流程。用户的
    ``/记忆关闭`` 状态在学习时已阻止写卡，删除卡自然读出空。"""
    if not settings.person_observe_enabled:
        return ()
    try:
        row = await memory_store.get_person_observation(group_id=group_id, user_id=user_id)
    except Exception:
        return ()
    if row is None or row.insufficient or not row.observation:
        return ()
    return (row.observation,)


def trigger_sender_id(items: Sequence[Any], event: Any, is_batch: bool) -> int | None:
    """当前触发消息的发送者（H8 观察卡归属）。批次路径取过滤后最后一条；
    单消息路径恒等于 event.user_id。返回 None 时调用方跳过注入。"""
    if is_batch:
        return items[-1].sender_user_id if items else None
    return getattr(event, "user_id", None)


# ---- 联网搜索（S11-AUTO-SEARCH）----


async def _auto_search(
    trigger_text: str,
    *,
    settings: BotSettings,
    client: Any,
    _request_completion: Any = None,
) -> str:
    """自主插话的联网判别+搜索（fail-open）：判别门说不需要、判别失败、
    搜索失败、无结果一律返回空串，聊天照常生成（banter 不为联网买单）。
    复用 @问答同一套判别门提示词与 Tavily 搜索；``_request_completion``
    为测试接缝，生产调用方不传。"""
    from qq_bot.services.ai_client import request_completion as _real_completion
    from qq_bot.services.search import (
        SEARCH_GATE_SYSTEM_PROMPT,
        build_search_gate_user_prompt,
        format_search_context,
        parse_search_intent,
        search_web,
    )

    complete = _request_completion or _real_completion
    try:
        content = await complete(
            system_prompt=SEARCH_GATE_SYSTEM_PROMPT,
            user_prompt=build_search_gate_user_prompt(trigger_text),
            settings=settings,
            client=client,
            model=settings.router_model,
            max_tokens=128,
            json_mode=True,
        )
        intent = parse_search_intent(content)
    except Exception as exc:
        metrics.SEARCH_GATE.labels("error").inc()
        record_error("auto_chat_search", classify_exception(exc).category.value)
        return ""
    if intent is None:
        metrics.SEARCH_GATE.labels("error").inc()
        return ""
    if not intent[0]:
        metrics.SEARCH_GATE.labels("skip").inc()
        return ""
    try:
        results = await search_web(intent[1], settings=settings, client=client)
    except Exception as exc:
        record_error("auto_chat_search", classify_exception(exc).category.value)
        return ""
    if not results:
        metrics.SEARCH_GATE.labels("empty").inc()
        return ""
    metrics.SEARCH_GATE.labels("search").inc()
    return format_search_context(results)


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
    react: Callable[[int, str], Awaitable[bool]] | None = None,
    trigger_message_id: int | None = None,
    sticker_library: StickerLibrary | None = None,
    send_sticker: Callable[[Sticker], Awaitable[bool]] | None = None,
    nickname_resolver: Callable[[frozenset[int]], Awaitable[Mapping[int, str] | None]]
    | None = None,
    trigger_images: Sequence[str] = (),
) -> None:
    """Full pipeline for one non-addressed group message (spec 一/二/三/四节).

    ``batch``（S10-HUMAN-P0）传入收集器预筛后的批次项时走批次路径：跳过
    static_prefilter（收集器已逐条预筛），批次文本作为最后一条触发上下文；
    ``batch=None`` 时为单消息路径，行为与历史版本逐行等价。
    ``quoted_self``（spec §5.3）标记引用机器人的消息：分类为 QUOTED，
    跳过采样与决策门（必回旁路）。

    ``react``/``trigger_message_id``（S10-HUMAN-P2 H9）：贴表情回调与被反应的
    触发消息 id。回调为 None 或无触发消息（收集器批次交付路径）时 react 通道
    自然不可用，决策门即使给 action=react 也回落文本回复。

    ``sticker_library``/``send_sticker``（S10-HUMAN-P2 H10）：本地表情包库快照
    与发送回调。任一缺失（或库为空）时 sticker 通道不可用，候选块与指令都不进
    prompt，门给 action=sticker 也回落文本路径。

    ``nickname_resolver``（S11-AUTO-04）：按需解析群昵称的异步回调
    ``(user_ids) -> Mapping[int, str] | None``，插件经 OneBot 拉取并缓存。
    上下文行加载后调用一次，结果进门/生成 prompt 与 tune log；None 或缺
    某人的昵称时该人回落「用户A/B」字母代称。    ``_request_completion`` is a test seam defaulting to the real
    ``request_completion``; production callers never pass it. Send and quota
    are injected so this service stays matcher- and runtime-free.

    ``trigger_images``（S11-VISION-01，V2-lite）：触发消息携带的图片源。
    决策门永远纯文本（控成本）；生成链仅在 ``auto_chat_vision_enabled`` 开启
    时把图随请求发给模型，关闭时上下文里只有历史遗留的 ``[图片]`` 占位。"""
    from qq_bot.services.ai_client import request_completion as _real_completion

    complete = _request_completion or _real_completion
    state = state or _SHARED_STATE
    persona = load_persona(settings)
    group_id = event.group_id

    def _metric(stage: str, result: str) -> None:
        metrics.AUTO_CHAT.labels(stage, result).inc()

    # 可用表情 ID 提示（H9）的准入：除了设置开关与白名单，还要求本轮贴表情
    # 通道真的可用（react 回调在、有触发消息 id、通道未被判 unsupported）。
    # 收集器批次交付路径 react=None，提示绝不能出现——模型被承诺的表情通道
    # 这一轮并不存在。整轮只算一次，与下方 react 分支的前置闸门保持同源。
    reaction_available = (
        react is not None
        and trigger_message_id is not None
        and settings.auto_chat_reaction_enabled
        and bool(settings.auto_chat_reaction_id_list)
        and not reaction_unsupported()
    )

    # 表情包候选（H10）的准入：开关开、库非空、发送回调在、通道未被判
    # unsupported。与 react 同一"同进同出"原则：不可用时候选块与指令都不进
    # prompt。候选整轮只取一次（重试 prompt 复用同一份列表）。
    sticker_candidates: tuple[Sticker, ...] = ()
    if (
        send_sticker is not None
        and settings.auto_chat_sticker_enabled
        and sticker_library is not None
        and not sticker_unsupported()
    ):
        sticker_candidates = sticker_library.candidates(settings.auto_chat_sticker_max_candidates)

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
    # 昵称按需解析（S11-AUTO-04）：所需 id = 上下文行 + 批次项发送者；目录
    # 只在遇到未知 id 且过了防抖窗口才真正拉 OneBot，失败返回 None 回落字母。
    nicknames: Mapping[int, str] | None = None
    if nickname_resolver is not None:
        needed = {row.user_id for row in rows}
        if is_batch:
            needed |= {item.sender_user_id for item in items}
        try:
            nicknames = await nickname_resolver(frozenset(needed))
        except Exception:
            nicknames = None
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
        # react 指令 + 可用表情 ID 提示（H9，Final Fix）：整个条件块仅在通道
        # 本轮真的可用时进 user prompt——react 回调不在、无触发消息 id（收集器
        # 批次交付路径）、开关关、白名单空或已判 unsupported 时整块缺席；系统
        # 提示词不含 react 文案（不可用部署不为此付 token）。白名单非空已由
        # reaction_available 保证，无需再提示"列表为空时不得 react"。
        reaction_hint = ""
        if reaction_available:
            ids = "、".join(sorted(settings.auto_chat_reaction_id_list))
            reaction_hint = (
                f"\n{_GATE_REACT_INSTRUCTION}\n"
                f"可用表情 ID：{ids}（仅当 action=react 时选一个；不适合就 action=reply）"
            )
        # 表情包候选块（H10）：与 react 同进同出，库为空/通道不可用时整块缺席。
        sticker_hint = ""
        if sticker_candidates:
            listed = "\n".join(
                f"- {sticker.id}：{sticker.description}" for sticker in sticker_candidates
            )
            sticker_hint = (
                f"\n{_GATE_STICKER_INSTRUCTION}\n"
                f"可用表情包候选（ID：描述）：\n{listed}\n"
                "（仅当 action=sticker 时选一个 ID；不适合就 action=reply）"
            )
        try:
            content = await complete(
                system_prompt=gate_system_prompt(settings.persona_name),
                user_prompt=build_gate_user_prompt(
                    rows,
                    batch_rows=_batch_as_rows(items, group_id) if is_batch else (),
                    jargon_refs=jargon_refs,
                    nicknames=nicknames,
                )
                + reaction_hint
                + sticker_hint,
                settings=settings,
                client=client,
                model=settings.router_model,
                max_tokens=300,
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

        # ---- 贴表情反应通道（S10-HUMAN-P2 H9）----
        # 进入条件 = "这轮真的打算贴表情"：门给 react 且所有前置闸门通过。
        # 任一前置条件不满足（通道关/白名单空/emoji 不在白名单/已判
        # unsupported/无回调或触发消息）都视为"未尝试"——自然落到下方文本路径。
        # 注意：本分支有意不取 state.lock（plan 明示）——贴表情不占 _last_sent/
        # _hourly/_daily 的文本冷却与计数，并发同群多轮只可能让反应独立限额
        # 被极小幅超额（状态自身的 dict 读写是单线程事件循环原子操作）；
        # 下方文本路径的锁只覆盖它自己的临界段（limit_reason→acquire→生成）。
        if (
            decision.action == "react"
            and settings.auto_chat_reaction_enabled
            and not _REACTION_UNSUPPORTED
            and react is not None
            and trigger_message_id is not None
            and decision.emoji_id in settings.auto_chat_reaction_id_list
        ):
            _metric("gate", "react_intent")
            if state.reaction_limit_reason(group_id, settings=settings):
                # 反应限额（独立计数）已满 = 本轮"未尝试"发送：记指标后落到下方
                # 文本路径（与"emoji 不在白名单"同族的前置未尝试条件，unsupported
                # 之外的失败态都不静默丢轮）。限额判定先于 quota——不为一个不会
                # 发出的表情消耗 quota 滑动窗口配额（文本路径自己会检一次）。
                metrics.AUTO_CHAT_REACTIONS.labels("limit").inc()
                _metric("prefilter", "reaction_limit")
            else:
                if quota_check is not None and not await quota_check():
                    _metric("gate", "quota_denied")
                    return
                try:
                    ok = await react(trigger_message_id, decision.emoji_id)
                except Exception:
                    ok = False
                metrics.AUTO_CHAT_REACTIONS.labels(_note_reaction_result(ok)).inc()
                if ok:
                    state.note_reaction(group_id)
                    _metric("gate", "react")
                    return  # 不生成/不 note_reply/不冷场/不发文本
                _metric("gate", "react_failed")
                return  # 发送中失败即结束本轮：门已判定只值得贴表情，不强行生成文本

        # ---- 表情包通道（S10-HUMAN-P2 H10）----
        # 与 react 同构：门给 sticker 且 id 命中本轮候选（即库中启用条目）
        # 才算"这轮真的打算发图"。任一前置不满足视为未尝试，落文本路径。
        if (
            decision.action == "sticker"
            and send_sticker is not None
            and not _STICKER_UNSUPPORTED
            and any(candidate.id == decision.sticker_id for candidate in sticker_candidates)
        ):
            sticker = next(c for c in sticker_candidates if c.id == decision.sticker_id)
            _metric("gate", "sticker_intent")
            if state.sticker_limit_reason(group_id, settings=settings):
                # 表情包限额满 = 未尝试：与 react 同族，落文本路径不静默丢轮
                metrics.AUTO_CHAT_STICKERS.labels("limit").inc()
                _metric("prefilter", "sticker_limit")
            else:
                if quota_check is not None and not await quota_check():
                    _metric("gate", "quota_denied")
                    return
                try:
                    ok = await send_sticker(sticker)
                except Exception:
                    ok = False
                metrics.AUTO_CHAT_STICKERS.labels(_note_sticker_result(ok)).inc()
                if ok:
                    state.note_sticker(group_id)
                    _metric("gate", "sticker")
                    return  # 不生成/不 note_reply/不热聊/不冷场
                _metric("gate", "sticker_failed")
                return  # 门已判定只值得发图，不强行生成文本

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
            # 联网搜索（S11-AUTO-SEARCH）：生成前由判别门决定是否搜索，搜到
            # 资料才注入生成 prompt；判别/搜索失败 fail-open 照常聊天。
            search_refs = ""
            if settings.auto_chat_search_enabled and settings.has_search_config():
                if state.search_limit_reason(group_id, settings=settings):
                    _metric("search", "limit")
                else:
                    search_refs = await _auto_search(
                        trigger_text,
                        settings=settings,
                        client=client,
                        _request_completion=complete,
                    )
                    if search_refs:
                        state.note_search(group_id)
                        _metric("search", "on")
                    else:
                        _metric("search", "off")
            # 表达样本每次 run 只取一次：重试 prompt 复用同一批参考（重试不是
            # 新的一轮群聊），id 也一并留待发送成功后回写。
            expression_rows = await _expression_sample(memory_store, group_id, settings=settings)
            expression_refs = tuple(r.template for r in expression_rows)
            expression_ids = [r.id for r in expression_rows]
            # 观察卡对应当前发言人（H8）：与表达样本同生命周期取一次，
            # 重试 prompt 复用同一份，不重复读库。
            observe_refs = await _person_observe_refs(
                memory_store,
                group_id,
                trigger_sender_id(items, event, is_batch),
                settings=settings,
            )

            generation_images = tuple(trigger_images) if settings.auto_chat_vision_enabled else ()
            vision_note = (
                "\n（最新那条消息里的 [图片] 已随本条请求附给你，回应要结合图片内容。）"
                if generation_images
                else ""
            )

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
                            person_observe_refs=observe_refs,
                            persona_examples=settings.persona_example_list,
                            nicknames=nicknames,
                            search_refs=search_refs,
                        )
                        + f"\n风格要求：{style_directive(rng)}"
                        + vision_note
                    ),
                    settings=settings,
                    client=client,
                    model=settings.auto_chat_reply_model or settings.ai_model,
                    max_tokens=256,
                    temperature=settings.auto_chat_reply_temperature,
                    images=generation_images,
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
        # 长度护栏（S10-HUMAN-P3-01）：超限先带上下文重写一次（上限而非一刀切，
        # 长度方差保留给生成端），重写仍超则句末截断兜底。实测 glm-4-flash 在
        # 人设锚定后平均 41 字/条、最长 83 字，群友连续两天抱怨"怎么话这么长"。
        max_chars = settings.auto_chat_max_reply_chars
        if max_chars > 0 and _visible_len(reply) > max_chars:
            _metric("generate", "too_long")
            try:
                shortened = (
                    await complete(
                        system_prompt=casual_system_prompt(persona),
                        user_prompt=(
                            build_casual_user_prompt(
                                rows,
                                recent_replies=_recent_replies(group_id),
                                batch_rows=_batch_as_rows(items, group_id) if is_batch else (),
                                expression_refs=expression_refs,
                                jargon_refs=jargon_refs,
                                person_observe_refs=observe_refs,
                                persona_examples=settings.persona_example_list,
                                nicknames=nicknames,
                                search_refs=search_refs,
                            )
                            + "\n重写要求：你刚才的回复太长了。用 20 个字以内重说一遍，"
                            "只说最想说的那句，直接输出那句话。"
                        ),
                        settings=settings,
                        client=client,
                        model=settings.auto_chat_reply_model or settings.ai_model,
                        max_tokens=256,
                        temperature=settings.auto_chat_reply_temperature,
                    )
                ).strip()
            except Exception:
                shortened = ""
            if shortened and not _has_id_artifact(shortened):
                reply = _strip_excl_and_emoji_punct(shortened)
            if _visible_len(reply) > max_chars:
                reply = truncate_at_sentence(reply, max_chars)
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
                    person_observe_refs=observe_refs,
                    persona_examples=settings.persona_example_list,
                    search_refs=search_refs,
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
                        max_tokens=256,
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
            # 重试产物同样受长度护栏约束：只做确定性截断，不再花一次重写
            if max_chars > 0 and _visible_len(reply) > max_chars:
                reply = truncate_at_sentence(reply, max_chars)
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
        nicknames=nicknames,
    )
    parts = [reply] if plusone else split_casual_reply(reply, rng=rng)
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
        model=settings.auto_chat_reply_model or settings.ai_model,
        nicknames=nicknames,
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
