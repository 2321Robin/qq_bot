"""Evaluation dataset schema tests (S2-EVAL-01..05)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from qq_bot.evaluation.models import (
    DatasetValidationError,
    EvalCase,
    EvalRoute,
    compute_dataset_hash,
    load_dataset,
    validate_dataset,
    write_dataset,
)

ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = ROOT / "evals" / "cases" / "roco_agent_v1.jsonl"


def _valid_case(**overrides: object) -> EvalCase:
    fields: dict[str, object] = {
        "id": "case-001",
        "split": "dev",
        "prompt": "TestPetA 的编号是多少？",
        "tags": ["number_query"],
        "expected_route": EvalRoute.LOCAL_KNOWLEDGE,
        "allowed_tools": ["lookup_pet"],
        "required_facts": ["001"],
        "forbidden_facts": [],
        "expected_refusal": False,
        "expected_evidence_types": ["local"],
        "freshness_required": False,
    }
    fields.update(overrides)
    return EvalCase(**fields)


def test_valid_case_round_trips() -> None:
    case = _valid_case()
    assert case.id == "case-001"
    assert case.expected_route is EvalRoute.LOCAL_KNOWLEDGE
    dumped = case.model_dump(mode="json")
    assert EvalCase.model_validate(dumped) == case


def test_unknown_route_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _valid_case(expected_route="teleport")


def test_missing_required_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        EvalCase(
            id="case-002",
            prompt="hello",
            expected_route=EvalRoute.DIRECT_CHAT,
        )


def test_invalid_split_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _valid_case(split="production")


def test_expected_evidence_types_only_allow_local_web_memory() -> None:
    with pytest.raises(ValidationError, match="expected_evidence_types"):
        _valid_case(expected_evidence_types=["database"])
    with pytest.raises(ValidationError):
        _valid_case(expected_evidence_types=["local", "cloud"])


def test_allowed_evidence_types_are_accepted() -> None:
    for evidence_type in ("local", "web", "memory"):
        case = _valid_case(expected_evidence_types=[evidence_type])
        assert case.expected_evidence_types == [evidence_type]


def test_extra_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        _valid_case(extra_field="boom")


def test_empty_prompt_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _valid_case(prompt="   ")


def test_duplicate_case_ids_are_rejected() -> None:
    cases = [_valid_case(), _valid_case(id="case-001")]
    with pytest.raises(DatasetValidationError, match="duplicate"):
        validate_dataset(cases)


def test_empty_dataset_is_rejected() -> None:
    with pytest.raises(DatasetValidationError, match="empty"):
        validate_dataset([])


def test_dataset_hash_is_deterministic_and_sensitive() -> None:
    first = compute_dataset_hash([_valid_case()])
    second = compute_dataset_hash([_valid_case()])
    assert first == second
    changed = compute_dataset_hash([_valid_case(required_facts=["999"])])
    assert changed != first


def test_write_and_reload_round_trip(tmp_path: Path) -> None:
    cases = [_valid_case(), _valid_case(id="case-002", split="test")]
    path = tmp_path / "cases.jsonl"
    manifest = write_dataset(cases, path)
    assert manifest.case_count == 2
    assert manifest.split_counts == {"dev": 1, "test": 1}

    loaded, loaded_manifest = load_dataset(path)
    assert loaded == cases
    assert loaded_manifest.dataset_hash == manifest.dataset_hash


def test_load_dataset_rejects_duplicate_ids(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text(
        "\n".join(
            [
                '{"id": "a", "split": "dev", "prompt": "p1", "expected_route": "direct_chat"}',
                '{"id": "a", "split": "dev", "prompt": "p2", "expected_route": "direct_chat"}',
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(DatasetValidationError, match="duplicate"):
        load_dataset(path)


def test_committed_dataset_loads_with_expected_scale() -> None:
    if not DATASET_PATH.exists():
        pytest.skip("dataset not generated yet")
    cases, manifest = load_dataset(DATASET_PATH)
    assert 100 <= manifest.case_count <= 300
    assert len({case.id for case in cases}) == manifest.case_count
    assert set(manifest.split_counts) <= {"dev", "test", "private"}


def test_committed_dataset_has_enough_human_reviewed_cases() -> None:
    if not DATASET_PATH.exists():
        pytest.skip("dataset not generated yet")
    cases, _ = load_dataset(DATASET_PATH)
    reviewed = [case for case in cases if "human_reviewed" in case.tags]
    assert len(reviewed) >= 40


# ---------------------------------------------------------------------------
# Metrics (S2-EVAL-06..13)
# ---------------------------------------------------------------------------

from qq_bot.evaluation.metrics import (  # noqa: E402
    CostEstimate,
    FactSample,
    Price,
    RefusalOutcome,
    TokenSummary,
    Usage,
    citation_provenance_rate,
    estimate_cost,
    fabrication_rate,
    fact_accuracy,
    illegal_tool_call_rate,
    latency_percentiles,
    over_refusal_rate,
    refusal_recall,
    route_accuracy,
    tool_selection_exact_match,
    token_summary,
    url_domain_valid_rate,
    url_syntax_rate,
)
from qq_bot.evaluation.runner import OfflineRunner, RunConfig, _default_executor  # noqa: E402


def test_metrics_accept_empty_input() -> None:
    assert route_accuracy([], []) == 0.0
    assert tool_selection_exact_match([], []) == 0.0
    assert illegal_tool_call_rate([], []) == 0.0
    assert fact_accuracy([]) == 0.0
    assert citation_provenance_rate([]) == 0.0
    assert url_syntax_rate([]) == 0.0
    assert url_domain_valid_rate([]) == 0.0
    assert refusal_recall([]) == 0.0
    assert over_refusal_rate([]) == 0.0
    assert fabrication_rate([]) == 0.0
    assert latency_percentiles([], [50.0, 95.0]) == {50.0: None, 95.0: None}


def test_route_accuracy_and_tool_selection() -> None:
    assert route_accuracy(["a", "b"], ["a", "b"]) == 1.0
    assert route_accuracy(["a", "a"], ["a", "b"]) == 0.5
    assert tool_selection_exact_match([["x"], ["a", "b"]], [["x"], ["b", "a"]]) == 1.0
    assert tool_selection_exact_match([["x", "y"]], [["x"]]) == 0.0


def test_illegal_tool_call_rate() -> None:
    assert illegal_tool_call_rate(["lookup_pet", "rm"], ["lookup_pet"]) == 0.5
    assert illegal_tool_call_rate(["lookup_pet"], ["lookup_pet"]) == 0.0


def test_fact_accuracy_canonical_and_human_override() -> None:
    samples = [
        FactSample(answer="编号 001", required_facts=["001"]),
        FactSample(answer="编号 002", required_facts=["001"]),
        FactSample(answer="有 001", forbidden_facts=["001"]),
        # human label overrides the canonical verdict in both directions
        FactSample(answer="没有资料", required_facts=[], human_label=True),
        FactSample(answer="编号 001", required_facts=["001"], human_label=False),
    ]
    assert fact_accuracy(samples) == 2 / 5


def test_citation_provenance_rate_requires_allowed_urls() -> None:
    from qq_bot.evaluation.metrics import CitationSample

    samples = [
        CitationSample(shown_urls=["https://a.example/1"], allowed_urls=["https://a.example/1"]),
        CitationSample(shown_urls=["https://a.example/1"], allowed_urls=["https://b.example/2"]),
        CitationSample(shown_urls=[], allowed_urls=[]),
    ]
    assert citation_provenance_rate(samples) == 2 / 3


def test_url_metrics() -> None:
    urls = [
        "https://example.com/page",
        "http://sub.example.org/x",
        "ftp://bad.example/x",
        "https:// 有空格.example/x",
        "not-a-url",
    ]
    assert url_syntax_rate(urls) == 2 / 5
    assert url_domain_valid_rate(urls) == 2 / 5


def test_refusal_metrics() -> None:
    outcomes = [
        RefusalOutcome(refused=True, expected=True),
        RefusalOutcome(refused=False, expected=True),
        RefusalOutcome(refused=True, expected=False),
        RefusalOutcome(refused=False, expected=False),
    ]
    assert refusal_recall(outcomes) == 0.5
    assert over_refusal_rate(outcomes) == 0.5


def test_latency_percentiles_nearest_rank() -> None:
    values = [1.0, 2.0, 3.0, 4.0]
    percentiles = latency_percentiles(values, [50.0, 95.0])
    assert percentiles[50.0] == 2.0
    assert percentiles[95.0] == 4.0
    with pytest.raises(ValueError):
        latency_percentiles(values, [101.0])


def test_token_summary_with_partial_usage() -> None:
    usages = [
        Usage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
        Usage(prompt_tokens=None, completion_tokens=None, total_tokens=None),
        Usage(prompt_tokens=10, completion_tokens=20, total_tokens=30, estimated=True),
    ]
    summary = token_summary(usages)
    assert isinstance(summary, TokenSummary)
    assert summary.prompt_tokens == 110
    assert summary.completion_tokens == 70
    assert summary.total_tokens == 180
    assert summary.estimated is True
    assert summary.cases_with_usage == 2
    assert summary.total_cases == 3


def test_token_summary_empty_is_unknown() -> None:
    summary = token_summary([])
    assert summary.prompt_tokens is None
    assert summary.total_tokens is None
    assert summary.estimated is False


def test_estimate_cost_never_guesses_numbers() -> None:
    price = Price(input_per_1k=0.01, output_per_1k=0.03, currency="USD")
    prices = {"model-a": price}
    # full data -> actual
    full = [
        Usage(prompt_tokens=1000, completion_tokens=1000, total_tokens=2000, model_id="model-a")
    ]
    assert estimate_cost(full, prices) == CostEstimate(0.04, "USD", "actual")
    # missing usage -> estimated, partial cost still reported
    partial = [
        Usage(prompt_tokens=1000, completion_tokens=1000, total_tokens=2000, model_id="model-a"),
        Usage(prompt_tokens=None, completion_tokens=None, total_tokens=None, model_id="model-a"),
    ]
    assert estimate_cost(partial, prices).status == "estimated"
    assert estimate_cost(partial, prices).cost == 0.04
    # no price table -> unknown, never a guessed number
    assert estimate_cost(full, {}) == CostEstimate(None, None, "unknown")
    # string "unknown" prices -> unknown
    unknown_prices = {"model-a": Price(input_per_1k="unknown", output_per_1k="unknown")}
    assert estimate_cost(full, unknown_prices).status == "unknown"
    # empty usages -> unknown
    assert estimate_cost([], prices) == CostEstimate(None, None, "unknown")


def test_fabrication_rate() -> None:
    assert fabrication_rate([True, False, False]) == 1 / 3


# ---------------------------------------------------------------------------
# Offline runner (S2-EVAL-14..16)
# ---------------------------------------------------------------------------

FIXED_CLOCK = "2026-08-01T00:00:00+00:00"


def _run_offline(tmp_path: Path, **overrides: object) -> object:
    report_path = tmp_path / "report.json"
    config = RunConfig(
        dataset_path=DATASET_PATH,
        split="dev",
        report_path=report_path,
        code_revision="test-rev",
        clock=lambda: __import__("datetime").datetime.fromisoformat(FIXED_CLOCK),
        **overrides,  # type: ignore[arg-type]
    )
    code = OfflineRunner(config).run()
    from qq_bot.evaluation.runner import EvalReport

    report = EvalReport.model_validate_json(report_path.read_text(encoding="utf-8"))
    return code, report


def test_offline_run_reports_clean_metrics(tmp_path: Path) -> None:
    code, report = _run_offline(tmp_path)
    assert code == 0
    assert report.case_count == 66
    assert report.split == "dev"
    assert report.mode == "offline"
    assert report.code_revision == "test-rev"
    assert report.dataset_hash
    assert report.failures == []
    assert report.metrics["route_accuracy"] == 1.0
    assert report.metrics["tool_selection_exact_match"] == 1.0
    assert report.metrics["illegal_tool_call_rate"] == 0.0
    assert report.metrics["fact_accuracy"] == 1.0
    assert report.metrics["citation_provenance_rate"] == 1.0
    assert report.metrics["refusal_recall"] == 1.0
    assert report.metrics["over_refusal_rate"] == 0.0
    assert report.metrics["fabrication_rate"] == 0.0
    assert report.metrics["latency_p50"] is not None
    assert report.metrics["token_summary"]["cases_with_usage"] == 66
    assert report.metrics["cost"]["status"] == "unknown"  # no price table offline


def test_offline_run_identical_for_identical_input(tmp_path: Path) -> None:
    _, first = _run_offline(tmp_path)
    _, second = _run_offline(tmp_path)
    assert first.model_dump() == second.model_dump()


def test_offline_run_records_failures_and_keeps_metrics(tmp_path: Path) -> None:
    from qq_bot.evaluation.metrics import Observation

    def broken_executor(case: EvalCase, index: int) -> Observation:
        if case.id.endswith("-001"):
            raise RuntimeError("boom")
        return Observation(
            case_id=case.id,
            route="wrong-route",
            confidence=0.0,
            answer="",
            refused=not case.expected_refusal,
        )

    code, report = _run_offline(tmp_path, executor=broken_executor)
    assert code == 1
    categories = {failure["category"] for failure in report.failures}
    assert "executor" in categories
    assert "route" in categories
    assert "refusal" in categories
    assert "tool_selection" in categories
    # metrics stay computable over the observed cases
    assert report.metrics["route_accuracy"] == 0.0


def test_offline_run_with_partial_usage_marks_estimated(tmp_path: Path) -> None:
    from qq_bot.evaluation.metrics import Observation

    def sparse_executor(case: EvalCase, index: int) -> Observation:
        observation = _default_executor(case, index)
        if index % 2:
            observation = Observation(**{**observation.__dict__, "usage": None})
        return observation

    code, report = _run_offline(tmp_path, executor=sparse_executor)
    assert code == 0
    summary = report.metrics["token_summary"]
    assert summary["estimated"] is True
    assert summary["cases_with_usage"] < summary["total_cases"]


def test_offline_run_empty_split_fails(tmp_path: Path) -> None:
    config = RunConfig(
        dataset_path=DATASET_PATH,
        split="private",
        report_path=tmp_path / "x.json",
    )
    assert OfflineRunner(config).run() == 1


def test_pricing_example_loads_into_price_table() -> None:
    import json

    pricing_path = ROOT / "evals" / "pricing.example.json"
    if not pricing_path.exists():
        pytest.skip("pricing example not present")
    raw = json.loads(pricing_path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1
    prices: dict[str, Price] = {}
    for model, entry in raw["models"].items():
        prices[model] = Price(
            input_per_1k=entry["input_per_1k_tokens"],
            output_per_1k=entry["output_per_1k_tokens"],
            currency=entry["currency"],
        )
    assert prices
    # unknown prices stay explicit strings; numbers flow into estimates
    usage = Usage(prompt_tokens=1000, completion_tokens=1000, total_tokens=2000, model_id=model)
    estimate = estimate_cost([usage], prices)
    assert estimate.status in {"actual", "estimated", "unknown"}
    assert estimate.cost is None or isinstance(estimate.cost, float)


# ---------------------------------------------------------------------------
# Live benchmark (Task 15, S2-EVAL-07/12/14/15, S2-GATE-03)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_mode_refuses_without_explicit_opt_in(tmp_path, monkeypatch) -> None:
    from qq_bot.evaluation.live import LiveRunner

    monkeypatch.delenv("AGENT_EVAL_LIVE", raising=False)
    config = RunConfig(dataset_path=DATASET_PATH, split="test", report_path=tmp_path / "x.json")
    assert LiveRunner(config).run() == 2
    assert not (tmp_path / "x.json").exists()  # nothing fabricated, no report


@pytest.mark.asyncio
async def test_live_mode_refuses_without_provider_config(tmp_path, monkeypatch) -> None:
    from qq_bot.evaluation.live import LiveRunner

    monkeypatch.setenv("AGENT_EVAL_LIVE", "1")
    monkeypatch.setenv("AI_API_KEY", "")
    monkeypatch.setenv("AI_MODEL", "")
    config = RunConfig(dataset_path=DATASET_PATH, split="test", report_path=tmp_path / "x.json")
    assert LiveRunner(config).run() == 2


@pytest.mark.asyncio
async def test_live_mode_refuses_tampered_dataset(tmp_path, monkeypatch) -> None:
    import json as _json
    import shutil

    from qq_bot.evaluation.live import LiveRunner

    monkeypatch.setenv("AGENT_EVAL_LIVE", "1")
    monkeypatch.setenv("AI_API_KEY", "secret-key")
    monkeypatch.setenv("AI_MODEL", "test-model")
    dataset = tmp_path / "roco_agent_v1.jsonl"
    shutil.copyfile(DATASET_PATH, dataset)
    shutil.copyfile(
        ROOT / "evals" / "cases" / "roco_agent_v1.manifest.json",
        tmp_path / "roco_agent_v1.manifest.json",
    )
    lines = dataset.read_text(encoding="utf-8").splitlines()
    last = _json.loads(lines[-1])
    last["prompt"] = last["prompt"] + " 修改"
    lines[-1] = _json.dumps(last, ensure_ascii=False)
    dataset.write_text("\n".join(lines) + "\n", encoding="utf-8")

    config = RunConfig(dataset_path=dataset, split="test", report_path=tmp_path / "x.json")
    assert LiveRunner(config).run() == 1


def test_live_report_shape_is_honest_and_secret_free(tmp_path, monkeypatch) -> None:
    """With injected executors the comparison report is produced; it must
    contain both pipelines, the quality-gate table and zero secrets."""
    import json

    from qq_bot.evaluation.live import LiveRunner
    from qq_bot.evaluation.metrics import Observation

    monkeypatch.setenv("AGENT_EVAL_LIVE", "1")
    monkeypatch.setenv("AI_API_KEY", "sk-super-secret-value")
    monkeypatch.setenv("AI_MODEL", "test-model")
    monkeypatch.setenv("AI_FALLBACK_MODEL", "")

    calls: dict[str, int] = {"legacy": 0, "agent": 0}

    async def fake_legacy(case: EvalCase, index: int) -> Observation:
        calls["legacy"] += 1
        return Observation(
            case_id=case.id,
            route="legacy_context",
            confidence=0.0,
            answer="好的，这是本地回答。",
            refused=case.expected_refusal,
            usage=None,
            latency_seconds=0.01,
        )

    async def fake_agent(case: EvalCase, index: int) -> Observation:
        calls["agent"] += 1
        return Observation(
            case_id=case.id,
            route=case.expected_route.value,
            confidence=0.9,
            selected_tools=tuple(case.allowed_tools),
            answer="根据本地图鉴：" + "，".join(case.required_facts) + "。",
            evidence_ids=tuple(f"L{i}" for i in range(1, len(case.expected_evidence_types) + 1)),
            evidence_source_types=tuple(case.expected_evidence_types),
            refused=case.expected_refusal,
            usage=Usage(
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
                model_id="test-model",
            ),
            latency_seconds=0.02,
        )

    report_path = tmp_path / "live.json"
    config = RunConfig(
        dataset_path=DATASET_PATH,
        split="test",
        report_path=report_path,
        code_revision="test-rev",
        legacy_executor=fake_legacy,  # type: ignore[arg-type]
        agent_executor=fake_agent,  # type: ignore[arg-type]
    )
    assert LiveRunner(config).run() == 0

    raw = report_path.read_text(encoding="utf-8")
    assert "sk-super-secret-value" not in raw
    report = json.loads(raw)
    assert report["mode"] == "live"
    assert (
        report["dataset_hash"] == "4924602133fe8ee9137d6d6b40041be9c80380a31bf2cc4c0f0e3966d87302f0"
    )
    assert set(report["results"]) == {"legacy_context", "tool_agent"}
    legacy = report["results"]["legacy_context"]["metrics"]
    assert "route_accuracy" not in legacy  # not applicable, never a fake zero
    assert report["results"]["tool_agent"]["metrics"]["route_accuracy"] == 1.0
    assert report["results"]["tool_agent"]["metrics"]["tool_selection_exact_match"] == 1.0
    assert len(report["quality_gates"]) == 5
    assert report["quality_gates"][0]["metric"] == "tool_selection_exact_match"
    # provider identity recorded, no key material
    assert report["provider"] == {"model": "test-model", "fallback_model": ""}
    assert calls["legacy"] == 84
    assert calls["agent"] == 84


def test_live_report_failures_never_contain_prompts(tmp_path, monkeypatch) -> None:
    import json

    from qq_bot.evaluation.live import LiveRunner
    from qq_bot.evaluation.metrics import Observation

    monkeypatch.setenv("AGENT_EVAL_LIVE", "1")
    monkeypatch.setenv("AI_API_KEY", "sk-secret")
    monkeypatch.setenv("AI_MODEL", "test-model")

    async def failing_agent(case: EvalCase, index: int) -> Observation:
        # wrong route + a fabricated URL + wrong refusal -> failures everywhere
        return Observation(
            case_id=case.id,
            route="web_search",
            confidence=0.9,
            selected_tools=(),
            answer="https://private.example.com/leak",
            refused=False,
            usage=Usage(total_tokens=None, model_id="test-model"),
            latency_seconds=0.01,
        )

    report_path = tmp_path / "live-fail.json"
    config = RunConfig(
        dataset_path=DATASET_PATH,
        split="test",
        report_path=report_path,
        code_revision="test-rev",
        legacy_executor=failing_agent,  # type: ignore[arg-type]
        agent_executor=failing_agent,  # type: ignore[arg-type]
    )
    assert LiveRunner(config).run() == 1  # quality gates unmet, reported honestly

    raw = report_path.read_text(encoding="utf-8")
    assert "sk-secret" not in raw
    report = json.loads(raw)
    assert report["results"]["tool_agent"]["failures"]
    for failure in report["results"]["tool_agent"]["failures"]:
        assert failure["case"]
        assert failure["category"]
    assert any(not gate["passed"] for gate in report["quality_gates"])
    # no prompt text anywhere in the report
    with open(DATASET_PATH, encoding="utf-8") as handle:
        for line in handle:
            case_prompt = json.loads(line)["prompt"]
            assert case_prompt not in raw, "report must not contain case prompts"
