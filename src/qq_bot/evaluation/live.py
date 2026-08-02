"""Live provider benchmark (S2-EVAL-07/12/14/15, S2-GATE-03).

Runs the frozen ``test`` split through both pipelines with a real provider
and writes ONE comparison report:

- ``legacy_context`` — stage-1 keyword + prompt assembly (local Roco context
  only; no chat memory, no Tavily — the eval never touches the production
  chat database), calling :func:`request_ai_reply` directly.
- ``tool_agent`` — the stage-2 structured pipeline: router -> orchestrator
  (tools, budget, verification) -> renderer.

Hard refusals (exit 2): live mode is off unless ``AGENT_EVAL_LIVE=1`` and a
real provider is configured. The frozen manifest gate from the offline
runner applies unchanged. Reports never contain API keys, full private
prompts, raw chat or provider headers; refusal detection is a documented
marker heuristic that human review (>= 20 boundary/failure cases) refines —
expecteds are never edited to fit model output (S2-EVAL-15).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from pydantic import BaseModel, ConfigDict, Field

from qq_bot.agent.evidence import EvidenceStore, render_answer
from qq_bot.agent.models import AgentRequest, AgentScope, SafeFailure
from qq_bot.agent.orchestrator import AgentOrchestrator
from qq_bot.agent.registry import ToolRegistry
from qq_bot.agent.router import route_request
from qq_bot.agent.token_budget import BudgetManager
from qq_bot.agent.tools.roco import register_roco_tools
from qq_bot.agent.tools.web import register_web_tool
from qq_bot.config import BotSettings
from qq_bot.evaluation.metrics import (
    Observation,
    Usage,
    estimate_cost,
    latency_percentiles,
    metrics_dict,
    token_summary,
)
from qq_bot.evaluation.models import EvalCase, load_dataset
from qq_bot.evaluation.runner import OfflineRunner, RunConfig
from qq_bot.services.ai_client import AIReplyError, AiModelGateway, request_ai_reply
from qq_bot.services.roco_knowledge import build_roco_context

_EVIDENCE_TYPE_BY_PREFIX = {"L": "local", "W": "web", "M": "memory"}

_REFUSAL_MARKERS = (
    "帮不了",
    "不能帮你",
    "无法帮你",
    "拒绝回答",
    "不能回答",
    "无法提供",
)

_REFUSE_NO_LIVE = (
    "live mode is disabled: set AGENT_EVAL_LIVE=1 and configure a real "
    "provider (AI_API_KEY/AI_MODEL) before running. No results are "
    "fabricated without a provider."
)

_REFUSE_NO_PROVIDER = (
    "live mode requires a real provider configuration (AI_API_KEY/AI_MODEL); "
    "refusing to run without one."
)

_QUALITY_GATES: tuple[tuple[str, str, float], ...] = (
    ("tool_selection_exact_match", "ge", 0.90),
    ("fact_accuracy", "ge", 0.85),
    ("citation_provenance_rate", "eq", 1.0),
    ("refusal_recall", "ge", 0.90),
    ("fabrication_rate", "le", 0.05),
)

Executor = Callable[[EvalCase, int], Awaitable[Observation]]


class ModeResult(BaseModel):
    """One pipeline's results over the frozen split."""

    model_config = ConfigDict(extra="forbid")

    mode: str
    case_count: int
    metrics: dict[str, Any]
    token_summary: dict[str, Any]
    cost: dict[str, Any]
    latency: dict[str, Any]
    failures: list[dict[str, str]] = Field(default_factory=list)


