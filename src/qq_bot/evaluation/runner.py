"""Offline/live evaluation runner (S2-EVAL-14..16).

OfflineRunner executes the dataset with a deterministic fixture executor: no
network, no API keys, CI-safe. It validates per case — schema, route decision,
tool selection, evidence references, refusal behaviour — and produces a unified
``EvalReport`` with dataset hash, code revision, metrics, and failure details.

Live mode (Task 15) reuses the same report shape with a real provider; it
refuses to run without an explicit local configuration.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from pydantic import BaseModel, ConfigDict, Field

from qq_bot.evaluation.metrics import (
    Observation,
    Usage,
    extract_urls,
    metrics_dict,
)
from qq_bot.evaluation.models import DatasetManifest, EvalCase, load_dataset

_EVIDENCE_PREFIX = {"local": "L", "web": "W", "memory": "M"}


class Executor(Protocol):
    """Runs one case; returns the observation the metrics are computed from."""

    def __call__(self, case: EvalCase, index: int) -> Observation: ...


class EvalReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    mode: str
    created_at: str
    code_revision: str
    dataset: str
    dataset_hash: str
    split: str
    case_count: int
    metrics: dict[str, Any]
    failures: list[dict[str, str]] = Field(default_factory=list)
    route_observations: list[dict[str, Any]] = Field(default_factory=list)


def _default_executor(case: EvalCase, index: int) -> Observation:
    """Deterministic fixture pipeline used offline (never a real model)."""
    route = case.expected_route.value
    if case.expected_refusal:
        answer = "抱歉，这个我帮不了你。"
        selected: tuple[str, ...] = ()
    elif route == "direct_chat":
        answer = "你好呀！"
        selected = ()
    elif route == "web_search":
        answer = "根据搜索结果，我找到了相关的最新信息。"
        selected = ("search_web",)
    elif route == "chat_memory":
        answer = "根据最近的聊天记录，我找到了相关信息。"
        selected = ("search_chat_memory",)
    else:
        required = case.required_facts
        if required:
            answer = "根据本地图鉴：" + "，".join(required) + "。"
        else:
            answer = "本地图鉴暂时没有收录这条信息。"
        selected = tuple(case.allowed_tools)

    counters: dict[str, int] = {}
    evidence_ids: list[str] = []
    for source in case.expected_evidence_types:
        prefix = _EVIDENCE_PREFIX[source]
        counters[prefix] = counters.get(prefix, 0) + 1
        evidence_ids.append(f"{prefix}{counters[prefix]}")

    return Observation(
        case_id=case.id,
        route=route,
        confidence=0.9,
        selected_tools=selected,
        answer=answer,
        evidence_ids=tuple(evidence_ids),
        evidence_source_types=tuple(case.expected_evidence_types),
        usage=Usage(
            prompt_tokens=80 + index,
            completion_tokens=40 + index,
            total_tokens=120 + 2 * index,
            model_id="fixture",
        ),
        refused=case.expected_refusal,
        latency_seconds=0.05 + 0.01 * (index % 7),
    )


def _git_revision() -> str:
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


@dataclass
class RunConfig:
    dataset_path: Path
    split: str = "test"
    report_path: Path | None = None
    code_revision: str | None = None
    executor: Callable[[EvalCase, int], Observation] | None = None
    legacy_executor: Callable[[EvalCase, int], Any] | None = None
    agent_executor: Callable[[EvalCase, int], Any] | None = None
    clock: Callable[[], datetime] = field(
        default_factory=lambda: lambda: datetime.now(timezone.utc)
    )


class OfflineRunner:
    def __init__(self, config: RunConfig) -> None:
        self.config = config
        self.executor = config.executor or _default_executor

    def run(self) -> int:
        cases, manifest = load_dataset(self.config.dataset_path)
        gate_error = self._verify_frozen_manifest(manifest)
        if gate_error is not None:
            print(gate_error, file=sys.stderr)
            return 1
        split_cases = [case for case in cases if case.split == self.config.split]
        if not split_cases:
            print(
                f"offline run FAILED: no cases with split={self.config.split}",
                file=sys.stderr,
            )
            return 1

        observations: list[Observation] = []
        failures: list[dict[str, str]] = []
        for index, case in enumerate(split_cases):
            try:
                observation = self.executor(case, index)
            except Exception as exc:  # executor bugs must surface in the report
                failures.append({"case": case.id, "category": "executor", "detail": str(exc)})
                continue
            observations.append(observation)
            failures.extend(self._check(case, observation))

        metrics = metrics_dict(
            observations=observations,
            cases=[case for case in split_cases if case.id in {o.case_id for o in observations}],
            usages=[o.usage for o in observations],
        )
        report = EvalReport(
            mode="offline",
            created_at=self.config.clock().isoformat(),
            code_revision=self.config.code_revision or _git_revision(),
            dataset=str(self.config.dataset_path),
            dataset_hash=manifest.dataset_hash,
            split=self.config.split,
            case_count=len(split_cases),
            metrics=metrics,
            failures=failures,
            route_observations=[
                {
                    "case_id": observation.case_id,
                    "route": observation.route,
                    "confidence": observation.confidence,
                }
                for observation in observations
            ],
        )

        target = self.config.report_path or (
            Path(__file__).resolve().parents[3]
            / "evals"
            / "reports"
            / f"offline-{self.config.split}.json"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(report.model_dump_json(indent=2), encoding="utf-8")

        print(f"offline run OK: {len(split_cases)} cases, {len(failures)} failures")
        print(f"report -> {target}")
        print(f"dataset_hash={report.dataset_hash}")
        print(f"route_accuracy={metrics['route_accuracy']}")
        print(f"tool_selection_exact_match={metrics['tool_selection_exact_match']}")
        print(f"fact_accuracy={metrics['fact_accuracy']}")
        print(f"citation_provenance_rate={metrics['citation_provenance_rate']}")
        print(f"refusal_recall={metrics['refusal_recall']}")
        return 0 if not failures else 1

    def _verify_frozen_manifest(self, manifest: DatasetManifest) -> str | None:
        """The frozen ``<dataset>.manifest.json`` pins the dataset hash
        (S2-GATE-01): the offline gate refuses to run when the dataset was
        edited without regenerating the manifest."""
        frozen_path = self.config.dataset_path.with_suffix(".manifest.json")
        if not frozen_path.exists():
            return f"offline run FAILED: frozen manifest {frozen_path} is missing"
        try:
            frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return f"offline run FAILED: frozen manifest unreadable: {exc}"
        frozen_hash = frozen.get("dataset_hash") if isinstance(frozen, dict) else None
        if frozen_hash != manifest.dataset_hash:
            return (
                f"offline run FAILED: dataset_hash {manifest.dataset_hash} does not "
                f"match frozen manifest {frozen_path} ({frozen_hash})"
            )
        return None

    def _check(self, case: EvalCase, observation: Observation) -> list[dict[str, str]]:
        failures: list[dict[str, str]] = []
        if observation.route != case.expected_route.value:
            failures.append(
                {
                    "case": case.id,
                    "category": "route",
                    "detail": f"expected {case.expected_route.value}, got {observation.route}",
                }
            )
        if set(observation.selected_tools) != set(case.allowed_tools):
            failures.append(
                {
                    "case": case.id,
                    "category": "tool_selection",
                    "detail": (
                        f"expected {sorted(case.allowed_tools)}, "
                        f"got {sorted(observation.selected_tools)}"
                    ),
                }
            )
        if observation.refused != case.expected_refusal:
            failures.append(
                {
                    "case": case.id,
                    "category": "refusal",
                    "detail": f"expected_refusal={case.expected_refusal}, refused={observation.refused}",
                }
            )
        expected_prefixes = {_EVIDENCE_PREFIX[t] for t in case.expected_evidence_types}
        actual_prefixes = {eid[0] for eid in observation.evidence_ids}
        if expected_prefixes and expected_prefixes != actual_prefixes:
            failures.append(
                {
                    "case": case.id,
                    "category": "evidence",
                    "detail": f"expected sources {sorted(expected_prefixes)}, got {sorted(actual_prefixes)}",
                }
            )
        if observation.evidence_source_types != tuple(case.expected_evidence_types):
            failures.append(
                {
                    "case": case.id,
                    "category": "evidence_types",
                    "detail": f"expected {case.expected_evidence_types}, got {observation.evidence_source_types}",
                }
            )
        for fact in case.required_facts:
            if fact not in observation.answer:
                failures.append({"case": case.id, "category": "fact_missing", "detail": fact})
        for fact in case.forbidden_facts:
            if fact in observation.answer:
                failures.append({"case": case.id, "category": "fact_forbidden", "detail": fact})
        urls = extract_urls(observation.answer)
        if urls:
            failures.append(
                {
                    "case": case.id,
                    "category": "citation",
                    "detail": f"unattributed URL in offline answer: {urls[0]}",
                }
            )
        return failures
