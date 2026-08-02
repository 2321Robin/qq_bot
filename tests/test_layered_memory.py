"""Layered memory service, memory tool and memory command tests
(S2-MEM-01..09, S2-TOOL-06, S2-TOOL-09, S2-CONFIG-03).

Hermetic: a real repository over a temp database; the summary gateway is a
scripted fake; no real AI calls.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import bot  # noqa: F401  (initializes NoneBot for matcher creation)
import pytest
from nonebot.adapters.onebot.v11 import Message

from qq_bot.agent.models import AgentScope
from qq_bot.agent.registry import ToolContext, ToolRegistry
from qq_bot.agent.tools.memory import (
    SearchChatMemoryInput,
    register_memory_tools,
)
from qq_bot.config import BotSettings
from qq_bot.plugins import memory_commands
from qq_bot.services.chat_memory import ChatMemoryRepository
from qq_bot.services.layered_memory import LayeredMemoryService

_UTC = timezone.utc

_NOW = datetime(2026, 5, 11, 12, 0, tzinfo=_UTC)


@pytest.fixture
async def repository(tmp_path) -> ChatMemoryRepository:
    repo = ChatMemoryRepository(tmp_path / "memory.sqlite3", retention_days=3)
    await repo.open()
    yield repo
    await repo.close()


def _settings(**overrides) -> BotSettings:
    defaults = {"memory_summary_enabled": True, "memory_preference_max_chars": 200}
    defaults.update(overrides)
    return BotSettings(**defaults)


async def _seed(repository: ChatMemoryRepository) -> tuple[int, int]:
    first = await repository.add_message(
        1001, 2001, "讨论主题：宠物养成", created_at=_NOW - timedelta(hours=2), now=_NOW
    )
    second = await repository.add_message(
        1001, 2001, "决定：优先练火系", created_at=_NOW - timedelta(hours=1), now=_NOW
    )
    await repository.add_message(
        1001, 2002, "其他人的消息", created_at=_NOW - timedelta(hours=3), now=_NOW
    )
    return first, second


# ---------------------------------------------------------------------------
# Layers (S2-MEM-01..06)
# ---------------------------------------------------------------------------


async def test_recent_layer_reuses_repository_query(repository) -> None:
    service = LayeredMemoryService(repository, _settings())
    await _seed(repository)

    rows = await service.recent_layer(group_id=1001, limit=5, now=_NOW)

    assert [row.message_text for row in rows] == [
        "其他人的消息",
        "讨论主题：宠物养成",
        "决定：优先练火系",
    ]


async def test_summary_layer_disabled_by_default(repository) -> None:
    service = LayeredMemoryService(repository, _settings(memory_summary_enabled=False))
    await _seed(repository)

    assert await service.summary_layer(group_id=1001, now=_NOW) == []


def _plan(*, dropped: int) -> object:
    """Minimal BudgetPlan stand-in exposing the fields the service reads."""

    class _Alloc:
        source = "recent_messages"
        tokens = 99
        estimated = True
        reason = "quota_exceeded" if dropped else ""
        dropped_units = dropped

    class _Plan:
        allocations = (_Alloc(),)

    return _Plan()


class _SummaryResponse:
    def __init__(self, text: str):
        self.text = text


async def test_summary_layer_generates_structured_summary_only_over_budget(
    repository,
) -> None:
    class FakeGateway:
        def __init__(self):
            self.calls = 0

        async def request_model_turn(self, *, messages, response_format, settings, **kwargs):
            self.calls += 1
            assert response_format["type"] == "json_object"
            return _SummaryResponse(
                json.dumps(
                    {
                        "speaker_range": "user 2001",
                        "time_range": "2026-05-11T10:00Z..12:00Z",
                        "topics": ["宠物养成", "火系"],
                        "decisions": ["优先练火系"],
                        "explicit_preferences": [],
                        "open_questions": [],
                    },
                    ensure_ascii=False,
                )
            )

    class TinyBudget:
        def allocate(self, **kwargs):
            return _plan(dropped=3)

    gateway = FakeGateway()
    service = LayeredMemoryService(repository, _settings(), gateway=gateway, budget=TinyBudget())
    await _seed(repository)

    summaries = await service.summary_layer(group_id=1001, now=_NOW)

    assert gateway.calls == 1
    assert len(summaries) == 1
    payload = json.loads(summaries[0].summary)
    assert set(payload) == {
        "speaker_range",
        "time_range",
        "topics",
        "decisions",
        "explicit_preferences",
        "open_questions",
    }
    assert payload["decisions"] == ["优先练火系"]
    # reused on the next call — no second model call
    assert await service.summary_layer(group_id=1001, now=_NOW) == summaries
    assert gateway.calls == 1


async def test_summary_layer_not_generated_when_history_fits(repository) -> None:
    class FakeGateway:
        async def request_model_turn(self, **kwargs):
            raise AssertionError("model must not be called when history fits")

    class GenerousBudget:
        def allocate(self, **kwargs):
            return _plan(dropped=0)

    service = LayeredMemoryService(
        repository, _settings(), gateway=FakeGateway(), budget=GenerousBudget()
    )
    await _seed(repository)

    assert await service.summary_layer(group_id=1001, now=_NOW) == []


async def test_summary_layer_survives_model_failure(repository) -> None:
    class FailingGateway:
        async def request_model_turn(self, **kwargs):
            raise RuntimeError("provider down")

    class TinyBudget:
        def allocate(self, **kwargs):
            return _plan(dropped=3)

    service = LayeredMemoryService(
        repository, _settings(), gateway=FailingGateway(), budget=TinyBudget()
    )
    await _seed(repository)

    assert await service.summary_layer(group_id=1001, now=_NOW) == []


async def test_preference_layer_returns_only_own_enabled_preferences(
    repository,
) -> None:
    service = LayeredMemoryService(repository, _settings())
    await repository.save_preference(
        group_id=1001, user_id=2001, preference="喜欢火系宠物", now=_NOW
    )
    await repository.save_preference(
        group_id=1001, user_id=2001, preference="不喜欢自动回复", now=_NOW + timedelta(minutes=1)
    )
    await repository.save_preference(group_id=1001, user_id=2002, preference="别人的偏好", now=_NOW)

    own, enabled = await service.preference_layer(group_id=1001, user_id=2001)
    assert enabled is True
    assert own == ["不喜欢自动回复", "喜欢火系宠物"]
    assert await service.preference_layer(group_id=1001, user_id=2002) == (
        ["别人的偏好"],
        True,
    )


async def test_preference_layer_caps_total_chars_whole_preferences(repository) -> None:
    service = LayeredMemoryService(repository, _settings(memory_preference_max_chars=30))
    await repository.save_preference(
        group_id=1001, user_id=2001, preference="很长的偏好" * 20, now=_NOW
    )
    await repository.save_preference(
        group_id=1001, user_id=2001, preference="短偏好", now=_NOW + timedelta(minutes=1)
    )

    kept, enabled = await service.preference_layer(group_id=1001, user_id=2001)
    assert enabled is True
    assert kept == ["短偏好"]  # the oversized one is dropped whole, never split


async def test_preference_layer_empty_after_close(repository) -> None:
    service = LayeredMemoryService(repository, _settings())
    await repository.save_preference(group_id=1001, user_id=2001, preference="待清除", now=_NOW)
    await service.close_preferences(group_id=1001, user_id=2001)

    kept, enabled = await service.preference_layer(group_id=1001, user_id=2001)
    assert kept == []
    assert enabled is False


async def test_save_preference_enforces_length_and_service_write_boundary(
    repository,
) -> None:
    service = LayeredMemoryService(repository, _settings(memory_preference_max_chars=10))
    with pytest.raises(ValueError, match="10 character"):
        await service.save_preference(group_id=1001, user_id=2001, content="x" * 11)
    with pytest.raises(ValueError, match="empty"):
        await service.save_preference(group_id=1001, user_id=2001, content="   ")

    preference_id = await service.save_preference(group_id=1001, user_id=2001, content="简短偏好")
    rows, enabled = await service.list_preferences(group_id=1001, user_id=2001)
    assert enabled is True
    assert [row.id for row in rows] == [preference_id]
    assert (
        await service.delete_preference(group_id=1001, user_id=2001, preference_id=preference_id)
        is True
    )
    assert (
        await service.delete_preference(group_id=1001, user_id=2001, preference_id=preference_id)
        is False
    )


async def test_delete_all_removes_user_memory_and_invalidates_summaries(
    repository,
) -> None:
    service = LayeredMemoryService(repository, _settings())
    first, second = await _seed(repository)
    await repository.add_summary(1001, "含本人源的摘要", [first, second], now=_NOW)
    await repository.save_preference(group_id=1001, user_id=2001, preference="本人偏好", now=_NOW)

    affected = await service.delete_all(group_id=1001, user_id=2001)

    assert set(affected) == {first, second}
    assert await repository.get_summaries(group_id=1001, now=_NOW) == []
    assert await repository.get_preferences(group_id=1001, user_id=2001) == []
    rows = await repository.recent_group_messages(group_id=1001, limit=10, now=_NOW)
    assert [row.user_id for row in rows] == [2002]  # other user untouched


# ---------------------------------------------------------------------------
# Memory tool (S2-TOOL-06)
# ---------------------------------------------------------------------------


def _memory_registry(repository: ChatMemoryRepository) -> ToolRegistry:
    registry = ToolRegistry()
    register_memory_tools(registry, repository)
    registry.validate()
    return registry


def _memory_context(**scope_overrides) -> ToolContext:
    defaults = {"group_id": "1001", "user_id": "2001", "can_use_chat_memory": True}
    defaults.update(scope_overrides)
    return ToolContext(scope=AgentScope(**defaults), evidence_index=4)


async def _seed_recent(repository: ChatMemoryRepository) -> tuple[int, int]:
    """Messages close to the real clock: the memory tool runs the
    repository's retention cleanup with real time."""
    now = datetime.now(_UTC)
    first = await repository.add_message(
        1001,
        2001,
        "讨论主题：宠物养成",
        is_ai_prompt=True,
        created_at=now - timedelta(hours=2),
        now=now,
    )
    second = await repository.add_message(
        1001,
        2001,
        "决定：优先练火系",
        is_ai_prompt=True,
        created_at=now - timedelta(hours=1),
        now=now,
    )
    await repository.add_message(
        1001, 2002, "其他人的消息", created_at=now - timedelta(hours=3), now=now
    )
    return first, second


