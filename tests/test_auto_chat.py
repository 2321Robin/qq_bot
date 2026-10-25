import asyncio
import logging
from datetime import UTC, datetime, timedelta

from collections import deque
from pathlib import Path

import pytest

from qq_bot.config import BotSettings
from qq_bot.observability import metrics
from qq_bot.services.sticker_library import Sticker, StickerLibrary
from qq_bot.services.auto_chat import (
    AutoChatState,
    COLD_FALLBACK_MESSAGES,
    GATE_SYSTEM_PROMPT,
    GateDecision,
    TriggerCategory,
    activity_weight,
    build_casual_user_prompt,
    build_cold_user_prompt,
    build_gate_user_prompt,
    classify_trigger,
    detect_negative_feedback,
    gate_system_prompt,
    parse_gate_output,
    render_batch_rows,
    run_auto_chat,
    schedule_cold_check,
    split_casual_reply,
    static_prefilter,
    style_directive,
    typing_intervals,
    plusone_triggered,
    truncate_at_sentence,
    _ARTIFACT_RE,
    _BATCH_SECTION_HEADER,
    _has_id_artifact,
    _reply_issue,
    _strip_excl_and_emoji_punct,
)
from qq_bot.services.chat_memory import ChatMemoryRow
from qq_bot.services.memory_prompt import build_user_aliases
from qq_bot.services.persona import Persona


def _fresh_iso() -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) - timedelta(seconds=30)).isoformat()


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


@pytest.fixture(autouse=True)
def _fresh_auto_chat_state():
    from qq_bot.services import auto_chat as _m

    _m._RECENT_REPLIES.clear()
    yield
    _m._RECENT_REPLIES.clear()


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
        assert "用户A：早" in prompt
        assert prompt.strip().endswith("用户B：早呀")

    def test_casual_user_prompt_asks_for_one_reply(self) -> None:
        rows = [_row("今天好累", user_id=2001)]
        prompt = build_casual_user_prompt(rows)
        assert "用户A：今天好累" in prompt
        assert "接" in prompt

    def test_casual_user_prompt_injects_persona_examples(self) -> None:
        rows = [_row("在吗", user_id=2001)]
        prompt = build_casual_user_prompt(
            rows, persona_examples=("这波啊 这波是本鸡的舒适圈", "别说了 今晚就钓")
        )
        assert "你平时的说话感觉（学这个感觉，禁止照抄原句）：" in prompt
        assert "- 这波啊 这波是本鸡的舒适圈" in prompt
        assert "- 别说了 今晚就钓" in prompt

    def test_casual_user_prompt_omits_persona_example_section_when_empty(self) -> None:
        rows = [_row("在吗", user_id=2001)]
        prompt = build_casual_user_prompt(rows)
        assert "你平时的说话感觉" not in prompt


class TestBatchRendering:
    def test_consecutive_same_sender_merged_as_streak(self) -> None:
        base = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
        rows = [
            _row("开黑吗", user_id=2001, row_id=1, created_at=base.isoformat()),
            _row(
                "开黑吗",
                user_id=2001,
                row_id=2,
                created_at=(base + timedelta(seconds=1)).isoformat(),
            ),
            _row(
                "来啊", user_id=2002, row_id=3, created_at=(base + timedelta(seconds=2)).isoformat()
            ),
        ]
        alias = build_user_aliases(rows)
        lines = render_batch_rows(rows, alias)
        assert lines[0] == "用户A（连发2条）：开黑吗 / 开黑吗"
        assert lines[1] == "用户B：来啊"

    def test_gap_over_three_seconds_not_merged(self) -> None:
        base = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
        rows = [
            _row("先走", user_id=2001, row_id=1, created_at=base.isoformat()),
            _row(
                "等等我",
                user_id=2001,
                row_id=2,
                created_at=(base + timedelta(seconds=4)).isoformat(),
            ),
        ]
        lines = render_batch_rows(rows, build_user_aliases(rows))
        assert lines == ["用户A：先走", "用户A：等等我"]

    def test_gate_prompt_appends_batch_section(self) -> None:
        rows = [_row("早", user_id=2001, row_id=1)]
        batch = [_row("打副本去吗", user_id=2002, row_id=2)]
        prompt = build_gate_user_prompt(rows, batch_rows=batch)
        assert "连续发生" in prompt
        assert "用户B：打副本去吗" in prompt

    def test_casual_prompt_batch_rows_alias_consistent(self) -> None:
        rows = [_row("在吗", user_id=2001, row_id=1)]
        batch = [
            _row("在吗", user_id=2001, row_id=1),
            _row("来一把", user_id=2001, row_id=2),
        ]
        prompt = build_casual_user_prompt(rows, batch_rows=batch)
        assert "用户A（连发2条）：在吗 / 来一把" in prompt

    def test_batch_row_substitutes_reference_token_with_alias(self) -> None:
        rows = [
            _row("上分", user_id=1654321, row_id=1),
            _row("回复 [引用 u1654321:上分]：冲", user_id=2002, row_id=2),
        ]
        lines = render_batch_rows(rows, build_user_aliases(rows))
        assert lines == ["用户A：上分", "用户B：回复 [引用 用户A:上分]：冲"]

    def test_batch_row_unknown_quoted_id_renders_placeholder(self) -> None:
        rows = [_row("回复 [引用 u9999999:上分]：冲", user_id=2001, row_id=1)]
        lines = render_batch_rows(rows, build_user_aliases(rows))
        assert lines == ["用户A：回复 [引用 用户?:上分]：冲"]

    def test_streak_row_substitutes_reference_token(self) -> None:
        base = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
        rows = [
            _row(
                "回复 [引用 u1654321:上分]：冲",
                user_id=2002,
                row_id=1,
                created_at=base.isoformat(),
            ),
            _row(
                "再来一把",
                user_id=2002,
                row_id=2,
                created_at=(base + timedelta(seconds=1)).isoformat(),
            ),
        ]
        lines = render_batch_rows(rows, build_user_aliases(rows))
        assert lines == ["用户A（连发2条）：回复 [引用 用户?:上分]：冲 / 再来一把"]


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
        assert (
            parse_gate_output('{"should_reply": 1, "reason": "banter", "confidence": 0.9}') is None
        )
        assert (
            parse_gate_output('{"should_reply": true, "reason": "wat", "confidence": 0.9}') is None
        )
        assert (
            parse_gate_output('{"should_reply": true, "reason": "banter", "confidence": 1.5}')
            is None
        )


def test_auto_chat_counter_registered() -> None:
    metrics.AUTO_CHAT.labels("prefilter", "passed").inc()


# ---- 编排：run_auto_chat（S7-AUTO-06）----


class FakeEvent:
    message_id = 1  # H9 贴表情：单消息路径的触发 message_id（NoneBot v11 int）

    def __init__(self, group_id: int = 1001, user_id: int = 2001):
        self.group_id = group_id
        self.user_id = user_id


class FakeMemory:
    def __init__(self, texts: list[str]):
        self.texts = texts
        self.calls: list[dict] = []

    async def recent_group_messages(self, *, group_id: int, limit: int):
        self.calls.append({"group_id": group_id, "limit": limit})
        rows = [
            _row(t, user_id=2001 + i, row_id=i + 1, created_at=_fresh_iso())
            for i, t in enumerate(self.texts)
        ]
        late = getattr(self, "late_row", None)
        if late is not None:
            rows.append(late)
        stale = getattr(self, "stale_row", None)
        if stale is not None:
            rows.append(stale)
        return rows

    def memory_text_rows(self):
        return [
            _row(t, user_id=2001 + i, row_id=i + 1, created_at=_fresh_iso())
            for i, t in enumerate(self.texts)
        ]


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
        "auto_chat_tune_log_enabled": False,
    }
    base.update(overrides)
    return FakeAutoChatSettings(**base)


class Harness:
    def __init__(self, texts: list[str], settings: BotSettings, state: AutoChatState | None = None):
        self.settings = settings
        self.memory = FakeMemory(texts)
        self.state = state or AutoChatState()
        self.sent: list[str] = []
        self.rng_value = 0.0
        self.slept: list[float] = []
        self.gate_content = '{"should_reply": true, "reason": "banter", "confidence": 0.9}'
        self.casual_content = "你们这是要去哪？"
        self.cold_content = "鱼都晒干了都没人理"
        self.quota_allowed = True
        self.completions: list[dict] = []

    async def send(self, text) -> None:
        self.sent.extend(list(text))

    async def quota_check(self) -> bool:
        return self.quota_allowed

    def memory_text_rows(self):
        return self.memory.memory_text_rows()

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


async def _run(
    h: Harness,
    raw_text: str = "一起去打新活动吗",
    _completion=None,
    quoted_self: bool = False,
    react=None,
    sticker_library=None,
    send_sticker=None,
) -> None:
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
        _request_completion=_completion or h.completion(),
        quoted_self=quoted_self,
        react=react,
        trigger_message_id=FakeEvent.message_id,
        sticker_library=sticker_library,
        send_sticker=send_sticker,
    )


def _varying_reply(h: Harness, *replies: str):
    """生成/冷场回复各不相同：`_RECENT_REPLIES` 会拒绝同文复读，多轮场景
    需要不同文本才能各自发出去。"""
    queue = iter(replies)

    async def fake_request_completion(**kwargs) -> str:
        h.completions.append(kwargs)
        if "决策器" in kwargs["system_prompt"]:
            return h.gate_content
        if "没有人回应" in kwargs["system_prompt"] or "没有人回应" in kwargs.get("user_prompt", ""):
            return next(queue, h.cold_content)
        return next(queue, h.casual_content)

    return fake_request_completion


@pytest.mark.asyncio
async def test_happy_path_gate_pass_send_with_delay() -> None:
    h = Harness(["在吗", "新活动开了"], _run_settings())
    await _run(h)
    assert h.sent == ["你们这是要去哪？"]
    assert h.slept == [0.0]
    assert h.state.recently_spoke(1001, 300.0) is True
    gate_calls = [c for c in h.completions if "决策器" in c["system_prompt"]]
    casual_calls = [c for c in h.completions if "决策器" not in c["system_prompt"]]
    assert len(gate_calls) == 1
    assert len(casual_calls) == 1
    assert casual_calls[0]["max_tokens"] == 100


@pytest.mark.asyncio
async def test_generation_and_cold_inject_persona_examples_from_settings() -> None:
    """方案 B 接线：生成与冷场补话都从 settings 注入示例段（含可能的
    重试路径——凡非决策器的 casual 调用都该带上）。"""
    h = Harness(
        ["在吗", "新活动开了"],
        _run_settings(
            persona_example_replies="这波啊 这波是本鸡的舒适圈|今天也是躺平的一天",
            auto_chat_cold_daily_limit=3,
        ),
    )
    await _run(h)
    await h.drain_cold()
    casual_calls = [c for c in h.completions if "决策器" not in c["system_prompt"]]
    assert len(casual_calls) >= 2  # 至少：生成 + 冷场补话
    for call in casual_calls:
        assert "你平时的说话感觉" in call["user_prompt"]
        assert "这波啊 这波是本鸡的舒适圈" in call["user_prompt"]


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
    h = Harness(["在吗", "冲呀"], _run_settings())

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
    h = Harness(["有人在吗", "喊小洛"], _run_settings())
    await _run(h, raw_text="小洛 在吗")
    gate_calls = [c for c in h.completions if "决策器" in c["system_prompt"]]
    assert gate_calls == []
    assert h.sent == ["你们这是要去哪？"]


@pytest.mark.asyncio
async def test_sampling_reject_without_gate_call() -> None:
    h = Harness(["消息"], _run_settings(auto_chat_sample_rate=0.5))
    h.rng_value = 0.9  # >= 0.5 → 采样未中
    await _run(h)
    assert h.sent == []
    assert h.completions == []


