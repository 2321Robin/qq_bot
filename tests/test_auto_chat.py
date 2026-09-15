import asyncio

from qq_bot.config import BotSettings
from qq_bot.observability import metrics
from qq_bot.services.auto_chat import (
    AutoChatState,
    GateDecision,
    build_casual_user_prompt,
    build_gate_user_prompt,
    detect_negative_feedback,
    parse_gate_output,
    static_prefilter,
)
from qq_bot.services.chat_memory import ChatMemoryRow
from qq_bot.services.persona import Persona


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _settings(**overrides) -> BotSettings:
    defaults = {
        "auto_chat_cooldown_seconds": 300.0,
        "auto_chat_hourly_limit": 2,
        "auto_chat_daily_limit": 3,
    }
    defaults.update(overrides)
    return BotSettings(**defaults)


def test_limit_reason_empty_then_cooldown_after_acquire() -> None:
    clock = FakeClock()
    state = AutoChatState(clock=clock)
    settings = _settings()
    assert state.limit_reason(1, settings=settings) == ""
    assert state.acquire(1, settings=settings) == ""
    assert state.limit_reason(1, settings=settings) == "cooldown"
    clock.advance(301)
    assert state.limit_reason(1, settings=settings) == ""


def test_acquire_is_atomic_recheck_under_cooldown() -> None:
    clock = FakeClock()
    state = AutoChatState(clock=clock)
    settings = _settings()
    assert state.acquire(1, settings=settings) == ""
    assert state.acquire(1, settings=settings) == "cooldown"