async def test_memory_tool_recent_returns_own_turns(repository) -> None:
    await _seed_recent(repository)
    spec = _memory_registry(repository).get("search_chat_memory")
    assert spec is not None

    result = await spec.execute({"query_type": "recent", "limit": 5}, _memory_context())

    assert result.status == "ok"
    evidence = result.evidence[0]
    assert evidence.id == "M5"  # derives from the request evidence_index
    assert evidence.source_type == "memory"
    users = {message["user_id"] for message in evidence.facts["messages"]}
    assert users == {2001}
    texts = [message["text"] for message in evidence.facts["messages"]]
    assert "决定：优先练火系" in texts


async def test_memory_tool_keyword_search_is_group_wide(repository) -> None:
    await _seed_recent(repository)
    spec = _memory_registry(repository).get("search_chat_memory")
    assert spec is not None

    result = await spec.execute(
        {"query_type": "keyword", "keyword": "其他人的", "limit": 5},
        _memory_context(),
    )

    assert result.status == "ok"
    users = {message["user_id"] for message in result.evidence[0].facts["messages"]}
    assert users == {2002}


async def test_memory_tool_user_query_scoped_to_current_user(repository) -> None:
    await _seed_recent(repository)
    spec = _memory_registry(repository).get("search_chat_memory")
    assert spec is not None

    result = await spec.execute({"query_type": "user", "limit": 5}, _memory_context())

    assert result.status == "ok"
    users = {message["user_id"] for message in result.evidence[0].facts["messages"]}
    assert users == {2001}


