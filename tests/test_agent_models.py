"""Agent protocol model tests (S2-ROUTE-01/02, S2-TOOL-03/04, S2-EVID-01..03)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from qq_bot.agent.models import (
    AgentOutcome,
    AgentRequest,
    AgentScope,
    Claim,
    Evidence,
    FailureCode,
    GroundedAnswer,
    NormalizedResponse,
    ProviderCapabilities,
    ReasonCode,
    RouteDecision,
    RouteKind,
    SafeFailure,
    ToolCall,
    ToolResult,
)


def _route(**overrides: object) -> RouteDecision:
    fields: dict[str, object] = {
        "primary_route": RouteKind.LOCAL_KNOWLEDGE,
        "confidence": 0.9,
        "reason_code": ReasonCode.STRUCTURED_CLASSIFIER,
        "needs_clarification": False,
        "secondary_route": None,
        "allowed_tools": ("lookup_pet",),
    }
    fields.update(overrides)
    return RouteDecision(**fields)


def _tool_call(**overrides: object) -> ToolCall:
    fields: dict[str, object] = {
        "id": "call_1",
        "name": "lookup_pet",
        "arguments": {"query": "TestPetA"},
    }
    fields.update(overrides)
    return ToolCall(**fields)


def _evidence(**overrides: object) -> Evidence:
    fields: dict[str, object] = {
        "id": "L1",
        "source_type": "local",
        "title": "TestPetA",
        "facts": {"number": "001"},
        "url": None,
    }
    fields.update(overrides)
    return Evidence(**fields)


def test_route_kind_values_are_strict() -> None:
    values = {kind.value for kind in RouteKind}
    assert values == {"local_knowledge", "web_search", "chat_memory", "direct_chat"}


def test_route_round_trips() -> None:
    route = _route()
    dumped = route.model_dump_json()
    assert RouteDecision.model_validate_json(dumped) == route


def test_unknown_route_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _route(primary_route="teleport")


def test_confidence_out_of_range_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _route(confidence=1.5)
    with pytest.raises(ValidationError):
        _route(confidence=-0.1)


def test_secondary_route_equal_to_primary_is_rejected() -> None:
    with pytest.raises(ValidationError, match="secondary_route"):
        _route(secondary_route=RouteKind.LOCAL_KNOWLEDGE)


def test_secondary_route_different_is_accepted() -> None:
    route = _route(secondary_route=RouteKind.WEB_SEARCH)
    assert route.secondary_route == RouteKind.WEB_SEARCH


def test_route_extra_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        _route(model_provided_tools=["anything"])


def test_tool_call_round_trips() -> None:
    call = _tool_call()
    assert ToolCall.model_validate_json(call.model_dump_json()) == call


def test_tool_call_name_must_be_bare_identifier() -> None:
    with pytest.raises(ValidationError):
        _tool_call(name="search web")
    with pytest.raises(ValidationError):
        _tool_call(name="")


def test_tool_call_arguments_must_be_json_safe() -> None:
    with pytest.raises(ValidationError):
        _tool_call(arguments={"query": object()})
    with pytest.raises(ValidationError):
        _tool_call(arguments={"nested": {"bad": object()}})
    with pytest.raises(ValidationError):
        _tool_call(arguments={f"key{i}": i for i in range(33)})
    call = _tool_call(arguments={"a": [1, {"b": None, "c": True}]})
    assert call.arguments == {"a": [1, {"b": None, "c": True}]}


def test_tool_call_overlong_name_and_id_rejected() -> None:
    with pytest.raises(ValidationError):
        _tool_call(name="x" * 65)
    with pytest.raises(ValidationError):
        _tool_call(id="y" * 129)


def test_evidence_prefix_must_match_source() -> None:
    for source, prefix in (("local", "L"), ("web", "W"), ("memory", "M")):
        evidence = _evidence(id=f"{prefix}1", source_type=source)
        assert evidence.source_type == source
    with pytest.raises(ValidationError, match="must start with"):
        _evidence(id="W1", source_type="local")
    with pytest.raises(ValidationError, match="must start with"):
        _evidence(id="X1", source_type="local")
    with pytest.raises(ValidationError):
        _evidence(id="L", source_type="local")


def test_evidence_url_must_be_http_with_host() -> None:
    with pytest.raises(ValidationError):
        _evidence(id="W1", url="javascript:alert(1)", source_type="web")
    with pytest.raises(ValidationError):
        _evidence(id="W1", url="file:///etc/passwd", source_type="web")
    with pytest.raises(ValidationError):
        _evidence(id="W1", url="https://" + "a" * 2100, source_type="web")
    ok = _evidence(id="W1", url="https://example.com/x", source_type="web")
    assert ok.url == "https://example.com/x"


def test_tool_result_round_trip_is_json_serializable() -> None:
    result = ToolResult(
        tool="lookup_pet",
        status="ok",
        evidence=(_evidence(), _evidence(id="L2", title="第二")),
        warnings=("注意",),
        truncated=False,
    )
    loaded = ToolResult.model_validate_json(result.model_dump_json())
    assert loaded == result


def test_tool_result_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ToolResult(tool="x", status="ok", stack_trace="secret")


def test_tool_result_schema_version_only_one() -> None:
    with pytest.raises(ValidationError):
        ToolResult(tool="x", status="ok", schema_version=2)


def test_tool_result_statuses_are_strict() -> None:
    with pytest.raises(ValidationError):
        ToolResult(tool="x", status="maybe")


def test_claim_and_grounded_answer() -> None:
    claim = Claim(text="编号是 001", evidence_ids=("L1",))
    answer = GroundedAnswer(claims=(claim,), closing="还有别的吗？")
    assert answer.claims[0].kind == "factual"
    assert answer.model_dump()["closing"] == "还有别的吗？"
    with pytest.raises(ValidationError):
        Claim(text="", evidence_ids=("L1",))


def test_agent_outcome_union() -> None:
    answer: AgentOutcome = GroundedAnswer(claims=())
    failure: AgentOutcome = SafeFailure(code=FailureCode.ROUND_LIMIT, message="轮次用完了")
    assert isinstance(answer, GroundedAnswer)
    assert isinstance(failure, SafeFailure)


def test_safe_failure_requires_code_and_message() -> None:
    with pytest.raises(ValidationError):
        SafeFailure(code="not-a-code", message="x")
    with pytest.raises(ValidationError):
        SafeFailure(code=FailureCode.CALL_LIMIT, message="")


def test_agent_scope_and_request() -> None:
    scope = AgentScope(group_id="12345", user_id="67890", can_use_chat_memory=True)
    request = AgentRequest(
        prompt="TestPetA 的编号是多少？",
        scope=scope,
        route=_route(),
        deadline=datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc),
    )
    assert request.scope.can_use_chat_memory is True
    with pytest.raises(ValidationError):
        AgentRequest(prompt="", scope=scope, route=_route(), deadline=request.deadline)
    with pytest.raises(ValidationError):
        AgentRequest(
            prompt="x" * 2001,
            scope=scope,
            route=_route(),
            deadline=request.deadline,
        )


def test_normalized_response_round_trip() -> None:
    response = NormalizedResponse(
        text="好的",
        tool_calls=(_tool_call(),),
        finish_reason="tool_calls",
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )
    assert NormalizedResponse.model_validate_json(response.model_dump_json()) == response


def test_normalized_response_usage_missing_is_none() -> None:
    response = NormalizedResponse(text="hi", finish_reason="stop")
    assert response.usage is None


def test_normalized_response_rejects_unknown_usage_keys() -> None:
    with pytest.raises(ValidationError):
        NormalizedResponse(text="hi", finish_reason="stop", usage={"cached_tokens": 5})


def test_provider_capabilities_round_trip() -> None:
    caps = ProviderCapabilities(tools=True, structured_output=False, usage=True)
    assert ProviderCapabilities.model_validate_json(caps.model_dump_json()) == caps