class LiveRunReport(BaseModel):
    """Comparison report; structurally identical for every run so reports
    are diffable. Secrets and private content are rejected by construction —
    only case ids, categories and metric numbers are recorded."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    mode: str = "live"
    created_at: str
    code_revision: str
    dataset: str
    dataset_hash: str
    split: str
    case_count: int
    provider: dict[str, str]
    config: dict[str, Any]
    temperature: dict[str, float]
    quality_gates: list[dict[str, Any]]
    results: dict[str, ModeResult]
    route_observations: list[dict[str, Any]] = Field(default_factory=list)


class _UsageGateway(AiModelGateway):
    """Records usage of every model turn (router + orchestrator)."""

    def __init__(self, settings: BotSettings, *, client: httpx.AsyncClient) -> None:
        super().__init__(settings, client=client)
        self.prompt_tokens: list[int] = []
        self.completion_tokens: list[int] = []

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
    ):
        response = await super().request_model_turn(
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            response_format=response_format,
            settings=settings,
            client=client,
            provider=provider,
        )
        if response.usage:
            self.prompt_tokens.append(response.usage.get("prompt_tokens", 0))
            self.completion_tokens.append(response.usage.get("completion_tokens", 0))
        return response

    def usage(self, model_id: str) -> Usage:
        if not self.prompt_tokens:
            return Usage(model_id=model_id, estimated=True)
        return Usage(
            prompt_tokens=sum(self.prompt_tokens),
            completion_tokens=sum(self.completion_tokens),
            total_tokens=sum(self.prompt_tokens) + sum(self.completion_tokens),
            estimated=False,
            model_id=model_id,
        )


class LiveRunner(OfflineRunner):
    """Live benchmark (Task 15). Extends the offline runner so the frozen
    manifest gate and per-case checks apply unchanged."""

    def __init__(self, config: RunConfig) -> None:
        super().__init__(config)
        self._legacy_executor: Executor | None = config.legacy_executor
        self._agent_executor: Executor | None = config.agent_executor
        self._clock = config.clock

    def run(self) -> int:
        if os.environ.get("AGENT_EVAL_LIVE") != "1":
            print(_REFUSE_NO_LIVE, file=sys.stderr)
            return 2
        settings = BotSettings()
        if not settings.has_ai_config():
            print(_REFUSE_NO_PROVIDER, file=sys.stderr)
            return 2

        cases, manifest = load_dataset(self.config.dataset_path)
        gate_error = self._verify_frozen_manifest(manifest)
        if gate_error is not None:
            print(gate_error, file=sys.stderr)
            return 1
        split_cases = [case for case in cases if case.split == self.config.split]
        if not split_cases:
            print(
                f"live run FAILED: no cases with split={self.config.split}",
                file=sys.stderr,
            )
            return 1

        prices = self._load_prices()
        report = asyncio.run(self._run_all(split_cases, settings, prices, manifest))
        target = self.config.report_path or (
            Path(__file__).resolve().parents[3]
            / "evals"
            / "reports"
            / f"live-{self.config.split}.json"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        print(f"live run finished: {len(split_cases)} cases -> {target}")
        print(f"dataset_hash={report.dataset_hash}")
        for mode, result in report.results.items():
            print(
                f"{mode}: fact_accuracy={result.metrics['fact_accuracy']} "
                f"refusal_recall={result.metrics['refusal_recall']} "
                f"fabrication_rate={result.metrics['fabrication_rate']} "
                f"failures={len(result.failures)}"
            )
        unmet = [gate for gate in report.quality_gates if not gate["passed"]]
        if unmet:
            print(
                f"quality gates NOT met: {[gate['metric'] for gate in unmet]}",
                file=sys.stderr,
            )
            return 1
        print("quality gates met")
        return 0

    async def _run_all(
        self,
        split_cases: list[EvalCase],
        settings: BotSettings,
        prices: dict[str, Any],
        manifest: Any,
    ) -> LiveRunReport:
        results: dict[str, ModeResult] = {}
        route_observations: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=httpx.Timeout(settings.ai_timeout_seconds)) as client:
            legacy_executor = self._legacy_executor or _legacy_executor(client, settings)
            agent_executor = self._agent_executor or _agent_executor(client, settings)
            results["legacy_context"] = await self._run_mode(
                "legacy_context", split_cases, legacy_executor, prices
            )
            agent_result, agent_routes = await self._run_mode_agent(
                split_cases, agent_executor, prices
            )
            results["tool_agent"] = agent_result
            route_observations = agent_routes

        config: dict[str, Any] = {
            "router_confidence_threshold": settings.ai_router_confidence_threshold,
            "agent_max_rounds": settings.agent_max_rounds,
            "agent_max_tool_calls": settings.agent_max_tool_calls,
            "agent_tools_per_round": settings.agent_tools_per_round,
            "agent_deadline_seconds": settings.agent_deadline_seconds,
            "context_window_tokens": settings.ai_context_window_tokens,
            "output_reserve_tokens": settings.ai_output_reserve_tokens,
            "token_safety_margin": settings.ai_token_safety_margin,
            "semantic_verifier": settings.ai_semantic_verifier_enabled,
            "refusal_detection": "marker heuristic (human review refines)",
            "memory": "disabled (live eval never reads the production chat database)",
        }
        gates: list[dict[str, Any]] = []
        agent_metrics = results["tool_agent"].metrics
        for metric, op, threshold in _QUALITY_GATES:
            actual = agent_metrics.get(metric)
            if op == "ge":
                passed = actual is not None and actual >= threshold
            elif op == "le":
                passed = actual is not None and actual <= threshold
            else:
                passed = actual == threshold
            gates.append(
                {
                    "metric": metric,
                    "operator": op,
                    "threshold": threshold,
                    "actual": actual,
                    "passed": bool(passed),
                }
            )

        return LiveRunReport(
            created_at=self._clock().isoformat(),
            code_revision=self.config.code_revision or _git_revision(),
            dataset=str(self.config.dataset_path),
            dataset_hash=manifest.dataset_hash,
            split=self.config.split,
            case_count=len(split_cases),
            provider={
                "model": settings.ai_model,
                "fallback_model": settings.ai_fallback_model or "",
            },
            config=config,
            temperature={
                "legacy_context": 0.7,
                "tool_agent_structured": 0.2,
                "tool_agent_plain": 0.7,
            },
            quality_gates=gates,
            results=results,
            route_observations=route_observations,
        )

    async def _run_mode(
        self,
        mode: str,
        split_cases: list[EvalCase],
        executor: Executor,
        prices: dict[str, Any],
    ) -> ModeResult:
        observations: list[Observation] = []
        failures: list[dict[str, str]] = []
        for index, case in enumerate(split_cases):
            try:
                observation = await executor(case, index)
            except Exception as exc:  # provider/executor failures surface in the report
                failures.append(
                    {
                        "mode": mode,
                        "case": case.id,
                        "category": "executor",
                        "detail": str(exc)[:300],
                    }
                )
                continue
            observations.append(observation)
            failures.extend({**failure, "mode": mode} for failure in self._check(case, observation))
        return self._mode_result(mode, split_cases, observations, failures, prices)

    async def _run_mode_agent(
        self,
        split_cases: list[EvalCase],
        executor: Executor,
        prices: dict[str, Any],
    ) -> tuple[ModeResult, list[dict[str, Any]]]:
        observations: list[Observation] = []
        failures: list[dict[str, str]] = []
        routes: list[dict[str, Any]] = []
        for index, case in enumerate(split_cases):
            try:
                observation = await executor(case, index)
            except Exception as exc:
                failures.append(
                    {
                        "mode": "tool_agent",
                        "case": case.id,
                        "category": "executor",
                        "detail": str(exc)[:300],
                    }
                )
                continue
            observations.append(observation)
            routes.append(
                {
                    "case_id": observation.case_id,
                    "route": observation.route,
                    "confidence": observation.confidence,
                }
            )
            failures.extend(
                {**failure, "mode": "tool_agent"} for failure in self._check(case, observation)
            )
        return (
            self._mode_result("tool_agent", split_cases, observations, failures, prices),
            routes,
        )

    def _mode_result(
        self,
        mode: str,
        split_cases: list[EvalCase],
        observations: list[Observation],
        failures: list[dict[str, str]],
        prices: dict[str, Any],
    ) -> ModeResult:
        observed_ids = {observation.case_id for observation in observations}
        cases = [case for case in split_cases if case.id in observed_ids]
        usages = [observation.usage for observation in observations]
        metrics = metrics_dict(
            observations=observations,
            cases=cases,
            usages=usages,
            prices=prices,
        )
        if mode == "legacy_context":
            # route/tool metrics do not apply to the stage-1 pipeline; they
            # are omitted rather than reported as misleading zeros.
            for key in (
                "route_accuracy",
                "tool_selection_exact_match",
                "illegal_tool_call_rate",
            ):
                metrics.pop(key, None)
        percentiles = latency_percentiles(
            [observation.latency_seconds for observation in observations], (50.0, 95.0)
        )
        cost = estimate_cost([u for u in usages if u is not None], prices)
        summary = token_summary([u for u in usages if u is not None])
        return ModeResult(
            mode=mode,
            case_count=len(cases),
            metrics=metrics,
            token_summary={
                "prompt_tokens": summary.prompt_tokens,
                "completion_tokens": summary.completion_tokens,
                "total_tokens": summary.total_tokens,
                "estimated": summary.estimated,
                "cases_with_usage": summary.cases_with_usage,
                "total_cases": summary.total_cases,
            },
            cost={
                "cost": cost.cost,
                "currency": cost.currency,
                "status": cost.status,
            },
            latency={"p50": percentiles.get(50.0), "p95": percentiles.get(95.0)},
            failures=failures,
        )

    def _load_prices(self) -> dict[str, Any]:
        """Price table is optional; missing/unknown entries make cost
        ``estimated``/``unknown`` — never guessed numbers (S2-EVAL-12)."""
        prices_path = Path(__file__).resolve().parents[3] / "evals" / "pricing.json"
        if not prices_path.exists():
            return {}
        try:
            payload = json.loads(prices_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            print(f"warning: unreadable price table {prices_path}; cost=unknown", file=sys.stderr)
            return {}
        return payload.get("models", {}) if isinstance(payload, dict) else {}


def _git_revision() -> str:
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def _legacy_executor(client: httpx.AsyncClient, settings: BotSettings) -> Executor:
    """Stage-1 keyword + prompt-assembly chain, called directly (S2-EVAL-14).
    Local Roco context only — no chat memory, no Tavily, no production data."""

    async def execute(case: EvalCase, index: int) -> Observation:
        started = time.perf_counter()
        answer = ""
        try:
            roco_context = build_roco_context(case.prompt)
            answer = await request_ai_reply(
                case.prompt,
                settings=settings,
                client=client,
                roco_context=roco_context,
            )
        except AIReplyError as exc:
            answer = ""
            raise RuntimeError(f"legacy provider call failed: {exc}") from exc
        return Observation(
            case_id=case.id,
            route="legacy_context",
            confidence=0.0,
            answer=answer,
            refused=any(marker in answer for marker in _REFUSAL_MARKERS),
            usage=None,
            latency_seconds=time.perf_counter() - started,
        )

    return execute


def _agent_executor(client: httpx.AsyncClient, settings: BotSettings) -> Executor:
    """Stage-2 structured pipeline: router -> orchestrator -> renderer.
    The memory tool is intentionally not registered: live eval never reads
    the production chat database, so memory routes degrade to clarification
    instead of fabricating context (S2-GATE-03)."""

    registry = ToolRegistry()
    register_roco_tools(registry)
    register_web_tool(registry, settings=settings, client=client)
    registry.validate()
    gateway = _UsageGateway(settings, client=client)
    budget = BudgetManager(settings)
    orchestrator = AgentOrchestrator(
        registry=registry,
        gateway=gateway,
        settings=settings,
        budget=budget,
        memory=None,
    )

    async def execute(case: EvalCase, index: int) -> Observation:
        started = time.perf_counter()
        route, trace = await route_request(
            case.prompt,
            settings=settings,
            gateway=gateway,
            can_use_chat_memory=False,
        )
        from datetime import datetime, timedelta, timezone

        request = AgentRequest(
            prompt=case.prompt,
            scope=AgentScope(user_id="eval", can_use_chat_memory=False),
            route=route,
            deadline=datetime.now(timezone.utc)
            + timedelta(seconds=settings.agent_deadline_seconds),
        )
        outcome = await orchestrator.run(request)
        if isinstance(outcome, SafeFailure):
            answer = ""
            selected = ()
            evidence_ids: tuple[str, ...] = ()
            source_types: tuple[str, ...] = ()
        else:
            store = orchestrator.last_store or EvidenceStore()
            answer = render_answer(outcome, store)
            selected = tuple(
                entry["tool"] for entry in orchestrator.call_log if entry["status"] != "denied"
            )
            evidence_ids = tuple(
                evidence_id for claim in outcome.claims for evidence_id in claim.evidence_ids
            )
            source_types = tuple(
                _EVIDENCE_TYPE_BY_PREFIX[evidence_id[0]]
                for evidence_id in evidence_ids
                if evidence_id[:1] in _EVIDENCE_TYPE_BY_PREFIX
            )
        return Observation(
            case_id=case.id,
            route=route.primary_route.value,
            confidence=route.confidence,
            selected_tools=selected,
            answer=answer,
            evidence_ids=evidence_ids,
            evidence_source_types=source_types,
            refused=any(marker in answer for marker in _REFUSAL_MARKERS),
            usage=gateway.usage(settings.ai_model),
            latency_seconds=time.perf_counter() - started,
        )

    return execute
