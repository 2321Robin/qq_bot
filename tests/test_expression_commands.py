"""Admin command plugin tests (S10-HUMAN-P1-05).

Covers the learning trigger (``/表达学习``) plus the read/manage commands
(``/表达 …`` / ``/黑话 …``): authorization first (开关不豁免鉴权), the counts-only
broadcast wording (never any template/jargon content in the automatic output), and
the repo calls the management commands make.
"""

from __future__ import annotations

import asyncio
import contextlib

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


def _expression_row(row_id: int, template: str, count: int = 3) -> ExpressionRow:
    return ExpressionRow(
        id=row_id,
        group_id=1001,
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

    async def list_active_expressions(self, *, group_id: int):
        return list(self.expressions)

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
    _patch_matchers(monkeypatch, matcher)

    await _drive(plugin.handle_expression(FakeEvent(), _args("禁用 7")))  # type: ignore[arg-type]

    assert repo.disabled_expression_ids == [7]
    assert "已禁用" in matcher.finished[0]
    assert "#7" in matcher.finished[0]


async def test_expression_disable_missing_template(
    monkeypatch: pytest.MonkeyPatch, admin_settings: BotSettings, repo: SpyRepository, matcher
) -> None:
    repo.disable_expression_result = False
    _patch_matchers(monkeypatch, matcher)

    await _drive(plugin.handle_expression(FakeEvent(), _args("禁用 7")))  # type: ignore[arg-type]

    assert "未找到或已禁用" in matcher.finished[0]


async def test_expression_disable_requires_id(
    monkeypatch: pytest.MonkeyPatch, admin_settings: BotSettings, repo: SpyRepository, matcher
) -> None:
    _patch_matchers(monkeypatch, matcher)

    await _drive(plugin.handle_expression(FakeEvent(), _args("禁用")))  # type: ignore[arg-type]

    assert repo.disabled_expression_ids == []
    assert "用法" in matcher.finished[0]


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