def test_hourly_limit_trips_then_resets_next_hour() -> None:
    from datetime import UTC, datetime, timedelta

    clock = FakeClock()

    def bucket() -> datetime:
        hours = int((clock.now - 1000.0) // 3600)
        return datetime(2026, 9, 16, 10, 0, tzinfo=UTC) + timedelta(hours=hours)

    state = AutoChatState(clock=clock, bucket_clock=bucket)
    settings = _settings(auto_chat_cooldown_seconds=0.0)
    assert state.acquire(1, settings=settings) == ""
    clock.advance(1)
    assert state.acquire(1, settings=settings) == ""
    assert state.acquire(1, settings=settings) == "hourly_limit"
    clock.advance(3600)
    assert state.acquire(1, settings=settings) == ""


def test_daily_limit_trips() -> None:
    from datetime import UTC, datetime

    def fixed_bucket() -> datetime:
        return datetime(2026, 9, 16, tzinfo=UTC)

    state = AutoChatState(bucket_clock=fixed_bucket)
    settings = _settings(auto_chat_hourly_limit=10, auto_chat_cooldown_seconds=0.0)
    assert state.acquire(1, settings=settings) == ""
    assert state.acquire(1, settings=settings) == ""
    assert state.acquire(1, settings=settings) == ""
    assert state.acquire(1, settings=settings) == "daily_limit"


def test_backoff_window_and_expiry() -> None:
    clock = FakeClock()
    state = AutoChatState(clock=clock)
    assert state.in_backoff(1) is False
    state.set_backoff(1, 1800.0)
    assert state.in_backoff(1) is True
    clock.advance(1801)
    assert state.in_backoff(1) is False


def test_recently_spoke_within_window() -> None:
    clock = FakeClock()
    state = AutoChatState(clock=clock)
    assert state.recently_spoke(1, 300.0) is False
    state.acquire(1, settings=_settings())
    assert state.recently_spoke(1, 300.0) is True
    clock.advance(301)
    assert state.recently_spoke(1, 300.0) is False


def test_lock_is_per_group() -> None:
    state = AutoChatState()
    assert state.lock(1) is state.lock(1)
    assert state.lock(1) is not state.lock(2)
    asyncio.run(state.lock(1).acquire())
    assert state.lock(1).locked()


# ---- 纯函数层：预筛 / 负反馈 / prompt 构建 / 决策门解析（S7-AUTO-05）----


def _row(text: str, user_id: int = 2001, row_id: int = 1) -> ChatMemoryRow:
    return ChatMemoryRow(
        id=row_id,
        group_id=1001,
        user_id=user_id,
        message_text=text,
        created_at="2026-09-16T00:00:00+00:00",
        is_ai_prompt=False,
        ai_reply="",
    )


class TestStaticPrefilter:
    def test_command_prefix_rejected(self) -> None:
        settings = BotSettings()
        assert static_prefilter("/帮助", settings=settings) == "command"
        assert static_prefilter("ai 你好", settings=settings) == "command"
        assert static_prefilter("ai", settings=settings) == "command"

    def test_too_short_or_pure_emoji_rejected(self) -> None:
        settings = BotSettings()
        assert static_prefilter("😂😂", settings=settings) == "too_short"
        assert static_prefilter("！！", settings=settings) == "too_short"
        assert static_prefilter("嗯", settings=settings) == "too_short"

    def test_sensitive_word_rejected(self) -> None:
        settings = BotSettings()
        assert static_prefilter("有没有人玩博彩", settings=settings) == "sensitive"

    def test_normal_text_passes(self) -> None:
        assert static_prefilter("今天新出的活动有人一起吗", settings=BotSettings()) == "pass"


class TestNegativeFeedback:
    def test_negative_word_with_nickname_triggers(self) -> None:
        persona = Persona(name="小洛", aliases=(), prompt="x")
        rows = [_row("小洛你闭嘴啊", user_id=2002)]
        assert (
            detect_negative_feedback(
                rows, persona=persona, settings=BotSettings(), bot_recently_spoke=False
            )
            is True
        )

    def test_negative_word_after_recent_bot_reply_triggers(self) -> None:
        persona = Persona(name="小洛", aliases=(), prompt="x")
        rows = [_row("吵死了", user_id=2002)]
        assert (
            detect_negative_feedback(
                rows, persona=persona, settings=BotSettings(), bot_recently_spoke=True
            )
            is True
        )

    def test_negative_word_without_bot_reference_ignores(self) -> None:
        persona = Persona(name="小洛", aliases=(), prompt="x")
        rows = [_row("让他说闭嘴", user_id=2002)]
        assert (
            detect_negative_feedback(
                rows, persona=persona, settings=BotSettings(), bot_recently_spoke=False
            )
            is False
        )


class TestPrompts:
    def test_gate_user_prompt_lists_messages_with_last_is_newest(self) -> None:
        rows = [_row("早", user_id=2001, row_id=1), _row("早呀", user_id=2002, row_id=2)]
        prompt = build_gate_user_prompt(rows)
        assert "用户2001：早" in prompt
        assert prompt.strip().endswith("用户2002：早呀")

    def test_casual_user_prompt_asks_for_one_reply(self) -> None:
        rows = [_row("今天好累", user_id=2001)]
        prompt = build_casual_user_prompt(rows)
        assert "用户2001：今天好累" in prompt
        assert "接" in prompt


class TestGateParsing:
    def test_valid_output_parses(self) -> None:
        decision = parse_gate_output(
            '{"should_reply": true, "reason": "banter", "confidence": 0.9}'
        )
        assert decision == GateDecision(should_reply=True, reason="banter", confidence=0.9)

    def test_markdown_fence_tolerated(self) -> None:
        decision = parse_gate_output(
            '```json\n{"should_reply": false, "reason": "none", "confidence": 0.4}\n```'
        )
        assert decision is not None and decision.should_reply is False

    def test_fail_closed_on_garbage(self) -> None:
        assert parse_gate_output(None) is None
        assert parse_gate_output("") is None
        assert parse_gate_output("不是json") is None
        assert parse_gate_output('{"should_reply": 1, "reason": "banter", "confidence": 0.9}') is None
        assert parse_gate_output('{"should_reply": true, "reason": "wat", "confidence": 0.9}') is None
        assert parse_gate_output('{"should_reply": true, "reason": "banter", "confidence": 1.5}') is None


def test_auto_chat_counter_registered() -> None:
    metrics.AUTO_CHAT.labels("prefilter", "passed").inc()
