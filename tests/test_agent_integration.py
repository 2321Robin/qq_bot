"""Stage-2 end-to-end integration tests (Task 13, S2-AGENT-09, S2-MEM-09).

The full agent stack is assembled the way ``runtime.py`` does it: real
ToolRegistry, real BudgetManager, real LayeredMemoryService over a real
SQLite repository — only the model gateway is faked. These tests pin the
memory-layer contract between the orchestrator and the layered memory
service: layers reach the budget and the model turn, are scoped by
``AgentScope``/route, and degrade gracefully.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from qq_bot.agent.models import (
    AgentRequest,
    AgentScope,
    GroundedAnswer,
    NormalizedResponse,
    ReasonCode,
    RouteDecision,
    RouteKind,
)
from qq_bot.agent.orchestrator import AgentOrchestrator
from qq_bot.agent.registry import ToolRegistry
from qq_bot.agent.token_budget import BudgetManager
from qq_bot.agent.tools.memory import register_memory_tools
from qq_bot.agent.tools.roco import register_roco_tools
from qq_bot.config import BotSettings
from qq_bot.services.chat_memory import ChatMemoryRepository
from qq_bot.services.layered_memory import LayeredMemoryService

_UTC = timezone.utc
_DEADLINE = datetime(2099, 1, 1, tzinfo=_UTC)


class FakeGateway:
    """Records every model turn; pops scripted responses."""

    def __init__(self, responses: list[NormalizedResponse]):
        self.responses = list(responses)
        self.messages: list[list[dict[str, str]]] = []

    async def request_model_turn(
        self,
        *,
        messages,
        tools=None,
        tool_choice=None,
        response_format=None,
        settings=None,
        client=None,
        provider="primary",
    ) -> NormalizedResponse:
        self.messages.append(messages)
        if not self.responses:
            return NormalizedResponse(text='{"claims": []}', finish_reason="stop")
        return self.responses.pop(0)


def _final(claims: list[dict], closing: str | None = None) -> NormalizedResponse:
    return NormalizedResponse(
        text=json.dumps({"claims": claims, "closing": closing}, ensure_ascii=False),
        finish_reason="stop",
    )


@pytest.fixture
async def memory_repository(tmp_path) -> ChatMemoryRepository:
    repo = ChatMemoryRepository(tmp_path / "memory.sqlite3", retention_days=30)
    await repo.open()
    yield repo
    await repo.close()


def _build_stack(
    memory_repository: ChatMemoryRepository,
    gateway: FakeGateway,
    budget: BudgetManager | None = None,
    *,
    settings: BotSettings | None = None,
) -> AgentOrchestrator:
    registry = ToolRegistry()
    register_roco_tools(registry)
    register_memory_tools(registry, memory_repository)
    active_settings = settings or BotSettings(ai_api_key="test-secret", ai_model="test-model")
    active_budget = budget or BudgetManager(active_settings)
    memory = LayeredMemoryService(
        memory_repository, active_settings, gateway=gateway, budget=active_budget
    )
    return AgentOrchestrator(
        registry=registry,
        gateway=gateway,
        settings=active_settings,
        budget=active_budget,
        memory=memory,
    )


def _request(
    prompt: str,
    *,
    scope: AgentScope | None = None,
    route: RouteKind = RouteKind.CHAT_MEMORY,
) -> AgentRequest:
    active_scope = scope or AgentScope(group_id="1001", user_id="2001", can_use_chat_memory=True)
    return AgentRequest(
        prompt=prompt,
        scope=active_scope,
        route=RouteDecision(
            primary_route=route,
            confidence=0.95,
            reason_code=ReasonCode.EXPLICIT_COMMAND,
            allowed_tools=("search_chat_memory",),
        ),
        deadline=_DEADLINE,
    )


@pytest.mark.asyncio
async def test_memory_layers_reach_budget_and_model_turn(
    memory_repository,
) -> None:
    """Recent messages and preferences flow into the budget allocation and
    the model's user message (S2-MEM-09, S2-TOKEN-04)."""
    now = datetime.now(_UTC)
    await memory_repository.add_message(
        group_id=1001,
        user_id=2001,
        message_text="ai 我最近在练暗影格斗",
        is_ai_prompt=True,
        created_at=now,
        now=now,
    )
    await memory_repository.save_preference(
        group_id=1001, user_id=2001, preference="喜欢暗影系精灵"
    )

    allocations: dict[str, object] = {}

    class RecordingBudget(BudgetManager):
        def allocate(self, **kwargs):
            allocations.update(kwargs)
            return super().allocate(**kwargs)

    gateway = FakeGateway([_final([{"text": "好的", "kind": "conversational"}])])
    orchestrator = _build_stack(
        memory_repository,
        gateway,
        RecordingBudget(BotSettings(ai_api_key="test-secret", ai_model="test-model")),
    )

    outcome = await orchestrator.run(_request("我最近在练什么？"))

    assert isinstance(outcome, GroundedAnswer)
    assert "ai 我最近在练暗影格斗" in allocations["recent_messages"]  # type: ignore[operator]
    assert "喜欢暗影系精灵" in allocations["preferences"]  # type: ignore[operator]
    user_content = gateway.messages[0][1]["content"]
    assert "近期消息" in user_content
    assert "暗影格斗" in user_content
    assert "用户长期偏好" in user_content
    assert orchestrator.last_store is not None