async def test_memory_tool_denied_without_memory_permission(repository) -> None:
    spec = _memory_registry(repository).get("search_chat_memory")
    assert spec is not None

    result = await spec.execute(
        {"query_type": "recent", "limit": 5},
        _memory_context(can_use_chat_memory=False),
    )

    assert result.status == "denied"
    assert result.evidence == ()


async def test_memory_tool_scope_is_not_forgeable(repository) -> None:
    spec = _memory_registry(repository).get("search_chat_memory")
    assert spec is not None

    # the model cannot specify group/user scope in the input schema
    schema = SearchChatMemoryInput.model_json_schema()
    assert "group_id" not in schema["properties"]
    assert "user_id" not in schema["properties"]
    assert schema["additionalProperties"] is False

    # ...and extra scope arguments are rejected, not silently accepted
    result = await spec.execute(
        {"query_type": "recent", "limit": 5, "group_id": "9999"}, _memory_context()
    )
    assert result.status == "invalid_argument"


async def test_memory_tool_input_limits_and_not_found(repository) -> None:
    spec = _memory_registry(repository).get("search_chat_memory")
    assert spec is not None

    assert (
        await spec.execute({"query_type": "recent", "limit": 0}, _memory_context())
    ).status == "invalid_argument"
    assert (
        await spec.execute({"query_type": "recent", "limit": 11}, _memory_context())
    ).status == "invalid_argument"
    assert (
        await spec.execute({"query_type": "recent", "limit": 5}, _memory_context())
    ).status == "not_found"


