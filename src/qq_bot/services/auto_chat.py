"""Autonomous group chat (S7-AUTO): prefilter, sampling, LLM gate decision,
casual persona generation and in-memory throttling.

Design contract (spec 2026-09-16-auto-chat-design.md): decisions fail
closed; guardrails (cooldown/limits/backoff) apply to the nicknamed
fast-path too; state is process-local and lost on restart by design."""

from __future__ import annotations

import asyncio
import json
import random
from collections import deque
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable, Sequence

from qq_bot.config import BotSettings
from qq_bot.observability import metrics, record_error
from qq_bot.observability.logging import current_request_id
from qq_bot.observability.tracing import get_tracer
from qq_bot.services.chat_memory import ChatMemoryRow
from qq_bot.services.persona import (
    Persona,
    casual_system_prompt,
    load_persona,
    mentions_persona,
)
from qq_bot.services.reliability import classify_exception


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
        self._hot_until: dict[int, float] = {}
        self._hot_streak: dict[int, int] = {}
        self._cold_daily: dict[tuple[int, str], int] = {}

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


def _render_rows(rows: Sequence[ChatMemoryRow]) -> list[str]:
    lines: list[str] = []
    for row in rows:
        lines.append(f"用户{row.user_id}：{row.message_text}")
        if row.ai_reply:
            lines.append(f"机器人：{row.ai_reply}")
    return lines


def build_gate_user_prompt(rows: Sequence[ChatMemoryRow]) -> str:
    lines = ["最近群消息（最后一条是最新消息）："]
    lines.extend(_render_rows(rows))
    return "\n".join(lines)


def build_casual_user_prompt(
    rows: Sequence[ChatMemoryRow], recent_replies: Sequence[str] = ()
) -> str:
    lines = ["最近群消息（最后一条是最新消息）："]
    lines.extend(_render_rows(rows))
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
    "这条回复用一个平铺直叙的短句收尾，别反问",
    "这条回复只发一个 3 到 8 个字的短语",
)

# 每群最近自主发言（不含被 @ 问答），注入 prompt 让模型避开自己的旧句式
_RECENT_REPLIES: dict[int, deque[str]] = {}
_RECENT_REPLIES_MAX = 8


def _recent_replies(group_id: int) -> tuple[str, ...]:
    return tuple(_RECENT_REPLIES.get(group_id, ()))


def _remember_reply(group_id: int, text: str) -> None:
    if text:
        _RECENT_REPLIES.setdefault(group_id, deque(maxlen=_RECENT_REPLIES_MAX)).append(text)


def style_directive(rng: Callable[[], float]) -> str:
    return _STYLE_DIRECTIVES[int(rng() * len(_STYLE_DIRECTIVES)) % len(_STYLE_DIRECTIVES)]

COLD_FALLBACK_MESSAGES = (
    "怎么没人理我…鱼都晒干了",
    "就当我说的是空气吧",
    "冷场了？行吧，我躺回去了",
)

_COLD_TASKS: set[asyncio.Task] = set()


def shared_state() -> AutoChatState:
    """公开的模块级状态入口（插件被 @ 路径使用）。"""
    return _SHARED_STATE


def build_cold_user_prompt(rows: Sequence[ChatMemoryRow], bot_reply_text: str) -> str:
    lines = ["最近群消息（最后一条是最新消息）："]
    lines.extend(_render_rows(rows))
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
    send: Callable[[str], Awaitable[None]],
    quota_check: Callable[[], Awaitable[bool]] | None = None,
    client: Any | None = None,
    state: AutoChatState | None = None,
    rng: Callable[[], float] = random.random,
    _request_completion: Any = None,
    _sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """发言后调度一次冷场检查（spec 第五节）。进程内 task，重启丢失可接受。"""

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
                            build_cold_user_prompt(rows, bot_reply_text)
                            + f"\n风格要求：{style_directive(random.random)}"
                        ),
                        settings=settings,
                        client=client,
                        model=settings.ai_model,
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
            await send(reply)
            _cold("ok")
        except Exception:
            _cold("error")

    task = asyncio.ensure_future(_check())
    _COLD_TASKS.add(task)
    task.add_done_callback(_COLD_TASKS.discard)


