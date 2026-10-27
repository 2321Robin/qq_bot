"""断线补答（S12-CATCHUP-01）测试。

解析/计划是纯函数直接断言；编排层用注入的 fake IO 验证端到端行为：
补库条数、补答次数、announce 一次性、quota 拒绝静默、水位推进幂等。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from qq_bot.config import BotSettings
from qq_bot.services import catchup as catchup_service
from qq_bot.services.catchup import (
    CatchupStateStore,
    HistoricalMessage,
    LiveWatermark,
    parse_history,
    plan_catchup,
    run_catch_up,
)

SELF_ID = 2381444078


@pytest.fixture(autouse=True)
def _isolated_live_watermark(monkeypatch: pytest.MonkeyPatch, tmp_path_factory):
    """进程级共享水位单例每例重置：测试间互不泄漏（在线水位跨测试残留
    会改变 plan 的有效水位）。"""
    store = CatchupStateStore(tmp_path_factory.mktemp("live") / "live_state.json")
    monkeypatch.setattr(catchup_service, "_LIVE_WATERMARK", LiveWatermark(store))


def _history_payload(*items: dict) -> dict:
    return {"data": {"messages": list(items)}}


def _msg(
    time: int,
    user_id: int,
    segments: list[dict] | str,
) -> dict:
    return {
        "time": time,
        "user_id": user_id,
        "sender": {"user_id": user_id, "nickname": f"user{user_id}"},
        "message": segments,
    }


def _text(value: str) -> dict:
    return {"type": "text", "data": {"text": value}}


def _at(qq: int) -> dict:
    return {"type": "at", "data": {"qq": str(qq)}}


# ---- parse_history ----


def test_parse_history_extracts_text_and_self_mention() -> None:
    parsed = parse_history(
        _history_payload(
            _msg(100, 2001, [_at(SELF_ID), _text(" 在吗")]),
            _msg(101, 2002, [_text("闲聊一句")]),
        ),
        self_id=SELF_ID,
    )
    assert parsed == [
        HistoricalMessage(time=100, user_id=2001, text="在吗", mentions_self=True),
        HistoricalMessage(time=101, user_id=2002, text="闲聊一句", mentions_self=False),
    ]


def test_parse_history_skips_bot_self_and_invalid_rows() -> None:
    parsed = parse_history(
        _history_payload(
            _msg(100, SELF_ID, [_text("机器人自己的回复")]),  # 自己
            {"time": 0, "user_id": 2001, "message": []},  # 无时间
            _msg(102, 2003, []),  # 无任何内容
            _msg(103, 2004, [_text("正常")]),
        ),
        self_id=SELF_ID,
    )
    assert [m.user_id for m in parsed] == [2004]


def test_parse_history_image_and_face_placeholders() -> None:
    parsed = parse_history(
        _history_payload(
            _msg(100, 2001, [{"type": "image", "data": {"url": "x"}}]),
            _msg(101, 2002, [{"type": "face", "data": {"id": "1"}}]),
            _msg(102, 2003, [_at(SELF_ID)]),  # 只 @ 不说话：无文本无占位，跳过
        ),
        self_id=SELF_ID,
    )
    assert [m.text for m in parsed] == ["[图片]", "[表情]"]


def test_parse_history_cq_string_format() -> None:
    raw = f"[CQ:at,qq={SELF_ID}] 帮我查一下"
    parsed = parse_history(
        _history_payload(_msg(100, 2001, raw)),
        self_id=SELF_ID,
    )
    assert parsed[0].text == "帮我查一下"
    assert parsed[0].mentions_self is True


def test_parse_history_sorts_by_time() -> None:
    parsed = parse_history(
        _history_payload(_msg(200, 2001, [_text("后")]), _msg(100, 2002, [_text("先")])),
        self_id=SELF_ID,
    )
    assert [m.time for m in parsed] == [100, 200]


def test_parse_history_tolerates_foreign_shapes() -> None:
    assert parse_history(None, self_id=SELF_ID) == []
    assert parse_history({"messages": "not-a-list"}, self_id=SELF_ID) == []
    assert parse_history({"data": [_msg(100, 2001, [_text("裸列表")])]}, self_id=SELF_ID)


# ---- plan_catchup ----


def _hist(time: int, user_id: int = 2001, *, call: bool = False) -> HistoricalMessage:
    return HistoricalMessage(time=time, user_id=user_id, text="问个事", mentions_self=call)


def test_plan_first_start_initializes_watermark_without_replay() -> None:
    plan = plan_catchup(
        [_hist(100), _hist(200, call=True)],
        watermark=None,
        now_epoch=1000,
        max_age_seconds=7200,
        max_replies=3,
    )
    assert plan.first_start is True
    assert plan.backfill == ()
    assert plan.calls == ()
    assert plan.new_watermark == 200


def test_plan_first_start_without_messages_uses_now() -> None:
    plan = plan_catchup([], watermark=None, now_epoch=1000, max_age_seconds=7200, max_replies=3)
    assert plan.first_start is True
    assert plan.new_watermark == 1000


def test_plan_no_new_messages_keeps_watermark() -> None:
    plan = plan_catchup(
        [_hist(100)],
        watermark=100,
        now_epoch=1000,
        max_age_seconds=7200,
        max_replies=3,
    )
    assert plan.backfill == ()
    assert plan.new_watermark == 100


def test_plan_within_window_backfills_and_collects_calls() -> None:
    plan = plan_catchup(
        [_hist(100), _hist(101), _hist(102, call=True), _hist(103)],
        watermark=100,
        now_epoch=103,
        max_age_seconds=7200,
        max_replies=3,
    )
    assert [m.time for m in plan.backfill] == [101, 102, 103]
    assert [m.time for m in plan.calls] == [102]
    assert plan.skipped_old == 0
    assert plan.new_watermark == 103


def test_plan_drops_calls_older_than_window_but_advances_watermark() -> None:
    # watermark=1000，now=9000，max_age=3600 → 窗口起点 5400：
    # 1100 超窗跳过；5500 在窗内。
    plan = plan_catchup(
        [_hist(1100, call=True), _hist(5500, call=True)],
        watermark=1000,
        now_epoch=9000,
        max_age_seconds=3600,
        max_replies=3,
    )
    assert plan.skipped_old == 1
    assert [m.time for m in plan.calls] == [5500]
    assert plan.new_watermark == 5500  # 超窗消息一次性放行


def test_plan_truncates_calls_to_latest_max_replies() -> None:
    plan = plan_catchup(
        [_hist(101, call=True), _hist(102, call=True), _hist(103, call=True), _hist(104)],
        watermark=100,
        now_epoch=104,
        max_age_seconds=7200,
        max_replies=2,
    )
    assert [m.time for m in plan.calls] == [102, 103]  # 保留最新两条
    assert plan.truncated_calls == 1
    assert len(plan.backfill) == 4  # 被裁的呼唤仍补库


def test_plan_placeholder_only_call_is_not_answered() -> None:
    placeholder = HistoricalMessage(time=101, user_id=2001, text="[图片]", mentions_self=True)
    plan = plan_catchup(
        [placeholder],
        watermark=100,
        now_epoch=101,
        max_age_seconds=7200,
        max_replies=3,
    )
    assert plan.calls == ()
    assert plan.backfill == (placeholder,)


# ---- CatchupStateStore ----


def test_state_store_roundtrip(tmp_path) -> None:
    store = CatchupStateStore(tmp_path / "catchup_state.json")
    store.save({1001: 123, 1002: 456})
    assert store.load() == {1001: 123, 1002: 456}


def test_state_store_tolerates_corrupt_file(tmp_path) -> None:
    path = tmp_path / "catchup_state.json"
    path.write_text("{not json", encoding="utf-8")
    assert CatchupStateStore(path).load() == {}


def test_state_store_skips_invalid_entries(tmp_path) -> None:
    path = tmp_path / "catchup_state.json"
    path.write_text(json.dumps({"1001": 5, "bad": "x", "1002": "nan"}), encoding="utf-8")
    assert CatchupStateStore(path).load() == {1001: 5}


# ---- run_catch_up 编排 ----


class FakeMemoryStore:
    def __init__(self) -> None:
        self.added: list[dict] = []
        self.replies: dict[int, str] = {}
        self.duplicates: set[str] = set()  # has_message_like 命中的文本
        self._next_id = 1

    async def has_message_like(
        self, group_id, user_id, message_text, *, around, window_seconds: int = 180
    ) -> bool:
        return message_text in self.duplicates

    async def add_message(self, **kwargs) -> int:
        row_id = self._next_id
        self._next_id += 1
        self.added.append({**kwargs, "row_id": row_id})
        return row_id

    async def update_ai_reply(self, message_id: int, ai_reply: str) -> None:
        self.replies[message_id] = ai_reply

    async def recent_group_messages(self, *, group_id: int, limit: int):
        return ()


class Harness:
    """注入版 run_catch_up 环境，全部 IO 可编程。"""

    def __init__(self, payloads: dict[int, object], *, settings: BotSettings | None = None):
        self.settings = settings or BotSettings()
        self.payloads = payloads
        self.memory = FakeMemoryStore()
        self.sent: list[tuple[int, str, int | None]] = []
        self.reply_prompts: list[str] = []
        self.reply_contexts: list[str] = []
        self.reply_hints: list[str] = []
        self.reply_result: str | Exception = "补答内容"
        self.quota_allowed = True
        self.quota_groups: list[int] = []
        self.sleeps: list[float] = []

    async def fetch_history(self, **kwargs):
        return self.payloads[kwargs["group_id"]]

    async def group_ids(self) -> list[int]:
        return list(self.payloads)

    async def send(self, group_id: int, text: str, at_user_id: int | None) -> None:
        self.sent.append((group_id, text, at_user_id))

    async def reply(self, prompt: str, **kwargs) -> str:
        if isinstance(self.reply_result, Exception):
            raise self.reply_result
        self.reply_prompts.append(prompt)
        self.reply_contexts.append(kwargs.get("chat_context", ""))
        self.reply_hints.append(kwargs.get("extra_system_prompt", ""))
        return self.reply_result

    async def quota(self, group_id: int) -> bool:
        self.quota_groups.append(group_id)
        return self.quota_allowed

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    async def context_rows(self, group_id: int):
        return ()

    async def run(
        self,
        state_store: CatchupStateStore,
        *,
        self_id: int = SELF_ID,
        live_watermark: LiveWatermark | None = None,
    ) -> None:
        await run_catch_up(
            settings=self.settings,
            state_store=state_store,
            self_id=self_id,
            fetch_history_fn=self.fetch_history,
            group_ids_fn=self.group_ids,
            send_fn=self.send,
            reply_fn=self.reply,
            memory_store=self.memory,
            quota_check_fn=self.quota,
            context_rows_fn=self.context_rows,
            sleep_fn=self.sleep,
            now_epoch_fn=lambda: 10_000,
            rng=lambda: 0.5,
            live_watermark=live_watermark,
        )


@pytest.fixture
def state_store(tmp_path) -> CatchupStateStore:
    return CatchupStateStore(tmp_path / "catchup_state.json")


def test_run_first_start_only_writes_watermark(state_store) -> None:
    harness = Harness(
        {1001: _history_payload(_msg(9500, 2001, [_at(SELF_ID), _text("掉线前呼唤")]))}
    )
    asyncio.run(harness.run(state_store))
    assert harness.sent == []
    assert harness.memory.added == []
    assert state_store.load() == {1001: 9500}


def test_run_backfills_and_answers_missed_calls(state_store) -> None:
    state_store.save({1001: 9000})
    harness = Harness(
        {
            1001: _history_payload(
                _msg(9100, 2001, [_text("掉线期间闲聊")]),
                _msg(9200, 2002, [_at(SELF_ID), _text("鸡煲在吗")]),
                _msg(9300, 2001, [_text("还在掉线")]),
                _msg(9600, SELF_ID, [_text("机器人自己的话")]),  # 剔除
            )
        }
    )
    asyncio.run(harness.run(state_store))

    # 补库：三条群消息按真实时间入库，呼唤标记 is_ai_prompt
    assert [(a["user_id"], a["is_ai_prompt"], a["message_text"]) for a in harness.memory.added] == [
        (2001, False, "掉线期间闲聊"),
        (2002, True, "鸡煲在吗"),
        (2001, False, "还在掉线"),
    ]
    assert all(a["created_at"].year for a in harness.memory.added)

    # 发送：一条 announce + 一条带 at 的补答
    announce = [s for s in harness.sent if s[2] is None]
    replies = [s for s in harness.sent if s[2] is not None]
    assert len(announce) == 1
    assert len(replies) == 1
    assert replies[0] == (1001, "补答内容", 2002)

    # 补答回填到呼唤的库行
    call_row = next(a for a in harness.memory.added if a["is_ai_prompt"])
    assert harness.memory.replies[call_row["row_id"]] == "补答内容"
    # 时效提示：now=10000、消息 time=9200 → 800 秒 ≈ 13 分钟前
    assert len(harness.reply_hints) == 1
    assert "13 分钟前" in harness.reply_hints[0]
    assert "掉线" in harness.reply_hints[0]
    # quota 按群检查过
    assert harness.quota_groups == [1001]
    # 水位推进到最新（机器人自身消息 9600 不参与，最新群消息是 9300）
    assert state_store.load() == {1001: 9300}


def test_run_quota_denied_sends_nothing(state_store) -> None:
    state_store.save({1001: 9000})
    harness = Harness({1001: _history_payload(_msg(9200, 2002, [_at(SELF_ID), _text("在吗")]))})
    harness.quota_allowed = False
    asyncio.run(harness.run(state_store))
    assert harness.sent == []
    assert harness.reply_prompts == []
    # 补库照常（记忆完整性不受 quota 影响）
    assert len(harness.memory.added) == 1


def test_run_model_failure_skips_call_but_keeps_backfill(state_store) -> None:
    state_store.save({1001: 9000})
    harness = Harness({1001: _history_payload(_msg(9200, 2002, [_at(SELF_ID), _text("在吗")]))})
    harness.reply_result = RuntimeError("model down")
    asyncio.run(harness.run(state_store))
    # announce 在模型调用之前已发出（quota 已过）；补答本身失败静默跳过
    assert [s[2] for s in harness.sent] == [None]
    assert len(harness.memory.added) == 1  # 补库不受模型失败影响


def test_run_announce_disabled(state_store) -> None:
    state_store.save({1001: 9000})
    settings = BotSettings(catchup_announce=False)
    harness = Harness(
        {1001: _history_payload(_msg(9200, 2002, [_at(SELF_ID), _text("在吗")]))},
        settings=settings,
    )
    asyncio.run(harness.run(state_store))
    assert all(s[2] is not None for s in harness.sent)  # 只有补答，没有 announce


def test_run_truncates_to_max_replies(state_store) -> None:
    state_store.save({1001: 9000})
    settings = BotSettings(catchup_max_replies=1)
    harness = Harness(
        {
            1001: _history_payload(
                _msg(9100, 2002, [_at(SELF_ID), _text("第一条呼唤")]),
                _msg(9200, 2003, [_at(SELF_ID), _text("第二条呼唤")]),
            )
        },
        settings=settings,
    )
    asyncio.run(harness.run(state_store))
    answered = [s for s in harness.sent if s[2] is not None]
    assert len(answered) == 1
    assert answered[0][2] == 2003  # 保留最新
    assert len(harness.memory.added) == 2  # 被裁的呼唤仍补库


# ---- S12-CATCHUP-02：在线水位 + 入库去重 ----


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_live_watermark_note_and_effective(tmp_path) -> None:
    store = CatchupStateStore(tmp_path / "state.json")
    store.save({1001: 100, 1002: 50})
    lw = LiveWatermark(store, clock=_FakeClock())

    assert lw.effective(1001, 100) == 100  # 未登记在线水位 → 持久化值
    lw.note(1001, 200)
    assert lw.effective(1001, 100) == 200  # 在线值更大
    assert lw.effective(1002, None) is None
    lw.note(1002, 40)  # 早于持久化值也登记（取 max 在 effective）
    assert lw.effective(1002, 50) == 50
    lw.note(1002, 60)
    assert lw.effective(1002, 50) == 60


def test_live_watermark_flush_is_throttled_and_merges(tmp_path) -> None:
    path = tmp_path / "state.json"
    store = CatchupStateStore(path)
    clock = _FakeClock()
    lw = LiveWatermark(store, flush_interval_seconds=30.0, clock=clock)

    lw.note(1001, 200)
    assert store.load() == {}  # 节流窗口内不落盘
    clock.advance(30.0)
    lw.note(1001, 300)
    assert store.load() == {1001: 300}  # 过窗合并写回
    lw.note(1001, 250)  # 更小的值不回退
    clock.advance(30.0)
    lw.note(1001, 250)
    assert store.load() == {1001: 300}


async def test_flush_writes_pending_watermark(tmp_path) -> None:
    store = CatchupStateStore(tmp_path / "state.json")
    lw = LiveWatermark(store, clock=_FakeClock())
    lw.note(1001, 500)
    await lw.flush()
    assert store.load() == {1001: 500}


def test_run_live_watermark_prevents_reanswer_of_online_messages(state_store, tmp_path) -> None:
    """部署重启场景（2026-10-27 线上事故）：持久化水位落后于在线水位——
    在线期间已处理的消息不再被补答重复入库/重答。"""
    live = LiveWatermark(CatchupStateStore(tmp_path / "live.json"))
    live.note(1001, 9800)  # 在线已处理到 9800
    state_store.save({1001: 9000})  # 补答水位落后
    harness = Harness(
        {
            1001: _history_payload(
                _msg(9200, 2002, [_at(SELF_ID), _text("在线时已答过的呼唤")]),
                _msg(9900, 2002, [_at(SELF_ID), _text("真掉线期间的呼唤")]),
            )
        }
    )
    import asyncio

    asyncio.run(harness.run(state_store, live_watermark=live))
    replies = [s for s in harness.sent if s[2] is not None]
    assert len(replies) == 1
    assert harness.reply_prompts == ["真掉线期间的呼唤"]
    assert [a["message_text"] for a in harness.memory.added] == ["真掉线期间的呼唤"]
    assert state_store.load() == {1001: 9900}
    assert live.get(1001) == 9900  # 补答水位同步回在线状态


def test_run_deduped_calls_skip_backfill_and_announce(state_store) -> None:
    """水位缝隙兜底：在线 handler 已入库的同文同行不再补库，呼唤不再重答，
    全部呼唤被去重时连 announce 都不发（无事可做的补答最惊吓）。"""
    state_store.save({1001: 9000})
    harness = Harness(
        {
            1001: _history_payload(
                _msg(9100, 2001, [_text("在线时聊过的话")]),
                _msg(9200, 2002, [_at(SELF_ID), _text("在线时已答过的呼唤")]),
                _msg(9300, 2003, [_text("真掉线的闲聊")]),
            )
        }
    )
    harness.memory.duplicates = {"在线时聊过的话", "在线时已答过的呼唤"}
    import asyncio

    asyncio.run(harness.run(state_store))
    assert [a["message_text"] for a in harness.memory.added] == ["真掉线的闲聊"]
    assert harness.sent == []  # 无呼唤可答 → 无 announce 无补答
    assert harness.quota_groups == []


def test_run_dedup_probe_failure_falls_back_to_insert(state_store) -> None:
    """去重探测不可用（旧存储/异常）→ 放行原路径，不因探测失败丢补库。"""
    state_store.save({1001: 9000})
    harness = Harness({1001: _history_payload(_msg(9200, 2002, [_text("闲聊")]))})

    class BrokenProbeStore(FakeMemoryStore):
        async def has_message_like(self, *args, **kwargs):
            raise RuntimeError("probe unavailable")

    harness.memory = BrokenProbeStore()
    import asyncio

    asyncio.run(harness.run(state_store))
    assert len(harness.memory.added) == 1


def test_run_second_round_is_idempotent(state_store) -> None:
    state_store.save({1001: 9000})
    payload = {1001: _history_payload(_msg(9200, 2002, [_at(SELF_ID), _text("在吗")]))}
    harness = Harness(payload)
    asyncio.run(harness.run(state_store))
    first_sent = len(harness.sent)

    harness2 = Harness(payload)
    asyncio.run(harness2.run(state_store))
    assert harness2.sent == []
    assert harness2.memory.added == []
    assert first_sent == 2  # announce + 补答


def test_run_skips_groups_beyond_window(state_store) -> None:
    # 水位 100，消息 200，now=10_000，窗口 7200 → 200 超窗，全部跳过
    # （窗口显式传入：默认值已放宽到 12h，不再天然覆盖此场景）
    state_store.save({1001: 100})
    harness = Harness(
        {1001: _history_payload(_msg(200, 2002, [_at(SELF_ID), _text("太久远了")]))},
        settings=BotSettings(catchup_max_age_seconds=7200),
    )
    asyncio.run(harness.run(state_store))
    assert harness.sent == []
    assert harness.memory.added == []
    assert state_store.load() == {1001: 200}  # 水位推进，一次性放行


def test_run_disabled_is_noop(state_store) -> None:
    settings = BotSettings(catchup_enabled=False)
    harness = Harness({1001: _history_payload()}, settings=settings)
    asyncio.run(harness.run(state_store))
    assert harness.sent == []
    assert state_store.load() == {}


def test_run_group_fetch_error_does_not_break_other_groups(state_store) -> None:
    class FlakyHarness(Harness):
        async def fetch_history(self, **kwargs):
            if kwargs["group_id"] == 1001:
                raise RuntimeError("api error")
            return await super().fetch_history(**kwargs)

    state_store.save({1001: 9000, 1002: 9000})
    harness = FlakyHarness(
        {
            1001: _history_payload(),
            1002: _history_payload(_msg(9200, 2002, [_at(SELF_ID), _text("在吗")])),
        }
    )
    asyncio.run(harness.run(state_store))
    # 1002 正常补答，1001 无消息但水位仍在
    assert any(s[2] is not None for s in harness.sent)
