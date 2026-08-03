"""OTel-shaped in-process tracer (S4-TRACE-05). Log-export only; swapping to
the official SDK later keeps call sites unchanged.

Every AI message produces a span tree whose spans share the request id as
``trace_id`` (S4-TRACE-01); ``end_span`` emits one structured log event and
feeds the per-phase histogram (S4-TRACE-05). Span attributes are restricted
to the whitelist below — never message bodies, prompts, group/user ids,
outputs or stacks (S4-TRACE-06).
"""

from __future__ import annotations

import contextvars
import logging
import time
from dataclasses import dataclass, field
from itertools import count

# Attribute whitelist (S4-TRACE-06): everything else is dropped on entry.
_SPAN_ATTRIBUTE_WHITELIST = frozenset(
    {"phase", "name", "provider", "tool", "category", "route", "reason_code", "dependency"}
)

# Fixed span-name -> phase mapping for the histogram (S4-TRACE-05).
_PHASE_BY_NAME = {
    "msg.receive": "receive",
    "msg.total": "total",
    "route.classify": "route",
    "memory.retrieve": "memory",
    "knowledge.lookup": "knowledge",
    "search.call": "search",
    "model.call": "model",
    "qq.send": "send",
    "agent.loop": "agent",
}

_span_ids = count(1)


@dataclass
class Span:
    name: str
    trace_id: str
    parent_id: str | None
    start: float
    end: float | None = None
    status: str = "ok"  # ok | error
    attributes: dict[str, str] = field(default_factory=dict)
    span_id: str = field(default_factory=lambda: f"span-{next(_span_ids)}")


class Tracer:
    """contextvars stack of spans; ``end_span`` emits a log event and feeds
    the per-phase histogram (once per span, S4-TRACE-05)."""

    def __init__(self, *, enabled: bool, logger: logging.Logger, clock=time.perf_counter) -> None:
        self._enabled = enabled
        self._logger = logger
        self._clock = clock
        self._stack: contextvars.ContextVar[tuple[Span, ...]] = contextvars.ContextVar(
            "qq_bot_span_stack", default=()
        )

    def start_span(self, name: str, *, trace_id: str, **attributes: str) -> Span:
        """Open a child of the innermost active span (same task/context).
        Disabled tracers return a bare no-op span (S4-TRACE-07)."""
        if not self._enabled:
            return Span(name=name, trace_id=trace_id, parent_id=None, start=self._clock())
        parent = self._stack.get()[-1] if self._stack.get() else None
        filtered = {
            key: value for key, value in attributes.items() if key in _SPAN_ATTRIBUTE_WHITELIST
        }
        span = Span(
            name=name,
            trace_id=trace_id,
            parent_id=parent.span_id if parent is not None else None,
            start=self._clock(),
            attributes=filtered,
        )
        self._stack.set((*self._stack.get(), span))
        return span

    def end_span(self, span: Span, *, status: str = "ok", **attributes: str) -> None:
        """Close a span: record duration + status, emit the structured log
        event, and pop the span off the stack. No-op for disabled tracers.
        Extra attributes are whitelist-filtered like start attributes
        (e.g. the classified ``category`` on failure spans, S4-TRACE-07)."""
        if not self._enabled:
            return
        span.end = self._clock()
        span.status = status
        span.attributes.update(
            {key: value for key, value in attributes.items() if key in _SPAN_ATTRIBUTE_WHITELIST}
        )
        phase = _PHASE_BY_NAME.get(span.name, span.name)
        elapsed = max(0.0, span.end - span.start)
        from qq_bot.observability import metrics

        metrics.SPAN_DURATION.labels(phase).observe(elapsed)
        stack = self._stack.get()
        if stack and stack[-1] is span:
            self._stack.set(stack[:-1])
        from qq_bot.observability.logging import record_event

        record_event(
            self._logger,
            logging.INFO,
            "span_end",
            message="span ended",
            span_name=span.name,
            phase=phase,
            trace_id=span.trace_id,
            parent_id=span.parent_id or "",
            status=span.status,
            duration_ms=round(elapsed * 1000.0, 3),
            **span.attributes,
        )


_tracer = Tracer(enabled=False, logger=logging.getLogger("qq_bot.observability"))


def get_tracer() -> Tracer:
    """The process-wide tracer (module singleton)."""
    return _tracer


def set_trace_enabled(enabled: bool) -> None:
    """Toggle span emission (S4-TRACE-07). Called once at runtime startup;
    disabled tracers make spans zero-overhead no-ops."""
    _tracer._enabled = enabled
