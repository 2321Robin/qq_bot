"""Agent orchestrator tests (S2-AGENT-03..08, S2-TOOL-10, S2-EVID-07).

Hermetic: the gateway is scripted, the registry runs the deterministic
fixture roster, and the clock is injectable.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from qq_bot.agent.models import (
    AgentRequest,
    AgentScope,
    FailureCode,
    GroundedAnswer,
    NormalizedResponse,
    ReasonCode,
    RouteDecision,
    RouteKind,
    SafeFailure,
    ToolCall,
)
from qq_bot.agent.orchestrator import AgentOrchestrator, _parse_answer
from qq_bot.agent.registry import ToolRegistry
from qq_bot.agent.router import derive_allowed_tools
from qq_bot.agent.tools.roco import register_roco_tools
from qq_bot.config import BotSettings
from qq_bot.services.roco_pets import load_pet_records
from qq_bot.services.roco_skills import load_skill_records

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "roco_pet_details"


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_roco_tools(
        registry,
        pet_records=tuple(load_pet_records(FIXTURE_DIR)),
        skill_records=tuple(load_skill_records(FIXTURE_DIR)),
    )
    registry.validate()
    return registry


def _settings(**overrides) -> BotSettings:
    defaults = {"ai_api_key": "test-secret", "ai_model": "test-model"}
    defaults.update(overrides)
    return BotSettings(**defaults)


def _request(
    route_kind: RouteKind = RouteKind.LOCAL_KNOWLEDGE,
    *,
    prompt: str = "TestPetA 是什么",
    clarify: bool = False,
    allowed: tuple[str, ...] | None = None,
    deadline: datetime | None = None,
) -> AgentRequest:
    tools = allowed if allowed is not None else derive_allowed_tools(route_kind)
    route = RouteDecision(
        primary_route=route_kind,
        confidence=0.95,
        reason_code=ReasonCode.EXPLICIT_COMMAND,
        needs_clarification=clarify,
        allowed_tools=tools,
    )
    return AgentRequest(
        prompt=prompt,
        scope=AgentScope(user_id="user-1", group_id=None),
        route=route,
        deadline=deadline or (datetime.now(timezone.utc) + timedelta(seconds=60)),
    )


def _tool_call(name: str, arguments: dict, call_id: str = "call_1") -> NormalizedResponse:
    return NormalizedResponse(
        text=None,
        tool_calls=(ToolCall(id=call_id, name=name, arguments=arguments),),
        finish_reason="tool_calls",
    )


def _tool_calls(items: list[tuple[str, dict, str]]) -> NormalizedResponse:
    return NormalizedResponse(
        text=None,
        tool_calls=tuple(
            ToolCall(id=call_id, name=name, arguments=arguments)
            for name, arguments, call_id in items
        ),
        finish_reason="tool_calls",
    )


def _final(payload: dict) -> NormalizedResponse:
    return NormalizedResponse(text=json.dumps(payload, ensure_ascii=False), finish_reason="stop")


def _final_text(text: str) -> NormalizedResponse:
    return NormalizedResponse(text=text, finish_reason="stop")


def _answer_with(evidence_id: str = "L1", text: str = "TestPetA 是洛克王国精灵") -> dict:
    return {
        "claims": [{"text": text, "kind": "factual", "evidence_ids": [evidence_id]}],
        "closing": "以上。",
    }


class FakeGateway:
    def __init__(self, responses: list[NormalizedResponse]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def request_model_turn(
        self,
        *,
        messages: list[dict],
        tools: list[dict] | None,
        tool_choice: str | None,
        response_format: dict | None,
        settings: BotSettings,
        client=None,
        provider: str = "primary",
    ) -> NormalizedResponse:
        self.calls.append({"messages": messages, "tools": tools, "tool_choice": tool_choice})
        return self.responses.pop(0)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _run(orc: AgentOrchestrator, request: AgentRequest):
    import asyncio

    return asyncio.run(orc.run(request))


# ---------------------------------------------------------------------------
# Limits (S2-AGENT-03)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_round_limit_returns_safe_failure() -> None:
    gateway = FakeGateway([_tool_call("lookup_pet", {"query": "TestPetA"})] * 3)
    orc = AgentOrchestrator(registry=_registry(), gateway=gateway, settings=_settings())

    outcome = await orc.run(_request())

    assert isinstance(outcome, SafeFailure)
    assert outcome.code == FailureCode.ROUND_LIMIT
    assert outcome.message
    assert len(gateway.calls) == 3


@pytest.mark.asyncio
async def test_total_call_limit_checked_before_next_turn() -> None:
    gateway = FakeGateway(
        [
            _tool_calls(
                [
                    ("lookup_pet", {"query": "TestPetA"}, "c1"),
                    ("lookup_pet", {"query": "TestPetC"}, "c2"),
                ]
            ),
            _tool_calls(
                [
                    ("lookup_pet", {"query": "TestPetB"}, "c3"),
                    ("lookup_pet", {"query": "TestPetZ"}, "c4"),
                ]
            ),
        ]
    )
    orc = AgentOrchestrator(
        registry=_registry(),
        gateway=gateway,
        settings=_settings(agent_max_tool_calls=4),
    )

    outcome = await orc.run(_request())

    assert isinstance(outcome, SafeFailure)
    assert outcome.code == FailureCode.CALL_LIMIT
    assert len(gateway.calls) == 2  # the third turn is never requested


@pytest.mark.asyncio
async def test_per_round_call_limit() -> None:
    gateway = FakeGateway(
        [
            _tool_calls(
                [
                    ("lookup_pet", {"query": "TestPetA"}, "c1"),
                    ("lookup_pet", {"query": "TestPetB"}, "c2"),
                    ("lookup_pet", {"query": "TestPetC"}, "c3"),
                ]
            )
        ]
    )
    orc = AgentOrchestrator(
        registry=_registry(),
        gateway=gateway,
        settings=_settings(agent_tools_per_round=2),
    )

    outcome = await orc.run(_request())

    assert isinstance(outcome, SafeFailure)
    assert outcome.code == FailureCode.CALL_LIMIT
    assert len(gateway.calls) == 1


# ---------------------------------------------------------------------------
# Deadline (S2-AGENT-06)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deadline_already_passed_fails_immediately() -> None:
    gateway = FakeGateway([])
    orc = AgentOrchestrator(registry=_registry(), gateway=gateway, settings=_settings())
    request = _request(deadline=datetime.now(timezone.utc) - timedelta(seconds=1))

    outcome = await orc.run(request)

    assert isinstance(outcome, SafeFailure)
    assert outcome.code == FailureCode.DEADLINE_EXCEEDED
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_deadline_mid_run_cancels_following_steps() -> None:
    clock = FakeClock()
    gate = asyncio.Event()

    class GatedGateway(FakeGateway):
        async def request_model_turn(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 2:
                await gate.wait()  # hold the final-answer turn
            return self.responses.pop(0)

    gateway = GatedGateway(
        [
            _tool_call("lookup_pet", {"query": "TestPetA"}, "c1"),
            _final(_answer_with("L1")),
        ]
    )
    orc = AgentOrchestrator(
        registry=_registry(),
        gateway=gateway,
        settings=_settings(),
        clock=clock,
    )

    task = asyncio.create_task(orc.run(_request()))
    await asyncio.sleep(0)
    # Wait deterministically until the second model turn is actually in
    # flight. asyncio.sleep(0) only guarantees one scheduling step, and the
    # event loop's step granularity differs between platforms (Proactor vs
    # Selector), so the run may not have reached the gated turn yet.
    for _ in range(1000):
        if len(gateway.calls) == 2:
            break
        await asyncio.sleep(0.001)
    assert len(gateway.calls) == 2, "second model turn never started"
    clock.now = 1e9  # deadline passes while the model turn is in flight
    gate.set()
    outcome = await task

    assert isinstance(outcome, SafeFailure)
    assert outcome.code == FailureCode.DEADLINE_EXCEEDED
    assert len(gateway.calls) == 2
    assert orc.call_log[0]["status"] == "ok"


# ---------------------------------------------------------------------------
# Call cache (S2-TOOL-10)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repeated_call_reuses_result_and_logs_cache_hit() -> None:
    gateway = FakeGateway(
        [
            _tool_call("lookup_pet", {"query": "TestPetA"}, "c1"),
            _tool_call("lookup_pet", {"query": "TestPetA"}, "c2"),
            _final(_answer_with("L1")),
        ]
    )
    orc = AgentOrchestrator(registry=_registry(), gateway=gateway, settings=_settings())

    outcome = await orc.run(_request())

    assert isinstance(outcome, GroundedAnswer)
    assert len(outcome.claims) == 1
    log = orc.call_log
    assert len(log) == 2
    assert log[0]["cached"] is False
    assert log[1]["cached"] is True
    assert log[0]["tool"] == "lookup_pet"
    assert log[0]["evidence_ids"] == ["L1"]
    assert log[1]["evidence_ids"] == ["L1"]


# ---------------------------------------------------------------------------
# Route scope (S2-AGENT-05)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_out_of_route_tool_is_denied() -> None:
    gateway = FakeGateway([_tool_call("search_web", {"query": "最新公告"}, "c1")])
    orc = AgentOrchestrator(registry=_registry(), gateway=gateway, settings=_settings())

    outcome = await orc.run(_request())

    assert isinstance(outcome, SafeFailure)
    assert outcome.code == FailureCode.TOOL_DENIED
    assert orc.call_log == []


@pytest.mark.asyncio
async def test_not_found_does_not_expand_tool_scope() -> None:
    gateway = FakeGateway(
        [
            _tool_call("lookup_pet", {"query": "不存在的精灵"}, "c1"),
            _tool_call("search_web", {"query": "查一下网上"}, "c2"),
        ]
    )
    orc = AgentOrchestrator(registry=_registry(), gateway=gateway, settings=_settings())

    outcome = await orc.run(_request(prompt="不存在的精灵"))

    assert isinstance(outcome, SafeFailure)
    assert outcome.code == FailureCode.TOOL_DENIED
    assert orc.call_log[0]["status"] == "not_found"


@pytest.mark.asyncio
async def test_clarification_decision_never_runs_tools() -> None:
    gateway = FakeGateway(
        [
            _final(
                {
                    "claims": [
                        {
                            "text": "需要确认一下：你是想查询本地图鉴还是最新活动？",
                            "kind": "conversational",
                        }
                    ]
                }
            )
        ]
    )
    orc = AgentOrchestrator(registry=_registry(), gateway=gateway, settings=_settings())

    outcome = await orc.run(_request(RouteKind.WEB_SEARCH, clarify=True, prompt="今天有什么新消息"))

    assert isinstance(outcome, GroundedAnswer)
    assert outcome.claims[0].kind == "conversational"
    assert gateway.calls[0]["tools"] is None
    assert gateway.calls[0]["tool_choice"] is None
    assert orc.call_log == []


# ---------------------------------------------------------------------------
# Verification (S2-EVID-07/08)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unverifiable_draft_ends_safely_after_repair() -> None:
    gateway = FakeGateway(
        [
            _tool_call("lookup_pet", {"query": "TestPetA"}, "c1"),
            _final(_answer_with("L99")),  # evidence id does not exist
            _final({"claims": []}),  # repair produces nothing usable
        ]
    )
    orc = AgentOrchestrator(registry=_registry(), gateway=gateway, settings=_settings())

    outcome = await orc.run(_request())

    assert isinstance(outcome, SafeFailure)
    assert outcome.code == FailureCode.VERIFICATION_FAILED
    assert len(gateway.calls) == 3  # tool turn + draft + repair turn


@pytest.mark.asyncio
async def test_repair_can_fix_a_draft() -> None:
    gateway = FakeGateway(
        [
            _tool_call("lookup_pet", {"query": "TestPetA"}, "c1"),
            _final(_answer_with("L99")),  # bad evidence id
            _final(_answer_with("L1", text="TestPetA 是洛克王国精灵（修复后）")),
        ]
    )
    orc = AgentOrchestrator(registry=_registry(), gateway=gateway, settings=_settings())

    outcome = await orc.run(_request())

    assert isinstance(outcome, GroundedAnswer)
    assert outcome.claims[0].evidence_ids == ("L1",)


@pytest.mark.asyncio
async def test_unparseable_final_is_verification_failure() -> None:
    gateway = FakeGateway([_final_text("抱歉，我不太确定这个问题的答案。")])
    orc = AgentOrchestrator(registry=_registry(), gateway=gateway, settings=_settings())

    outcome = await orc.run(_request())

    assert isinstance(outcome, SafeFailure)
    assert outcome.code == FailureCode.VERIFICATION_FAILED


# ---------------------------------------------------------------------------
# Routes and failure surfacing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_direct_chat_runs_without_tools() -> None:
    gateway = FakeGateway([_final({"claims": [{"text": "你好呀！", "kind": "conversational"}]})])
    orc = AgentOrchestrator(registry=_registry(), gateway=gateway, settings=_settings())

    outcome = await orc.run(_request(RouteKind.DIRECT_CHAT, prompt="在吗"))

    assert isinstance(outcome, GroundedAnswer)
    assert outcome.claims[0].kind == "conversational"
    assert gateway.calls[0]["tools"] is None
    assert orc.call_log == []


@pytest.mark.asyncio
async def test_gateway_exception_is_internal_error() -> None:
    class ExplodingGateway:
        async def request_model_turn(self, **kwargs):
            raise RuntimeError("boom")

    orc = AgentOrchestrator(registry=_registry(), gateway=ExplodingGateway(), settings=_settings())

    outcome = await orc.run(_request())

    assert isinstance(outcome, SafeFailure)
    assert outcome.code == FailureCode.INTERNAL_ERROR
    assert "boom" not in outcome.message


# ---------------------------------------------------------------------------
# Token budget seam (S2-TOKEN-01..08; wired in Task 10)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_insufficient_never_calls_model() -> None:
    class TightBudget:
        def allocate(self, **kwargs):
            return {"insufficient": True}

    gateway = FakeGateway([])
    orc = AgentOrchestrator(
        registry=_registry(),
        gateway=gateway,
        settings=_settings(),
        budget=TightBudget(),
    )

    outcome = await orc.run(_request())

    assert isinstance(outcome, SafeFailure)
    assert outcome.code == FailureCode.BUDGET_INSUFFICIENT
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_allocating_budget_is_consulted_before_each_turn() -> None:
    allocations: list[dict] = []

    class CountingBudget:
        def allocate(self, **kwargs):
            allocations.append(kwargs)
            return {"insufficient": False}

    gateway = FakeGateway(
        [
            _tool_call("lookup_pet", {"query": "TestPetA"}, "c1"),
            _final(_answer_with("L1")),
        ]
    )
    orc = AgentOrchestrator(
        registry=_registry(),
        gateway=gateway,
        settings=_settings(),
        budget=CountingBudget(),
    )

    outcome = await orc.run(_request())

    assert isinstance(outcome, GroundedAnswer)
    assert len(allocations) == 2  # once per model turn
    assert allocations[0]["question"] == "TestPetA 是什么"
    assert allocations[0]["tool_schemas"]
    assert allocations[1]["local_evidence"]  # tool evidence flows into budget


@pytest.mark.asyncio
async def test_real_budget_manager_fits_the_orchestrator_seam() -> None:
    """The Task 10 BudgetManager plan is consumed by the orchestrator and
    the kept evidence reaches the model turn (S2-TOKEN-05)."""
    from qq_bot.agent.token_budget import BudgetManager

    settings = _settings(ai_model="test-model", ai_context_window_tokens=60000)
    orc = AgentOrchestrator(
        registry=_registry(),
        gateway=FakeGateway(
            [
                _tool_call("lookup_pet", {"query": "TestPetA"}, "c1"),
                _final(_answer_with("L1")),
            ]
        ),
        settings=settings,
        budget=BudgetManager(settings),
    )

    outcome = await orc.run(_request())

    assert isinstance(outcome, GroundedAnswer)
    assert outcome.claims[0].evidence_ids == ("L1",)


@pytest.mark.asyncio
async def test_real_budget_manager_insufficient_fails_before_model() -> None:
    from qq_bot.agent.token_budget import BudgetManager

    settings = _settings(
        ai_model="test-model",
        ai_context_window_tokens=4000,
        ai_output_reserve_tokens=2048,
        ai_token_safety_margin=1024,
    )
    gateway = FakeGateway([])
    orc = AgentOrchestrator(
        registry=_registry(),
        gateway=gateway,
        settings=settings,
        budget=BudgetManager(settings),
    )

    outcome = await orc.run(_request())

    assert isinstance(outcome, SafeFailure)
    assert outcome.code == FailureCode.BUDGET_INSUFFICIENT
    assert gateway.calls == []


# ---------------------------------------------------------------------------
# Draft parsing
# ---------------------------------------------------------------------------


def test_parse_answer_accepts_fenced_json() -> None:
    answer = _parse_answer('```json\n{"claims": []}\n```')
    assert answer is not None
    assert answer.claims == ()


def test_parse_answer_rejects_garbage() -> None:
    assert _parse_answer(None) is None
    assert _parse_answer("") is None
    assert _parse_answer("不是 JSON") is None
    assert _parse_answer("[1, 2, 3]") is None
    assert _parse_answer('{"unknown_field": true}') is None


def test_call_log_never_contains_arguments_or_content() -> None:
    gateway = FakeGateway(
        [
            _tool_call("lookup_pet", {"query": "TestPetA"}, "c1"),
            _final(_answer_with("L1")),
        ]
    )
    orc = AgentOrchestrator(registry=_registry(), gateway=gateway, settings=_settings())
    _run(orc, _request())

    serialized = json.dumps(orc.call_log, ensure_ascii=False)
    assert "TestPetA" not in serialized  # query argument never logged
    assert "洛克王国" not in serialized  # draft text never logged