@pytest.mark.asyncio
async def test_memory_layers_gated_by_scope_and_route(
    memory_repository,
) -> None:
    """can_use_chat_memory=False or a route without CHAT_MEMORY keeps the
    layers out of both the budget and the model turn (S2-MEM-09)."""
    now = datetime.now(_UTC)
    await memory_repository.add_message(
        group_id=1001,
        user_id=2001,
        message_text="ai 秘密消息",
        is_ai_prompt=True,
        created_at=now,
        now=now,
    )

    allocations: dict[str, object] = {}

    class RecordingBudget(BudgetManager):
        def allocate(self, **kwargs):
            allocations.update(kwargs)
            return super().allocate(**kwargs)

    gateway = FakeGateway([_final([{"text": "好的", "kind": "conversational"}])])
    orchestrator = _build_stack(
        memory_repository,
        gateway,
        RecordingBudget(BotSettings(ai_api_key="test-secret", ai_model="test-model")),
    )

    denied_scope = AgentScope(group_id="1001", user_id="2001", can_use_chat_memory=False)
    outcome = await orchestrator.run(_request("我最近聊过什么？", scope=denied_scope))

    assert isinstance(outcome, GroundedAnswer)
    assert allocations["recent_messages"] == []
    assert allocations["preferences"] is None
    assert "秘密消息" not in gateway.messages[0][1]["content"]


@pytest.mark.asyncio
async def test_clarification_route_never_loads_memory(
    memory_repository,
) -> None:
    """A needs_clarification decision (allowed_tools=()) must not read the
    memory layers at all (Scenario E)."""
    now = datetime.now(_UTC)
    await memory_repository.add_message(
        group_id=1001,
        user_id=2001,
        message_text="ai 你记得我昨晚说了什么吗",
        is_ai_prompt=True,
        created_at=now,
        now=now,
    )
    gateway = FakeGateway([_final([{"text": "好的", "kind": "conversational"}])])
    orchestrator = _build_stack(memory_repository, gateway)
    request = AgentRequest(
        prompt="随便聊聊",
        scope=AgentScope(group_id="1001", user_id="2001", can_use_chat_memory=True),
        route=RouteDecision(
            primary_route=RouteKind.DIRECT_CHAT,
            confidence=0.4,
            reason_code=ReasonCode.CLARIFY,
            needs_clarification=True,
            allowed_tools=(),
        ),
        deadline=_DEADLINE,
    )

    outcome = await orchestrator.run(request)

    assert isinstance(outcome, GroundedAnswer)
    assert "你记得我昨晚" not in gateway.messages[0][1]["content"]


@pytest.mark.asyncio
async def test_memory_layer_failure_degrades_gracefully(
    memory_repository,
) -> None:
    """A failing memory layer never blocks the answer (S2-MEM-09)."""
    gateway = FakeGateway([_final([{"text": "好的", "kind": "conversational"}])])
    orchestrator = _build_stack(memory_repository, gateway)

    class ExplodingMemory(LayeredMemoryService):
        async def recent_layer(self, *, group_id, limit, now=None):
            raise RuntimeError("boom")

    orchestrator._memory = ExplodingMemory(
        memory_repository,
        BotSettings(ai_api_key="test-secret", ai_model="test-model"),
        gateway=gateway,
        budget=BudgetManager(BotSettings(ai_api_key="test-secret", ai_model="test-model")),
    )

    outcome = await orchestrator.run(_request("你好"))

    assert isinstance(outcome, GroundedAnswer)
    assert "boom" not in str(gateway.messages[0][1]["content"])


@pytest.mark.asyncio
async def test_memory_insufficient_budget_fails_before_model(
    memory_repository,
) -> None:
    """When the recent layer overflows a tight window, BUDGET_INSUFFICIENT
    fires before any model turn (S2-TOKEN-01)."""
    now = datetime.now(_UTC)
    for index in range(12):
        await memory_repository.add_message(
            group_id=1001,
            user_id=2001,
            message_text=f"ai 第{index}条很长的历史消息 " + "内容" * 200,
            is_ai_prompt=True,
            created_at=now,
            now=now,
        )
    gateway = FakeGateway([])
    tight = BotSettings(
        ai_api_key="test-secret",
        ai_model="test-model",
        ai_context_window_tokens=4000,
        ai_output_reserve_tokens=2048,
        ai_token_safety_margin=1024,
    )
    orchestrator = _build_stack(memory_repository, gateway, settings=tight)

    from qq_bot.agent.models import FailureCode, SafeFailure

    outcome = await orchestrator.run(_request("我最近聊了什么？"))

    assert isinstance(outcome, SafeFailure)
    assert outcome.code == FailureCode.BUDGET_INSUFFICIENT
    assert gateway.messages == []
