"""Structured router (S2-ROUTE-01..08).

Deterministic rule layer first (explicit syntax never waits for a model),
then a structured classifier over strict JSON. ``allowed_tools`` is always
derived server-side from the route — model output never supplies tool names.
All failure paths fall back to safe rules: never auto-web, never expand
memory scope.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

from qq_bot.agent.models import (
    NormalizedResponse,
    ReasonCode,
    RouteDecision,
    RouteKind,
)
from qq_bot.config import BotSettings

ROUTE_TOOLS: dict[RouteKind, tuple[str, ...]] = {
    RouteKind.LOCAL_KNOWLEDGE: (
        "lookup_pet",
        "find_skill_intersection",
        "get_evolution_routes",
    ),
    RouteKind.WEB_SEARCH: ("search_web", "lookup_pet", "get_evolution_routes"),
    RouteKind.CHAT_MEMORY: ("search_chat_memory",),
    RouteKind.DIRECT_CHAT: (),
}

ROUTER_SYSTEM_PROMPT = (
    "你是路由分类器。根据用户请求输出严格 JSON："
    '{"primary_route": "local_knowledge|web_search|chat_memory|direct_chat", '
    '"confidence": 0.0到1.0的小数}。'
    "local_knowledge=本地洛克王国图鉴查询；web_search=需要最新/联网信息；"
    "chat_memory=需要参考最近聊天记录；direct_chat=寒暄、闲聊或不应执行的内容。"
    "不要输出任何其他字段。"
)


class ModelGateway(Protocol):
    """Minimal gateway surface used by the router (full form: Task 7)."""

    async def request_model_turn(
        self,
        *,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None,
        tool_choice: str | None,
        response_format: dict[str, Any] | None,
        settings: BotSettings,
        client: Any | None = None,
        provider: str = "primary",
    ) -> NormalizedResponse: ...


@dataclass(frozen=True)
class RouteTrace:
    route: RouteKind
    confidence: float
    reason_code: ReasonCode
    is_rule: bool
    latency_ms: float
    needs_clarification: bool = False


def derive_allowed_tools(route: RouteKind) -> tuple[str, ...]:
    """Server-side tool allowlist per route (S2-ROUTE-03)."""
    return ROUTE_TOOLS[route]


def _is_small_talk(text: str) -> bool:
    return bool(
        re.fullmatch(
            r"(在吗|在不在|你好|你好呀|哈喽|hello|hi|嗨|谢谢|多谢|感谢|"
            r"晚安|再见|拜拜|辛苦了|哈哈|嗯嗯|在的)[!！。.~～]*",
            text,
            flags=re.IGNORECASE,
        )
    )


def _explicit_search(text: str) -> bool:
    markers = (
        "搜索",
        "联网",
        "搜一下",
        "网上查",
        "查一下网上",
        "最新",
        "公告",
        "活动",
        "更新内容",
        "版本更新",
        "今天有什么新",
    )
    return any(marker in text for marker in markers)


def _explicit_memory(text: str) -> bool:
    if any(verb in text for verb in ("删除", "清空", "泄露", "发给我", "越权")):
        # destructive or leak requests never open memory scope
        return False
    markers = (
        "参考最近",
        "最近的消息",
        "最近消息",
        "聊天记录",
        "刚才聊",
        "我们聊过",
        "提到过",
        "刚才说",
    )
    return any(marker in text for marker in markers)


def _explicit_command(text: str) -> bool:
    return bool(re.match(r"^/(精灵|技能|进化)\b", text)) or text.startswith(
        ("/精灵", "/技能", "/进化")
    )


def _rule_route(
    prompt: str, *, settings: BotSettings, can_use_chat_memory: bool
) -> RouteDecision | None:
    """High-precision deterministic rules; returns None when the model
    classifier must decide."""
    text = prompt.strip()
    if not text:
        return RouteDecision(
            primary_route=RouteKind.DIRECT_CHAT,
            confidence=0.9,
            reason_code=ReasonCode.CLARIFY,
            needs_clarification=True,
            allowed_tools=(),
        )
    if _explicit_command(text):
        return RouteDecision(
            primary_route=RouteKind.LOCAL_KNOWLEDGE,
            confidence=0.95,
            reason_code=ReasonCode.EXPLICIT_COMMAND,
            allowed_tools=derive_allowed_tools(RouteKind.LOCAL_KNOWLEDGE),
        )
    if _explicit_search(text):
        if settings.has_search_config():
            return RouteDecision(
                primary_route=RouteKind.WEB_SEARCH,
                confidence=0.95,
                reason_code=ReasonCode.EXPLICIT_COMMAND,
                allowed_tools=derive_allowed_tools(RouteKind.WEB_SEARCH),
            )
        return RouteDecision(
            primary_route=RouteKind.DIRECT_CHAT,
            confidence=0.8,
            reason_code=ReasonCode.CAPABILITY_ERROR,
            needs_clarification=True,
            allowed_tools=(),
        )
    if _explicit_memory(text):
        if can_use_chat_memory:
            return RouteDecision(
                primary_route=RouteKind.CHAT_MEMORY,
                confidence=0.95,
                reason_code=ReasonCode.EXPLICIT_COMMAND,
                allowed_tools=derive_allowed_tools(RouteKind.CHAT_MEMORY),
            )
        return RouteDecision(
            primary_route=RouteKind.CHAT_MEMORY,
            confidence=0.9,
            reason_code=ReasonCode.CAPABILITY_ERROR,
            needs_clarification=True,
            allowed_tools=(),
        )
    if _is_small_talk(text):
        return RouteDecision(
            primary_route=RouteKind.DIRECT_CHAT,
            confidence=0.9,
            reason_code=ReasonCode.EXPLICIT_COMMAND,
            allowed_tools=(),
        )
    return None


def _parse_classifier_output(content: str) -> tuple[RouteKind, float] | None:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        route = RouteKind(payload.get("primary_route", ""))
        confidence = float(payload.get("confidence", -1.0))
    except (ValueError, TypeError):
        return None
    if not (0.0 <= confidence <= 1.0):
        return None
    return route, confidence


def _apply_confidence_policy(
    route: RouteKind,
    confidence: float,
    *,
    settings: BotSettings,
    text: str,
) -> RouteDecision:
    threshold = settings.ai_router_confidence_threshold
    if confidence >= threshold:
        return RouteDecision(
            primary_route=route,
            confidence=confidence,
            reason_code=ReasonCode.STRUCTURED_CLASSIFIER,
            allowed_tools=derive_allowed_tools(route),
        )
    if confidence >= 0.5:
        if route in (RouteKind.LOCAL_KNOWLEDGE, RouteKind.DIRECT_CHAT):
            return RouteDecision(
                primary_route=route,
                confidence=confidence,
                reason_code=ReasonCode.STRUCTURED_CLASSIFIER,
                allowed_tools=derive_allowed_tools(route),
            )
        return RouteDecision(
            primary_route=route,
            confidence=confidence,
            reason_code=ReasonCode.STRUCTURED_CLASSIFIER,
            needs_clarification=True,
            allowed_tools=derive_allowed_tools(route),
        )
    if _is_small_talk(text):
        return RouteDecision(
            primary_route=RouteKind.DIRECT_CHAT,
            confidence=confidence,
            reason_code=ReasonCode.LOW_CONFIDENCE,
            allowed_tools=(),
        )
    return RouteDecision(
        primary_route=RouteKind.DIRECT_CHAT,
        confidence=confidence,
        reason_code=ReasonCode.CLARIFY,
        needs_clarification=True,
        allowed_tools=(),
    )


def _fallback_decision(text: str) -> RouteDecision:
    """Router unavailable/invalid output: rules already ran; remaining
    requests go to direct chat or clarification, never to web/memory."""
    if _is_small_talk(text):
        return RouteDecision(
            primary_route=RouteKind.DIRECT_CHAT,
            confidence=0.5,
            reason_code=ReasonCode.RULE_FALLBACK,
            allowed_tools=(),
        )
    return RouteDecision(
        primary_route=RouteKind.DIRECT_CHAT,
        confidence=0.5,
        reason_code=ReasonCode.RULE_FALLBACK,
        needs_clarification=True,
        allowed_tools=(),
    )


async def route_request(
    prompt: str,
    *,
    settings: BotSettings,
    gateway: ModelGateway | None = None,
    can_use_chat_memory: bool = False,
) -> tuple[RouteDecision, RouteTrace]:
    """Route one request. Returns the decision plus a privacy-safe trace
    (no user message, no group/user ids — S2-ROUTE-08)."""
    started = time.perf_counter()
    text = prompt.strip()

    rule_decision = _rule_route(text, settings=settings, can_use_chat_memory=can_use_chat_memory)
    if rule_decision is not None:
        return rule_decision, RouteTrace(
            route=rule_decision.primary_route,
            confidence=rule_decision.confidence,
            reason_code=rule_decision.reason_code,
            is_rule=True,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            needs_clarification=rule_decision.needs_clarification,
        )

    decision = _fallback_decision(text)
    if gateway is not None:
        try:
            response = await gateway.request_model_turn(
                messages=[
                    {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
                tools=None,
                tool_choice=None,
                response_format={"type": "json_object"},
                settings=settings,
            )
            parsed = None
            if response.text is not None and not response.tool_calls:
                parsed = _parse_classifier_output(response.text)
            if parsed is not None:
                route, confidence = parsed
                decision = _apply_confidence_policy(route, confidence, settings=settings, text=text)
        except Exception:
            decision = _fallback_decision(text)

    return decision, RouteTrace(
        route=decision.primary_route,
        confidence=decision.confidence,
        reason_code=decision.reason_code,
        is_rule=False,
        latency_ms=(time.perf_counter() - started) * 1000.0,
        needs_clarification=decision.needs_clarification,
    )
