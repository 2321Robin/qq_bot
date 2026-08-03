"""Observability facade (S4): structured logging, metrics and the unified
error outlet.

``record_error`` is the classified-error outlet (S4-METRIC-09): one call per
component catch point, feeding the error counter and a sanitized structured
event. It accepts only sanitized scalars — never payloads, bodies or ids.
"""

from __future__ import annotations

from qq_bot.observability.logging import (  # noqa: F401
    current_request_id,
    get_logger,
    record_error,
    record_event,
)
from qq_bot.observability.tracing import get_tracer, set_trace_enabled  # noqa: F401

OBSERVABILITY_SCHEMA_VERSION = 1
