"""Shared cost module and live token/cost accounting tests (S4-METRIC-07)."""

from __future__ import annotations

import json

import pytest

from qq_bot.config import BotSettings
from qq_bot.observability.cost import (
    CostEstimate,
    Price,
    TokenSummary,
    Usage,
    estimate_cost,
    load_price_table,
    token_summary,
)


async def _sample_value(name: str, labels: dict[str, str]) -> float:
    from qq_bot.plugins.health import metrics_endpoint

    response = await metrics_endpoint()
    from prometheus_client.parser import text_string_to_metric_families

    text = response.body.decode("utf-8")
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            if sample.name != name:
                continue
            if all(sample.labels.get(key) == value for key, value in labels.items()):
                return float(sample.value)
    return 0.0


def test_estimate_cost_full_prices_is_actual() -> None:
    prices = {"model-a": Price(input_per_1k=0.01, output_per_1k=0.03, currency="USD")}
    estimate = estimate_cost(
        [Usage(prompt_tokens=1000, completion_tokens=500, model_id="model-a")],
        prices,
    )
    assert estimate == CostEstimate(0.025, "USD", "actual")


def test_estimate_cost_no_usage_is_unknown() -> None:
    prices = {"model-a": Price(input_per_1k=0.01, output_per_1k=0.03)}
    assert estimate_cost([], prices) == CostEstimate(None, None, "unknown")


def test_estimate_cost_missing_price_is_unknown() -> None:
    estimate = estimate_cost(
        [Usage(prompt_tokens=1000, completion_tokens=500, model_id="model-a")],
        {},
    )
    assert estimate == CostEstimate(None, None, "unknown")


def test_estimate_cost_partial_pairs_is_estimated() -> None:
    prices = {"model-a": Price(input_per_1k=0.01, output_per_1k=0.03)}
    estimate = estimate_cost(
        [
            Usage(prompt_tokens=1000, completion_tokens=500, model_id="model-a"),
            Usage(prompt_tokens=1000, completion_tokens=500, model_id="model-b"),
        ],
        prices,
    )
    assert estimate == CostEstimate(0.025, "USD", "estimated")


def test_estimate_cost_string_price_never_guessed() -> None:
    prices = {"model-a": Price(input_per_1k="unknown", output_per_1k="unknown")}
    estimate = estimate_cost(
        [Usage(prompt_tokens=1000, completion_tokens=500, model_id="model-a")],
        prices,
    )
    assert estimate == CostEstimate(None, None, "unknown")


def test_token_summary_marks_estimated_when_missing() -> None:
    usages = [
        Usage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
        None,
    ]
    summary = token_summary(usages)
    assert summary == TokenSummary(
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
        estimated=True,
        cases_with_usage=1,
        total_cases=2,
    )


def test_token_summary_empty_input() -> None:
    assert token_summary([]) == TokenSummary(None, None, None, False, 0, 0)


def test_load_price_table_accepts_documented_and_short_keys(tmp_path) -> None:
    path = tmp_path / "pricing.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "models": {
                    "long-keys": {"input_per_1k_tokens": 0.01, "output_per_1k_tokens": 0.02},
                    "short-keys": {"input_per_1k": 0.03, "output_per_1k": 0.04},
                    "missing-rate": {"input_per_1k_tokens": 0.05},
                    "not-a-dict": 7,
                },
            }
        ),
        encoding="utf-8",
    )
    table = load_price_table(path)
    assert table["long-keys"] == Price(input_per_1k=0.01, output_per_1k=0.02)
    assert table["short-keys"] == Price(input_per_1k=0.03, output_per_1k=0.04)
    assert "missing-rate" not in table
    assert "not-a-dict" not in table


def test_load_price_table_missing_and_garbage_files_yield_empty(tmp_path) -> None:
    assert load_price_table(tmp_path / "nope.json") == {}
    garbage = tmp_path / "garbage.json"
    garbage.write_text("{not json", encoding="utf-8")
    assert load_price_table(garbage) == {}


