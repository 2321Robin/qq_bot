"""离线学习脚本 tests（S10-HUMAN-P1-06）。

直接 import 脚本模块（不走子进程）：在脚本的命名空间里 monkeypatch
``run_learning_round`` / ``ChatMemoryRepository`` / ``get_settings``，
用假仓库与罐头 RoundSummary 覆盖入口级行为，保持测试离线且密闭。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict

import httpx
import pytest

import scripts.learn_expressions as learn_script
from qq_bot.config import BotSettings
from qq_bot.services.chat_memory import ChatMemoryRepository
from qq_bot.services.expression_learner import RoundSummary
from scripts.learn_expressions import main


def _settings(**overrides) -> BotSettings:
    defaults = dict(
        expression_learn_enabled=True,
        chat_memory_path="data/chat_memory.sqlite3",
        chat_memory_retention_days=3,
        expression_learn_lookback_days=7,
        expression_learn_max_batches=5,
    )
    return BotSettings(**{**defaults, **overrides})


class _FakeRepository:
    """暴露 open/close/distinct_group_ids 的假仓库（round 本身已被替身）。"""

    def __init__(self, group_ids: list[int] | None = None) -> None:
        self.group_ids = list(group_ids or [])
        self.opened = False
        self.closed = False
        self.distinct_days: list[int] = []

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.closed = True

    async def distinct_group_ids(self, *, days: int, now=None) -> list[int]:
        self.distinct_days.append(days)
        return list(self.group_ids)


def _install(
    monkeypatch,
    settings: BotSettings,
    repo: _FakeRepository,
    statuses: dict[int, str] | None = None,
    raise_for: set[int] | None = None,
):
    """Patch get_settings / ChatMemoryRepository / run_learning_round in the
    script's namespace; return the list of kwargs the fake round received."""
    calls: list[dict] = []
    statuses = statuses or {}
    raise_for = raise_for or set()

    def fake_repo_factory(path, retention_days=None):
        assert path == settings.chat_memory_path
        assert retention_days == settings.chat_memory_retention_days
        return repo

    async def fake_round(**kwargs):
        calls.append(kwargs)
        group_id = kwargs["group_id"]
        if group_id in raise_for:
            raise RuntimeError("boom")
        return RoundSummary(status=statuses.get(group_id, "ok"), batches=1, messages_scanned=5)

    monkeypatch.setattr(learn_script, "get_settings", lambda: settings)
    monkeypatch.setattr(learn_script, "ChatMemoryRepository", fake_repo_factory)
    monkeypatch.setattr(learn_script, "run_learning_round", fake_round)
    return calls


def _json_lines(capsys) -> list[dict]:
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def test_disabled_exits_one_without_touching_repository(monkeypatch, capsys) -> None:
    settings = _settings(expression_learn_enabled=False)
    repo = _FakeRepository([1001])
    calls = _install(monkeypatch, settings, repo)

    assert main(["--all"]) == 1
    assert "表达学习未开启" in capsys.readouterr().out
    assert calls == []
    assert not repo.opened
    assert not repo.closed


def test_all_groups_print_json_lines_and_exit_zero(monkeypatch, capsys) -> None:
    settings = _settings(quota_enabled=False)
    repo = _FakeRepository([1001, 2002])
    calls = _install(
        monkeypatch, settings, repo, statuses={1001: "ok", 2002: "insufficient_material"}
    )

    assert main(["--all"]) == 0
    lines = _json_lines(capsys)
    assert [line["group_id"] for line in lines] == [1001, 2002]
    assert lines[0]["status"] == "ok"
    assert lines[0]["batches"] == 1
    assert lines[1]["status"] == "insufficient_material"  # 正常结局，不触发退出码 2
    assert len(calls) == 2
    assert calls[0]["memory_store"] is repo
    assert calls[0]["settings"] is settings
    assert calls[0]["quota"] is None
    assert repo.opened and repo.closed


def test_error_group_exits_two(monkeypatch, capsys) -> None:
    settings = _settings(quota_enabled=False)
    repo = _FakeRepository([1001])
    _install(monkeypatch, settings, repo, statuses={1001: "error"})

    assert main(["--all"]) == 2
    lines = _json_lines(capsys)
    assert lines == [
        {"group_id": 1001, **asdict(RoundSummary(status="error", batches=1, messages_scanned=5))}
    ]


