"""Scheduler plugin dispatch tests (S6-SCHED-03).

``run_scheduled_job`` is dependency-injected (bot + settings passed by the
caller) so the generalized path is testable without a live NoneBot driver.
"""

from __future__ import annotations

import asyncio
import json

from datetime import date

import bot  # noqa: F401  (initializes NoneBot before plugin imports)
import pytest
from nonebot.adapters.onebot.v11.exception import NetworkError
from prometheus_client import REGISTRY

from qq_bot.config import BotSettings
from qq_bot.plugins import scheduler as scheduler_plugin
from qq_bot.plugins.scheduler import run_scheduled_job
from qq_bot.services.game_calendar import GameCalendarError
from qq_bot.services import scheduler_jobs as scheduler_jobs_module
from qq_bot.services import scheduled_sender as scheduled_sender_module
from qq_bot.services.reliability import CircuitBreaker
from qq_bot.services.scheduler_jobs import ScheduledJob
from qq_bot.services.scheduled_sender import build_scheduler_jobs_kwargs


@pytest.fixture(autouse=True)
def _fresh_onebot_breaker(monkeypatch):
    """Isolate each test from the shared module-level OneBot breaker."""
    breaker = CircuitBreaker(name="onebot", failure_threshold=3, recovery_seconds=30)
    monkeypatch.setattr(scheduled_sender_module, "_onebot_breaker", lambda: breaker)
    return breaker


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, object]] = []

    async def send_group_msg(self, *, group_id: int, message: object) -> None:
        self.sent.append((group_id, message))


def _job_metric(job: str, result: str) -> float:
    return (
        REGISTRY.get_sample_value("qq_bot_scheduled_jobs_total", {"job": job, "result": result})
        or 0.0
    )


def _configured_settings(**overrides: str) -> BotSettings:
    values = {
        "scheduled_jobs": "static@09:00",
        "scheduled_group_ids": "111",
        "allowed_group_ids": "111",
        "scheduled_message": "早上好",
    }
    values.update(overrides)
    return BotSettings(**values)  # type: ignore[arg-type]


async def test_static_job_sends_configured_message() -> None:
    fake_bot = FakeBot()
    before = _job_metric("static", "ok")

    await run_scheduled_job(
        ScheduledJob(job_type="static", hour=9, minute=0),
        fake_bot,
        settings=_configured_settings(),
    )

    assert [(gid, msg.extract_plain_text()) for gid, msg in fake_bot.sent] == [(111, "早上好")]
    assert _job_metric("static", "ok") == before + 1


async def test_empty_builder_result_skips_sending(monkeypatch: pytest.MonkeyPatch) -> None:
    async def empty_builder(settings: BotSettings) -> str | None:
        return ""

    monkeypatch.setitem(scheduler_jobs_module._CONTENT_BUILDERS, "life_morning", empty_builder)
    fake_bot = FakeBot()
    before = _job_metric("life_morning", "skipped_empty")

    await run_scheduled_job(
        ScheduledJob(job_type="life_morning", hour=7, minute=30),
        fake_bot,
        settings=_configured_settings(),
    )

    assert fake_bot.sent == []
    assert _job_metric("life_morning", "skipped_empty") == before + 1


async def test_missing_builder_skips_with_metric() -> None:
    # game_morning 未注册是 Stage B 阶段常态（Stage C 落地时注册）
    fake_bot = FakeBot()
    before = _job_metric("game_morning", "skipped_no_builder")

    await run_scheduled_job(
        ScheduledJob(job_type="game_morning", hour=7, minute=30),
        fake_bot,
        settings=_configured_settings(),
    )

    assert fake_bot.sent == []
    assert _job_metric("game_morning", "skipped_no_builder") == before + 1


async def test_no_bot_skips_with_metric() -> None:
    before = _job_metric("static", "skipped_no_bot")

    await run_scheduled_job(
        ScheduledJob(job_type="static", hour=9, minute=0),
        None,
        settings=_configured_settings(),
    )

    assert _job_metric("static", "skipped_no_bot") == before + 1