@pytest.mark.asyncio
async def test_ai_client_accounts_tokens_and_cost(monkeypatch) -> None:
    from qq_bot.services import ai_client as ai_client_module

    async def fake_post_chat_completion(
        payload: object,
        *,
        settings: object,
        client: object,
        base_url: str,
        api_key: str,
        model: str,
        breaker_name: str,
    ) -> dict[str, object]:
        return {
            "choices": [{"message": {"content": "hi back"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        }

    monkeypatch.setattr(ai_client_module, "_post_chat_completion", fake_post_chat_completion)
    monkeypatch.setattr(
        ai_client_module,
        "_price_table",
        {"gpt-test": Price(input_per_1k=0.01, output_per_1k=0.03, currency="USD")},
    )
    settings = BotSettings(ai_api_key="secret", ai_model="gpt-test")

    prompt_before = await _sample_value(
        "qq_bot_tokens_total", {"kind": "prompt", "model": "gpt-test", "estimated": "false"}
    )
    completion_before = await _sample_value(
        "qq_bot_tokens_total", {"kind": "completion", "model": "gpt-test", "estimated": "false"}
    )
    cost_before = await _sample_value(
        "qq_bot_cost_usd_total", {"model": "gpt-test", "status": "actual"}
    )

    text = await ai_client_module.request_ai_reply("hi", settings=settings, client=object())

    assert text == "hi back"
    prompt_after = await _sample_value(
        "qq_bot_tokens_total", {"kind": "prompt", "model": "gpt-test", "estimated": "false"}
    )
    completion_after = await _sample_value(
        "qq_bot_tokens_total", {"kind": "completion", "model": "gpt-test", "estimated": "false"}
    )
    cost_after = await _sample_value(
        "qq_bot_cost_usd_total", {"model": "gpt-test", "status": "actual"}
    )
    assert prompt_after == prompt_before + 100
    assert completion_after == completion_before + 50
    assert cost_after == pytest.approx(cost_before + 0.0025)


@pytest.mark.asyncio
async def test_ai_client_no_usage_records_nothing(monkeypatch) -> None:
    from qq_bot.services import ai_client as ai_client_module

    async def fake_post_chat_completion(
        payload: object,
        *,
        settings: object,
        client: object,
        base_url: str,
        api_key: str,
        model: str,
        breaker_name: str,
    ) -> dict[str, object]:
        return {"choices": [{"message": {"content": "no usage"}}]}

    monkeypatch.setattr(ai_client_module, "_post_chat_completion", fake_post_chat_completion)
    monkeypatch.setattr(
        ai_client_module,
        "_price_table",
        {"gpt-test": Price(input_per_1k=0.01, output_per_1k=0.03)},
    )
    settings = BotSettings(ai_api_key="secret", ai_model="gpt-test")

    tokens_before = await _sample_value(
        "qq_bot_tokens_total", {"kind": "prompt", "model": "gpt-test", "estimated": "false"}
    )
    cost_before = await _sample_value(
        "qq_bot_cost_usd_total", {"model": "gpt-test", "status": "actual"}
    )

    text = await ai_client_module.request_ai_reply("hi", settings=settings, client=object())

    assert text == "no usage"
    tokens_after = await _sample_value(
        "qq_bot_tokens_total", {"kind": "prompt", "model": "gpt-test", "estimated": "false"}
    )
    cost_after = await _sample_value(
        "qq_bot_cost_usd_total", {"model": "gpt-test", "status": "actual"}
    )
    assert tokens_after == tokens_before
    assert cost_after == cost_before


@pytest.mark.asyncio
async def test_cost_metrics_feed_the_endpoint() -> None:
    from qq_bot.plugins.health import metrics_endpoint

    response = await metrics_endpoint()
    from prometheus_client.parser import text_string_to_metric_families

    names = {
        family.name for family in text_string_to_metric_families(response.body.decode("utf-8"))
    }
    assert "qq_bot_tokens" in names
    assert "qq_bot_cost_usd" in names
