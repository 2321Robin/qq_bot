"""In-process OTel-shaped tracer tests (S4-TRACE-01..07)."""

from __future__ import annotations

import logging

import pytest
from prometheus_client.parser import text_string_to_metric_families

from qq_bot.observability import get_tracer, set_trace_enabled
from qq_bot.observability.logging import LogContext, current_request_id, new_request_id
from qq_bot.observability.tracing import Tracer


async def _read_sample(name: str, labels: dict[str, str]) -> float:
    from qq_bot.plugins.health import metrics_endpoint

    response = await metrics_endpoint()
    text = response.body.decode("utf-8")
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            if sample.name != name:
                continue
            if all(sample.labels.get(key) == value for key, value in labels.items()):
                return float(sample.value)
    return 0.0


def _tracer(clock: list[float]) -> Tracer:
    return Tracer(enabled=True, logger=logging.getLogger("test_tracer"), clock=lambda: clock[0])


def test_span_tree_nesting_and_trace_id_consistency() -> None:
    clock = [0.0]
    tracer = _tracer(clock)
    parent = tracer.start_span("msg.total", trace_id="req-1")
    child = tracer.start_span("model.call", trace_id="req-1")
    assert child.trace_id == parent.trace_id == "req-1"
    assert child.parent_id == parent.span_id

    tracer.end_span(child)
    sibling = tracer.start_span("qq.send", trace_id="req-1")
    assert sibling.parent_id == parent.span_id
    tracer.end_span(sibling)
    tracer.end_span(parent)

    outside = tracer.start_span("msg.receive", trace_id="req-2")
    assert outside.parent_id is None
    assert outside.trace_id == "req-2"


def test_elapsed_uses_injected_clock_and_is_non_negative() -> None:
    clock = [0.0]
    tracer = _tracer(clock)
    span = tracer.start_span("model.call", trace_id="r")
    clock[0] = 12.5
    tracer.end_span(span)
    assert span.end == 12.5
    assert span.end - span.start == 12.5


def test_error_span_carries_status_and_category() -> None:
    clock = [0.0]
    tracer = _tracer(clock)
    span = tracer.start_span("search.call", trace_id="r")
    tracer.end_span(span, status="error", category="transient")
    assert span.status == "error"
    assert span.attributes["category"] == "transient"


def test_attribute_whitelist_drops_sensitive_keys() -> None:
    clock = [0.0]
    tracer = _tracer(clock)
    span = tracer.start_span(
        "model.call",
        trace_id="r",
        provider="primary",
        group_id="12345",
        user_id="67890",
        message="secret body",
        prompt="secret prompt",
    )
    assert set(span.attributes) == {"provider"}
    assert "group_id" not in span.attributes
    assert "message" not in span.attributes


def test_end_span_attributes_are_whitelisted_too() -> None:
    clock = [0.0]
    tracer = _tracer(clock)
    span = tracer.start_span("qq.send", trace_id="r")
    tracer.end_span(span, status="error", category="permanent", stack="trace\nsecret")
    assert span.attributes == {"category": "permanent"}


def test_disabled_tracer_is_zero_side_effect(caplog) -> None:
    clock = [0.0]
    tracer = Tracer(
        enabled=False, logger=logging.getLogger("test_disabled"), clock=lambda: clock[0]
    )
    span = tracer.start_span("model.call", trace_id="r", provider="primary")
    tracer.end_span(span, status="error", category="transient")
    # No-op: the span is never stamped and nothing is emitted.
    assert span.end is None
    assert span.status == "ok"
    assert span.attributes == {}


@pytest.mark.asyncio
async def test_span_duration_histogram_observed_once_and_log_emitted(caplog) -> None:
    set_trace_enabled(True)
    try:
        with caplog.at_level(logging.INFO, logger="qq_bot.observability"):
            tracer = get_tracer()
            before = await _read_sample("qq_bot_span_duration_seconds_count", {"phase": "route"})
            span = tracer.start_span("route.classify", trace_id="req-trace")
            tracer.end_span(span)
            after = await _read_sample("qq_bot_span_duration_seconds_count", {"phase": "route"})
            assert after == before + 1
        events = [
            record.extra_fields.get("event")
            for record in caplog.records
            if getattr(record, "extra_fields", None)
        ]
        assert "span_end" in events
    finally:
        set_trace_enabled(False)


@pytest.mark.asyncio
async def test_end_span_feeds_only_its_phase() -> None:
    set_trace_enabled(True)
    try:
        tracer = get_tracer()
        before = await _read_sample("qq_bot_span_duration_seconds_count", {"phase": "model"})
        span = tracer.start_span("model.call", trace_id="r")
        tracer.end_span(span)
        after = await _read_sample("qq_bot_span_duration_seconds_count", {"phase": "model"})
        assert after == before + 1
        send_before = await _read_sample("qq_bot_span_duration_seconds_count", {"phase": "send"})
        send_after = await _read_sample("qq_bot_span_duration_seconds_count", {"phase": "send"})
        # qq.send was never opened: its phase stays untouched by model.call.
        assert send_after == send_before
    finally:
        set_trace_enabled(False)


def test_current_request_id_follows_log_context() -> None:
    request_id = new_request_id()
    with LogContext(request_id=request_id, group_id=1001):
        assert current_request_id() == request_id
    assert current_request_id() != request_id
