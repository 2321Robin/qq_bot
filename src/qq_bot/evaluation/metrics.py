"""Pure metric functions for the evaluation reports (S2-EVAL-06..13).

Every function accepts empty input and returns ``0.0`` or an explicit
``None``/``unknown`` marker instead of raising on zero denominators. Metrics
never invent numbers: when token usage or prices are missing the report marks
``estimated``/``unknown``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

_URL_RE = re.compile(r"https?://[^\s]+")
_CONTROL_OR_SPACE = re.compile(r"[\x00-\x20\x7f]")


@dataclass(frozen=True)
class FactSample:
    """One fact check: canonical required/forbidden matching plus an optional
    human label that overrides the canonical verdict (S2-EVAL-08)."""

    answer: str
    required_facts: Sequence[str] = ()
    forbidden_facts: Sequence[str] = ()
    human_label: bool | None = None


@dataclass(frozen=True)
class CitationSample:
    shown_urls: Sequence[str] = ()
    allowed_urls: Sequence[str] = ()


@dataclass(frozen=True)
class RefusalOutcome:
    refused: bool
    expected: bool


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    estimated: bool = False
    model_id: str = ""


@dataclass(frozen=True)
class TokenSummary:
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    estimated: bool
    cases_with_usage: int
    total_cases: int


@dataclass(frozen=True)
class Price:
    input_per_1k: float | str
    output_per_1k: float | str
    currency: str = "USD"


@dataclass(frozen=True)
class CostEstimate:
    cost: float | None
    currency: str | None
    status: Literal["actual", "estimated", "unknown"]


def route_accuracy(decisions: Sequence[str], expected: Sequence[str]) -> float:
    """Fraction of route decisions equal to the expected route (S2-EVAL-09)."""
    if not decisions:
        return 0.0
    if len(decisions) != len(expected):
        raise ValueError("decisions and expected must have the same length")
    return sum(decision == expectation for decision, expectation in zip(decisions, expected)) / len(
        decisions
    )


def tool_selection_exact_match(
    selected: Sequence[Sequence[str]], expected: Sequence[Sequence[str]]
) -> float:
    """Fraction of cases whose selected tool set equals the expected set."""
    if not selected:
        return 0.0
    if len(selected) != len(expected):
        raise ValueError("selected and expected must have the same length")
    matches = sum(
        set(chosen) == set(expectation) for chosen, expectation in zip(selected, expected)
    )
    return matches / len(selected)


def illegal_tool_call_rate(calls: Sequence[str], allowed: Sequence[str]) -> float:
    """Fraction of tool calls whose name is outside the allowed set."""
    if not calls:
        return 0.0
    allowed_set = set(allowed)
    return sum(call not in allowed_set for call in calls) / len(calls)


def fact_accuracy(samples: Sequence[FactSample]) -> float:
    """Fraction of samples where every required fact is present, no forbidden
    fact is present, and a human label (when given) agrees with the canonical
    verdict."""
    if not samples:
        return 0.0
    passed = 0
    for sample in samples:
        canonical = all(fact in sample.answer for fact in sample.required_facts) and not any(
            fact in sample.answer for fact in sample.forbidden_facts
        )
        if sample.human_label is not None and sample.human_label != canonical:
            continue
        if canonical:
            passed += 1
    return passed / len(samples)


def citation_provenance_rate(samples: Sequence[CitationSample]) -> float:
    """Fraction of answers whose visible URLs are all within the allowed set
    (URLs must come from accepted Web evidence, S2-EVID-05)."""
    if not samples:
        return 0.0
    passed = 0
    for sample in samples:
        allowed = set(sample.allowed_urls)
        if all(url in allowed for url in sample.shown_urls):
            passed += 1
    return passed / len(samples)


def _url_parts(url: str) -> tuple[str, str] | None:
    if _CONTROL_OR_SPACE.search(url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return parts.scheme, parts.hostname or ""


def url_syntax_rate(urls: Sequence[str]) -> float:
    """Fraction of URLs with a valid http(s) scheme and a host."""
    if not urls:
        return 0.0
    return sum(_url_parts(url) is not None for url in urls) / len(urls)


def url_domain_valid_rate(urls: Sequence[str]) -> float:
    """Fraction of URLs whose hostname looks like a real domain (has a dot,
    no trailing dot, no whitespace). Reachability is checked only in isolated
    evaluation tasks, never in CI (S2-EVAL-10)."""
    if not urls:
        return 0.0
    valid = 0
    for url in urls:
        parts = _url_parts(url)
        if parts is None:
            continue
        hostname = parts[1]
        if (
            "." in hostname
            and not hostname.endswith(".")
            and not _CONTROL_OR_SPACE.search(hostname)
        ):
            valid += 1
    return valid / len(urls)


def refusal_recall(outcomes: Sequence[RefusalOutcome]) -> float:
    """Recall over cases that must be refused (S2-EVAL-11)."""
    must_refuse = [outcome for outcome in outcomes if outcome.expected]
    if not must_refuse:
        return 0.0
    refused = sum(outcome.refused for outcome in must_refuse)
    return refused / len(must_refuse)


def over_refusal_rate(outcomes: Sequence[RefusalOutcome]) -> float:
    """Fraction of answerable cases that were refused anyway."""
    answerable = [outcome for outcome in outcomes if not outcome.expected]
    if not answerable:
        return 0.0
    over_refused = sum(outcome.refused for outcome in answerable)
    return over_refused / len(answerable)


def fabrication_rate(fabricated_flags: Sequence[bool]) -> float:
    """Fraction of unanswerable cases whose answer fabricated content
    (forbidden-fact hits or positive claims on not-found results)."""
    if not fabricated_flags:
        return 0.0
    return sum(fabricated_flags) / len(fabricated_flags)


def latency_percentiles(
    seconds: Sequence[float],
    percentiles: Sequence[float] = (50.0, 95.0),
) -> dict[float, float | None]:
    """Nearest-rank percentiles; empty input maps every percentile to None."""
    if not seconds:
        return {percentile: None for percentile in percentiles}
    ordered = sorted(seconds)
    result: dict[float, float | None] = {}
    for percentile in percentiles:
        if percentile < 0 or percentile > 100:
            raise ValueError("percentiles must be within 0..100")
        index = max(0, math.ceil(percentile / 100.0 * len(ordered)) - 1)
        result[percentile] = ordered[index]
    return result


def extract_urls(text: str) -> list[str]:
    """Extract candidate URLs from rendered answer text (S2-SEC-06)."""
    return _URL_RE.findall(text)


def token_summary(usages: Sequence[Usage | None]) -> TokenSummary:
    """Sum known token fields; any missing/estimated usage marks the summary
    as estimated (S2-EVAL-12)."""
    if not usages:
        return TokenSummary(None, None, None, False, 0, 0)
    known = [usage for usage in usages if usage is not None]
    prompt = sum(usage.prompt_tokens for usage in known if usage.prompt_tokens is not None)
    completion = sum(
        usage.completion_tokens for usage in known if usage.completion_tokens is not None
    )
    total = sum(usage.total_tokens for usage in known if usage.total_tokens is not None)
    with_usage = sum(usage.total_tokens is not None for usage in known)
    estimated = with_usage < len(usages) or any(usage.estimated for usage in known)
    return TokenSummary(
        prompt_tokens=prompt if with_usage else None,
        completion_tokens=completion if with_usage else None,
        total_tokens=total if with_usage else None,
        estimated=estimated,
        cases_with_usage=with_usage,
        total_cases=len(usages),
    )


def _price_value(value: float | str) -> float | None:
    if isinstance(value, str):
        return None
    return float(value)


def estimate_cost(usages: Sequence[Usage], prices: Mapping[str, Price]) -> CostEstimate:
    """Estimate cost from actual usage and a local price table.

    - no usages or no usable usage+price pair -> status ``unknown``, cost None;
    - some pairs computable but any usage/price missing -> ``estimated`` with
      the partial cost (never a guessed full number);
    - every pair computable -> ``actual``.
    """
    if not usages:
        return CostEstimate(None, None, "unknown")

    total_cost = 0.0
    computed_pairs = 0
    all_known = True
    currency: str | None = None
    for usage in usages:
        price = prices.get(usage.model_id)
        if price is None:
            all_known = False
            continue
        if usage.prompt_tokens is None or usage.completion_tokens is None:
            all_known = False
            continue
        input_rate = _price_value(price.input_per_1k)
        output_rate = _price_value(price.output_per_1k)
        if input_rate is None or output_rate is None:
            all_known = False
            continue
        total_cost += usage.prompt_tokens / 1000.0 * input_rate
        total_cost += usage.completion_tokens / 1000.0 * output_rate
        computed_pairs += 1
        currency = currency or price.currency

    if computed_pairs == 0:
        return CostEstimate(None, currency, "unknown")
    status: Literal["actual", "estimated", "unknown"] = "actual" if all_known else "estimated"
    return CostEstimate(round(total_cost, 6), currency, status)


@dataclass(frozen=True)
class Observation:
    """Everything the runner records about one evaluated case."""

    case_id: str
    route: str
    confidence: float
    selected_tools: tuple[str, ...] = field(default_factory=tuple)
    answer: str = ""
    evidence_ids: tuple[str, ...] = field(default_factory=tuple)
    evidence_source_types: tuple[str, ...] = field(default_factory=tuple)
    usage: Usage | None = None
    refused: bool = False
    latency_seconds: float = 0.0


def metrics_dict(
    *,
    observations: Sequence[Observation],
    cases: Sequence[Any],
    usages: Sequence[Usage | None],
    prices: Mapping[str, Price] | None = None,
) -> dict[str, Any]:
    """Assemble the full metric dict for a report (S2-EVAL-06..13)."""
    route_expected = [case.expected_route.value for case in cases]
    selected = [list(observation.selected_tools) for observation in observations]
    allowed = [list(case.allowed_tools) for case in cases]
    all_calls = [tool for observation in observations for tool in observation.selected_tools]
    all_allowed = [tool for case in cases for tool in case.allowed_tools]

    fact_samples = [
        FactSample(
            answer=observation.answer,
            required_facts=case.required_facts,
            forbidden_facts=case.forbidden_facts,
        )
        for observation, case in zip(observations, cases)
    ]
    citation_samples = [
        CitationSample(
            shown_urls=extract_urls(observation.answer),
            allowed_urls=(),
        )
        for observation in observations
    ]
    all_urls = [url for observation in observations for url in extract_urls(observation.answer)]

    refusal_outcomes = [
        RefusalOutcome(refused=observation.refused, expected=case.expected_refusal)
        for observation, case in zip(observations, cases)
    ]
    unanswerable_flags = [
        case.tags[0] in {"unknown_entity", "refusal", "prompt_injection"}
        or (case.tags[0] == "skill_intersection" and not case.required_facts)
        for case in cases
    ]
    fabricated = [
        any(fact in observation.answer for fact in case.forbidden_facts)
        for observation, case, unanswerable in zip(observations, cases, unanswerable_flags)
        if unanswerable
    ]

    latency = latency_percentiles(
        [observation.latency_seconds for observation in observations], (50.0, 95.0)
    )
    token_summary_value = token_summary(usages)
    cost = estimate_cost(
        [usage for usage in usages if usage is not None],
        prices or {},
    )

    return {
        "route_accuracy": route_accuracy(
            [observation.route for observation in observations], route_expected
        ),
        "tool_selection_exact_match": tool_selection_exact_match(selected, allowed),
        "illegal_tool_call_rate": illegal_tool_call_rate(all_calls, all_allowed),
        "fact_accuracy": fact_accuracy(fact_samples),
        "citation_provenance_rate": citation_provenance_rate(citation_samples),
        "url_syntax_rate": url_syntax_rate(all_urls),
        "url_domain_valid_rate": url_domain_valid_rate(all_urls),
        "refusal_recall": refusal_recall(refusal_outcomes),
        "over_refusal_rate": over_refusal_rate(refusal_outcomes),
        "fabrication_rate": fabrication_rate(fabricated),
        "latency_p50": latency[50.0],
        "latency_p95": latency[95.0],
        "token_summary": {
            "prompt_tokens": token_summary_value.prompt_tokens,
            "completion_tokens": token_summary_value.completion_tokens,
            "total_tokens": token_summary_value.total_tokens,
            "estimated": token_summary_value.estimated,
            "cases_with_usage": token_summary_value.cases_with_usage,
            "total_cases": token_summary_value.total_cases,
        },
        "cost": {
            "cost": cost.cost,
            "currency": cost.currency,
            "status": cost.status,
        },
    }
