"""Structured router tests (S2-ROUTE-01..08)."""

from __future__ import annotations

import asyncio

from qq_bot.agent.models import NormalizedResponse, ReasonCode, RouteKind
from qq_bot.agent.router import (
    RouteTrace,
    derive_allowed_tools,
    route_request,
)
from qq_bot.config import BotSettings


class FakeGateway:
    def __init__(
        self,
        response: NormalizedResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def request_model_turn(self, **kwargs: object) -> NormalizedResponse:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        if self.response is not None:
            return self.response
        return NormalizedResponse(text="{}", finish_reason="stop")


def _settings(**overrides: object) -> BotSettings:
    fields: dict[str, object] = {
        "ai_router_confidence_threshold": 0.75,
        "search_enabled": False,
        "tavily_api_key": "",
    }
    fields.update(overrides)
    return BotSettings(**fields)  # type: ignore[arg-type]


def _json_response(primary_route: str, confidence: float) -> NormalizedResponse:
    return NormalizedResponse(
        text=f'{{"primary_route": "{primary_route}", "confidence": {confidence}}}',
        finish_reason="stop",
    )


def test_explicit_command_route_skips_model() -> None:
    gateway = FakeGateway(error=RuntimeError("must not be called"))
    decision, trace = asyncio.run(
        route_request("/精灵 TestPetA 的编号", settings=_settings(), gateway=gateway)
    )
    assert decision.primary_route == RouteKind.LOCAL_KNOWLEDGE
    assert decision.reason_code == ReasonCode.EXPLICIT_COMMAND
    assert decision.confidence == 0.95
    assert trace.is_rule is True
    assert gateway.calls == []
    assert decision.allowed_tools == derive_allowed_tools(RouteKind.LOCAL_KNOWLEDGE)


def test_explicit_search_route_with_config() -> None:
    settings = _settings(search_enabled=True, tavily_api_key="test-key")
    decision, trace = asyncio.run(
        route_request("搜索最新的公告", settings=settings, gateway=FakeGateway())
    )
    assert decision.primary_route == RouteKind.WEB_SEARCH
    assert decision.reason_code == ReasonCode.EXPLICIT_COMMAND
    assert trace.is_rule is True
    assert decision.allowed_tools == derive_allowed_tools(RouteKind.WEB_SEARCH)


def test_explicit_search_without_config_never_opens_web() -> None:
    decision, _ = asyncio.run(
        route_request("搜索最新的公告", settings=_settings(), gateway=FakeGateway())
    )
    assert decision.primary_route == RouteKind.DIRECT_CHAT
    assert decision.reason_code == ReasonCode.CAPABILITY_ERROR
    assert decision.needs_clarification is True
    assert decision.allowed_tools == ()


def test_explicit_memory_route_when_allowed() -> None:
    decision, trace = asyncio.run(
        route_request(
            "参考最近的消息，我上次问了什么？",
            settings=_settings(),
            gateway=FakeGateway(),
            can_use_chat_memory=True,
        )
    )
    assert decision.primary_route == RouteKind.CHAT_MEMORY
    assert trace.is_rule is True
    assert decision.allowed_tools == ("search_chat_memory",)


def test_explicit_memory_denied_without_scope() -> None:
    decision, _ = asyncio.run(
        route_request(
            "参考最近的消息，我上次问了什么？",
            settings=_settings(),
            gateway=FakeGateway(),
            can_use_chat_memory=False,
        )
    )
    assert decision.primary_route == RouteKind.CHAT_MEMORY
    assert decision.reason_code == ReasonCode.CAPABILITY_ERROR
    assert decision.needs_clarification is True
    assert decision.allowed_tools == ()


def test_destructive_memory_request_never_opens_memory() -> None:
    decision, _ = asyncio.run(
        route_request(
            "帮我删除群里的聊天记录",
            settings=_settings(),
            gateway=FakeGateway(),
            can_use_chat_memory=True,
        )
    )
    assert decision.primary_route != RouteKind.CHAT_MEMORY
    assert decision.allowed_tools == ()


def test_small_talk_routes_to_direct_chat() -> None:
    for greeting in ("在吗", "你好呀", "谢谢！", "晚安", "哈哈"):
        decision, trace = asyncio.run(
            route_request(greeting, settings=_settings(), gateway=FakeGateway())
        )
        assert decision.primary_route == RouteKind.DIRECT_CHAT
        assert trace.is_rule is True
        assert decision.allowed_tools == ()


def test_classifier_high_confidence_executes() -> None:
    gateway = FakeGateway(response=_json_response("local_knowledge", 0.9))
    decision, trace = asyncio.run(
        route_request("TestPetA 的编号是多少？", settings=_settings(), gateway=gateway)
    )
    assert decision.primary_route == RouteKind.LOCAL_KNOWLEDGE
    assert decision.reason_code == ReasonCode.STRUCTURED_CLASSIFIER
    assert trace.is_rule is False
    assert decision.needs_clarification is False
    assert decision.allowed_tools == derive_allowed_tools(RouteKind.LOCAL_KNOWLEDGE)


def test_classifier_low_confidence_web_needs_clarification() -> None:
    gateway = FakeGateway(response=_json_response("web_search", 0.6))
    decision, _ = asyncio.run(
        route_request("外面天气怎么样", settings=_settings(), gateway=gateway)
    )
    assert decision.primary_route == RouteKind.WEB_SEARCH
    assert decision.needs_clarification is True
    # 0.6 >= 0.5 but below threshold: no external side effect without asking


def test_classifier_low_confidence_local_still_executes() -> None:
    gateway = FakeGateway(response=_json_response("local_knowledge", 0.6))
    decision, _ = asyncio.run(route_request("精灵资料", settings=_settings(), gateway=gateway))
    assert decision.primary_route == RouteKind.LOCAL_KNOWLEDGE
    assert decision.needs_clarification is False


def test_classifier_below_half_clarifies_or_small_talk() -> None:
    gateway = FakeGateway(response=_json_response("web_search", 0.3))
    decision, _ = asyncio.run(route_request("随便问问", settings=_settings(), gateway=gateway))
    assert decision.reason_code == ReasonCode.CLARIFY
    assert decision.needs_clarification is True
    assert decision.allowed_tools == ()
    # obvious small talk never reaches the classifier: the rule layer sends
    # it straight to direct_chat before any model call
    small_talk_gateway = FakeGateway(error=RuntimeError("must not be called"))
    decision2, trace2 = asyncio.run(
        route_request("在吗", settings=_settings(), gateway=small_talk_gateway)
    )
    assert decision2.primary_route == RouteKind.DIRECT_CHAT
    assert decision2.reason_code == ReasonCode.EXPLICIT_COMMAND
    assert trace2.is_rule is True


def test_classifier_invalid_json_falls_back_safely() -> None:
    gateway = FakeGateway(response=NormalizedResponse(text="不是JSON", finish_reason="stop"))
    decision, trace = asyncio.run(route_request("查个精灵", settings=_settings(), gateway=gateway))
    assert decision.reason_code == ReasonCode.RULE_FALLBACK
    assert trace.is_rule is False
    assert decision.primary_route == RouteKind.DIRECT_CHAT
    assert decision.needs_clarification is True
    assert decision.allowed_tools == ()


def test_classifier_gateway_error_falls_back() -> None:
    gateway = FakeGateway(error=RuntimeError("provider down"))
    decision, _ = asyncio.run(route_request("查个精灵", settings=_settings(), gateway=gateway))
    assert decision.reason_code == ReasonCode.RULE_FALLBACK
    assert decision.primary_route == RouteKind.DIRECT_CHAT


def test_classifier_invalid_confidence_value_falls_back() -> None:
    gateway = FakeGateway(response=_json_response("web_search", 1.5))
    decision, _ = asyncio.run(route_request("查个精灵", settings=_settings(), gateway=gateway))
    assert decision.reason_code == ReasonCode.RULE_FALLBACK


def test_allowed_tools_are_server_derived_not_model_supplied() -> None:
    # the model only outputs route+confidence; the decision's tools always
    # come from the static policy map
    gateway = FakeGateway(response=_json_response("direct_chat", 0.99))
    decision, _ = asyncio.run(route_request("你好", settings=_settings(), gateway=gateway))
    assert decision.allowed_tools == ()


def test_trace_has_no_sensitive_fields() -> None:
    gateway = FakeGateway(response=_json_response("local_knowledge", 0.9))
    _, trace = asyncio.run(
        route_request("TestPetA 的编号是多少？", settings=_settings(), gateway=gateway)
    )
    assert isinstance(trace, RouteTrace)
    assert trace.latency_ms >= 0
    serialized = str(trace)
    assert "TestPetA" not in serialized
    assert "group" not in serialized and "user" not in serialized


def test_route_kinds_are_strict() -> None:
    assert derive_allowed_tools(RouteKind.LOCAL_KNOWLEDGE) == (
        "lookup_pet",
        "find_skill_intersection",
        "get_evolution_routes",
    )
    assert derive_allowed_tools(RouteKind.WEB_SEARCH) == (
        "search_web",
        "lookup_pet",
        "get_evolution_routes",
    )
    assert derive_allowed_tools(RouteKind.DIRECT_CHAT) == ()
