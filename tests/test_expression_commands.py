"""Admin command plugin tests (S10-HUMAN-P1-05).

Covers the learning trigger (``/表达学习``) plus the read/manage commands
(``/表达 …`` / ``/黑话 …``): authorization first (开关不豁免鉴权), the counts-only
broadcast wording (never any template/jargon content in the automatic output), and
the repo calls the management commands make.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc

import bot  # noqa: F401  (NoneBot init for on_command)
import pytest
from nonebot.adapters.onebot.v11 import Message
from nonebot.exception import FinishedException

from qq_bot.config import BotSettings
from qq_bot.plugins import expression_commands as plugin
from qq_bot.runtime import RuntimeStateError
from qq_bot.services.chat_memory import ExpressionRow
from qq_bot.services.expression_learner import RoundSummary

ADMIN_ID = 2880000001
MEMBER_ID = 123456


class FakeEvent:
    group_id = 1001
    self_id = 2880000001

    def __init__(self, user_id: int = ADMIN_ID):
        self.user_id = user_id

    def get_message(self) -> "FakeEvent":
        return self

    def extract_plain_text(self) -> str:
        return ""


class FakeMatcher:
    """Records send/finish calls. ``finish`` raises ``FinishedException`` — the
    real NoneBot completion signal that
    ``finish_with_send_errors_logged`` treats as a successful send and re-raises.
    """

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.finished: list[str] = []

    async def send(self, message: object) -> None:
        self.sent.append(str(message))

    async def finish(self, message: object) -> None:
        self.finished.append(str(message))
        raise FinishedException()


def _settings(**overrides) -> BotSettings:
    base = {
        "allowed_group_ids": "1001",
        "admin_user_ids": str(ADMIN_ID),
        "expression_learn_enabled": True,
        "expression_min_active": 10,
        "jargon_inject_max": 10,
    }
    base.update(overrides)
    return BotSettings(**base)


class SpyLearningRound:
    """Records whether run_learning_round was invoked and with what arguments."""

    def __init__(self, summary: RoundSummary | None = None, error: Exception | None = None):
        self.summary = summary
        self.error = error
        self.calls: list[dict] = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.summary


def _expression_row(
    row_id: int, template: str, count: int = 3, group_id: int = 1001
) -> ExpressionRow:
    return ExpressionRow(
        id=row_id,
        group_id=group_id,
        template=template,
        slot_count=1,
        evidence_row_id=1,
        evidence_digest="d",
        status="active",
        count=count,
        manual=0,
        created_at="2026-10-01T00:00:00+00:00",
        last_used_at=None,
    )


def _jargon_row(term: str, definition: str = "") -> object:
    from qq_bot.services.chat_memory import JargonRow

    return JargonRow(
        id=1,
        group_id=1001,
        term=term,
        definition=definition,
        status="confirmed",
        hits=5,
        manual=0,
        first_seen_at="2026-10-01T00:00:00+00:00",
        last_seen_at="2026-10-02T00:00:00+00:00",
        confirmed_at="2026-10-02T00:00:00+00:00",
    )


class SpyRepository:
    """Records the repo calls the management commands make."""

    def __init__(self) -> None:
        self.expressions: list[ExpressionRow] = []
        self.jargon: list[object] = []
        self.unconfirmed_jargon: list[object] = []
        self.disabled_expression_ids: list[int] = []
        self.overridden: list[dict] = []
        self.disabled_jargon_terms: list[str] = []
        self.disable_expression_result = True
        self.override_jargon_result = True
        self.disable_jargon_result = True
        self.list_active_calls: list[int] = []

    async def list_active_expressions(self, *, group_id: int):
        self.list_active_calls.append(group_id)
        return [row for row in self.expressions if row.group_id == group_id]

    async def disable_expression(self, *, expression_id: int) -> bool:
        self.disabled_expression_ids.append(expression_id)
        return self.disable_expression_result

    async def list_jargon(self, *, group_id: int, status: str, limit: int = 10):
        if status == "unconfirmed":
            return list(self.unconfirmed_jargon)
        return list(self.jargon)

    async def override_jargon(self, *, group_id: int, term: str, definition: str, now=None) -> bool:
        self.overridden.append({"term": term, "definition": definition})
        return self.override_jargon_result

    async def disable_jargon(self, *, group_id: int, term: str) -> bool:
        self.disabled_jargon_terms.append(term)
        return self.disable_jargon_result


class FakeRuntime:
    def __init__(self, quota=None) -> None:
        self._quota = quota

    def get_quota_service(self):
        return self._quota


class BrokenRuntime:
    def __call__(self):
        raise RuntimeStateError("runtime not ready")


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch) -> SpyRepository:
    repository = SpyRepository()
    monkeypatch.setattr(plugin, "get_chat_repository", lambda: repository)
    return repository


@pytest.fixture
def admin_settings(monkeypatch: pytest.MonkeyPatch) -> BotSettings:
    settings = _settings()
    monkeypatch.setattr(plugin, "get_settings", lambda: settings)
    return settings


@pytest.fixture
def matcher() -> FakeMatcher:
    return FakeMatcher()


def _patch_matchers(monkeypatch: pytest.MonkeyPatch, matcher: FakeMatcher) -> None:
    for command in (plugin.learn_command, plugin.expr_command, plugin.jargon_command):
        monkeypatch.setattr(command, "send", matcher.send)
        monkeypatch.setattr(command, "finish", matcher.finish)


def _record_denials(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    recorded: list[str] = []
    monkeypatch.setattr(plugin, "record_error", lambda *a, **k: recorded.append(f"{a[0]}/{a[1]}"))
    return recorded


async def _drive(coro) -> None:
    """Run one handler; ``finish`` terminates via FinishedException."""
    with contextlib.suppress(FinishedException):
        await coro


async def _drain_background_tasks() -> None:
    """Drive the detached learning-round task to completion."""
    for _ in range(200):
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


def _args(text: str) -> Message:
    return Message(text)


def _enable_round(monkeypatch: pytest.MonkeyPatch, spy: SpyLearningRound) -> None:
    monkeypatch.setattr(plugin, "run_learning_round", spy)
    monkeypatch.setattr(plugin, "get_runtime", lambda: FakeRuntime())


# ---------------------------------------------------------------------------
# /表达学习
# ---------------------------------------------------------------------------


async def test_learn_command_non_admin_is_rejected_without_a_round(
    monkeypatch: pytest.MonkeyPatch, admin_settings: BotSettings, matcher
) -> None:
    spy = SpyLearningRound(error=AssertionError("learning must not run"))
    monkeypatch.setattr(plugin, "run_learning_round", spy)
    _patch_matchers(monkeypatch, matcher)
    recorded = _record_denials(monkeypatch)

    await _drive(plugin.handle_learn(FakeEvent(user_id=MEMBER_ID)))  # type: ignore[arg-type]
    await _drain_background_tasks()

    assert recorded == ["learn/permission"]
    assert spy.calls == []
    assert matcher.finished == []
    assert matcher.sent == []


async def test_learn_command_disabled_by_config(
    monkeypatch: pytest.MonkeyPatch, repo: SpyRepository, matcher
) -> None:
    monkeypatch.setattr(plugin, "get_settings", lambda: _settings(expression_learn_enabled=False))
    spy = SpyLearningRound()
    monkeypatch.setattr(plugin, "run_learning_round", spy)
    _patch_matchers(monkeypatch, matcher)

    await _drive(plugin.handle_learn(FakeEvent()))  # type: ignore[arg-type]
    await _drain_background_tasks()

    assert "表达学习未开启" in matcher.finished[0]
    assert spy.calls == []


async def test_learn_command_admin_broadcasts_counts_only(
    monkeypatch: pytest.MonkeyPatch,
    admin_settings: BotSettings,
    repo: SpyRepository,
    matcher,
) -> None:
    summary = RoundSummary(
        status="ok",
        batches=2,
        messages_scanned=40,
        expressions_extracted=5,
        rejected_validation=2,
        rejected_review=1,
        stored=4,
        duplicated=1,
        jargon_new=2,
        jargon_hit=3,
        jargon_confirmed=1,
        llm_calls_used=6,
    )
    spy = SpyLearningRound(summary=summary)
    _enable_round(monkeypatch, spy)
    _patch_matchers(monkeypatch, matcher)

    await _drive(plugin.handle_learn(FakeEvent()))  # type: ignore[arg-type]
    # 立即回执先到（matcher.send 不终止 handler）
    assert matcher.sent == ["开始后台学习…"]
    await _drain_background_tasks()

    assert len(spy.calls) == 1
    call = spy.calls[0]
    assert call["group_id"] == 1001
    assert call["memory_store"] is repo
    assert call["settings"].expression_learn_enabled is True

    assert len(matcher.finished) == 1
    broadcast = matcher.finished[0]
    assert "扫描 40 条消息 2 批" in broadcast
    assert "提取5" in broadcast
    assert "入库4" in broadcast
    assert "重复1" in broadcast
    assert "新词2" in broadcast
    assert "确认1" in broadcast
    assert "调用 6 次" in broadcast
    # counts-only：模板/黑话文本绝不进自动播报
    assert "{" not in broadcast
    assert "对象" not in broadcast
    assert "咸鱼" not in broadcast


async def test_learn_command_quota_denied_message(
    monkeypatch: pytest.MonkeyPatch, admin_settings: BotSettings, repo: SpyRepository, matcher
) -> None:
    _enable_round(monkeypatch, SpyLearningRound(summary=RoundSummary(status="quota_denied")))
    _patch_matchers(monkeypatch, matcher)

    await _drive(plugin.handle_learn(FakeEvent()))  # type: ignore[arg-type]
    await _drain_background_tasks()

    assert matcher.finished == ["今日学习配额已用完"]


async def test_learn_command_insufficient_material_message(
    monkeypatch: pytest.MonkeyPatch, admin_settings: BotSettings, repo: SpyRepository, matcher
) -> None:
    _enable_round(
        monkeypatch, SpyLearningRound(summary=RoundSummary(status="insufficient_material"))
    )
    _patch_matchers(monkeypatch, matcher)

    await _drive(plugin.handle_learn(FakeEvent()))  # type: ignore[arg-type]
    await _drain_background_tasks()

    assert matcher.finished == ["最近素材不足，跳过学习"]


async def test_learn_command_round_failure_broadcasts_generic_line(
    monkeypatch: pytest.MonkeyPatch, admin_settings: BotSettings, repo: SpyRepository, matcher
) -> None:
    _enable_round(monkeypatch, SpyLearningRound(error=ValueError("boom")))
    _patch_matchers(monkeypatch, matcher)
    recorded: list[str] = []
    monkeypatch.setattr(plugin, "record_error", lambda c, k, **kw: recorded.append(f"{c}/{k}"))

    await _drive(plugin.handle_learn(FakeEvent()))  # type: ignore[arg-type]
    await _drain_background_tasks()

    assert matcher.finished == ["学习失败，请稍后再试"]
    assert "boom" not in "".join(matcher.finished)
    assert recorded and recorded[0] == "learn/unknown"


async def test_learn_command_without_runtime_still_runs(
    monkeypatch: pytest.MonkeyPatch, admin_settings: BotSettings, repo: SpyRepository, matcher
) -> None:
    spy = SpyLearningRound(summary=RoundSummary(status="ok", stored=1))
    monkeypatch.setattr(plugin, "run_learning_round", spy)
    monkeypatch.setattr(plugin, "get_runtime", BrokenRuntime())
    _patch_matchers(monkeypatch, matcher)

    await _drive(plugin.handle_learn(FakeEvent()))  # type: ignore[arg-type]
    await _drain_background_tasks()

    assert len(spy.calls) == 1
    assert spy.calls[0]["quota"] is None
    assert len(matcher.finished) == 1
    assert "入库1" in matcher.finished[0]


async def test_learn_command_runtime_not_ready_is_reported(
    monkeypatch: pytest.MonkeyPatch, admin_settings: BotSettings, matcher
) -> None:
    spy = SpyLearningRound(error=AssertionError("learning must not run"))
    monkeypatch.setattr(plugin, "run_learning_round", spy)

    def _broken_repository():
        raise RuntimeStateError("runtime not ready")

    monkeypatch.setattr(plugin, "get_chat_repository", _broken_repository)
    _patch_matchers(monkeypatch, matcher)

    await _drive(plugin.handle_learn(FakeEvent()))  # type: ignore[arg-type]
    await _drain_background_tasks()

    assert spy.calls == []
    assert "学习失败" in matcher.finished[0]


async def test_learn_command_holds_task_strong_reference(
    monkeypatch: pytest.MonkeyPatch, admin_settings: BotSettings, repo: SpyRepository, matcher
) -> None:
    """GC 防护（Fix Round 1）：进行中的学习轮次必须被 _LEARN_TASKS 强引用持有，
    asyncio 只保存弱引用，否则任务可能在播报前被垃圾回收。"""

    class SlowRound:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def __call__(self, **kwargs):
            self.started.set()
            await self.release.wait()
            return RoundSummary(status="ok", stored=1)

    round_task = SlowRound()
    monkeypatch.setattr(plugin, "run_learning_round", round_task)
    monkeypatch.setattr(plugin, "get_runtime", lambda: FakeRuntime())
    _patch_matchers(monkeypatch, matcher)

    await _drive(plugin.handle_learn(FakeEvent()))  # type: ignore[arg-type]

    # 轮次进行中（等待释放）：任务必须在强引用集合里，GC 拿不走
    await round_task.started.wait()
    assert plugin._LEARN_TASKS, "learning task must be held against GC while in flight"
    held = next(iter(plugin._LEARN_TASKS))
    assert not held.done()

    # 即使强制垃圾回收，任务依然存活并最终完成播报
    gc.collect()
    round_task.release.set()
    await _drain_background_tasks()

    assert plugin._LEARN_TASKS == set(), "finished task must be discarded from the hold set"
    assert len(matcher.finished) == 1
    assert "入库1" in matcher.finished[0]


async def test_learn_command_metric_counted_once(
    monkeypatch: pytest.MonkeyPatch, admin_settings: BotSettings, repo: SpyRepository, matcher
) -> None:
    """COMMANDS 指标只在 handler 侧计一次（Fix Round 1）：round 内的旧增量已删，
    ok 状态不再被重复计数。"""
    counted: list[str] = []

    class FakeMetric:
        def labels(self, name: str) -> "FakeMetric":
            self._name = name
            return self

        def inc(self) -> None:
            counted.append(self._name)

    monkeypatch.setattr(plugin.metrics, "COMMANDS", FakeMetric())
    _enable_round(monkeypatch, SpyLearningRound(summary=RoundSummary(status="ok", stored=1)))
    _patch_matchers(monkeypatch, matcher)

    await _drive(plugin.handle_learn(FakeEvent()))  # type: ignore[arg-type]
    await _drain_background_tasks()

    assert counted == ["表达学习"]


# ---------------------------------------------------------------------------
# /表达 列表
# ---------------------------------------------------------------------------


async def test_expression_list_shows_templates_and_count(
    monkeypatch: pytest.MonkeyPatch, admin_settings: BotSettings, repo: SpyRepository, matcher
) -> None:
    repo.expressions = [
        _expression_row(1, "这不是{对象}吗"),
        _expression_row(2, "确实"),
    ]
    _patch_matchers(monkeypatch, matcher)

    await _drive(plugin.handle_expression(FakeEvent(), _args("列表")))  # type: ignore[arg-type]

    assert len(matcher.finished) == 1
    text = matcher.finished[0]
    assert "本群表达模板 2 条" in text
    assert "这不是{对象}吗" in text
    assert "确实" in text
    assert "#1" in text
    assert "#2" in text


async def test_expression_list_empty(
    monkeypatch: pytest.MonkeyPatch, admin_settings: BotSettings, repo: SpyRepository, matcher
) -> None:
    _patch_matchers(monkeypatch, matcher)

    await _drive(plugin.handle_expression(FakeEvent(), _args("")))  # type: ignore[arg-type]

    assert matcher.finished == ["本群暂无表达模板"]


async def test_expression_list_non_admin_is_rejected(
    monkeypatch: pytest.MonkeyPatch, admin_settings: BotSettings, repo: SpyRepository, matcher
) -> None:
    _patch_matchers(monkeypatch, matcher)
    recorded = _record_denials(monkeypatch)

    await _drive(
        plugin.handle_expression(FakeEvent(user_id=MEMBER_ID), _args("列表"))  # type: ignore[arg-type]
    )

    assert recorded == ["learn/permission"]
    assert matcher.finished == []


# ---------------------------------------------------------------------------
# /表达 禁用
# ---------------------------------------------------------------------------


async def test_expression_disable_non_admin_is_rejected(
    monkeypatch: pytest.MonkeyPatch, admin_settings: BotSettings, repo: SpyRepository, matcher
) -> None:
    _patch_matchers(monkeypatch, matcher)
    recorded = _record_denials(monkeypatch)

    await _drive(
        plugin.handle_expression(FakeEvent(user_id=MEMBER_ID), _args("禁用 7"))  # type: ignore[arg-type]
    )

    assert recorded == ["learn/permission"]
    assert repo.disabled_expression_ids == []
    assert matcher.finished == []


async def test_expression_disable_admin(
    monkeypatch: pytest.MonkeyPatch, admin_settings: BotSettings, repo: SpyRepository, matcher
) -> None:
    repo.expressions = [_expression_row(7, "这不是{对象}吗")]
    _patch_matchers(monkeypatch, matcher)

    await _drive(plugin.handle_expression(FakeEvent(), _args("禁用 7")))  # type: ignore[arg-type]

    assert repo.disabled_expression_ids == [7]
    assert "已禁用" in matcher.finished[0]
    assert "#7" in matcher.finished[0]


async def test_expression_disable_missing_template(
    monkeypatch: pytest.MonkeyPatch, admin_settings: BotSettings, repo: SpyRepository, matcher
) -> None:
    repo.expressions = [_expression_row(7, "这不是{对象}吗")]
    repo.disable_expression_result = False
    _patch_matchers(monkeypatch, matcher)

    await _drive(plugin.handle_expression(FakeEvent(), _args("禁用 7")))  # type: ignore[arg-type]

    assert "未找到或已禁用" in matcher.finished[0]


async def test_expression_disable_foreign_group_id_is_not_disabled(
    monkeypatch: pytest.MonkeyPatch, admin_settings: BotSettings, repo: SpyRepository, matcher
) -> None:
    """群隔离（Fix Round 1）：id 属于别的群 → 拒绝且不动仓库状态。"""
    repo.expressions = [_expression_row(7, "这不是{对象}吗", group_id=2002)]
    _patch_matchers(monkeypatch, matcher)

    await _drive(plugin.handle_expression(FakeEvent(), _args("禁用 7")))  # type: ignore[arg-type]

    assert "未找到该 id 的表达模板 #7" in matcher.finished[0]
    assert repo.disabled_expression_ids == []
    assert repo.list_active_calls == [1001]


async def test_expression_disable_disabled_template_id_is_rejected(
    monkeypatch: pytest.MonkeyPatch, admin_settings: BotSettings, repo: SpyRepository, matcher
) -> None:
    """已禁用（不在活跃池）的 id → 同样走群作用域预检，不再调用 disable。"""
    repo.expressions = []  # 活跃池为空：#7 已禁用
    _patch_matchers(monkeypatch, matcher)

    await _drive(plugin.handle_expression(FakeEvent(), _args("禁用 7")))  # type: ignore[arg-type]

    assert "未找到该 id 的表达模板 #7" in matcher.finished[0]
    assert repo.disabled_expression_ids == []


async def test_expression_disable_requires_id(
    monkeypatch: pytest.MonkeyPatch, admin_settings: BotSettings, repo: SpyRepository, matcher
) -> None:
    _patch_matchers(monkeypatch, matcher)

    await _drive(plugin.handle_expression(FakeEvent(), _args("禁用")))  # type: ignore[arg-type]

    assert repo.disabled_expression_ids == []
    assert "用法" in matcher.finished[0]


# ---------------------------------------------------------------------------
# 只读命令不受总开关限制 / 运行时未就绪的兜底（Fix Round 1）
# ---------------------------------------------------------------------------


async def test_expression_list_reads_repo_with_learn_switch_off(
    monkeypatch: pytest.MonkeyPatch, matcher
) -> None:
    """只读命令不受总开关限制：``expression_learn_enabled=False`` 时仍读库渲染。"""
    repo = SpyRepository()
    repo.expressions = [_expression_row(1, "这不是{对象}吗"), _expression_row(2, "确实")]
    monkeypatch.setattr(plugin, "get_chat_repository", lambda: repo)
    monkeypatch.setattr(plugin, "get_settings", lambda: _settings(expression_learn_enabled=False))
    _patch_matchers(monkeypatch, matcher)

    await _drive(plugin.handle_expression(FakeEvent(), _args("列表")))  # type: ignore[arg-type]

    assert repo.list_active_calls == [1001]
    text = matcher.finished[0]
    assert "本群表达模板 2 条" in text
    assert "这不是{对象}吗" in text
    assert "#2" in text


async def test_expression_list_reads_repo_with_learn_switch_off_and_empty_pool(
    monkeypatch: pytest.MonkeyPatch, matcher
) -> None:
    """同上，空库时给出空列表措辞（而非被开关拦下）。"""
    repo = SpyRepository()
    monkeypatch.setattr(plugin, "get_chat_repository", lambda: repo)
    monkeypatch.setattr(plugin, "get_settings", lambda: _settings(expression_learn_enabled=False))
    _patch_matchers(monkeypatch, matcher)

    await _drive(plugin.handle_expression(FakeEvent(), _args("")))  # type: ignore[arg-type]

    assert repo.list_active_calls == [1001]
    assert matcher.finished == ["本群暂无表达模板"]


@pytest.mark.parametrize("args_text", ["列表", "禁用 7"])
async def test_expression_command_without_runtime_reports_unavailable(
    monkeypatch: pytest.MonkeyPatch, admin_settings: BotSettings, matcher, args_text: str
) -> None:
    """运行时未就绪 → 优雅兜底，不向调用方抛 RuntimeStateError。"""

    def _broken_repository():
        raise RuntimeStateError("runtime not ready")

    monkeypatch.setattr(plugin, "get_chat_repository", _broken_repository)
    _patch_matchers(monkeypatch, matcher)

    await _drive(  # type: ignore[arg-type]
        plugin.handle_expression(FakeEvent(), _args(args_text))
    )

    assert matcher.finished == ["记忆服务暂不可用，请稍后再试。"]


@pytest.mark.parametrize("args_text", ["列表", "禁用 咸鱼翻车"])
async def test_jargon_command_without_runtime_reports_unavailable(
    monkeypatch: pytest.MonkeyPatch, admin_settings: BotSettings, matcher, args_text: str
) -> None:
    def _broken_repository():
        raise RuntimeStateError("runtime not ready")

    monkeypatch.setattr(plugin, "get_chat_repository", _broken_repository)
    _patch_matchers(monkeypatch, matcher)

    await _drive(  # type: ignore[arg-type]
        plugin.handle_jargon(FakeEvent(), _args(args_text))
    )

    assert matcher.finished == ["记忆服务暂不可用，请稍后再试。"]


# ---------------------------------------------------------------------------
# /黑话 列表
# ---------------------------------------------------------------------------


async def test_jargon_list_shows_terms_and_unconfirmed_count(
    monkeypatch: pytest.MonkeyPatch, admin_settings: BotSettings, repo: SpyRepository, matcher
) -> None:
    repo.jargon = [_jargon_row("咸鱼翻车", definition="彻底搞砸")]
    repo.unconfirmed_jargon = [_jargon_row("新词一"), _jargon_row("新词二")]
    _patch_matchers(monkeypatch, matcher)

    await _drive(plugin.handle_jargon(FakeEvent(), _args("列表")))  # type: ignore[arg-type]

    text = matcher.finished[0]
    assert "已确认黑话 1 条" in text
    assert "待确认 2 条" in text
    assert "咸鱼翻车" in text
    assert "彻底搞砸" in text


async def test_jargon_list_empty(
    monkeypatch: pytest.MonkeyPatch, admin_settings: BotSettings, repo: SpyRepository, matcher
) -> None:
    _patch_matchers(monkeypatch, matcher)

    await _drive(plugin.handle_jargon(FakeEvent(), _args("")))  # type: ignore[arg-type]

    assert "暂无已确认黑话" in matcher.finished[0]


# ---------------------------------------------------------------------------
# /黑话 修正
# ---------------------------------------------------------------------------


async def test_jargon_override_admin(
    monkeypatch: pytest.MonkeyPatch, admin_settings: BotSettings, repo: SpyRepository, matcher
) -> None:
    _patch_matchers(monkeypatch, matcher)

    await _drive(
        plugin.handle_jargon(FakeEvent(), _args("修正 咸鱼翻车 彻底搞砸"))  # type: ignore[arg-type]
    )

    assert repo.overridden == [{"term": "咸鱼翻车", "definition": "彻底搞砸"}]
    text = matcher.finished[0]
    assert "咸鱼翻车" in text
    assert "彻底搞砸" in text


async def test_jargon_override_non_admin_is_rejected(
    monkeypatch: pytest.MonkeyPatch, admin_settings: BotSettings, repo: SpyRepository, matcher
) -> None:
    _patch_matchers(monkeypatch, matcher)
    recorded = _record_denials(monkeypatch)

    await _drive(
        plugin.handle_jargon(FakeEvent(user_id=MEMBER_ID), _args("修正 咸鱼翻车 彻底搞砸"))  # type: ignore[arg-type]
    )

    assert recorded == ["learn/permission"]
    assert repo.overridden == []
    assert matcher.finished == []


async def test_jargon_override_requires_definition(
    monkeypatch: pytest.MonkeyPatch, admin_settings: BotSettings, repo: SpyRepository, matcher
) -> None:
    _patch_matchers(monkeypatch, matcher)

    await _drive(
        plugin.handle_jargon(FakeEvent(), _args("修正 咸鱼翻车"))  # type: ignore[arg-type]
    )

    assert repo.overridden == []
    assert "用法" in matcher.finished[0]


async def test_jargon_override_unknown_term(
    monkeypatch: pytest.MonkeyPatch, admin_settings: BotSettings, repo: SpyRepository, matcher
) -> None:
    repo.override_jargon_result = False
    _patch_matchers(monkeypatch, matcher)

    await _drive(
        plugin.handle_jargon(FakeEvent(), _args("修正 咸鱼翻车 彻底搞砸"))  # type: ignore[arg-type]
    )

    assert "未找到" in matcher.finished[0]


# ---------------------------------------------------------------------------
# /黑话 禁用
# ---------------------------------------------------------------------------


async def test_jargon_disable_admin(
    monkeypatch: pytest.MonkeyPatch, admin_settings: BotSettings, repo: SpyRepository, matcher
) -> None:
    _patch_matchers(monkeypatch, matcher)

    await _drive(
        plugin.handle_jargon(FakeEvent(), _args("禁用 咸鱼翻车"))  # type: ignore[arg-type]
    )

    assert repo.disabled_jargon_terms == ["咸鱼翻车"]
    assert "已禁用" in matcher.finished[0]
    assert "咸鱼翻车" in matcher.finished[0]


async def test_jargon_disable_non_admin_is_rejected(
    monkeypatch: pytest.MonkeyPatch, admin_settings: BotSettings, repo: SpyRepository, matcher
) -> None:
    _patch_matchers(monkeypatch, matcher)
    recorded = _record_denials(monkeypatch)

    await _drive(
        plugin.handle_jargon(FakeEvent(user_id=MEMBER_ID), _args("禁用 咸鱼翻车"))  # type: ignore[arg-type]
    )

    assert recorded == ["learn/permission"]
    assert repo.disabled_jargon_terms == []
    assert matcher.finished == []


# ---------------------------------------------------------------------------
# 真实 SQLite 仓库端到端（验证仓库方法签名与命令调用一致）
# ---------------------------------------------------------------------------


@pytest.fixture
async def real_repository(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from qq_bot.services.chat_memory import ChatMemoryRepository

    repository = ChatMemoryRepository(tmp_path / "learn.sqlite3", retention_days=30)
    await repository.open()
    monkeypatch.setattr(plugin, "get_chat_repository", lambda: repository)
    yield repository
    await repository.close()


async def test_expression_disable_and_override_against_real_repository(
    monkeypatch: pytest.MonkeyPatch, admin_settings: BotSettings, real_repository, matcher
) -> None:
    await real_repository.upsert_expression(
        group_id=1001,
        template="这不是{对象}吗",
        slot_count=1,
        evidence_row_id=None,
        evidence_digest="d",
    )
    await real_repository.upsert_jargon(group_id=1001, term="咸鱼翻车")
    _patch_matchers(monkeypatch, matcher)

    await _drive(plugin.handle_expression(FakeEvent(), _args("列表")))  # type: ignore[arg-type]
    listed = matcher.finished[0]
    assert "这不是{对象}吗" in listed

    await _drive(
        plugin.handle_expression(FakeEvent(), _args("禁用 1"))  # type: ignore[arg-type]
    )
    assert "已禁用" in matcher.finished[-1]
    remaining = await real_repository.list_active_expressions(group_id=1001)
    assert remaining == []

    await _drive(
        plugin.handle_jargon(FakeEvent(), _args("修正 咸鱼翻车 彻底搞砸"))  # type: ignore[arg-type]
    )
    assert "已修正" in matcher.finished[-1]
    row = await real_repository.get_jargon(group_id=1001, term="咸鱼翻车")
    assert row is not None
    assert row.status == "confirmed"
    assert row.manual == 1
    assert row.definition == "彻底搞砸"

    await _drive(
        plugin.handle_jargon(FakeEvent(), _args("禁用 咸鱼翻车"))  # type: ignore[arg-type]
    )
    assert "已禁用" in matcher.finished[-1]
    row = await real_repository.get_jargon(group_id=1001, term="咸鱼翻车")
    assert row is not None
    assert row.status == "disabled"


async def test_expression_disable_foreign_group_id_against_real_repository(
    monkeypatch: pytest.MonkeyPatch, admin_settings: BotSettings, real_repository, matcher
) -> None:
    """真实仓库上的群隔离（Fix Round 1）：别的群插入的模板 id 不被本群禁用。"""
    await real_repository.upsert_expression(
        group_id=2002,
        template="别的群的模板",
        slot_count=1,
        evidence_row_id=None,
        evidence_digest="other",
    )
    _patch_matchers(monkeypatch, matcher)

    await _drive(plugin.handle_expression(FakeEvent(), _args("禁用 1")))  # type: ignore[arg-type]

    assert "未找到该 id 的表达模板 #1" in matcher.finished[0]
    # 别的群的模板仍是 active，跨群禁用没有发生
    assert await real_repository.list_active_expressions(group_id=2002) != []
    assert await real_repository.list_active_expressions(group_id=1001) == []