async def run_auto_chat(
    *,
    event: Any,
    raw_text: str,
    settings: BotSettings,
    memory_store: Any,
    send: Callable[[str], Awaitable[None]],
    quota_check: Callable[[], Awaitable[bool]] | None = None,
    client: Any | None = None,
    state: AutoChatState | None = None,
    rng: Callable[[], float] = random.random,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    _cold_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    _request_completion: Any = None,
) -> None:
    """Full pipeline for one non-addressed group message (spec 一/二/三/四节).

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

    if event.user_id in settings.auto_chat_ignored_user_id_list:
        _metric("prefilter", "ignored")
        return
    static = static_prefilter(raw_text, settings=settings)
    if static != "pass":
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
    if not rows:
        _metric("prefilter", "no_context")
        return

    bot_recently = state.recently_spoke(group_id, settings.auto_chat_cooldown_seconds)
    if detect_negative_feedback(
        rows, persona=persona, settings=settings, bot_recently_spoke=bot_recently
    ):
        state.set_backoff(group_id, settings.auto_chat_negative_backoff_seconds)
        _metric("prefilter", "backoff")
        return
    if state.in_backoff(group_id):
        _metric("prefilter", "backoff")
        return
    hot = state.hot_active(group_id, settings=settings)
    limit = state.limit_reason(
        group_id,
        settings=settings,
        cooldown_seconds=(settings.auto_chat_hot_cooldown_seconds if hot else None),
        skip_hourly=hot,
    )
    if limit == "cooldown":
        _metric("prefilter", "cooldown")
        return
    if limit:
        _metric("prefilter", "limit")
        return

    named = mentions_persona(raw_text, persona)
    you_plural = settings.auto_chat_you_plural_reply and "你们" in raw_text
    you_ref = "你" in raw_text
    tracer = get_tracer()
    if named:
        _metric("prefilter", "nicknamed")
    elif you_plural:
        _metric("prefilter", "you_plural")
    elif hot:
        _metric("prefilter", "hot_reply")
    else:
        if not you_ref and rng() >= settings.auto_chat_sample_rate:
            _metric("prefilter", "sampled_out")
            return
        if you_ref:
            _metric("prefilter", "you_to_gate")
        else:
            _metric("prefilter", "passed")
        gate_span = tracer.start_span("auto.gate", trace_id=current_request_id())
        try:
            content = await complete(
                system_prompt=GATE_SYSTEM_PROMPT,
                user_prompt=build_gate_user_prompt(rows),
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
            cooldown_seconds=(settings.auto_chat_hot_cooldown_seconds if hot else None),
            skip_hourly=hot,
        ):
            _metric("prefilter", "limit")
            return
        generate_span = tracer.start_span("auto.generate", trace_id=current_request_id())

        async def _generate() -> str:
            return await complete(
                system_prompt=casual_system_prompt(persona),
                user_prompt=(
                    build_casual_user_prompt(rows, recent_replies=_recent_replies(group_id))
                    + f"\n风格要求：{style_directive(rng)}"
                ),
                settings=settings,
                client=client,
                model=settings.ai_model,
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
                generate_span, status="error", category=classify_exception(exc).category.value
            )
            record_error("auto_chat", classify_exception(exc).category.value)
            _metric("generate", "error")
            return  # 冷却已被占用：有意的保守行为
        tracer.end_span(generate_span)

    if not reply:
        _metric("generate", "error")
        return
    _metric("generate", "ok")
    if any(word in reply for word in settings.auto_chat_sensitive_word_list):
        _metric("generate", "filtered")
        return
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
    await send(reply)
    _metric("send", "ok")  # 已移交发送器；发送失败由 onebot_send 的 SEND_RESULTS 计数