async def test_no_allowed_groups_skips_with_metric() -> None:
    fake_bot = FakeBot()
    before = _job_metric("static", "skipped_no_groups")

    await run_scheduled_job(
        ScheduledJob(job_type="static", hour=9, minute=0),
        fake_bot,
        settings=_configured_settings(scheduled_group_ids=""),
    )

    assert fake_bot.sent == []
    assert _job_metric("static", "skipped_no_groups") == before + 1


async def test_legacy_path_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _configured_settings(
        scheduled_jobs="",
        scheduled_cron_times="11:00,16:10",
    )

    assert [kwargs["id"] for kwargs in build_scheduler_jobs_kwargs(settings)] == [
        "daily_group_message_1100",
        "daily_group_message_1610",
    ]

    fake_bot = FakeBot()
    recorded: list[tuple[object, list[int], str]] = []

    async def fake_send_group_messages(bot_arg, group_ids, message, **kwargs):
        recorded.append((bot_arg, group_ids, message))
        return []

    monkeypatch.setattr(scheduler_plugin, "get_settings", lambda: settings)
    monkeypatch.setattr(scheduler_plugin, "OneBotV11Bot", FakeBot)
    monkeypatch.setattr(scheduler_plugin, "get_bots", lambda: {"conn1": fake_bot})
    monkeypatch.setattr(scheduler_plugin, "send_group_messages", fake_send_group_messages)

    await scheduler_plugin.send_daily_messages()

    assert recorded == [(fake_bot, [111], "早上好")]