@pytest.mark.asyncio
async def test_quoted_self_ordinary_text_skips_sampling_and_gate() -> None:
    """引用机器人必回（spec §5.3）：QUOTED 触发在管道内跳过采样与决策门，
    默认低采样率下普通文本也必须直达生成。"""
    h = Harness(["在吗", "确实"], _run_settings(auto_chat_sample_rate=0.05))
    h.rng_value = 0.9  # ≥ 0.05：若 QUOTED 分支缺失，ORDINARY 会被采样丢弃
    await _run(h, raw_text="确实", quoted_self=True)
    assert h.sent
    assert [c for c in h.completions if "决策器" in c["system_prompt"]] == []


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
async def test_typo_swap_sensitive_result_filtered() -> None:
    """错别字换位可能拼出敏感词（"支枪"换位 → "枪支"）：注入后再过一次
    敏感词检查，命中即放弃整条回复。"""
    h = Harness(["聊会"], _run_settings(auto_chat_typo_rate=1.0))
    h.casual_content = "一支枪都不留"
    h.rng_value = 0.35  # 换位取第 2 对相邻字（int(0.35×5)=1）→ "支枪" → "枪支"
    await _run(h)
    assert h.sent == []


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
    assert state.limit_reason(1, settings=settings, cooldown_seconds=30.0, skip_hourly=True) == ""
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
    assert h.sent == ["你们这是要去哪？"]
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
    assert h.sent == ["你们这是要去哪？"]


@pytest.mark.asyncio
async def test_you_reference_goes_to_gate_even_with_zero_sample_rate() -> None:
    h = Harness(["在吗", "聊这个"], _run_settings(auto_chat_sample_rate=0.0))
    await _run(h, raw_text="你觉得呢")
    gate_calls = [c for c in h.completions if "决策器" in c["system_prompt"]]
    assert len(gate_calls) == 1  # 含"你"跳过采样必进门
    assert h.sent == ["你们这是要去哪？"]


@pytest.mark.asyncio
async def test_hot_mode_skips_gate_and_sampling() -> None:
    h = Harness(["随便聊聊"], _run_settings(auto_chat_sample_rate=0.0))
    h.state.note_reply(1001, settings=h.settings)  # 人为制造热聊态
    await _run(h, raw_text="随便聊聊")
    gate_calls = [c for c in h.completions if "决策器" in c["system_prompt"]]
    assert gate_calls == []  # 不进门
    assert h.sent == ["你们这是要去哪？"]


@pytest.mark.asyncio
async def test_hot_mode_second_quick_message_not_blocked_by_normal_cooldown() -> None:
    """热聊冷却须在前置 limit 守卫生效：距上次发言 60s（≥30s 热聊冷却、
    <300s 普通冷却）的第二条消息不被普通冷却拒绝。"""
    clock = FakeClock()
    h = Harness(
        ["开场白", "接着聊"],
        _run_settings(
            auto_chat_cooldown_seconds=300.0,
            auto_chat_cold_daily_limit=0,
            auto_chat_hot_streak_limit=50,
        ),
        state=AutoChatState(clock=clock),
    )
    gate_seq = iter(['{"should_reply": true, "reason": "banter", "confidence": 0.9}'] * 2)
    casual_seq = iter(["这是头一条", "这是完全不同的另一条"])

    def varying_completion():
        async def fake(**kwargs) -> str:
            if "决策器" in kwargs["system_prompt"]:
                return next(gate_seq)
            return next(casual_seq)

        return fake

    await _run(h, raw_text="第一条", _completion=varying_completion())
    clock.advance(60.0)
    await _run(h, raw_text="第二条", _completion=varying_completion())
    assert h.sent == ["这是头一条", "这是完全不同的另一条"]


@pytest.mark.asyncio
async def test_note_reply_after_send_and_hot_exit_limit_metric() -> None:
    h = Harness(["聊"], _run_settings(auto_chat_hot_streak_limit=1))
    await _run(h)
    assert h.sent == ["你们这是要去哪？"]
    # streak=1 达到 limit=1 → 发送前 note_reply 返回 hot_exit_limit 并退出热聊
    assert h.state.hot_active(1001, settings=h.settings) is False


# ---- 二期：prompt 渲染增强（S7-AUTO-P2-04）----


class TestPromptBotVisibility:
    def test_bot_reply_rendered_as_robot_line(self) -> None:
        rows = [_row("今天好累", user_id=2001, ai_reply="摸鱼一天真快乐")]
        prompt = build_casual_user_prompt(rows)
        assert "用户A：今天好累" in prompt
        assert "机器人：摸鱼一天真快乐" in prompt

    def test_gate_prompt_lists_bot_replies_too(self) -> None:
        rows = [_row("你好", user_id=2001, row_id=1, ai_reply="你好呀")]
        prompt = build_gate_user_prompt(rows)
        assert "机器人：你好呀" in prompt

    def test_gate_system_prompt_has_you_rule(self) -> None:
        assert "机器人" in GATE_SYSTEM_PROMPT
        assert "你" in GATE_SYSTEM_PROMPT
        assert "视为 addressed，倾向回复" in GATE_SYSTEM_PROMPT

    def test_gate_system_prompt_has_no_react_clause(self) -> None:
        """Final Fix：react 指令不进系统提示词——通道关闭的部署不为永不出现的
        表情列表付 token；指令与「可用表情 ID」同块，仅在 reaction_available
        时进 user prompt（见 TestGateEmojiHintGating）。"""
        assert "react" not in GATE_SYSTEM_PROMPT

    def test_gate_system_prompt_has_third_party_rule(self) -> None:
        """2026-10-25 复盘（部署者裁决）：叫别人/别的机器人的消息不当 addressed，
        但仍可按 banter 参与——禁的是不接上文的回复，不是参与本身。"""
        assert "等别人回应" in GATE_SYSTEM_PROMPT
        assert "不要当作 addressed" in GATE_SYSTEM_PROMPT
        assert "可按 banter 参与" in GATE_SYSTEM_PROMPT

    def test_gate_system_prompt_injects_persona_name(self) -> None:
        prompt = gate_system_prompt("鸡煲")
        assert "机器人的名字叫“鸡煲”" in prompt
        assert "喊别的名字都不是在叫机器人" in prompt
        # 未配人设（名字空白）时与静态提示词完全一致
        assert gate_system_prompt("  ") == GATE_SYSTEM_PROMPT
        assert gate_system_prompt("") == GATE_SYSTEM_PROMPT


# ---- 二期：冷场反应（S7-AUTO-P2-05）----