# ---------------------------------------------------------------------------
# Memory commands (S2-MEM-05..08)
# ---------------------------------------------------------------------------


class FakeEvent:
    group_id = 1001
    user_id = 2001

    def __init__(self, text: str = "", *, user_id: int | None = None):
        self.text = text
        if user_id is not None:
            self.user_id = user_id

    def get_message(self) -> "FakeEvent":
        return self

    def extract_plain_text(self) -> str:
        return self.text

    def __iter__(self):
        return iter(())


class FinishCalled(Exception):
    def __init__(self, message: object):
        self.message = message


async def _drive_command(matcher, handler, event, args_text: str | None = None) -> str:
    """Run a command handler with a patched finish; returns the finish text."""

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    original = matcher.finish
    matcher.finish = fake_finish  # type: ignore[method-assign]
    try:
        if args_text is not None:
            args = Message(args_text)
            with pytest.raises(FinishCalled) as exc_info:
                await handler(event, args)
        else:
            with pytest.raises(FinishCalled) as exc_info:
                await handler(event)
        return str(exc_info.value.message)
    finally:
        matcher.finish = original  # type: ignore[method-assign]


@pytest.fixture
def _patch_repository(monkeypatch, repository) -> ChatMemoryRepository:
    monkeypatch.setattr(memory_commands, "get_chat_repository", lambda: repository)
    return repository


async def test_memory_save_command_persists_current_user_only(
    _patch_repository,
) -> None:
    message = await _drive_command(
        memory_commands.memory_save_command,
        memory_commands.handle_memory_save,
        FakeEvent(),
        "喜欢火系宠物",
    )
    assert "已保存" in message

    rows = await _patch_repository.get_preferences(group_id=1001, user_id=2001)
    assert [row.preference for row in rows] == ["喜欢火系宠物"]


async def test_memory_save_command_rejects_oversized_content(_patch_repository) -> None:
    message = await _drive_command(
        memory_commands.memory_save_command,
        memory_commands.handle_memory_save,
        FakeEvent(),
        "长" * 300,
    )
    assert "保存失败" in message


async def test_memory_view_command_shows_only_own_preferences_and_state(
    _patch_repository,
) -> None:
    repo = _patch_repository
    await repo.save_preference(group_id=1001, user_id=2001, preference="我的偏好", now=_NOW)
    await repo.save_preference(group_id=1001, user_id=2002, preference="别人的", now=_NOW)

    message = await _drive_command(
        memory_commands.memory_view_command, memory_commands.handle_memory_view, FakeEvent()
    )

    assert "我的偏好" in message
    assert "别人的" not in message
    assert "已开启" in message


async def test_memory_delete_command_single_and_all(_patch_repository) -> None:
    repo = _patch_repository
    preference_id = await repo.save_preference(
        group_id=1001, user_id=2001, preference="待删除", now=_NOW
    )
    await _seed(repo)

    message = await _drive_command(
        memory_commands.memory_delete_command,
        memory_commands.handle_memory_delete,
        FakeEvent(),
        str(preference_id),
    )
    assert "已删除该条偏好" in message
    assert await repo.get_preferences(group_id=1001, user_id=2001) == []

    message = await _drive_command(
        memory_commands.memory_delete_command,
        memory_commands.handle_memory_delete,
        FakeEvent(),
        "全部",
    )
    assert "全部记忆" in message
    rows = await repo.recent_group_messages(group_id=1001, limit=10, now=_NOW)
    assert [row.user_id for row in rows] == [2002]  # other user's messages kept


async def test_memory_close_command_clears_and_blocks(_patch_repository) -> None:
    repo = _patch_repository
    await repo.save_preference(group_id=1001, user_id=2001, preference="将被清除", now=_NOW)

    message = await _drive_command(
        memory_commands.memory_close_command, memory_commands.handle_memory_close, FakeEvent()
    )

    assert "已关闭长期偏好" in message
    assert await repo.preferences_enabled(group_id=1001, user_id=2001) is False
