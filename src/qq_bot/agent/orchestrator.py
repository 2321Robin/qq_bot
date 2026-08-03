"""Agent orchestrator (S2-AGENT-03..08, S2-TOOL-10, S2-EVID-07).

One request loop with hard limits (rounds, calls, per-round calls, total
deadline, token budget seam), sequential tool execution with server-side
scope injection, a per-request call cache, and mandatory verification of
the final draft. All failure paths return a ``SafeFailure`` with a stable
code; diagnostics never contain draft text or evidence content
(S2-EVID-09, S2-AGENT-08).

Scope policy (S2-AGENT-05): a tool failure or not_found result never
expands the allowlist; switching routes follows the original
``RouteDecision`` only (the router never emits a secondary route in stage
2, so the orchestrator never switches). A route flagged
``needs_clarification`` runs without tools (Scenario E: clarify or safe
direct chat — never external search).
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable, Protocol

from qq_bot.agent.evidence import (
    EvidenceStore,
    SemanticVerifier,
    verify_and_repair,
)
from qq_bot.agent.guardrails import UNTRUSTED_CONTENT_POLICY
from qq_bot.agent.models import (
    AgentRequest,
    Evidence,
    FailureCode,
    GroundedAnswer,
    NormalizedResponse,
    RouteKind,
    SafeFailure,
    ToolResult,
    tool_arguments_json,
)
from qq_bot.agent.registry import ToolContext, ToolRegistry
from qq_bot.config import BotSettings
from qq_bot.observability import metrics
from qq_bot.observability.logging import current_request_id
from qq_bot.observability.tracing import get_tracer
from qq_bot.services.layered_memory import LayeredMemoryService

logger = logging.getLogger("qq_bot.agent.orchestrator")

ORCHESTRATOR_SYSTEM_PROMPT = (
    "你是洛克王国问答助手。根据提供的工具结果回答用户问题。"
    "最终必须输出严格 JSON："
    '{"claims": [{"text": "一句话事实", "kind": "factual|conversational", '
    '"evidence_ids": ["L1"]}], "closing": "可选收尾"}。'
    "factual 声明必须引用本次请求中真实存在的证据 ID（本地 L、网页 W、记忆 M 前缀）；"
    "寒暄、闲聊等无证据内容使用 kind=conversational 且不填 evidence_ids。"
    "不得编造证据 ID、URL 或数据；资料不足时如实说明找不到。不要输出任何其他字段。"
)

REPAIR_SYSTEM_PROMPT = (
    "你正在修复一条不符合验证规则的答案。规则：每条 factual 声明必须引用"
    "真实存在的证据 ID；无证据的寒暄必须用 kind=conversational。"
    "输出修正后的严格 JSON（结构同上），不得编造证据 ID 或 URL。"
)

_FAILURE_MESSAGES: dict[FailureCode, str] = {
    FailureCode.ROUND_LIMIT: "本轮处理次数已达上限，请换一种问法重试。",
    FailureCode.CALL_LIMIT: "本次查询步骤过多，请缩小问题范围后重试。",
    FailureCode.DEADLINE_EXCEEDED: "处理超时，请稍后重试。",
    FailureCode.BUDGET_INSUFFICIENT: "内容过长，请精简问题后重试。",
    FailureCode.VERIFICATION_FAILED: "暂时无法确认信息，请稍后再试。",
    FailureCode.TOOL_DENIED: "该操作不在允许范围内，已终止本次查询。",
    FailureCode.INTERNAL_ERROR: "内部错误，请稍后再试。",
}


class ModelGateway(Protocol):
    """Structured gateway surface (Task 7)."""

    async def request_model_turn(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        tool_choice: str | None,
        response_format: dict[str, Any] | None,
        settings: BotSettings,
        client: Any | None = None,
        provider: str = "primary",
    ) -> NormalizedResponse: ...


class BudgetManager(Protocol):
    """Token budget seam (S2-TOKEN-01..08; implemented in Task 10).

    The orchestrator consults ``allocate`` before each model turn; a plan
    flagged ``insufficient`` becomes a budget_insufficient safe failure and
    the model is never called.
    """

    def allocate(
        self,
        *,
        system: str,
        question: str,
        tool_schemas: list[dict[str, Any]],
        local_evidence: list[Evidence],
        web_evidence: list[Evidence],
        recent_messages: list[str],
        summaries: list[str],
        preferences: str | None,
    ) -> Any: ...


def _parse_answer(text: str | None) -> GroundedAnswer | None:
    """Parse the model's final JSON into a GroundedAnswer (S2-EVID-03).
    Markdown fences are tolerated; anything else is an explicit failure."""
    if not text or not text.strip():
        return None
    content = text.strip()
    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    try:
        payload = json.loads(content)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return GroundedAnswer.model_validate(payload)
    except Exception:
        return None


def _safe_failure(code: FailureCode) -> SafeFailure:
    return SafeFailure(code=code, message=_FAILURE_MESSAGES[code])


class AgentOrchestrator:
    """One request → one verified answer or one safe failure."""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        gateway: ModelGateway,
        settings: BotSettings,
        verifier: SemanticVerifier | None = None,
        budget: BudgetManager | None = None,
        memory: LayeredMemoryService | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._registry = registry
        self._gateway = gateway
        self._settings = settings
        self._verifier = verifier
        self._budget = budget
        self._memory = memory
        self._clock = clock
        self._call_log: list[dict[str, Any]] = []
        self._expires_at: float = 0.0
        self._last_store: EvidenceStore | None = None

    @property
    def call_log(self) -> list[dict[str, Any]]:
        """Diagnostic call record: tool/status/cached/evidence ids only —
        never arguments or result content (S2-AGENT-08)."""
        return list(self._call_log)

    @property
    def last_store(self) -> EvidenceStore | None:
        """The most recent request's evidence store, for answer rendering
        (visible URLs come from verified evidence only — S2-EVID-05)."""
        return self._last_store

    def _deadline_exceeded(self) -> bool:
        return self._clock() >= self._expires_at

    def _build_tool_schemas(self, allowlist: tuple[str, ...]) -> list[dict[str, Any]]:
        schemas: list[dict[str, Any]] = []
        for name in allowlist:
            spec = self._registry.get(name)
            if spec is None:
                continue
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": spec.to_json_schema(),
                    },
                }
            )
        return schemas

    def _check_budget(
        self,
        request: AgentRequest,
        store: EvidenceStore,
        tool_schemas: list[dict[str, Any]],
        *,
        recent_messages: list[str] = (),
        summaries: list[str] = (),
        preferences: str | None = None,
    ) -> SafeFailure | None:
        if self._budget is None:
            return None
        all_evidence = store.evidence()
        try:
            plan = self._budget.allocate(
                system=ORCHESTRATOR_SYSTEM_PROMPT,
                question=request.prompt,
                tool_schemas=tool_schemas,
                local_evidence=[e for e in all_evidence if e.source_type == "local"],
                web_evidence=[e for e in all_evidence if e.source_type == "web"],
                recent_messages=list(recent_messages),
                summaries=list(summaries),
                preferences=preferences,
            )
        except Exception:
            return _safe_failure(FailureCode.BUDGET_INSUFFICIENT)
        if isinstance(plan, dict):
            insufficient = bool(plan.get("insufficient", False))
        else:
            insufficient = bool(getattr(plan, "insufficient", False))
        if insufficient:
            return _safe_failure(FailureCode.BUDGET_INSUFFICIENT)
        return None

    async def _repair_once(
        self,
        answer: GroundedAnswer,
        store: EvidenceStore,
        request: AgentRequest,
        messages: list[dict[str, Any]],
    ) -> GroundedAnswer:
        if self._deadline_exceeded():
            return GroundedAnswer(claims=())
        try:
            response = await self._gateway.request_model_turn(
                messages=[
                    {"role": "system", "content": REPAIR_SYSTEM_PROMPT},
                    *messages,
                    {
                        "role": "user",
                        "content": json.dumps(answer.model_dump(), ensure_ascii=False),
                    },
                ],
                tools=None,
                tool_choice=None,
                response_format=None,
                settings=self._settings,
            )
        except Exception:
            return GroundedAnswer(claims=())
        repaired = _parse_answer(response.text)
        return repaired if repaired is not None else GroundedAnswer(claims=())

    async def _load_memory_layers(
        self, request: AgentRequest
    ) -> tuple[list[str], list[str], str | None, str]:
        """Fetch the request-scoped memory layers and build the prompt block
        (S2-MEM-09: layers only leave the box for scopes that can use chat
        memory, and only on routes that allow the memory tool)."""
        empty: tuple[list[str], list[str], str | None, str] = ([], [], None, "")
        if (
            self._memory is None
            or not request.scope.can_use_chat_memory
            or request.scope.group_id is None
            or request.route.primary_route != RouteKind.CHAT_MEMORY
        ):
            return empty
        try:
            group_id = int(request.scope.group_id)
            user_id = int(request.scope.user_id)
            limit = min(
                self._settings.chat_memory_default_turns,
                self._settings.chat_memory_max_results,
            )
            rows = await self._memory.recent_layer(group_id=group_id, limit=limit)
            summaries = await self._memory.summary_layer(group_id=group_id)
            preferences, _ = await self._memory.preference_layer(group_id=group_id, user_id=user_id)
        except Exception:
            logger.exception("memory layers failed; continuing without memory context")
            return empty
        recent_texts = [row.message_text for row in rows]
        summary_texts = [item.summary for item in summaries]
        preference_text = "；".join(preferences) if preferences else None
        blocks: list[str] = []
        if recent_texts:
            blocks.append("近期消息：\n" + "\n".join(f"- {text}" for text in recent_texts))
        if summary_texts:
            blocks.append("历史摘要：\n" + "\n".join(f"- {text}" for text in summary_texts))
        if preference_text:
            blocks.append(f"用户长期偏好：{preference_text}")
        block = "\n\n".join(blocks)
        return recent_texts, summary_texts, preference_text, block

    async def run(self, request: AgentRequest) -> GroundedAnswer | SafeFailure:
        """Run the agent loop and record the outcome (S4-METRIC-08).

        Pure additive observation: the recorded label never changes control
        flow, and the loop body is untouched. The ``agent.loop`` span is a
        sibling observation (S4-TRACE-01); failure status uses the stable
        failure code, never exception details.
        """
        tracer = get_tracer()
        span = tracer.start_span("agent.loop", trace_id=current_request_id())
        outcome = await self._run_agent_loop(request)
        code = outcome.code.value if isinstance(outcome, SafeFailure) else "ok"
        metrics.AGENT_OUTCOMES.labels(code).inc()
        if isinstance(outcome, SafeFailure):
            tracer.end_span(span, status="error", category=code)
        else:
            tracer.end_span(span)
        return outcome

    async def _run_agent_loop(self, request: AgentRequest) -> GroundedAnswer | SafeFailure:
        """Run the agent loop. Every limit check happens before the step it
        guards; failures never recurse and never leak draft content."""
        self._call_log = []
        self._last_store = None
        # Convert the wall-clock deadline to the injected clock's scale so
        # tests can advance a fake clock deterministically.
        wall_now = time.time()
        self._expires_at = self._clock() + (request.deadline.timestamp() - wall_now)
        if self._deadline_exceeded():
            return _safe_failure(FailureCode.DEADLINE_EXCEEDED)

        # S2-AGENT-05 + Scenario E: clarification decisions never run tools.
        allowlist = () if request.route.needs_clarification else request.route.allowed_tools
        tools = self._build_tool_schemas(allowlist)
        store = EvidenceStore()
        self._last_store = store
        system_prompt = ORCHESTRATOR_SYSTEM_PROMPT
        if any(
            (spec := self._registry.get(name)) is not None and spec.contains_untrusted
            for name in allowlist
        ):
            # S2-SEC-04: external content cannot change policy, tools, scope,
            # budget or verifier behavior.
            system_prompt = f"{system_prompt}\n{UNTRUSTED_CONTENT_POLICY}"
        recent_texts, summary_texts, preference_text, memory_block = await self._load_memory_layers(
            request
        )
        user_content = request.prompt if not memory_block else f"{request.prompt}\n\n{memory_block}"
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        rounds_used = 0
        calls_used = 0
        cache: dict[tuple[str, str], ToolResult] = {}

        while True:
            if self._deadline_exceeded():
                return _safe_failure(FailureCode.DEADLINE_EXCEEDED)
            if rounds_used >= self._settings.agent_max_rounds:
                return _safe_failure(FailureCode.ROUND_LIMIT)
            if calls_used >= self._settings.agent_max_tool_calls:
                return _safe_failure(FailureCode.CALL_LIMIT)
            budget_failure = self._check_budget(
                request,
                store,
                tools,
                recent_messages=recent_texts,
                summaries=summary_texts,
                preferences=preference_text,
            )
            if budget_failure is not None:
                return budget_failure

            try:
                response = await self._gateway.request_model_turn(
                    messages=messages,
                    tools=tools or None,
                    tool_choice="auto" if tools else None,
                    response_format=None,
                    settings=self._settings,
                )
            except Exception:
                return _safe_failure(FailureCode.INTERNAL_ERROR)
            rounds_used += 1

            if not response.tool_calls:
                draft = _parse_answer(response.text)
                if draft is None:
                    return _safe_failure(FailureCode.VERIFICATION_FAILED)
                if self._deadline_exceeded():
                    return _safe_failure(FailureCode.DEADLINE_EXCEEDED)
                verified, _ = await verify_and_repair(
                    draft,
                    store,
                    self._verifier,
                    repair=lambda answer, s: self._repair_once(answer, s, request, messages),
                )
                if not verified.claims:
                    return _safe_failure(FailureCode.VERIFICATION_FAILED)
                return verified

            if len(response.tool_calls) > self._settings.agent_tools_per_round:
                return _safe_failure(FailureCode.CALL_LIMIT)
            if calls_used + len(response.tool_calls) > self._settings.agent_max_tool_calls:
                return _safe_failure(FailureCode.CALL_LIMIT)

            executed: list[tuple[Any, ToolResult]] = []
            for call in response.tool_calls:
                if self._deadline_exceeded():
                    return _safe_failure(FailureCode.DEADLINE_EXCEEDED)
                if call.name not in allowlist or self._registry.get(call.name) is None:
                    return _safe_failure(FailureCode.TOOL_DENIED)
                spec = self._registry.get(call.name)
                assert spec is not None
                key = (call.name, tool_arguments_json(call))
                try:
                    if key in cache:
                        result = cache[key]
                        cached = True
                    else:
                        result = await spec.execute(
                            call.arguments,
                            ToolContext(scope=request.scope, evidence_index=calls_used),
                        )
                        cache[key] = result
                        calls_used += 1
                        cached = False
                        store.add(result)
                except Exception:
                    return _safe_failure(FailureCode.INTERNAL_ERROR)
                self._call_log.append(
                    {
                        "round": rounds_used,
                        "tool": call.name,
                        "status": result.status,
                        "cached": cached,
                        "evidence_ids": [e.id for e in result.evidence],
                    }
                )
                executed.append((call, result))

            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": tool_arguments_json(call),
                            },
                        }
                        for call, _ in executed
                    ],
                }
            )
            for call, result in executed:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result.model_dump(), ensure_ascii=False),
                    }
                )
