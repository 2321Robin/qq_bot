import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from qq_bot.config import BotSettings
from qq_bot.observability import metrics
from qq_bot.services.auto_chat import (
    AutoChatState,
    COLD_FALLBACK_MESSAGES,
    GATE_SYSTEM_PROMPT,
    GateDecision,
    build_casual_user_prompt,
    build_cold_user_prompt,
    build_gate_user_prompt,
    detect_negative_feedback,
    parse_gate_output,
    run_auto_chat,
    schedule_cold_check,
    static_prefilter,
)
from qq_bot.services.chat_memory import ChatMemoryRow
from qq_bot.services.persona import Persona


@pytest.fixture(autouse=True)
async def _cancel_leftover_tasks():
    """schedule_cold_check spawns in-process follow-up tasks; cancel whatever
    is still pending when a test ends so loops close without
    "Task was destroyed but it is pending" noise."""
    yield
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for t in pending:
        t.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


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


def _row(
    text: str,
    user_id: int = 2001,
    row_id: int = 1,
    ai_reply: str = "",
    created_at: str = "2026-09-16T00:00:00+00:00",
) -> ChatMemoryRow:
    return ChatMemoryRow(
        id=row_id,
        group_id=1001,
        user_id=user_id,
        message_text=text,
        created_at=created_at,
        is_ai_prompt=False,
        ai_reply=ai_reply,
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


# ---- 编排：run_auto_chat（S7-AUTO-06）----

class FakeEvent:
    def __init__(self, group_id: int = 1001, user_id: int = 2001):
        self.group_id = group_id
        self.user_id = user_id


class FakeMemory:
    def __init__(self, texts: list[str]):
        self.texts = texts
        self.calls: list[dict] = []

    async def recent_group_messages(self, *, group_id: int, limit: int):
        self.calls.append({"group_id": group_id, "limit": limit})
        rows = [_row(t, user_id=2001 + i, row_id=i + 1) for i, t in enumerate(self.texts)]
        late = getattr(self, "late_row", None)
        if late is not None:
            rows.append(late)
        return rows


class FakeAutoChatSettings(BotSettings):
    model_config = BotSettings.model_config.copy()
    model_config["env_file"] = None


def _run_settings(**overrides) -> BotSettings:
    base = {
        "ai_api_key": "k",
        "persona_name": "小洛",
        "auto_chat_enabled": True,
        "auto_chat_sample_rate": 1.0,
        "auto_chat_delay_min_seconds": 0.0,
        "auto_chat_delay_max_seconds": 0.0,
        "auto_chat_cooldown_seconds": 0.0,
        "auto_chat_hourly_limit": 10,
        "auto_chat_daily_limit": 10,
        "auto_chat_cold_daily_limit": 0,
    }
    base.update(overrides)
    return FakeAutoChatSettings(**base)


class Harness:
    def __init__(
        self, texts: list[str], settings: BotSettings, state: AutoChatState | None = None
    ):
        self.settings = settings
        self.memory = FakeMemory(texts)
        self.state = state or AutoChatState()
        self.sent: list[str] = []
        self.rng_value = 0.0
        self.slept: list[float] = []
        self.gate_content = '{"should_reply": true, "reason": "banter", "confidence": 0.9}'
        self.casual_content = "哈哈冲"
        self.cold_content = "鱼都晒干了都没人理"
        self.quota_allowed = True
        self.completions: list[dict] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def quota_check(self) -> bool:
        return self.quota_allowed

    async def sleeper(self, seconds: float) -> None:
        self.slept.append(seconds)

    def rng(self) -> float:
        return self.rng_value

    async def instant_cold_sleep(self, seconds: float) -> None:
        return None

    async def drain_cold(self) -> None:
        import asyncio as _aio

        pending = [t for t in _aio.all_tasks() if t is not _aio.current_task()]
        for t in pending:
            await t

    def completion(self):
        async def fake_request_completion(**kwargs) -> str:
            self.completions.append(kwargs)
            system = kwargs["system_prompt"]
            if "决策器" in system:
                return self.gate_content
            if "没有人回应" in system or "没有人回应" in kwargs.get("user_prompt", ""):
                return self.cold_content
            return self.casual_content

        return fake_request_completion


async def _run(h: Harness, raw_text: str = "一起去打新活动吗") -> None:
    await run_auto_chat(
        event=FakeEvent(),
        raw_text=raw_text,
        settings=h.settings,
        memory_store=h.memory,
        send=h.send,
        quota_check=h.quota_check,
        state=h.state,
        rng=h.rng,
        sleeper=h.sleeper,
        _cold_sleep=h.instant_cold_sleep,
        _request_completion=h.completion(),
    )


@pytest.mark.asyncio
async def test_happy_path_gate_pass_send_with_delay() -> None:
    h = Harness(["在吗", "新活动开了"], _run_settings())
    await _run(h)
    assert h.sent == ["哈哈冲"]
    assert h.slept == [0.0]
    assert h.state.recently_spoke(1001, 300.0) is True
    gate_calls = [c for c in h.completions if "决策器" in c["system_prompt"]]
    casual_calls = [c for c in h.completions if "决策器" not in c["system_prompt"]]
    assert len(gate_calls) == 1
    assert len(casual_calls) == 1
    assert casual_calls[0]["max_tokens"] == 100


@pytest.mark.asyncio
async def test_gate_no_reply_means_no_send_and_no_cooldown() -> None:
    h = Harness(["在吗", "新活动开了"], _run_settings())
    h.gate_content = '{"should_reply": false, "reason": "none", "confidence": 0.9}'
    await _run(h)
    assert h.sent == []
    assert h.state.recently_spoke(1001, 300.0) is False


@pytest.mark.asyncio
async def test_low_confidence_is_fail_closed() -> None:
    h = Harness(["在吗", "冲"], _run_settings())
    h.gate_content = '{"should_reply": true, "reason": "banter", "confidence": 0.5}'
    await _run(h)
    assert h.sent == []


@pytest.mark.asyncio
async def test_gate_error_is_fail_closed() -> None:
    h = Harness(["在吗", "冲"], _run_settings())

    async def boom(**kwargs):
        raise RuntimeError("gateway down")

    await run_auto_chat(
        event=FakeEvent(),
        raw_text="冲",
        settings=h.settings,
        memory_store=h.memory,
        send=h.send,
        quota_check=h.quota_check,
        state=h.state,
        rng=h.rng,
        sleeper=h.sleeper,
        _cold_sleep=h.instant_cold_sleep,
        _request_completion=boom,
    )
    assert h.sent == []
    assert h.state.recently_spoke(1001, 300.0) is False


@pytest.mark.asyncio
async def test_gate_bad_json_is_fail_closed() -> None:
    h = Harness(["在吗", "冲"], _run_settings())
    h.gate_content = "随便说点什么"
    await _run(h)
    assert h.sent == []
    assert h.state.recently_spoke(1001, 300.0) is False


@pytest.mark.asyncio
async def test_nicknamed_message_skips_gate() -> None:
    h = Harness(["有人在吗", "小洛 在吗"], _run_settings())
    await _run(h, raw_text="小洛 在吗")
    gate_calls = [c for c in h.completions if "决策器" in c["system_prompt"]]
    assert gate_calls == []
    assert h.sent == ["哈哈冲"]


@pytest.mark.asyncio
async def test_sampling_reject_without_gate_call() -> None:
    h = Harness(["消息"], _run_settings(auto_chat_sample_rate=0.5))
    h.rng_value = 0.9  # >= 0.5 → 采样未中
    await _run(h)
    assert h.sent == []
    assert h.completions == []


@pytest.mark.asyncio
async def test_negative_feedback_sets_backoff_and_blocks() -> None:
    h = Harness(["小洛你闭嘴啊"], _run_settings())
    await _run(h, raw_text="随便")
    assert h.sent == []
    assert h.state.in_backoff(1001) is True


@pytest.mark.asyncio
async def test_sensitive_reply_dropped_silently() -> None:
    h = Harness(["聊会"], _run_settings())
    h.casual_content = "来玩博彩吗"
    await _run(h)
    assert h.sent == []
    assert h.state.recently_spoke(1001, 300.0) is True  # 冷却已被占用（保守）


@pytest.mark.asyncio
async def test_quota_denied_blocks_before_generation() -> None:
    h = Harness(["聊会"], _run_settings())
    h.quota_allowed = False
    await _run(h)
    assert h.sent == []
    assert h.state.recently_spoke(1001, 300.0) is False
    # quota 检查在决策门之后、生成之前：门调用发生，生成调用不发生
    casual_calls = [c for c in h.completions if "决策器" not in c["system_prompt"]]
    assert casual_calls == []


@pytest.mark.asyncio
async def test_ignored_user_short_circuits() -> None:
    h = Harness(["聊会"], _run_settings(auto_chat_ignored_user_ids="2001"))
    await _run(h)
    assert h.sent == []
    assert h.memory.calls == []


@pytest.mark.asyncio
async def test_command_message_short_circuits() -> None:
    h = Harness(["/帮助"], _run_settings())
    await _run(h, raw_text="/帮助")
    assert h.sent == []
    assert h.memory.calls == []


@pytest.mark.asyncio
async def test_empty_history_skips_pipeline() -> None:
    h = Harness([], _run_settings())
    await _run(h)
    assert h.sent == []


@pytest.mark.asyncio
async def test_send_failure_propagates_after_state_occupied() -> None:
    h = Harness(["聊会"], _run_settings())

    async def failing_send(text: str) -> None:
        raise RuntimeError("send failed")

    with pytest.raises(RuntimeError):
        await run_auto_chat(
            event=FakeEvent(),
            raw_text="聊会",
            settings=h.settings,
            memory_store=h.memory,
            send=failing_send,
            quota_check=h.quota_check,
            state=h.state,
            rng=h.rng,
            sleeper=h.sleeper,
            _cold_sleep=h.instant_cold_sleep,
            _request_completion=h.completion(),
        )
    assert h.state.recently_spoke(1001, 300.0) is True


# ---- 二期：热聊状态与冷场计数（S7-AUTO-P2-02）----


def _hot_settings(**overrides) -> BotSettings:
    base = {
        "auto_chat_hot_window_seconds": 480.0,
        "auto_chat_hot_streak_limit": 3,
        "auto_chat_cold_daily_limit": 3,
    }
    base.update(overrides)
    return _settings(**base)


def test_note_reply_activates_and_refreshes_hot_window() -> None:
    clock = FakeClock()
    state = AutoChatState(clock=clock)
    settings = _hot_settings()
    assert state.hot_active(1, settings=settings) is False
    assert state.note_reply(1, settings=settings) == ""
    assert state.hot_active(1, settings=settings) is True
    clock.advance(479)
    assert state.note_reply(1, settings=settings) == ""  # 续期
    clock.advance(479)
    assert state.hot_active(1, settings=settings) is True  # 窗口被刷新过
    clock.advance(481)
    assert state.hot_active(1, settings=settings) is False  # 自然过期


def test_note_reply_streak_limit_exits_hot() -> None:
    clock = FakeClock()
    state = AutoChatState(clock=clock)
    settings = _hot_settings()  # streak limit = 3
    assert state.note_reply(1, settings=settings) == ""
    assert state.note_reply(1, settings=settings) == ""
    assert state.note_reply(1, settings=settings) == "hot_exit_limit"
    assert state.hot_active(1, settings=settings) is False


def test_set_backoff_forces_hot_exit() -> None:
    clock = FakeClock()
    state = AutoChatState(clock=clock)
    settings = _hot_settings()
    state.note_reply(1, settings=settings)
    assert state.hot_active(1, settings=settings) is True
    state.set_backoff(1, 1800.0)
    assert state.hot_active(1, settings=settings) is False


def test_limit_reason_cooldown_override_and_skip_hourly() -> None:
    from datetime import UTC, datetime

    clock = FakeClock()

    def fixed_bucket():
        return datetime(2026, 9, 17, tzinfo=UTC)

    state = AutoChatState(clock=clock, bucket_clock=fixed_bucket)
    settings = _settings(auto_chat_hourly_limit=1, auto_chat_daily_limit=99)
    assert state.acquire(1, settings=settings) == ""
    # 普通：300s 冷却内
    assert state.limit_reason(1, settings=settings) == "cooldown"
    # 热聊：覆盖为 30s 冷却并跳过小时上限
    clock.advance(31)
    assert (
        state.limit_reason(1, settings=settings, cooldown_seconds=30.0, skip_hourly=True)
        == ""
    )
    # 不跳过小时上限时，第 2 条触发 hourly_limit
    assert state.acquire(1, settings=settings, cooldown_seconds=30.0) == "hourly_limit"


def test_cold_daily_counter() -> None:
    from datetime import UTC, datetime

    def fixed_bucket():
        return datetime(2026, 9, 17, tzinfo=UTC)

    state = AutoChatState(bucket_clock=fixed_bucket)
    settings = _hot_settings()  # cold daily limit = 3
    assert state.cold_limit_reached(1, settings=settings) is False
    state.note_cold_reply(1, settings=settings)
    state.note_cold_reply(1, settings=settings)
    state.note_cold_reply(1, settings=settings)
    assert state.cold_limit_reached(1, settings=settings) is True


# ---- 二期：触发分类（S7-AUTO-P2-03）----


@pytest.mark.asyncio
async def test_you_plural_replies_without_gate() -> None:
    h = Harness(["你们谁去吃饭"], _run_settings(auto_chat_sample_rate=0.0))
    await _run(h, raw_text="你们谁去吃饭")
    assert h.sent == ["哈哈冲"]
    gate_calls = [c for c in h.completions if "决策器" in c["system_prompt"]]
    assert gate_calls == []  # 跳过决策门


@pytest.mark.asyncio
async def test_you_plural_disabled_falls_back_to_you_gate() -> None:
    """关闭准必回后，"你们"消息仍含"你"，回落到"你"判门路径而非采样。"""
    h = Harness(
        ["你们谁去吃饭"],
        _run_settings(auto_chat_sample_rate=0.0, auto_chat_you_plural_reply=False),
    )
    await _run(h, raw_text="你们谁去吃饭")
    gate_calls = [c for c in h.completions if "决策器" in c["system_prompt"]]
    assert len(gate_calls) == 1  # 不再准必回，但含"你"仍必进门判断
    assert h.sent == ["哈哈冲"]


@pytest.mark.asyncio
async def test_you_reference_goes_to_gate_even_with_zero_sample_rate() -> None:
    h = Harness(["在吗", "你觉得呢"], _run_settings(auto_chat_sample_rate=0.0))
    await _run(h, raw_text="你觉得呢")
    gate_calls = [c for c in h.completions if "决策器" in c["system_prompt"]]
    assert len(gate_calls) == 1  # 含"你"跳过采样必进门
    assert h.sent == ["哈哈冲"]


@pytest.mark.asyncio
async def test_hot_mode_skips_gate_and_sampling() -> None:
    h = Harness(["随便聊聊"], _run_settings(auto_chat_sample_rate=0.0))
    h.state.note_reply(1001, settings=h.settings)  # 人为制造热聊态
    await _run(h, raw_text="随便聊聊")
    gate_calls = [c for c in h.completions if "决策器" in c["system_prompt"]]
    assert gate_calls == []  # 不进门
    assert h.sent == ["哈哈冲"]


@pytest.mark.asyncio
async def test_hot_mode_second_quick_message_not_blocked_by_normal_cooldown() -> None:
    """热聊冷却须在前置 limit 守卫生效：距上次发言 60s（≥30s 热聊冷却、
    <300s 普通冷却）的第二条消息不被普通冷却拒绝。"""
    clock = FakeClock()
    h = Harness(
        ["第一条", "第二条"],
        _run_settings(
            auto_chat_cooldown_seconds=300.0,
            auto_chat_cold_daily_limit=0,
            auto_chat_hot_streak_limit=50,
        ),
        state=AutoChatState(clock=clock),
    )
    await _run(h, raw_text="第一条")
    clock.advance(60.0)
    await _run(h, raw_text="第二条")
    assert h.sent == ["哈哈冲", "哈哈冲"]


@pytest.mark.asyncio
async def test_note_reply_after_send_and_hot_exit_limit_metric() -> None:
    h = Harness(["聊"], _run_settings(auto_chat_hot_streak_limit=1))
    await _run(h)
    assert h.sent == ["哈哈冲"]
    # streak=1 达到 limit=1 → 发送前 note_reply 返回 hot_exit_limit 并退出热聊
    assert h.state.hot_active(1001, settings=h.settings) is False


# ---- 二期：prompt 渲染增强（S7-AUTO-P2-04）----


class TestPromptBotVisibility:
    def test_bot_reply_rendered_as_robot_line(self) -> None:
        rows = [_row("今天好累", user_id=2001, ai_reply="摸鱼一天真快乐")]
        prompt = build_casual_user_prompt(rows)
        assert "用户2001：今天好累" in prompt
        assert "机器人：摸鱼一天真快乐" in prompt

    def test_gate_prompt_lists_bot_replies_too(self) -> None:
        rows = [_row("你好", user_id=2001, row_id=1, ai_reply="你好呀")]
        prompt = build_gate_user_prompt(rows)
        assert "机器人：你好呀" in prompt

    def test_gate_system_prompt_has_you_rule(self) -> None:
        assert "机器人" in GATE_SYSTEM_PROMPT
        assert "你" in GATE_SYSTEM_PROMPT
        assert "视为 addressed，倾向回复" in GATE_SYSTEM_PROMPT


# ---- 二期：冷场反应（S7-AUTO-P2-05）----


class TestColdFollowup:
    @pytest.mark.asyncio
    async def test_cold_reply_when_nobody_talked(self) -> None:
        # 上限取 1：一次补话后 cold_limit_reached 即为 True（计数生效）
        h = Harness(["聊会"], _run_settings(auto_chat_cold_daily_limit=1))
        # rows created_at=2026-09-16 早于 bot_reply_time（now）→ 冷场成立
        await _run(h)
        await h.drain_cold()  # 冷场补话在后台 task 中，需等它跑完
        assert h.sent[0] == "哈哈冲"
        assert len(h.sent) == 2  # 原回复 + 冷场补话
        assert h.state.cold_limit_reached(1001, settings=h.settings) is True

    @pytest.mark.asyncio
    async def test_cold_ok_metric_recorded(self) -> None:
        """冷场补话成功必须记 cold/ok（曾因 send 抛 finish 控制流异常被误记为 cold/error）。"""
        h = Harness(["聊会"], _run_settings(auto_chat_cold_daily_limit=1))
        before = metrics.AUTO_CHAT.labels("cold", "ok")._value.get()
        await _run(h)
        await h.drain_cold()
        assert len(h.sent) == 2  # 确认补话真的发生了
        assert metrics.AUTO_CHAT.labels("cold", "ok")._value.get() == before + 1

    @pytest.mark.asyncio
    async def test_cold_skipped_when_someone_talked_after(self) -> None:
        h = Harness(["聊会"], _run_settings(auto_chat_cold_daily_limit=3))
        # 注入一条晚于 bot_reply_time 的新消息由 FakeMemory 返回：
        h.memory.late_row = _row(
            "我来接话",
            user_id=2002,
            row_id=99,
            created_at=(datetime.now(UTC) + timedelta(seconds=1)).isoformat(),
        )
        await _run(h)
        await h.drain_cold()  # 让后台 _check 真正跑完：跳过逻辑被破坏时会多发出补话
        assert len(h.sent) == 1  # 只有原回复，无补话

    @pytest.mark.asyncio
    async def test_cold_generation_failure_uses_fallback_pool(self) -> None:
        h = Harness(["聊会"], _run_settings(auto_chat_cold_daily_limit=3))
        h.cold_content = ""
        await _run(h)
        await h.drain_cold()  # 冷场补话在后台 task 中，需等它跑完
        assert len(h.sent) == 2
        assert h.sent[1] in COLD_FALLBACK_MESSAGES

    @pytest.mark.asyncio
    async def test_cold_sensitive_filtered(self) -> None:
        h = Harness(["聊会"], _run_settings(auto_chat_cold_daily_limit=3))
        h.cold_content = "来玩博彩吗"
        await _run(h)
        await h.drain_cold()  # 让后台 _check 真正跑完：敏感词过滤被破坏时会多发出补话
        assert len(h.sent) == 1
        assert h.state.cold_limit_reached(1001, settings=h.settings) is False

    @pytest.mark.asyncio
    async def test_cold_no_cascade(self) -> None:
        """补话本身不再调度新检查：sent 只有两条（原回复+补话）。"""
        h = Harness(["聊会"], _run_settings(auto_chat_cold_daily_limit=3))
        await _run(h)
        await h.drain_cold()  # 等待可能的后台任务完成
        assert len(h.sent) == 2

    def test_cold_user_prompt_renders_rows_and_instruction(self) -> None:
        rows = [_row("早", user_id=2001)]
        prompt = build_cold_user_prompt(rows, "早呀")
        assert "用户2001：早" in prompt
        assert "「早呀」" in prompt
        # 冷场指令标识：Harness.completion 三分路由依赖该词
        assert "没有人回应" in prompt

    @pytest.mark.asyncio
    async def test_schedule_cold_check_skips_at_zero_limit(self) -> None:
        h = Harness(["聊会"], _run_settings(auto_chat_cold_daily_limit=0))
        schedule_cold_check(
            group_id=1001,
            bot_reply_text="哈哈冲",
            bot_reply_time=datetime.now(UTC),
            settings=h.settings,
            memory_store=h.memory,
            send=h.send,
            quota_check=h.quota_check,
            client=None,
            state=h.state,
            rng=h.rng,
            _sleep=h.instant_cold_sleep,
        )
        await h.drain_cold()
        assert h.sent == []  # cold_daily_limit=0 → 立即 skip
        assert h.completions == []  # 未触达模型