async def test_game_job_registration_requires_valid_calendar(tmp_path) -> None:
    # S6-GAME-04：注册 game_* job 时加载日历；坏文件 → GameCalendarError（启动即失败）
    bad = tmp_path / "bad.json"
    bad.write_text('{"schema_version": 2}', encoding="utf-8")
    settings = _configured_settings(
        scheduled_jobs="game_morning@07:30",
        game_calendar_path=str(bad),
    )
    with pytest.raises(GameCalendarError):
        scheduler_plugin._register_game_builders(settings)

    # 合法文件（版本活动就在今天）→ builder 注册生效，run_scheduled_job 全链路产出消息
    good = tmp_path / "good.json"
    good.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "events": [
                    {
                        "game": "原神",
                        "kind": "version",
                        "title": "7.0版本更新",
                        "start": date.today().isoformat(),
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    settings = _configured_settings(
        scheduled_jobs="game_morning@07:30",
        game_calendar_path=str(good),
    )
    fake_bot = FakeBot()
    before = _job_metric("game_morning", "ok")
    try:
        scheduler_plugin._register_game_builders(settings)
        await run_scheduled_job(
            ScheduledJob(job_type="game_morning", hour=7, minute=30),
            fake_bot,
            settings=settings,
        )
    finally:
        scheduler_jobs_module._CONTENT_BUILDERS.pop("game_morning", None)
        scheduler_jobs_module._CONTENT_BUILDERS.pop("game_evening", None)

    assert len(fake_bot.sent) == 1
    group_id, message = fake_bot.sent[0]
    assert group_id == 111
    assert "【游戏早报】" in message.extract_plain_text()
    assert "原神 7.0版本更新 今日开服" in message.extract_plain_text()
    assert _job_metric("game_morning", "ok") == before + 1


async def test_life_job_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """life_* 全链路：注册 → 组装（假客户端）→ 发送；指标 ok。"""
    from qq_bot.services import daily_report as daily_report_module

    class _FakeResponse:
        def __init__(self, payload: object) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> object:
            return self._payload

    class _Client:
        async def get(self, url: str, *, timeout: float) -> _FakeResponse:
            payloads = {
                "news": {"items": [{"title": "新闻甲"}]},
                "hot": {"items": [{"title": "热搜一"}]},
                "heh": {"items": [{"title": "热帖一"}]},
            }
            return _FakeResponse(payloads[url.rsplit("/", 1)[-1]])

    monkeypatch.setattr(daily_report_module, "_resolve_client", lambda client: _Client())
    settings = _configured_settings(
        scheduled_jobs="life_morning@07:30",
        countdown_events="六级考试:2030-12-12",
        report_60s_base_url="http://x",
    )
    fake_bot = FakeBot()
    before = _job_metric("life_morning", "ok")
    try:
        scheduler_plugin._register_life_builders(settings)
        await run_scheduled_job(
            ScheduledJob(job_type="life_morning", hour=7, minute=30),
            fake_bot,
            settings=settings,
        )
    finally:
        scheduler_jobs_module._CONTENT_BUILDERS.pop("life_morning", None)
        scheduler_jobs_module._CONTENT_BUILDERS.pop("life_evening", None)

    assert len(fake_bot.sent) == 1
    group_id, message = fake_bot.sent[0]
    text = message.extract_plain_text()
    assert group_id == 111
    assert "【早报】" in text
    assert "1. 新闻甲" in text
    assert _job_metric("life_morning", "ok") == before + 1


async def test_ai_briefing_job_sends_briefing(monkeypatch: pytest.MonkeyPatch) -> None:
    """AI 早报任务端到端（S8-BRIEF）：注册 builder → 组装 → 共享发送管线。"""

    async def fake_build(settings, *, client=None, now=None):
        assert settings.scheduled_job_list == [("ai_morning", 9, 30)]
        return "【AI早报】9月22日 周二\n1. 测试新闻"

    monkeypatch.setattr(scheduler_plugin, "build_ai_briefing_message", fake_build)
    settings = _configured_settings(scheduled_jobs="ai_morning@09:30")
    fake_bot = FakeBot()
    before = _job_metric("ai_morning", "ok")
    try:
        scheduler_plugin._register_ai_briefing_builder(settings)
        await run_scheduled_job(
            ScheduledJob(job_type="ai_morning", hour=9, minute=30),
            fake_bot,
            settings=settings,
        )
    finally:
        scheduler_jobs_module._CONTENT_BUILDERS.pop("ai_morning", None)

    assert [(gid, msg.extract_plain_text()) for gid, msg in fake_bot.sent] == [
        (111, "【AI早报】9月22日 周二\n1. 测试新闻")
    ]
    assert _job_metric("ai_morning", "ok") == before + 1


def test_ai_briefing_builder_not_registered_without_job() -> None:
    settings = _configured_settings(scheduled_jobs="life_morning@07:30")
    scheduler_plugin._register_ai_briefing_builder(settings)
    assert scheduler_jobs_module.get_builder("ai_morning") is None


# ---- 定时发送失败后的延迟重投递（S6-SCHED-04）----


# 后端适配器对 send_group_msg 调用超时抛出的 NetworkError 带 API 动作名，
# is_send_timeout_error 依赖 "send_group_msg" + "timeout" 判定为模糊超时
_TIMEOUT_MSG = (
    "Error: Timeout: send_group_msg NTEvent serviceAndMethod:NodeIKernelMsgService/sendMsg"
)


class TimeoutBot:
    """始终以生产同款 NTQQ sendMsg 挂起超时失败（模糊超时，不立即重试）。"""

    sent: list = []

    async def send_group_msg(self, *, group_id: int, message: object) -> None:
        raise NetworkError(_TIMEOUT_MSG)


class FailThenRecoverBot:
    """前 fail_times 次调用按生产同款超时失败，之后恢复。"""

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.sent: list[tuple[int, object]] = []

    async def send_group_msg(self, *, group_id: int, message: object) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise NetworkError(_TIMEOUT_MSG)
        self.sent.append((group_id, message))


def _redeliver_metric(job: str, result: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "qq_bot_scheduled_redeliver_total", {"job": job, "result": result}
        )
        or 0.0
    )


async def _drain_pending_tasks() -> None:
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in pending:
        await task


async def test_send_failure_redelivered_after_delay() -> None:
    fake_bot = FailThenRecoverBot(fail_times=1)
    failed_before = _job_metric("static", "failed")
    ok_before = _redeliver_metric("static", "ok")

    await run_scheduled_job(
        ScheduledJob(job_type="static", hour=9, minute=0),
        fake_bot,
        settings=_configured_settings(scheduled_redeliver_delay_seconds=0.0),
    )
    await _drain_pending_tasks()

    # 首轮失败记 failed；延迟补投一轮后送达并记 redeliver ok
    assert _job_metric("static", "failed") == failed_before + 1
    assert _redeliver_metric("static", "ok") == ok_before + 1
    assert [(gid, msg.extract_plain_text()) for gid, msg in fake_bot.sent] == [(111, "早上好")]


async def test_redeliver_exhausted_records_failed() -> None:
    fake_bot = TimeoutBot()
    failed_before = _redeliver_metric("static", "failed")

    await run_scheduled_job(
        ScheduledJob(job_type="static", hour=9, minute=0),
        fake_bot,
        settings=_configured_settings(
            scheduled_redeliver_max=1, scheduled_redeliver_delay_seconds=0.0
        ),
    )
    await _drain_pending_tasks()

    assert fake_bot.sent == []
    assert _redeliver_metric("static", "failed") == failed_before + 1


async def test_redeliver_disabled_when_max_zero() -> None:
    fake_bot = TimeoutBot()
    ok_before = _redeliver_metric("static", "ok")
    failed_before = _redeliver_metric("static", "failed")

    await run_scheduled_job(
        ScheduledJob(job_type="static", hour=9, minute=0),
        fake_bot,
        settings=_configured_settings(scheduled_redeliver_max=0),
    )
    await _drain_pending_tasks()

    # 计数器跨测试累加，断言“不增长”而非“为 0”
    assert fake_bot.sent == []
    assert _redeliver_metric("static", "ok") == ok_before
    assert _redeliver_metric("static", "failed") == failed_before


# ---- 调度器加固：misfire 宽限 + 未预期异常可观测（Task 8）----


def test_typed_jobs_registered_with_misfire_grace_and_coalesce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """typed cron 注册必须带 misfire_grace_time=300 与 coalesce=True：
    APScheduler 默认宽限仅 1 秒，事件循环短暂卡顿即静默跳过当日任务。"""
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(
        scheduler_plugin.scheduler, "add_job", lambda *args, **kwargs: captured.append(kwargs)
    )
    settings = _configured_settings(scheduled_jobs="static@09:00,life_morning@07:30")

    scheduler_plugin._register_typed_jobs(scheduler_jobs_module.jobs_from_settings(settings))

    assert [kwargs["id"] for kwargs in captured] == ["static_0900", "life_morning_0730"]
    for kwargs in captured:
        assert kwargs["misfire_grace_time"] == 300
        assert kwargs["coalesce"] is True


def test_legacy_jobs_kwargs_carry_misfire_grace_and_coalesce() -> None:
    """legacy 泛化路径（SCHEDULED_CRON_*）注册参数同样带宽限与合并。"""
    settings = _configured_settings(scheduled_jobs="", scheduled_cron_times="11:00,16:10")

    kwargs_list = build_scheduler_jobs_kwargs(settings)

    assert [kwargs["id"] for kwargs in kwargs_list] == [
        "daily_group_message_1100",
        "daily_group_message_1610",
    ]
    for kwargs in kwargs_list:
        assert kwargs["misfire_grace_time"] == 300
        assert kwargs["coalesce"] is True


async def test_unexpected_builder_exception_is_recorded_and_reraised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(settings: BotSettings) -> str | None:
        raise RuntimeError("boom")

    monkeypatch.setattr(scheduler_plugin, "get_builder", lambda job_type: _boom)
    job = ScheduledJob(job_type="static", hour=9, minute=0)
    before = _job_metric(job.job_type, "failed")

    with pytest.raises(RuntimeError, match="boom"):
        await run_scheduled_job(job, None, settings=_configured_settings())

    # 未预期异常补记 failed 指标后原样上抛（不吞异常，只补可观测性）
    assert _job_metric(job.job_type, "failed") == before + 1