def test_round_exception_continues_and_exits_two(monkeypatch, capsys) -> None:
    settings = _settings(quota_enabled=False)
    repo = _FakeRepository([1001, 2002])
    _install(monkeypatch, settings, repo, raise_for={1001})

    assert main(["--all"]) == 2
    lines = _json_lines(capsys)
    assert [line["group_id"] for line in lines] == [1001, 2002]
    assert lines[0]["status"] == "error"
    assert lines[0]["error"] == "RuntimeError"
    assert lines[1]["status"] == "ok"
    assert repo.closed  # finally 保证关闭


def test_days_and_max_batches_override_reach_settings(monkeypatch, capsys) -> None:
    settings = _settings(quota_enabled=False)
    repo = _FakeRepository([1001])
    calls = _install(monkeypatch, settings, repo)

    assert main(["--all", "--days", "14", "--max-batches", "3"]) == 0
    assert repo.distinct_days == [14]  # --all 的群发现窗口也跟随覆盖
    overridden = calls[0]["settings"]
    assert overridden is not settings
    assert overridden.expression_learn_lookback_days == 14
    assert overridden.expression_learn_max_batches == 3

    assert main(["--all"]) == 0  # 未给旗标时原样透传 settings
    assert repo.distinct_days == [14, 7]
    assert calls[-1]["settings"] is settings


def test_group_flag_skips_group_discovery(monkeypatch, capsys) -> None:
    settings = _settings(quota_enabled=False)
    repo = _FakeRepository()
    calls = _install(monkeypatch, settings, repo)

    assert main(["--group", "3003", "--group", "3004"]) == 0
    assert repo.distinct_days == []  # 未走 --all，不做群发现
    assert [call["group_id"] for call in calls] == [3003, 3004]


def test_all_with_no_groups_exits_zero_with_note(monkeypatch, capsys) -> None:
    settings = _settings(quota_enabled=False)
    repo = _FakeRepository([])
    calls = _install(monkeypatch, settings, repo)

    assert main(["--all"]) == 0
    assert "没有群消息" in capsys.readouterr().out
    assert calls == []
    assert repo.closed


def test_round_receives_complete_with_injected_http_client(monkeypatch, capsys) -> None:
    """独立进程没有 NoneBot runtime：脚本必须给 request_completion 注入自带
    client（2026-10-26 服务器实测 client=None 时五批全 ambiguous 的回归钉）。"""
    settings = _settings(quota_enabled=False)
    repo = _FakeRepository([1001])
    calls = _install(monkeypatch, settings, repo)
    seen: dict = {}

    async def fake_completion(**kwargs):
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr(learn_script, "request_completion", fake_completion)

    assert main(["--all"]) == 0
    complete = calls[0]["complete"]
    assert callable(complete)
    # _llm 的调用形状本来就带 client=None：注入必须覆盖而不是追加
    # （追加会 TypeError: multiple values for keyword argument 'client'）。
    result = asyncio.run(
        complete(system_prompt="s", user_prompt="u", settings=settings, client=None)
    )
    assert result == "ok"
    assert isinstance(seen["client"], httpx.AsyncClient)


def test_missing_group_and_all_exits_one_via_argparse() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([])
    # argparse 默认参数错误退出码 2 与“部分群失败”冲突，脚本改为 1
    assert excinfo.value.code == 1


def test_build_quota_mirrors_runtime(tmp_path) -> None:
    from qq_bot.services.quota import QuotaService

    repo = ChatMemoryRepository(tmp_path / "quota.sqlite3", retention_days=30)
    assert learn_script._build_quota(_settings(quota_enabled=False), repo) is None
    quota = learn_script._build_quota(_settings(quota_enabled=True), repo)
    assert isinstance(quota, QuotaService)


def test_build_quota_falls_back_to_none_on_construction_failure(monkeypatch, tmp_path) -> None:
    from qq_bot.services import quota as quota_module

    repo = ChatMemoryRepository(tmp_path / "quota.sqlite3", retention_days=30)

    def _boom(*args, **kwargs):
        raise RuntimeError("no quota table")

    monkeypatch.setattr(quota_module, "QuotaService", _boom)
    quota = learn_script._build_quota(_settings(quota_enabled=True), repo)
    assert quota is None
