"""Cost estimation shared by evaluation reports and live accounting
(S4-METRIC-07, S2-EVAL-12).

Extracted from ``evaluation.metrics`` so the observability layer can price
live model calls without importing the evaluation package. Prices are always
locally loaded and never guessed: a missing price or usage keeps the status
``unknown``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping, Sequence


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


def _default_pricing_path() -> Path:
    return Path(__file__).resolve().parents[3] / "evals" / "pricing.json"


def load_price_table(path: Path | None = None) -> dict[str, Price]:
    """Load the optional local price table.

    Same semantics as the evaluation runner: a missing or unreadable file
    yields an empty table so cost stays ``unknown`` — never guessed numbers
    (S2-EVAL-12). Accepts both the documented ``*_per_1k_tokens`` key
    spellings and the short ``*_per_1k`` form.
    """
    if path is None:
        path = _default_pricing_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    models = payload.get("models", {}) if isinstance(payload, dict) else {}
    table: dict[str, Price] = {}
    for model_id, entry in models.items():
        if not isinstance(entry, dict):
            continue
        input_rate = entry.get("input_per_1k_tokens", entry.get("input_per_1k"))
        output_rate = entry.get("output_per_1k_tokens", entry.get("output_per_1k"))
        if input_rate is None or output_rate is None:
            continue
        table[model_id] = Price(
            input_per_1k=input_rate,
            output_per_1k=output_rate,
            currency=entry.get("currency", "USD"),
        )
    return table