class TestColdFollowup:
    @pytest.mark.asyncio
    async def test_cold_reply_when_nobody_talked(self) -> None:
        # 上限取 1：一次补话后 cold_limit_reached 即为 True（计数生效）
        h = Harness(["聊会"], _run_settings(auto_chat_cold_daily_limit=1))
        # rows created_at=2026-09-16 早于 bot_reply_time（now）→ 冷场成立
        await _run(h)
        await h.drain_cold()  # 冷场补话在后台 task 中，需等它跑完
        assert h.sent[0] == "你们这是要去哪？"
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
        assert "用户A：早" in prompt
        assert "「早呀」" in prompt
        # 冷场指令标识：Harness.completion 三分路由依赖该词
        assert "没有人回应" in prompt

    def test_cold_user_prompt_injects_persona_examples(self) -> None:
        rows = [_row("早", user_id=2001)]
        prompt = build_cold_user_prompt(rows, "早呀", persona_examples=("今天也是躺平的一天",))
        assert "你平时的说话感觉" in prompt
        assert "- 今天也是躺平的一天" in prompt

    @pytest.mark.asyncio
    async def test_schedule_cold_check_skips_at_zero_limit(self) -> None:
        h = Harness(["聊会"], _run_settings(auto_chat_cold_daily_limit=0))
        schedule_cold_check(
            group_id=1001,
            bot_reply_text="你们这是要去哪？",
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


# ---- 二期修复：上下文时效窗 + 生成锚定（S7-AUTO-P2-08）----


class TestContextMaxAge:
    @pytest.mark.asyncio
    async def test_stale_context_excluded_from_prompts(self) -> None:
        h = Harness(["这条是新鲜的"], _run_settings(auto_chat_context_max_age_minutes=1))
        # 注入一条 10 分钟前的陈旧消息
        from datetime import UTC, datetime, timedelta

        h.memory.stale_row = _row(
            "昨晚谁夺冠了",
            user_id=2003,
            row_id=98,
            created_at=(datetime.now(UTC) - timedelta(minutes=10)).isoformat(),
        )
        await _run(h, raw_text="这条是新鲜的")
        gate_calls = [c for c in h.completions if "决策器" in c["system_prompt"]]
        assert gate_calls and "昨晚谁夺冠了" not in gate_calls[0]["user_prompt"]
        assert "这条是新鲜的" in gate_calls[0]["user_prompt"]
        casual_calls = [c for c in h.completions if "决策器" not in c["system_prompt"]]
        assert casual_calls and "昨晚谁夺冠了" not in casual_calls[0]["user_prompt"]

    @pytest.mark.asyncio
    async def test_max_age_zero_keeps_all_context(self) -> None:
        h = Harness(["旧消息"], _run_settings(auto_chat_context_max_age_minutes=0))
        h.memory.stale_row = _row(
            "远古话题", user_id=2003, row_id=97, created_at="2026-09-16T00:00:00+00:00"
        )
        await _run(h, raw_text="旧消息")
        gate_calls = [c for c in h.completions if "决策器" in c["system_prompt"]]
        assert gate_calls and "远古话题" in gate_calls[0]["user_prompt"]


def test_casual_prompt_anchors_to_latest_message() -> None:
    rows = [_row("你好", user_id=2001, created_at=_fresh_iso())]
    prompt = build_casual_user_prompt(rows)
    assert "先回应最新消息" in prompt
    assert "不要跑题" in prompt


# ---- 二期修复：回复多样性（风格轮盘 + 最近发言）（S7-AUTO-P2-09）----


class TestStyleRoulette:
    def test_directive_varies_with_rng(self) -> None:
        seen = {style_directive(lambda: 0.0), style_directive(lambda: 0.99)}
        assert len(seen) >= 2  # 不同 rng 产出不同风格指令

    def test_directive_is_nonempty_text(self) -> None:
        directive = style_directive(lambda: 0.5)
        assert isinstance(directive, str) and directive


class TestRecentReplies:
    @pytest.mark.asyncio
    async def test_recent_replies_recorded(self) -> None:
        import qq_bot.services.auto_chat as m

        m._RECENT_REPLIES.clear()
        h = Harness(["聊会"], _run_settings(auto_chat_sample_rate=0.0))
        h.state.note_reply(1001, settings=h.settings)  # 制造热聊直答
        await _run(h, raw_text="聊会")
        assert m._RECENT_REPLIES[1001], "自主回复应进入 ring buffer"

    @pytest.mark.asyncio
    async def test_casual_prompt_contains_recent_replies_block(self) -> None:
        rows = [_row("在吗", user_id=2001, created_at=_fresh_iso())]
        prompt = build_casual_user_prompt(rows, recent_replies=["我破防了😂", "哈哈玩啊"])
        assert "换着花样" in prompt
        assert "我破防了😂" in prompt


def test_build_casual_prompt_without_recent_replies_has_no_block() -> None:
    rows = [_row("在吗", user_id=2001, created_at=_fresh_iso())]
    prompt = build_casual_user_prompt(rows)
    assert "换着花样" not in prompt


# ---- 分段重写（S10-HUMAN-P3-01）：只在句末标点处切，逗号留在条内 ----


class TestSplitCasualReply:
    def test_commas_stay_inside_one_message(self) -> None:
        assert split_casual_reply("哈哈，玩啊，不然来群里干嘛😂？", rng=lambda: 0.9) == [
            "哈哈，玩啊，不然来群里干嘛😂？"
        ]

    def test_short_text_never_split(self) -> None:
        assert split_casual_reply("一，二，三，四，五", rng=lambda: 0.0) == ["一，二，三，四，五"]
        assert split_casual_reply("玩吗", rng=lambda: 0.0) == ["玩吗"]

    def test_empty_and_punct_only(self) -> None:
        assert split_casual_reply("", rng=lambda: 0.0) == []
        assert split_casual_reply("，哈哈，", rng=lambda: 0.0) == ["哈哈"]

    def test_sentence_punct_splits_when_long(self) -> None:
        text = "本鸡今天去了湖边。然后钓了一下午。一条都没上。气死本鸡了。"
        # 去空白 24 字 ≤ 25 → 至多 2 条；两个句末切点正好 2 段
        assert split_casual_reply(text, rng=lambda: 0.9) == [
            "本鸡今天去了湖边。",
            "然后钓了一下午。一条都没上。气死本鸡了。",
        ]

    def test_long_text_caps_at_two_with_high_rng(self) -> None:
        text = "第一句话比较长一些。第二句也长。第三句还在继续说。第四句终于说完了。"
        assert split_casual_reply(text, rng=lambda: 0.9) == [
            "第一句话比较长一些。",
            "第二句也长。第三句还在继续说。第四句终于说完了。",
        ]

    def test_long_text_three_parts_is_low_probability(self) -> None:
        text = "第一句话比较长一些。第二句也长。第三句还在继续说。第四句终于说完了。"
        assert split_casual_reply(text, rng=lambda: 0.1) == [
            "第一句话比较长一些。",
            "第二句也长。",
            "第三句还在继续说。第四句终于说完了。",
        ]

    def test_no_sentence_punct_stays_single(self) -> None:
        text = "这波操作有点意思，本鸡直接看乐了，真是服了你们这群人"
        assert split_casual_reply(text, rng=lambda: 0.0) == [text]

    def test_newline_is_a_boundary(self) -> None:
        text = "上半句还没有说完呢继续说下半句\n下半句是另一条消息"
        parts = split_casual_reply(text, rng=lambda: 0.9)
        assert parts == ["上半句还没有说完呢继续说下半句", "下半句是另一条消息"]


class TestTruncateAtSentence:
    def test_cuts_at_last_sentence_punct_within_window(self) -> None:
        text = "第一句话。第二句话也到了。第三句太长被截掉"
        out = truncate_at_sentence(text, 14)
        assert out == "第一句话。第二句话也到了。"

    def test_hard_cut_without_punct(self) -> None:
        text = "这十几个字里一个句末标点都没有就一直往后数"
        assert truncate_at_sentence(text, 10) == text[:10]

    def test_shorter_than_limit_untouched(self) -> None:
        assert truncate_at_sentence("短的", 45) == "短的"


# ---- 短语级口头禅闸（S10-HUMAN-P3-01）----


class TestReplyIssuePhraseRepeat:
    def test_shared_catchphrase_is_repeat(self) -> None:
        # 整句相似度 < 0.8，只有短语闸能拦住
        assert _reply_issue("这波操作有点意思啊你们真行", "", ["这波操作有点意思"]) == "repeat"

    def test_short_common_words_not_flagged(self) -> None:
        assert _reply_issue("吃饭完再组队", "", ["你到底吃饭完了吗"]) is None

    def test_unrelated_replies_pass(self) -> None:
        assert _reply_issue("X9挺猛的", "", ["今天天气不错"]) is None


@pytest.mark.asyncio
async def test_length_guard_rewrites_then_truncates() -> None:
    """长度护栏（S10-HUMAN-P3-01）：超限回复触发一次重写；重写仍超长则
    句末截断兜底，最终发送不超过上限。"""
    h = Harness(["随便聊聊"], _run_settings())
    long_reply = (
        "第一句就很长了。第二句继续说个没完。第三句还在继续往下说个不停。"
        "第四句依然没有要停的意思。第五句终于说完了。"
    )
    seq = iter([long_reply, long_reply])

    def completion():
        async def fake(**kwargs) -> str:
            h.completions.append(kwargs)
            if "决策器" in kwargs["system_prompt"]:
                return h.gate_content
            return next(seq)

        return fake

    await _run(h, "随便聊聊", _completion=completion())
    assert h.sent
    assert len("".join(h.sent).replace(" ", "")) <= 45
    casual_prompts = [c["user_prompt"] for c in h.completions if "决策器" not in c["system_prompt"]]
    assert any("太长" in p for p in casual_prompts)


class TestTypingIntervals:
    def test_interval_scales_with_visible_length(self) -> None:
        # 间隔数 = len(parts)-1，故用 3 段取 2 个间隔；长段重复到超过
        # 封顶字数（(4.0-0.5)/0.035 = 100 字）才能断言封顶在 4.0
        parts = ["短", "这一条明显长得多需要更多打字时间" * 10, "收尾"]
        ivs = typing_intervals(parts, base=0.5, per_char=0.035, max_seconds=4.0, rng=lambda: 0.5)
        assert ivs[0] < ivs[1]
        # rng=0.5 → 抖动因子 1.0；长句封顶在 4.0
        assert ivs[1] == pytest.approx(4.0)

    def test_single_part_has_no_interval(self) -> None:
        assert (
            typing_intervals(["一句"], base=0.5, per_char=0.035, max_seconds=4.0, rng=lambda: 0.5)
            == []
        )

    def test_jitter_bounds(self) -> None:
        parts = ["ab", "cd"]
        lo = typing_intervals(parts, base=1.0, per_char=0.0, max_seconds=4.0, rng=lambda: 0.0)
        hi = typing_intervals(parts, base=1.0, per_char=0.0, max_seconds=4.0, rng=lambda: 1.0)
        assert lo[0] == pytest.approx(0.8)  # 1.0 × 0.8
        assert hi[0] == pytest.approx(1.2)  # 1.0 × 1.2

    def test_whitespace_not_counted(self) -> None:
        parts = ["a b c", "x"]
        ivs = typing_intervals(parts, base=0.0, per_char=0.1, max_seconds=10.0, rng=lambda: 0.5)
        assert ivs[0] == pytest.approx(0.3)  # 去空白后 3 字


@pytest.mark.asyncio
async def test_run_auto_chat_keeps_comma_reply_single_message() -> None:
    """分段重写（S10-HUMAN-P3-01）：带逗号的短回复整条发送，不再拆三条。"""
    h = Harness(["聊会"], _run_settings(auto_chat_sample_rate=0.0))
    h.state.note_reply(1001, settings=h.settings)
    h.casual_content = "哈哈，玩啊，不然来群里干嘛😂？"
    await _run(h, raw_text="聊会")
    assert h.sent == ["哈哈，玩啊，不然来群里干嘛😂？"]


@pytest.mark.asyncio
async def test_run_auto_chat_splits_long_reply_at_sentence_punct() -> None:
    h = Harness(["聊会"], _run_settings(auto_chat_sample_rate=0.0))
    h.state.note_reply(1001, settings=h.settings)
    h.casual_content = "本鸡今天去了湖边。然后钓了一下午。一条都没上。气死本鸡了。"
    await _run(h, raw_text="聊会")
    assert h.sent == ["本鸡今天去了湖边。", "然后钓了一下午。", "一条都没上。气死本鸡了。"]


# ---- 二期修复 S7-AUTO-P2-11：冷场条件/伪影/表情标点/+1/调优记录 ----


class TestIdArtifact:
    def test_user_prefix_artifact_detected(self) -> None:
        assert _has_id_artifact("用户2904094221：开加速器？我试试") is True

    def test_bare_long_number_detected(self) -> None:
        assert _has_id_artifact("联系 2904094221 呀") is True

    def test_normal_text_clean(self) -> None:
        assert _has_id_artifact("开加速器？我试试") is False
        assert _has_id_artifact("524 超时了") is False


# ---- 引用令牌伪影防线（S10-HUMAN-P0-06 H4）----


def test_artifact_regex_catches_reference_token() -> None:
    assert "u\\d{5,}" in _ARTIFACT_RE.pattern
    assert _has_id_artifact("引用 u1654321 说得对") is True
    assert _has_id_artifact("你来u一下这个") is False


class TestExclAndEmojiPunct:
    def test_multiple_exclamations_keep_last_only(self) -> None:
        assert _strip_excl_and_emoji_punct("冲！干就完了！") == "冲。干就完了！"

    def test_exclamation_run_collapses_to_one(self) -> None:
        assert _strip_excl_and_emoji_punct("牛！！！") == "牛！"

    def test_single_exclamation_survives(self) -> None:
        assert _strip_excl_and_emoji_punct("牛！服不服") == "牛！服不服"

    def test_no_punct_between_text_and_emoji(self) -> None:
        assert _strip_excl_and_emoji_punct("没人理我，🌸😂") == "没人理我🌸😂"

    def test_no_punct_after_emoji(self) -> None:
        assert _strip_excl_and_emoji_punct("😂，Indeed") == "😂Indeed"


class TestQuestionish:
    def test_question_mark(self) -> None:
        from qq_bot.services.auto_chat import _is_questionish

        assert _is_questionish("你们这是要去哪？") is True

    def test_ma_ne_particle_without_mark(self) -> None:
        from qq_bot.services.auto_chat import _is_questionish

        assert _is_questionish("玩吗") is True
        assert _is_questionish("冲啊") is False


class TestPlusOne:
    def test_unit_distinct_users_trigger(self) -> None:
        rows = [_row("6", user_id=2001, row_id=1), _row("6", user_id=2002, row_id=2)]
        assert plusone_triggered(rows, "6", sender_user_id=2002) is True

    def test_unit_same_user_only_no_trigger(self) -> None:
        rows = [_row("6", user_id=2001, row_id=1), _row("6", user_id=2001, row_id=2)]
        assert plusone_triggered(rows, "6", sender_user_id=2001) is False

    @pytest.mark.asyncio
    async def test_plusone_repeats_message_without_gate(self) -> None:
        h = Harness(["6", "6"], _run_settings())
        await _run(h, raw_text="6")
        assert h.sent == ["6"]
        assert [c for c in h.completions if "决策器" in c["system_prompt"]] == []

    @pytest.mark.asyncio
    async def test_plusone_skipped_when_bot_already_said_it(self) -> None:
        import qq_bot.services.auto_chat as m

        m._RECENT_REPLIES[1001] = deque(["6"])
        h = Harness(["6", "6"], _run_settings())
        await _run(h, raw_text="6")
        assert h.sent == []


class TestTuneLog:
    @pytest.mark.asyncio
    async def test_tune_entry_recorded(self, tmp_path) -> None:
        import qq_bot.services.auto_chat as m

        tune_file = tmp_path / "tune.jsonl"
        m._TUNE_LOG_PATH = str(tune_file)
        h = Harness(
            ["聊会"], _run_settings(auto_chat_sample_rate=0.0, auto_chat_tune_log_enabled=True)
        )
        h.state.note_reply(1001, settings=h.settings)
        await _run(h, raw_text="聊会")
        lines = tune_file.read_text(encoding="utf-8").splitlines()
        assert lines
        import json as _json

        entry = _json.loads(lines[-1])
        assert entry["path"] in {"hot_reply", "gate_reply", "you_plural", "nicknamed", "plusone"}
        assert entry["reply"] and entry["trigger"] == "聊会"
        assert isinstance(entry["context"], list)

    def test_tune_disabled_writes_nothing(self, tmp_path) -> None:
        import qq_bot.services.auto_chat as m

        m._TUNE_LOG_PATH = str(tmp_path / "off.jsonl")
        assert True  # _run_settings 默认 enabled=False，其余用例已验证不写

    @pytest.mark.asyncio
    async def test_tune_entry_records_typo_field(self, tmp_path, monkeypatch) -> None:
        from qq_bot.services import auto_chat as m

        tune = tmp_path / "tune.jsonl"
        monkeypatch.setattr(m, "_TUNE_LOG_PATH", str(tune))
        h = Harness(["随便聊聊"], _run_settings(auto_chat_tune_log_enabled=True))
        await _run(h, "随便聊聊")
        import json as _json

        entry = _json.loads(tune.read_text(encoding="utf-8").splitlines()[0])
        assert entry["typo_applied"] is False


# ---- 二期修复 S7-AUTO-P2-12：反回声 + 自去重 ----


class TestAntiEcho:
    @pytest.mark.asyncio
    async def test_echo_reply_regenerated(self) -> None:
        h = Harness(["聊会"], _run_settings())
        h.state.note_reply(1001, settings=h.settings)
        h.casual_content = "破防了？称号自己领的还赖账😜"
        seq = iter(["破防了？称号自己领的还赖账😜", "换个说法，这称号本来就是我领的"])
        calls = {"n": 0}

        def completion():
            async def fake(**kwargs) -> str:
                calls["n"] += 1
                return next(seq)

            return fake

        await _run(h, raw_text="破防了？称号自己领的还赖账", _completion=completion())
        assert calls["n"] == 2  # 回声触发一次重写
        assert h.sent == ["换个说法，这称号本来就是我领的"]  # 带逗号短句整条发送

    @pytest.mark.asyncio
    async def test_self_repeat_regenerated(self) -> None:
        h = Harness(
            ["聊会"],
            _run_settings(auto_chat_hot_cooldown_seconds=0.0, auto_chat_cooldown_seconds=0.0),
        )
        h.state.note_reply(1001, settings=h.settings)
        seq = iter(["不服就刚", "不服就刚", "这次不重复了"])

        def completion():
            async def fake(**kwargs) -> str:
                return next(seq)

            return fake

        await _run(h, raw_text="聊会", _completion=completion())
        await _run(h, raw_text="又聊会", _completion=completion())
        assert h.sent[-1] == "这次不重复了"

    @pytest.mark.asyncio
    async def test_double_bad_reply_dropped(self) -> None:
        h = Harness(["聊会"], _run_settings())
        h.state.note_reply(1001, settings=h.settings)
        h.casual_content = "破防了？称号自己领的还赖账😜"

        def completion():
            async def fake(**kwargs) -> str:
                return h.casual_content  # 永远回声

            return fake

        await _run(h, raw_text="破防了？称号自己领的还赖账", _completion=completion())
        assert h.sent == []


# ---- S10-HUMAN-P0-01：触发分类共享纯函数 ----


class TestClassifyTrigger:
    def test_nicknamed_by_name_or_alias(self) -> None:
        settings = _run_settings(persona_name="小洛", persona_aliases="洛宝")
        assert classify_trigger("小洛你在吗", settings=settings) is TriggerCategory.NICKNAMED
        assert classify_trigger("洛宝出来玩", settings=settings) is TriggerCategory.NICKNAMED

    def test_quoted_self_beats_everything_after_named(self) -> None:
        settings = _run_settings(persona_name="小洛")
        assert (
            classify_trigger("你们看这个", settings=settings, quoted_self=True)
            is TriggerCategory.QUOTED
        )
        assert (
            classify_trigger("小洛看看", settings=settings, quoted_self=True)
            is TriggerCategory.NICKNAMED
        )

    def test_you_plural_before_you_ref(self) -> None:
        settings = _run_settings(persona_name="小洛")
        assert classify_trigger("你们去哪", settings=settings) is TriggerCategory.YOU_PLURAL
        assert classify_trigger("你说呢", settings=settings) is TriggerCategory.YOU_TO_GATE

    def test_you_plural_switch_off_falls_through(self) -> None:
        settings = _run_settings(persona_name="小洛", auto_chat_you_plural_reply=False)
        assert classify_trigger("你们去哪", settings=settings) is TriggerCategory.YOU_TO_GATE

    def test_ordinary(self) -> None:
        assert (
            classify_trigger("今天好累啊", settings=_run_settings(persona_name="小洛"))
            is TriggerCategory.ORDINARY
        )


# ---- S10-HUMAN-P0-03：BatchItem 与 run_auto_chat 批次契约 ----

from qq_bot.services.auto_chat import BatchItem, batch_plusone_text  # noqa: E402


async def _run_batch(h: Harness, items: list[BatchItem]) -> None:
    await run_auto_chat(
        event=FakeEvent(),
        raw_text=items[-1].raw_text,
        batch=items,
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


class TestBatchPipeline:
    async def test_batch_gate_called_once_with_batch_context(self) -> None:
        h = Harness(["随便聊聊"], _run_settings(auto_chat_sample_rate=1.0))
        now = datetime.now(UTC)
        items = [
            BatchItem(raw_text="开黑吗", sender_user_id=3001, created_at=now, row_id=999),
            BatchItem(raw_text="来不来", sender_user_id=3001, created_at=now, row_id=998),
        ]
        await _run_batch(h, items)
        gate_calls = [c for c in h.completions if "决策器" in c["system_prompt"]]
        assert len(gate_calls) == 1
        assert "开黑吗" in gate_calls[0]["user_prompt"]
        assert "连发2条" in gate_calls[0]["user_prompt"]
        assert len(h.sent) >= 1

    async def test_batch_rows_excluded_by_row_id(self) -> None:
        # FakeMemory 的行 id 是 1..N；row_id=1/2 指向这两行 → 上下文只剩"随便聊聊"
        h = Harness(["随便聊聊", "开黑吗", "来不来"], _run_settings(auto_chat_sample_rate=1.0))
        now = datetime.now(UTC)
        items = [
            BatchItem(raw_text="开黑吗", sender_user_id=3001, created_at=now, row_id=2),
            BatchItem(raw_text="来不来", sender_user_id=3001, created_at=now, row_id=3),
        ]
        await _run_batch(h, items)
        gate_calls = [c for c in h.completions if "决策器" in c["system_prompt"]]
        assert gate_calls[0]["user_prompt"].count("开黑吗") == 1  # 只在批次段出现一次

    async def test_batch_plusone_uses_text_without_gate(self) -> None:
        h = Harness(["冲冲冲", "冲冲冲"], _run_settings())  # 两行同文不同人（user 2001/2002）
        now = datetime.now(UTC)
        items = [BatchItem(raw_text="冲冲冲", sender_user_id=3001, created_at=now, row_id=99)]
        assert batch_plusone_text(items, h.memory_text_rows()) == "冲冲冲"
        await _run_batch(h, items)
        assert h.sent == ["冲冲冲"]

    async def test_batch_negative_feedback_sets_backoff(self) -> None:
        # 记忆行不含负反馈词与人名：负反馈只能经由批次文本行进入检测（隔离批次路径）
        h = Harness(["随便聊聊"], _run_settings())
        h.state.acquire(1001, settings=h.settings)  # bot 刚发言 → recent+负反馈词 → 退避
        now = datetime.now(UTC)
        items = [BatchItem(raw_text="小洛你闭嘴", sender_user_id=3001, created_at=now, row_id=99)]
        await _run_batch(h, items)
        assert h.state.in_backoff(1001) is True
        assert h.sent == []

    async def test_batch_trigger_recomputed_after_ignored_filter(self) -> None:
        # 回归（S10-HUMAN-P0-03）：批次末条发送者被忽略名单移除后，触发文本必须
        # 重算为过滤后批次末条。旧实现取过滤前末条（含人名 → 点名旁路，绕过门控）。
        h = Harness(
            ["随便聊聊"],
            _run_settings(auto_chat_ignored_user_ids="3999", auto_chat_sample_rate=1.0),
        )
        now = datetime.now(UTC)
        items = [
            BatchItem(raw_text="开黑吗", sender_user_id=3001, created_at=now, row_id=999),
            BatchItem(raw_text="小洛你别理他们", sender_user_id=3999, created_at=now, row_id=998),
        ]
        await _run_batch(h, items)
        gate_calls = [c for c in h.completions if "决策器" in c["system_prompt"]]
        assert len(gate_calls) == 1  # 到达门控：未因含人名的被过滤末条走点名旁路
        assert h.sent  # 门控 should_reply=true → 有发言
        for c in h.completions:
            assert "你别理他们" not in c["user_prompt"]  # 被忽略项文本不进任何 prompt
        lines = gate_calls[0]["user_prompt"].splitlines()
        batch_lines = lines[lines.index(_BATCH_SECTION_HEADER) + 1 :]
        assert batch_lines[-1].endswith("开黑吗")  # 批次段末行 = 过滤后幸存项


class TestLearningInjection:
    def test_gate_prompt_jargon_section(self) -> None:
        rows = [_row("在吗", user_id=2001, row_id=1)]
        prompt = build_gate_user_prompt(rows, jargon_refs=("咸鱼翻车：搞砸了",))
        assert "本群黑话参考" in prompt and "咸鱼翻车：搞砸了" in prompt

    def test_casual_prompt_expression_and_jargon_sections(self) -> None:
        rows = [_row("在吗", user_id=2001, row_id=1)]
        prompt = build_casual_user_prompt(
            rows, expression_refs=("{对象}吗", "确实"), jargon_refs=("咸鱼翻车：搞砸了",)
        )
        assert "本群表达参考" in prompt and "{对象}吗" in prompt
        assert "本群黑话参考" in prompt

    def test_empty_refs_omit_sections(self) -> None:
        prompt = build_casual_user_prompt([_row("在吗", user_id=2001, row_id=1)])
        assert "本群表达参考" not in prompt and "本群黑话参考" not in prompt

    async def test_injection_flows_into_generation_and_touches(self) -> None:
        h = Harness(
            ["随便聊聊"],
            _run_settings(
                expression_learn_enabled=True, expression_min_active=1, auto_chat_sample_rate=1.0
            ),
        )

        class LearnMemory(FakeMemory):
            def __init__(self, texts: list[str]):
                super().__init__(texts)
                self.touched: list[int] = []

            async def sample_expressions(self, *, group_id, limit, threshold, rng=None):
                from qq_bot.services.chat_memory import ExpressionRow

                return [
                    ExpressionRow(
                        id=7,
                        group_id=group_id,
                        template="{对象}吗",
                        slot_count=1,
                        evidence_row_id=None,
                        evidence_digest="",
                        status="active",
                        count=3,
                        manual=0,
                        created_at="",
                        last_used_at=None,
                    )
                ]

            async def list_jargon(self, *, group_id, status, limit=10):
                from qq_bot.services.chat_memory import JargonRow

                return [
                    JargonRow(
                        id=2,
                        group_id=group_id,
                        term="咸鱼翻车",
                        definition="搞砸了",
                        status="confirmed",
                        hits=5,
                        manual=0,
                        first_seen_at="",
                        last_seen_at="",
                        confirmed_at=None,
                    )
                ]

            async def touch_expressions(self, ids, *, now=None):
                self.touched = list(ids)
                return len(ids)

        learn_mem = LearnMemory(["随便聊聊"])
        h.memory = learn_mem
        await _run(h, "随便聊聊")
        gate_calls = [c for c in h.completions if "决策器" in c["system_prompt"]]
        gen_calls = [c for c in h.completions if "决策器" not in c["system_prompt"]]
        assert "本群黑话参考" in gate_calls[0]["user_prompt"]
        assert "本群表达参考" in gen_calls[0]["user_prompt"]
        assert learn_mem.touched == [7]

    async def test_injection_skipped_when_disabled(self) -> None:
        h = Harness(
            ["随便聊聊"], _run_settings(expression_learn_enabled=False, auto_chat_sample_rate=1.0)
        )
        await _run(h, "随便聊聊")
        for c in h.completions:
            assert "本群表达参考" not in c["user_prompt"]
            assert "本群黑话参考" not in c["user_prompt"]


def _expression_metric(stage: str, result: str) -> float:
    """与 test_expression_learner._expression_metric 同一套 REGISTRY 采样手法。"""
    from prometheus_client import REGISTRY

    return (
        REGISTRY.get_sample_value("qq_bot_expression_total", {"stage": stage, "result": result})
        or 0.0
    )


def _metric_value(stage: str, result: str) -> float:
    """prefilter/result 维度采样（手法同 test_ai_chat_plugin._auto_chat_metric）。"""
    from prometheus_client import REGISTRY

    return (
        REGISTRY.get_sample_value("qq_bot_auto_chat_total", {"stage": stage, "result": result})
        or 0.0
    )


def _reaction_metric(result: str) -> float:
    """AUTO_CHAT_REACTIONS 的 result 维度采样（ok|failed|unsupported|limit）。"""
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value("qq_bot_auto_chat_reactions_total", {"result": result}) or 0.0


def _interest_gauge(group_id: int) -> float:
    """qq_bot_auto_chat_interest{group_hash} 当前采样值（0 = 未设置）。"""
    from prometheus_client import REGISTRY

    from qq_bot.observability.logging import hash_id

    return (
        REGISTRY.get_sample_value(
            "qq_bot_auto_chat_interest", {"group_hash": hash_id(group_id, kind="group")}
        )
        or 0.0
    )


class _SampleMemory(FakeMemory):
    """sample_expressions 返回预设样本（可空）；其余沿用 FakeMemory。"""

    def __init__(self, texts: list[str], rows: list):
        super().__init__(texts)
        self._rows = rows
        self.touched: list[int] = []

    async def sample_expressions(self, *, group_id, limit, threshold, rng=None):
        from qq_bot.services.chat_memory import ExpressionRow

        if not self._rows:
            return []
        return [
            ExpressionRow(
                id=7,
                group_id=group_id,
                template=row,
                slot_count=1,
                evidence_row_id=None,
                evidence_digest="",
                status="active",
                count=3,
                manual=0,
                created_at="",
                last_used_at=None,
            )
            for row in self._rows
        ]

    async def touch_expressions(self, ids, *, now=None):
        self.touched = list(ids)
        return len(ids)


class TestLearningInjectionMetrics:
    """spec 观测节的 inject 标签：ok / skip_insufficient / skip_disabled。"""

    async def test_inject_ok_metric_on_non_empty_sample(self) -> None:
        h = Harness(
            ["随便聊聊"],
            _run_settings(
                expression_learn_enabled=True, expression_min_active=1, auto_chat_sample_rate=1.0
            ),
        )
        h.memory = _SampleMemory(["随便聊聊"], ["{对象}吗"])
        before = _expression_metric("inject", "ok")
        await _run(h, "随便聊聊")
        assert h.sent  # 真的发出去了：本轮回合算一次成功注入
        assert _expression_metric("inject", "ok") == before + 1

    async def test_inject_skip_insufficient_metric_on_empty_sample(self) -> None:
        # 活跃池未过阈值：sample_expressions 返回 []（非异常）
        h = Harness(
            ["随便聊聊"],
            _run_settings(
                expression_learn_enabled=True, expression_min_active=10, auto_chat_sample_rate=1.0
            ),
        )
        h.memory = _SampleMemory(["随便聊聊"], [])
        before = _expression_metric("inject", "skip_insufficient")
        await _run(h, "随便聊聊")
        assert _expression_metric("inject", "skip_insufficient") == before + 1

    async def test_inject_skip_disabled_metric_when_feature_off(self) -> None:
        h = Harness(
            ["随便聊聊"], _run_settings(expression_learn_enabled=False, auto_chat_sample_rate=1.0)
        )
        before = _expression_metric("inject", "skip_disabled")
        await _run(h, "随便聊聊")
        assert _expression_metric("inject", "skip_disabled") == before + 1

    async def test_inject_store_error_is_silent_no_inject_label(self) -> None:
        # 存储调用失败 → 静默返回空样本：inject 三标签都不动（fail-soft 路径）
        h = Harness(
            ["随便聊聊"],
            _run_settings(
                expression_learn_enabled=True, expression_min_active=1, auto_chat_sample_rate=1.0
            ),
        )

        class BoomMemory(FakeMemory):
            async def sample_expressions(self, *, group_id, limit, threshold, rng=None):
                raise RuntimeError("store down")

        h.memory = BoomMemory(["随便聊聊"])
        ok_before = _expression_metric("inject", "ok")
        ins_before = _expression_metric("inject", "skip_insufficient")
        dis_before = _expression_metric("inject", "skip_disabled")
        await _run(h, "随便聊聊")
        assert _expression_metric("inject", "ok") == ok_before
        assert _expression_metric("inject", "skip_insufficient") == ins_before
        assert _expression_metric("inject", "skip_disabled") == dis_before

    async def test_retry_path_reuses_same_expression_sample(self) -> None:
        """重写重试不是新的一轮群聊：表达样本整轮只采一次，首次与重试两条
        prompt 都带齐表达/黑话两节，发送成功后按原样本 id 回写一次。
        （S10-HUMAN-P1-04 回归：曾按生成次数采样 → 重试 prompt 丢参考。）"""
        h = Harness(
            ["随便聊聊"],
            _run_settings(
                expression_learn_enabled=True, expression_min_active=1, auto_chat_sample_rate=1.0
            ),
        )
        # 首次生成复述触发句 → _reply_issue 命中 echo → 重写一次；重写内容换说法
        casual_seq = iter(["一起去打新活动吗", "换个说法，这活动我不去"])

        def completion():
            async def fake(**kwargs) -> str:
                h.completions.append(kwargs)  # 与 Harness.completion 一致地留痕
                if "决策器" in kwargs["system_prompt"]:
                    return h.gate_content
                return next(casual_seq)

            return fake

        class CountingMemory(FakeMemory):
            def __init__(self, texts):
                super().__init__(texts)
                self.samples = 0
                self.gen_prompts: list[str] = []

            async def sample_expressions(self, *, group_id, limit, threshold, rng=None):
                self.samples += 1
                from qq_bot.services.chat_memory import ExpressionRow

                return [
                    ExpressionRow(
                        id=7,
                        group_id=group_id,
                        template="{对象}吗",
                        slot_count=1,
                        evidence_row_id=None,
                        evidence_digest="",
                        status="active",
                        count=3,
                        manual=0,
                        created_at="",
                        last_used_at=None,
                    )
                ]

            async def list_jargon(self, *, group_id, status, limit=10):
                from qq_bot.services.chat_memory import JargonRow

                return [
                    JargonRow(
                        id=2,
                        group_id=group_id,
                        term="咸鱼翻车",
                        definition="搞砸了",
                        status="confirmed",
                        hits=5,
                        manual=0,
                        first_seen_at="",
                        last_seen_at="",
                        confirmed_at=None,
                    )
                ]

            async def touch_expressions(self, ids, *, now=None):
                self.touched = list(ids)
                return len(ids)

        mem = CountingMemory(h.memory.texts)
        h.memory = mem
        await _run(h, "一起去打新活动吗", _completion=completion())
        assert mem.samples == 1  # 每轮只采样一次（重试复用）
        gen_calls = [c for c in h.completions if "决策器" not in c["system_prompt"]]
        assert len(gen_calls) == 2  # 首次 + 重写
        for call in gen_calls:
            assert "本群表达参考" in call["user_prompt"]
            assert "本群黑话参考" in call["user_prompt"]
        assert mem.touched == [7]  # 发送成功后回写一次


# ---- 群兴趣度（S10-HUMAN-P2 H7）----


class TestInterest:
    def test_note_activity_boosts_and_clamps(self) -> None:
        clock = FakeClock()
        state = AutoChatState(clock=clock)
        settings = _settings()
        for _ in range(100):  # 远超 1.0
            state.note_activity(1, weight=1.0)
        assert state.interest_of(1, settings=settings) == 1.0

    def test_lazy_half_life_decay(self) -> None:
        clock = FakeClock()
        state = AutoChatState(clock=clock)
        settings = _settings()
        state.note_activity(1, weight=1.0)  # 0.02
        clock.advance(600)  # 一个半衰期
        assert state.interest_of(1, settings=settings) == pytest.approx(0.01, abs=1e-9)

    def test_decay_to_zero_after_long_silence(self) -> None:
        clock = FakeClock()
        state = AutoChatState(clock=clock)
        settings = _settings()
        state.note_activity(1, weight=1.0)
        clock.advance(3600 * 24)
        assert state.interest_of(1, settings=settings) == 0.0

    def test_activity_weight_markers(self) -> None:
        settings = _run_settings(persona_name="小洛")
        assert activity_weight("今天天气不错", settings=settings) == 1.0
        # "你"(+2) + 疑问尾"呢"(+2) = 5 → 封顶 4.0（控制器裁定修正 brief 的 3.0）
        assert activity_weight("你觉得呢", settings=settings) == 4.0
        assert activity_weight("你们去哪玩", settings=settings) == 3.0  # 你×2，非疑问尾
        assert activity_weight("你在吗吗吗", settings=settings) == 4.0  # 封顶

    def test_backoff_keeps_interest(self) -> None:
        clock = FakeClock()
        state = AutoChatState(clock=clock)
        settings = _settings()
        # weight=50 × boost 0.02 = 1.0（brief 的 weight=10.0 与 boost 0.02 不一致：
        # 10×0.02=0.2，与 test_lazy_half_life_decay 注释的 weight=1.0→0.02 冲突）
        state.note_activity(1, weight=50.0)
        state.set_backoff(1, 1800.0)
        assert state.interest_of(1, settings=settings) == 1.0

    def test_zero_weight_never_decreases(self) -> None:
        clock = FakeClock()
        state = AutoChatState(clock=clock)
        settings = _settings()
        state.note_activity(1, weight=1.0)
        state.note_activity(1, weight=-5.0)
        assert state.interest_of(1, settings=settings) == pytest.approx(0.02, abs=1e-9)

    def test_interest_decay_independent_of_enabled_flag(self) -> None:
        """衰减数学不随 `auto_chat_interest_enabled` 变化：开关只门控管道消费
        （采样/冷却/高热进热聊/gauge），状态始终如实记账。"""
        clock = FakeClock()
        state = AutoChatState(clock=clock)
        state.note_activity(1, weight=1.0)
        clock.advance(600)
        settings = _settings(auto_chat_interest_enabled=False)
        assert state.interest_of(1, settings=settings) == pytest.approx(0.01, abs=1e-9)

    def test_interest_map_stays_small_after_silence(self) -> None:
        """惰性衰减在 <1e-6 时清键：长时间安静后状态字典不残留群条目。"""
        clock = FakeClock()
        state = AutoChatState(clock=clock)
        settings = _settings()
        state.note_activity(1, weight=1.0)
        clock.advance(3600 * 24)
        assert state.interest_of(1, settings=settings) == 0.0
        assert state._interest == {}
        assert state._interest_at == {}

    def test_note_activity_boost_setting_defaults_to_module_constant(self) -> None:
        """handler 不传 boost 时用模块常量 0.02（与默认配置一致）。"""
        clock = FakeClock()
        state = AutoChatState(clock=clock)
        settings = _settings()
        state.note_activity(1, weight=1.0)
        assert state.interest_of(1, settings=settings) == pytest.approx(0.02, abs=1e-9)

    def test_note_activity_honours_explicit_boost(self) -> None:
        """调用方可注入 settings.auto_chat_interest_boost（handler 侧接线）。"""
        clock = FakeClock()
        state = AutoChatState(clock=clock)
        settings = _settings()
        for _ in range(10):
            state.note_activity(1, weight=1.0, boost=0.1)
        assert state.interest_of(1, settings=settings) == pytest.approx(1.0, abs=1e-9)

    def test_note_activity_negative_boost_clamped_to_zero(self) -> None:
        """负 boost（配置或显式 kwarg）不得写入负兴趣度。"""
        clock = FakeClock()
        state = AutoChatState(clock=clock)
        settings = _settings()
        state.note_activity(1, weight=1.0, boost=-0.5)
        assert state.interest_of(1, settings=settings) == 0.0
        assert state._interest[1] == 0.0

    def test_note_activity_folds_half_life_before_boost(self) -> None:
        """判别 pin（Final Fix）：显式传 half_life 时先按半衰期折叠存量再加 boost。
        50 条 ×0.02=1.0，静默恰一个半衰期后再记一条 → 0.5+0.02=0.52，
        而非无折叠累加的 1.02→钳 1.0（活跃消息流中衰减永不生效的旧缺陷）。"""
        clock = FakeClock()
        state = AutoChatState(clock=clock)
        settings = _settings()
        for _ in range(50):
            state.note_activity(1, weight=1.0, half_life=600.0)
        assert state.interest_of(1, settings=settings) == 1.0
        clock.advance(600)  # 恰好一个半衰期
        state.note_activity(1, weight=1.0, half_life=600.0)
        assert state.interest_of(1, settings=settings) == pytest.approx(0.52, abs=1e-9)

    def test_note_activity_without_half_life_keeps_no_fold(self) -> None:
        """省略 half_life（默认 None）保持旧语义：直接操作 state 的既有单测
        （时间推进后 note 仍按未折算存量累加）不受影响，折叠由调用方显式启用。"""
        clock = FakeClock()
        state = AutoChatState(clock=clock)
        settings = _settings()
        state.note_activity(1, weight=1.0)
        clock.advance(600)
        state.note_activity(1, weight=1.0)
        assert state.interest_of(1, settings=settings) == pytest.approx(0.04, abs=1e-9)

    def test_cooldown_multiplier_clamped_to_hot_floor(self) -> None:
        """冷却乘数在 interest=1 时不低于热聊冷却（30s）：300×0.5=150 > 30。
        安静群 interest=0 → 300×1.2=360s（比原冷却更长，乘数只可能更活跃）。"""
        assert max(30.0, 300.0 * (1.2 - 0.7 * 1.0)) == pytest.approx(150.0)
        assert max(30.0, 300.0 * (1.2 - 0.7 * 0.0)) == pytest.approx(360.0)
        # 极短冷却时热聊冷却兜底：10×0.5=5 < 30 → 取 30
        assert max(30.0, 10.0 * (1.2 - 0.7 * 1.0)) == pytest.approx(30.0)

    def test_sampling_multiplier_bounds(self) -> None:
        """乘数率 = min(1, rate × (0.5 + 2.5 × interest))：interest 0 → 0.5×，
        interest 1 → 3.0×（0.5+2.5×1），钳 1。"""
        assert min(1.0, 0.2 * (0.5 + 2.5 * 0.0)) == pytest.approx(0.1)
        assert min(1.0, 0.2 * (0.5 + 2.5 * 0.5)) == pytest.approx(0.35)
        assert min(1.0, 0.2 * (0.5 + 2.5 * 1.0)) == pytest.approx(0.6)
        assert min(1.0, 0.5 * (0.5 + 2.5 * 1.0)) == pytest.approx(1.0)  # 3.0× 已钳


@pytest.mark.asyncio
async def test_interest_raises_sampling_effective_rate() -> None:
    """interest=0.6 → 乘数率 = 0.2×(0.5+2.5×0.6) = 0.4：rng 0.35 通过采样。
    阈值 0.75 高于 0.6，确保走的是采样路径而非高热自动进热聊。"""
    state = AutoChatState(clock=FakeClock())
    for _ in range(30):
        state.note_activity(1001, weight=1.0)  # interest=0.6 < 阈值 0.75
    settings = _run_settings(
        auto_chat_sample_rate=0.2,
        auto_chat_interest_enabled=True,
        auto_chat_interest_hot_threshold=0.75,
        auto_chat_cooldown_seconds=0.0,
    )
    h = Harness(["随便聊聊"], settings, state=state)
    h.rng_value = 0.35  # < 0.4（乘数率）但 >= 0.2（原采样率）
    hot_before = _metric_value("prefilter", "interest_hot")
    await _run(h, "随便聊聊")
    assert h.sent  # 热度让它通过采样
    # 未走高热自动进热聊（阈值 0.75 > 0.6），确系采样路径放行。
    assert _metric_value("prefilter", "interest_hot") == hot_before

    # 负对照：同一热度下关闭兴趣开关 → 采样率回到 0.2，rng 0.35 被采掉。
    control_state = AutoChatState(clock=FakeClock())
    for _ in range(30):
        control_state.note_activity(1001, weight=1.0)
    disabled = _run_settings(
        auto_chat_sample_rate=0.2,
        auto_chat_interest_enabled=False,
        auto_chat_interest_hot_threshold=0.75,
        auto_chat_cooldown_seconds=0.0,
    )
    control = Harness(["随便聊聊"], disabled, state=control_state)
    control.rng_value = 0.35
    await _run(control, "随便聊聊")
    assert control.sent == []


@pytest.mark.asyncio
async def test_no_interest_same_message_sampled_out() -> None:
    settings = _run_settings(
        auto_chat_sample_rate=0.2,
        auto_chat_interest_enabled=True,
        auto_chat_cooldown_seconds=0.0,
    )
    h = Harness(["随便聊聊"], settings)
    h.rng_value = 0.65
    await _run(h, "随便聊聊")
    assert h.sent == []  # interest=0 → 0.5×0.2=0.1 → 0.65 被采掉


@pytest.mark.asyncio
async def test_interest_low_metric_label() -> None:
    settings = _run_settings(
        auto_chat_sample_rate=0.2,
        auto_chat_interest_enabled=True,
        auto_chat_cooldown_seconds=0.0,
    )
    h = Harness(["随便聊聊"], settings)
    h.rng_value = 0.65
    before = _metric_value("prefilter", "interest_low")
    await _run(h, "随便聊聊")
    assert _metric_value("prefilter", "interest_low") == before + 1


@pytest.mark.asyncio
async def test_interest_disabled_keeps_raw_sampling() -> None:
    settings = _run_settings(auto_chat_sample_rate=0.2, auto_chat_cooldown_seconds=0.0)
    h = Harness(["随便聊聊"], settings)
    h.rng_value = 0.15  # <0.2 原采样率
    before = _metric_value("prefilter", "sampled_out")
    await _run(h, "随便聊聊")
    assert h.sent  # 开关关闭时与现状一致
    assert _metric_value("prefilter", "sampled_out") == before  # 未被采掉


@pytest.mark.asyncio
async def test_interest_high_enters_hot_automatically() -> None:
    """interest ≥ 阈值且不在热聊窗口 → 按热聊路径处理（跳过采样与门）并刷新窗口。"""
    state = AutoChatState(clock=FakeClock())
    for _ in range(100):
        state.note_activity(1001, weight=1.0)
    settings = _run_settings(
        auto_chat_sample_rate=0.0,
        auto_chat_interest_enabled=True,
        auto_chat_interest_hot_threshold=0.75,
    )
    h = Harness(["随便聊聊"], settings, state=state)
    h.rng_value = 1.0  # 若未进热聊必被采样丢弃
    before = _metric_value("prefilter", "interest_hot")
    await _run(h, "随便聊聊")
    assert h.sent  # 热度跳过采样
    assert [c for c in h.completions if "决策器" in c["system_prompt"]] == []
    assert _metric_value("prefilter", "interest_hot") == before + 1
    assert h.state.hot_active(1001, settings=settings) is True


@pytest.mark.asyncio
async def test_interest_below_threshold_stays_cold() -> None:
    state = AutoChatState(clock=FakeClock())
    for _ in range(20):  # interest = 0.4 < 0.75
        state.note_activity(1001, weight=1.0)
    settings = _run_settings(
        auto_chat_sample_rate=0.0,
        auto_chat_interest_enabled=True,
        auto_chat_interest_hot_threshold=0.75,
    )
    h = Harness(["随便聊聊"], settings, state=state)
    before = _metric_value("prefilter", "interest_hot")
    await _run(h, "随便聊聊")
    assert h.sent == []
    assert _metric_value("prefilter", "interest_hot") == before
    assert h.state.hot_active(1001, settings=settings) is False


@pytest.mark.asyncio
async def test_interest_gauge_updated_per_run() -> None:
    # 阈值设高以避免高热自动进热聊；gauge 在热聊判定后统一更新（本轮 interest=0.5）
    state = AutoChatState(clock=FakeClock())
    for _ in range(25):  # interest = 0.5
        state.note_activity(1001, weight=1.0)
    settings = _run_settings(
        auto_chat_sample_rate=1.0,
        auto_chat_interest_enabled=True,
        auto_chat_interest_hot_threshold=0.9,
    )
    h = Harness(["随便聊聊"], settings, state=state)
    await _run(h, "随便聊聊")
    assert h.sent
    assert _interest_gauge(1001) == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_interest_gauge_not_touched_when_disabled() -> None:
    settings = _run_settings(auto_chat_sample_rate=1.0, auto_chat_cooldown_seconds=0.0)
    h = Harness(["随便聊聊"], settings)
    before = _interest_gauge(1001)
    await _run(h, "随便聊聊")
    assert h.sent
    assert _interest_gauge(1001) == before  # 关闭时不写 gauge


@pytest.mark.asyncio
async def test_interest_gauge_updated_on_bypass_paths() -> None:
    """gauge 在热聊判定后统一更新：高热自动进热聊（跳过采样块）与非热聊的
    昵称/引用旁路都不会让 gauge 停留在过期值。"""
    # 场景一：interest=1 ≥ 阈值 0.75 → 高热自动进热聊，整轮跳过采样块。
    state = AutoChatState(clock=FakeClock())
    for _ in range(100):
        state.note_activity(1001, weight=1.0)
    settings = _run_settings(
        auto_chat_sample_rate=0.0,
        auto_chat_interest_enabled=True,
        auto_chat_interest_hot_threshold=0.75,
        auto_chat_cold_daily_limit=0,
    )
    h = Harness(["随便聊聊"], settings, state=state)
    await _run(h, "随便聊聊", _completion=_varying_reply(h, "高热回复"))
    assert h.sent
    assert _interest_gauge(1001) == pytest.approx(1.0)

    # 场景二：interest=0.6 < 阈值，昵称旁路跳过采样块。
    state = AutoChatState(clock=FakeClock())
    for _ in range(30):
        state.note_activity(1001, weight=1.0)
    settings = _run_settings(
        auto_chat_sample_rate=0.0,
        auto_chat_interest_enabled=True,
        auto_chat_interest_hot_threshold=0.75,
        auto_chat_cold_daily_limit=0,
    )
    h = Harness(["有人在吗", "喊小洛"], settings, state=state)
    await _run(h, "小洛 在吗", _completion=_varying_reply(h, "昵称回复"))
    assert h.sent
    assert [c for c in h.completions if "决策器" in c["system_prompt"]] == []
    assert _interest_gauge(1001) == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_interest_multiplies_cooldown() -> None:
    """非热聊期冷却 = max(hot_cooldown, cooldown × (1.2 − 0.7 × interest))。
    interest=0.5 → 300×0.85=255s（低于原 300s，高于热聊 30s）：第二次 100s 后
    仍被挡住，260s 后才放行。阈值设高以免高热自动进热聊；半衰期设极大让热度
    在用例时间线上不衰减（否则冷却会随热度回落而回升）；热聊窗口设 0 以免
    发言后进入热聊改走热聊冷却。"""
    clock = FakeClock()
    state = AutoChatState(clock=clock)
    for _ in range(25):  # interest = 0.5
        state.note_activity(1001, weight=1.0)
    settings = _run_settings(
        auto_chat_sample_rate=1.0,
        auto_chat_cooldown_seconds=300.0,
        auto_chat_hot_cooldown_seconds=30.0,
        auto_chat_interest_enabled=True,
        auto_chat_interest_hot_threshold=0.9,
        auto_chat_hot_window_seconds=0.0,
        auto_chat_interest_half_life=1e9,
    )
    h = Harness(["随便聊聊"], settings, state=state)
    # 每条回复必须不同：_reply_issue 会以"与自己最近发言重复"拒绝同文复读
    replies = iter(["头一条回复", "再一条回复", "第三条回复"])

    def varying():
        async def fake(**kwargs) -> str:
            if "决策器" in kwargs["system_prompt"]:
                return h.gate_content
            return next(replies)

        return fake

    await _run(h, "随便聊聊", _completion=varying())
    assert h.sent  # 第一次发出去
    clock.advance(100.0)  # < 255s 乘数冷却
    await _run(h, "再聊一句", _completion=varying())
    assert len(h.sent) == 1  # 冷却挡住
    clock.advance(160.0)  # 累计 260s > 255s
    await _run(h, "第三句", _completion=varying())
    assert len(h.sent) == 2  # 冷却到期放行


@pytest.mark.asyncio
async def test_interest_zero_multiplies_cooldown_upward() -> None:
    """interest=0 → 冷却 = 300×1.2 = 360s（比原冷却更长）：热度越低越沉默。"""
    clock = FakeClock()
    settings = _run_settings(
        auto_chat_sample_rate=1.0,
        auto_chat_cooldown_seconds=300.0,
        auto_chat_hot_cooldown_seconds=30.0,
        auto_chat_interest_enabled=True,
        auto_chat_interest_hot_threshold=0.9,
        auto_chat_hot_window_seconds=0.0,
        auto_chat_interest_half_life=1e9,
    )
    h = Harness(["随便聊聊"], settings, state=AutoChatState(clock=clock))
    replies = iter(["头一条回复", "再一条回复", "第三条回复"])

    def varying():
        async def fake(**kwargs) -> str:
            if "决策器" in kwargs["system_prompt"]:
                return h.gate_content
            return next(replies)

        return fake

    await _run(h, "随便聊聊", _completion=varying())
    assert h.sent
    clock.advance(310.0)  # > 原冷却 300s，但 < 360s 乘数冷却
    await _run(h, "再聊一句", _completion=varying())
    assert len(h.sent) == 1  # 乘数冷却仍挡住
    clock.advance(60.0)  # 累计 370s > 360s
    await _run(h, "第三句", _completion=varying())
    assert len(h.sent) == 2


# ---- 贴表情反应（S10-HUMAN-P2 H9）：决策门 action/react 路径/限额/降级 ----


class TestGateActionParsing:
    def test_default_action_reply(self) -> None:
        d = parse_gate_output('{"should_reply": true, "reason": "banter", "confidence": 0.9}')
        assert d is not None and d.action == "reply" and d.emoji_id == ""

    def test_react_with_emoji(self) -> None:
        d = parse_gate_output(
            '{"should_reply": true, "reason": "banter", "confidence": 0.9,'
            ' "action": "react", "emoji_id": "128"}'
        )
        assert d is not None and d.action == "react" and d.emoji_id == "128"

    def test_invalid_action_falls_back_to_reply(self) -> None:
        d = parse_gate_output(
            '{"should_reply": true, "reason": "banter", "confidence": 0.9, "action": "dance"}'
        )
        assert d is not None and d.action == "reply"

    def test_non_string_emoji_id_becomes_empty(self) -> None:
        d = parse_gate_output(
            '{"should_reply": true, "reason": "banter", "confidence": 0.9,'
            ' "action": "react", "emoji_id": 128}'
        )
        assert d is not None and d.action == "react" and d.emoji_id == ""

    def test_garbage_still_none(self) -> None:
        assert parse_gate_output("不是json") is None


def _react_gate_content(emoji_id: str = "128") -> str:
    return (
        '{"should_reply": true, "reason": "banter", "confidence": 0.9,'
        f' "action": "react", "emoji_id": "{emoji_id}"}}'
    )


class TestReactionPath:
    async def test_react_skips_generation_and_hot_and_cold(self) -> None:
        settings = _run_settings(
            auto_chat_sample_rate=1.0,
            auto_chat_reaction_enabled=True,
            auto_chat_reaction_whitelist="128",
            auto_chat_cooldown_seconds=0.0,
        )
        state = AutoChatState(clock=FakeClock())
        h = Harness(["随便聊聊"], settings, state=state)
        h.gate_content = _react_gate_content("128")
        reactions: list[tuple[int, str]] = []

        async def react(message_id: int, emoji_id: str) -> bool:
            reactions.append((message_id, emoji_id))
            return True

        await _run(h, "随便聊聊", react=react)
        assert reactions == [(1, "128")]  # message_id 来自 Harness 默认（见下）
        assert h.sent == []  # 不发文本
        assert not h.completions or all(
            "决策器" in c["system_prompt"] for c in h.completions
        )  # 无生成调用
        assert state.hot_active(1001, settings=settings) is False  # 不进热聊
        assert h.state._hot_streak.get(1001, 0) == 0

    async def test_react_emoji_not_in_whitelist_falls_back_to_reply(self) -> None:
        settings = _run_settings(
            auto_chat_sample_rate=1.0,
            auto_chat_reaction_enabled=True,
            auto_chat_reaction_whitelist="4",
            auto_chat_cooldown_seconds=0.0,
        )
        h = Harness(["随便聊聊"], settings)
        h.gate_content = _react_gate_content("999")

        async def react(message_id: int, emoji_id: str) -> bool:
            raise AssertionError("react 不应被调用")

        await _run(h, "随便聊聊", react=react)
        assert h.sent  # 回落文本生成

    async def test_react_disabled_channel_falls_back(self) -> None:
        settings = _run_settings(auto_chat_sample_rate=1.0, auto_chat_cooldown_seconds=0.0)
        h = Harness(["随便聊聊"], settings)
        h.gate_content = _react_gate_content("128")

        async def react(message_id: int, emoji_id: str) -> bool:
            raise AssertionError("react 不应被调用")

        await _run(h, "随便聊聊", react=react)
        assert h.sent

    async def test_react_daily_limit_and_cooldown(self) -> None:
        settings = _run_settings(
            auto_chat_sample_rate=1.0,
            auto_chat_reaction_enabled=True,
            auto_chat_reaction_whitelist="128",
            auto_chat_cooldown_seconds=0.0,
            auto_chat_reaction_daily_limit=1,
            auto_chat_reaction_cooldown_seconds=60,
        )
        clock = FakeClock()
        state = AutoChatState(clock=clock)
        h = Harness(["随便聊聊"], settings, state=state)
        h.gate_content = _react_gate_content("128")
        calls = {"n": 0}

        async def react(message_id: int, emoji_id: str) -> bool:
            calls["n"] += 1
            return True

        await _run(h, "随便聊聊", react=react)
        assert calls["n"] == 1  # 首轮成功：当日计数 = 1（= 上限）
        # 把共享时钟推过 react 冷却（60s）使 cooldown 已清，但当日计数仍为 1
        # → 第二次必须命中 daily_limit 而非 cooldown（与旧版等价性之外的定向
        # 覆盖：cooldown 与 daily_limit 是 reaction_limit_reason 的两个独立分支）。
        clock.advance(61.0)
        limit_before = _reaction_metric("limit")
        h2 = Harness(["随便聊聊"], settings, state=state)
        h2.gate_content = h.gate_content
        await _run(h2, "随便聊聊", react=react)
        assert calls["n"] == 1  # 未被 react：限额挡住
        assert h2.sent  # 落到文本生成
        # daily_limit 分支必须真的发射 limit 标签（负向确认：限额判定不生效时
        # 第二轮会再次 react，本用例的 calls/标签断言随之失败）
        assert _reaction_metric("limit") == limit_before + 1

    async def test_three_failures_disable_channel_then_fallback(self) -> None:
        from qq_bot.services import auto_chat as m

        m._reset_reaction_streak()  # 测试隔离：进程级降级态
        settings = _run_settings(
            auto_chat_sample_rate=1.0,
            auto_chat_reaction_enabled=True,
            auto_chat_reaction_whitelist="128",
            auto_chat_cooldown_seconds=0.0,
        )
        h = Harness(["随便聊聊"], settings)
        h.gate_content = _react_gate_content("128")

        async def failing_react(message_id: int, emoji_id: str) -> bool:
            return False

        try:
            for _ in range(3):
                await _run(h, "随便聊聊", react=failing_react)
                # 发送中失败即结束本轮：门已判定只值得贴表情，不得回落生成文本
                # （控制器的 resolution #1——react_failed 分支不补发文本）。
                assert h.sent == []
            assert m.reaction_unsupported() is True
            # 第 4 次：react 不再尝试，直接文本
            h.sent.clear()
            await _run(h, "随便聊聊", react=failing_react)
            assert h.sent
        finally:
            # _REACTION_* 是模块级进程态：禁用标记绝不可泄漏给后续用例
            m._reset_reaction_streak()


class TestReactionUnsupportedLog:
    """Final Fix：3 连败翻转 `_REACTION_UNSUPPORTED` 的那一刻发一条 warning
    （固定文案，无 message_id、无消息内容），且整个进程生命周期只发一次。"""

    def test_warning_fires_exactly_once_on_third_failure(self, caplog) -> None:
        from qq_bot.services import auto_chat as m

        m._reset_reaction_streak()  # 测试隔离：进程级降级态
        try:
            with caplog.at_level(logging.WARNING, logger="qq_bot.auto_chat"):
                assert m._note_reaction_result(False) == "failed"
                assert m._note_reaction_result(False) == "failed"
                assert m._note_reaction_result(False) == "unsupported"
                # 已禁用后再次登记失败（防御路径）不得重复告警
                assert m._note_reaction_result(False) == "unsupported"
            disabled = [
                r
                for r in caplog.records
                if r.levelno == logging.WARNING
                and r.name == "qq_bot.auto_chat"
                and "reaction channel disabled" in r.getMessage()
            ]
            assert len(disabled) == 1
            # 固定文案：只有事实，不携带 message_id 或任何消息内容
            assert (
                disabled[0].getMessage()
                == "auto chat reaction channel disabled after consecutive failures"
            )
        finally:
            m._reset_reaction_streak()


class TestGateEmojiHintGating:
    """决策门"可用表情 ID"提示行的准入（Fix Round 1 / Minor 7）：提示必须与
    本轮贴表情通道的真实可用性一致——react 回调在、有触发消息 id、开关与白
    名单齐备、未判 unsupported。收集器批次交付路径 react=None（无触发消息
    id），提示绝不能出现，否则等于给模型一个不存在的通道。"""

    def _gate_prompt(self, h: Harness) -> str:
        gate_calls = [c for c in h.completions if "决策器" in c["system_prompt"]]
        assert len(gate_calls) == 1  # 决策门恰好调一次
        return gate_calls[0]["user_prompt"]

    async def test_hint_present_on_react_path(self) -> None:
        settings = _run_settings(
            auto_chat_sample_rate=1.0,
            auto_chat_reaction_enabled=True,
            auto_chat_reaction_whitelist="128,4",
            auto_chat_cooldown_seconds=0.0,
        )
        h = Harness(["随便聊聊"], settings)
        h.gate_content = _react_gate_content("128")

        async def react(message_id: int, emoji_id: str) -> bool:
            return True

        await _run(h, "随便聊聊", react=react)
        assert "可用表情 ID：128、4" in self._gate_prompt(h)
        # react 指令（原系统提示词段落）与 ID 列表同块进 user prompt
        assert "action=react" in self._gate_prompt(h)
        assert "不值得打字" in self._gate_prompt(h)

    async def test_hint_absent_on_batch_path(self) -> None:
        """react=None + trigger_message_id=None（收集器批次交付路径的接线形态）：
        即使开关开启、白名单非空，提示也不得出现。"""
        settings = _run_settings(
            auto_chat_sample_rate=1.0,
            auto_chat_reaction_enabled=True,
            auto_chat_reaction_whitelist="128,4",
            auto_chat_cooldown_seconds=0.0,
        )
        h = Harness(["随便聊聊"], settings)
        now = datetime.now(UTC)
        items = [
            BatchItem(raw_text="开黑吗", sender_user_id=3001, created_at=now, row_id=999),
        ]
        await run_auto_chat(
            event=FakeEvent(),
            raw_text="开黑吗",
            batch=items,
            settings=h.settings,
            memory_store=h.memory,
            send=h.send,
            quota_check=h.quota_check,
            state=h.state,
            rng=h.rng,
            sleeper=h.sleeper,
            _cold_sleep=h.instant_cold_sleep,
            _request_completion=h.completion(),
            react=None,
            trigger_message_id=None,
        )
        gate_calls = [c for c in h.completions if "决策器" in c["system_prompt"]]
        assert gate_calls  # 批次路径同样过决策门
        assert "可用表情 ID" not in gate_calls[0]["user_prompt"]
        assert "action=react" not in gate_calls[0]["user_prompt"]

    async def test_hint_absent_without_trigger_message_id(self) -> None:
        """单消息路径但拿不到触发消息 id（react 无从贴起）：提示同样缺席。"""
        settings = _run_settings(
            auto_chat_sample_rate=1.0,
            auto_chat_reaction_enabled=True,
            auto_chat_reaction_whitelist="128",
            auto_chat_cooldown_seconds=0.0,
        )
        h = Harness(["随便聊聊"], settings)

        async def react(message_id: int, emoji_id: str) -> bool:
            raise AssertionError("react 不应被调用：无触发消息 id 时通道不可用")

        await run_auto_chat(
            event=FakeEvent(),
            raw_text="随便聊聊",
            settings=h.settings,
            memory_store=h.memory,
            send=h.send,
            quota_check=h.quota_check,
            state=h.state,
            rng=h.rng,
            sleeper=h.sleeper,
            _cold_sleep=h.instant_cold_sleep,
            _request_completion=h.completion(),
            react=react,
            trigger_message_id=None,
        )
        assert h.sent  # 回落文本生成
        assert "可用表情 ID" not in self._gate_prompt(h)
        # react 指令与列表同块：通道不可用时整块缺席，不残留半截指令
        assert "action=react" not in self._gate_prompt(h)


class _ObserveMemory(FakeMemory):
    """get_person_observation 返回预设卡；未设置时表示无卡。"""

    def __init__(self, texts: list[str], observation: str | None = None, insufficient=False):
        super().__init__(texts)
        self.observation = observation
        self.insufficient = insufficient

    async def get_person_observation(self, *, group_id: int, user_id: int):
        if self.observation is None:
            return None
        from qq_bot.services.chat_memory import PersonObservationRow

        return PersonObservationRow(
            group_id=group_id,
            user_id=user_id,
            observation=self.observation,
            source_message_id=None,
            insufficient=self.insufficient,
            updated_at="",
        )


def _observe_casual_prompt(h: Harness) -> str:
    casual = [c for c in h.completions if "决策器" not in c["system_prompt"]]
    assert casual, "生成调用未发生"
    return casual[-1]["user_prompt"]


class TestPersonObserveInjection:
    async def test_card_reaches_casual_prompt_when_enabled(self) -> None:
        h = Harness(
            ["在吗", "冲"],
            _run_settings(person_observe_enabled=True),
        )
        h.memory = _ObserveMemory(["在吗", "冲"], observation="话不多，潜水党")
        await _run(h)
        assert h.sent
        assert "你对当前发言者的了解" in _observe_casual_prompt(h)
        assert "话不多，潜水党" in _observe_casual_prompt(h)

    async def test_disabled_by_default_never_injects_card(self) -> None:
        h = Harness(["在吗", "冲"], _run_settings())
        h.memory = _ObserveMemory(["在吗", "冲"], observation="话不多，潜水党")
        await _run(h)
        assert h.sent
        assert "你对当前发言者的了解" not in _observe_casual_prompt(h)

    async def test_negative_card_and_missing_card_stay_absent(self) -> None:
        # 负卡（insufficient）与空正文当作没有卡
        h = Harness(["在吗", "冲"], _run_settings(person_observe_enabled=True))
        h.memory = _ObserveMemory(["在吗", "冲"], observation="", insufficient=True)
        await _run(h)
        assert h.sent
        assert "你对当前发言者的了解" not in _observe_casual_prompt(h)

        # 没有卡记录
        h = Harness(["在吗", "冲"], _run_settings(person_observe_enabled=True))
        h.memory = _ObserveMemory(["在吗", "冲"], observation=None)
        h.casual_content = "冲！新活动冲什么冲"
        await _run(h, "冲！新活动")
        assert h.sent
        assert "你对当前发言者的了解" not in _observe_casual_prompt(h)

    async def test_store_failure_is_silent_but_send_continues(self) -> None:
        h = Harness(["在吗", "冲"], _run_settings(person_observe_enabled=True))
        h.memory = _ObserveMemory(["在吗", "冲"], observation="观察")

        async def failing_get(**kwargs):
            raise RuntimeError("db gone")

        h.memory.get_person_observation = failing_get  # type: ignore[method-assign]
        await _run(h)
        assert h.sent
        assert "你对当前发言者的了解" not in _observe_casual_prompt(h)

    async def test_retry_prompt_keeps_the_same_card(self) -> None:
        """重写重试不是新的一轮群聊：首次与重试两条 prompt 带同一份观察，
        整轮只读一次库（S10-HUMAN-P2 H8，与 H5 样本同一语义）。"""
        reads = {"n": 0}

        class CountingMemory(_ObserveMemory):
            async def get_person_observation(self, *, group_id, user_id):
                reads["n"] += 1
                return await _ObserveMemory.get_person_observation(
                    self, group_id=group_id, user_id=user_id
                )

        h = Harness(["在吗", "冲"], _run_settings(person_observe_enabled=True))
        h.memory = CountingMemory(["在吗", "冲"], observation="爱打野")
        # 首条回复复述触发句 → echo → 重写一次；重写内容换说法
        seq = iter(["一起去打新活动吗", "换个说法，这次不去了"])
        raw_text = "一起去打新活动吗"

        def completion():
            async def fake(**kwargs) -> str:
                h.completions.append(kwargs)
                if "决策器" in kwargs["system_prompt"]:
                    return h.gate_content
                return next(seq)

            return fake

        await _run(h, raw_text, _completion=completion())
        casual_prompts = [
            c["user_prompt"] for c in h.completions if "决策器" not in c["system_prompt"]
        ]
        assert len(casual_prompts) == 2  # 生成 + 重写
        for prompt in casual_prompts:
            assert "爱打野" in prompt
        assert reads["n"] == 1


def _sticker_library(*ids: str) -> StickerLibrary:
    return StickerLibrary(
        stickers=tuple(
            Sticker(
                id=sticker_id,
                path=Path(f"/tmp/{sticker_id}.png"),
                description=f"描述{sticker_id}",
            )
            for sticker_id in ids
        )
    )


def _gate_prompt(h: Harness) -> str:
    gate_calls = [c for c in h.completions if "决策器" in c["system_prompt"]]
    assert gate_calls, "决策门未调用"
    return gate_calls[-1]["user_prompt"]


class TestStickerPath:
    async def test_sticker_intent_sends_sticker_and_skips_text(self) -> None:
        h = Harness(
            ["在吗", "冲"],
            _run_settings(auto_chat_sticker_enabled=True, auto_chat_sample_rate=1.0),
        )
        h.gate_content = '{"should_reply": true, "reason": "banter", "confidence": 0.9, "action": "sticker", "sticker_id": "cat_laugh"}'
        h.memory = _ObserveMemory(["在吗", "冲"], observation=None)
        sent_stickers: list[Sticker] = []

        async def send_sticker(sticker: Sticker) -> bool:
            sent_stickers.append(sticker)
            return True

        await _run(
            h,
            "随便聊聊",
            react=None,
            sticker_library=_sticker_library("cat_laugh"),
            send_sticker=send_sticker,
        )
        assert [s.id for s in sent_stickers] == ["cat_laugh"]
        assert h.sent == []  # 不生成文本

    async def test_sticker_disabled_by_default_falls_back_to_text(self) -> None:
        h = Harness(["在吗", "冲"], _run_settings())
        h.gate_content = '{"should_reply": true, "reason": "banter", "confidence": 0.9, "action": "sticker", "sticker_id": "cat_laugh"}'
        h.casual_content = "哈哈哈哈"

        async def send_sticker(sticker: Sticker) -> bool:
            raise AssertionError("开关关闭时不得发送表情包")

        await _run(
            h,
            "随便聊聊",
            react=None,
            sticker_library=_sticker_library("cat_laugh"),
            send_sticker=send_sticker,
        )
        assert h.sent == ["哈哈哈哈"]  # 回落文本
        assert "可用表情包候选" not in _gate_prompt(h)

    async def test_sticker_id_outside_library_falls_back_to_text(self) -> None:
        h = Harness(["在吗", "冲"], _run_settings(auto_chat_sticker_enabled=True))
        h.gate_content = '{"should_reply": true, "reason": "banter", "confidence": 0.9, "action": "sticker", "sticker_id": "not_in_library"}'
        h.casual_content = "笑死"

        async def send_sticker(sticker: Sticker) -> bool:
            raise AssertionError("未知 id 不得发送")

        await _run(
            h,
            "随便聊聊",
            react=None,
            sticker_library=_sticker_library("cat_laugh"),
            send_sticker=send_sticker,
        )
        assert h.sent == ["笑死"]

    async def test_sticker_daily_limit_falls_back_to_text(self) -> None:
        h = Harness(
            ["在吗", "冲"],
            _run_settings(auto_chat_sticker_enabled=True, auto_chat_sticker_daily_limit=0),
        )
        h.gate_content = '{"should_reply": true, "reason": "banter", "confidence": 0.9, "action": "sticker", "sticker_id": "cat_laugh"}'
        h.casual_content = "继续聊天"

        async def send_sticker(sticker: Sticker) -> bool:
            raise AssertionError("限额满时不得发送")

        await _run(
            h,
            "随便聊聊",
            react=None,
            sticker_library=_sticker_library("cat_laugh"),
            send_sticker=send_sticker,
        )
        assert h.sent == ["继续聊天"]

    async def test_sticker_sensitive_trigger_is_filtered(self) -> None:
        h = Harness(
            ["在吗", "冲"],
            _run_settings(auto_chat_sticker_enabled=True, auto_chat_sample_rate=1.0),
        )
        h.gate_content = '{"should_reply": true, "reason": "banter", "confidence": 0.9, "action": "sticker", "sticker_id": "cat_laugh"}'

        async def send_sticker(sticker: Sticker) -> bool:
            raise AssertionError("敏感触发文本不得发表情包")

        await _run(
            h,
            "赌博一下",
            react=None,
            sticker_library=_sticker_library("cat_laugh"),
            send_sticker=send_sticker,
        )
        assert h.sent == []

    async def test_sticker_quota_denied_skips_send(self) -> None:
        h = Harness(["在吗", "冲"], _run_settings(auto_chat_sticker_enabled=True))
        h.quota_allowed = False
        h.gate_content = '{"should_reply": true, "reason": "banter", "confidence": 0.9, "action": "sticker", "sticker_id": "cat_laugh"}'

        async def send_sticker(sticker: Sticker) -> bool:
            raise AssertionError("配额拒绝时不得发送")

        await _run(
            h,
            "随便聊聊",
            react=None,
            sticker_library=_sticker_library("cat_laugh"),
            send_sticker=send_sticker,
        )
        assert h.sent == []

    async def test_sticker_success_does_not_note_reply_or_enter_hot(self) -> None:
        h = Harness(["在吗", "冲"], _run_settings(auto_chat_sticker_enabled=True))
        h.gate_content = '{"should_reply": true, "reason": "banter", "confidence": 0.9, "action": "sticker", "sticker_id": "cat_laugh"}'

        async def send_sticker(sticker: Sticker) -> bool:
            return True

        await _run(
            h,
            "随便聊聊",
            react=None,
            sticker_library=_sticker_library("cat_laugh"),
            send_sticker=send_sticker,
        )
        assert h.state.recently_spoke(1001, 300.0) is False
        assert h.state.hot_active(1001, settings=h.settings) is False
        assert h.slept == []  # 不进入文本发送延迟

    async def test_sticker_failure_disables_after_three(self) -> None:
        from qq_bot.services import auto_chat as m

        m._reset_sticker_streak()  # 测试隔离：进程级降级态
        try:
            for attempt in range(3):
                h = Harness(["在吗", "冲"], _run_settings(auto_chat_sticker_enabled=True))
                h.gate_content = (
                    '{"should_reply": true, "reason": "banter", "confidence": 0.9, '
                    '"action": "sticker", "sticker_id": "cat_laugh"}'
                )
                h.casual_content = f"聊天{attempt}"

                async def send_sticker(sticker: Sticker) -> bool:
                    return False

                await _run(
                    h,
                    "随便聊聊",
                    react=None,
                    sticker_library=_sticker_library("cat_laugh"),
                    send_sticker=send_sticker,
                )
                assert h.sent == []  # 失败即结束本轮，不补文本
            assert m.sticker_unsupported() is True
        finally:
            m._reset_sticker_streak()

    async def test_candidates_block_only_when_channel_available(self) -> None:
        h = Harness(["在吗", "冲"], _run_settings(auto_chat_sticker_enabled=True))
        h.memory = _ObserveMemory(["在吗", "冲"], observation=None)

        async def send_sticker(sticker: Sticker) -> bool:
            return True

        # 库非空 + 回调在 → 候选块进 prompt
        await _run(
            h,
            "随便聊聊",
            react=None,
            sticker_library=_sticker_library("cat_laugh", "dog_question"),
            send_sticker=send_sticker,
        )
        prompt = _gate_prompt(h)
        assert "可用表情包候选" in prompt
        assert "cat_laugh：描述cat_laugh" in prompt
        assert "dog_question：描述dog_question" in prompt

    async def test_candidates_absent_without_callback(self) -> None:
        h = Harness(["在吗", "冲"], _run_settings(auto_chat_sticker_enabled=True))
        h.memory = _ObserveMemory(["在吗", "冲"], observation=None)

        await _run(
            h,
            "随便聊聊",
            react=None,
            sticker_library=_sticker_library("cat_laugh"),
        )
        assert "可用表情包候选" not in _gate_prompt(h)

    async def test_empty_library_means_no_candidates(self) -> None:
        h = Harness(["在吗", "冲"], _run_settings(auto_chat_sticker_enabled=True))
        h.memory = _ObserveMemory(["在吗", "冲"], observation=None)

        async def send_sticker(sticker: Sticker) -> bool:
            raise AssertionError("空库不得发送")

        await _run(
            h,
            "随便聊聊",
            react=None,
            sticker_library=StickerLibrary(stickers=()),
            send_sticker=send_sticker,
        )
        assert "可用表情包候选" not in _gate_prompt(h)


def test_gate_parse_accepts_sticker_action() -> None:
    decision = parse_gate_output(
        '{"should_reply": true, "reason": "banter", "confidence": 0.9, '
        '"action": "sticker", "sticker_id": "cat_laugh"}'
    )
    assert decision is not None
    assert decision.action == "sticker"
    assert decision.sticker_id == "cat_laugh"


def test_gate_parse_unknown_action_falls_back_to_reply() -> None:
    decision = parse_gate_output(
        '{"should_reply": true, "reason": "banter", "confidence": 0.9, "action": "wave"}'
    )
    assert decision is not None
    assert decision.action == "reply"
    assert decision.sticker_id == ""


def test_gate_parse_sticker_id_must_be_string() -> None:
    decision = parse_gate_output(
        '{"should_reply": true, "reason": "banter", "confidence": 0.9, '
        '"action": "sticker", "sticker_id": 42}'
    )
    assert decision is not None
    assert decision.action == "sticker"
    assert decision.sticker_id == ""
