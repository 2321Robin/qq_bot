"""Structured logging facade (S4-LOG-01..07).

One facade on top of stdlib ``logging`` plus NoneBot's loguru logger:

- ``hash_id`` is the single identity-hashing entry point (S4-LOG-02);
- ``LogContext`` carries ``request_id``/``group_hash``/``user_hash`` through
  ``contextvars`` for the whole message-processing chain (S4-LOG-01) without
  changing service signatures;
- ``JsonFormatter`` renders single-line JSON from a fixed key whitelist
  (S4-LOG-03/04); ``install_logging`` wires it to the stdlib root logger and
  replaces NoneBot's loguru stdout sink so that in one process both
  ``qq_bot.*`` and ``nonebot`` logger output are JSON lines (S4-LOG-05).

No third-party logging library is introduced (no structlog).
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import secrets
import sys
from typing import Any

_log_context: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "log_context", default={}
)

_ALLOWED_KEYS = frozenset(
    {
        "ts",
        "level",
        "logger",
        "event",
        "message",
        "request_id",
        "group_hash",
        "user_hash",
        "provider",
        "tool",
        "duration_ms",
        "attempt",
        "max_attempts",
        "error_category",
        "circuit_state",
        "reason_code",
        "route",
        "category",
        "phase",
        "status",
        "span_name",
        "parent_id",
        "dependency",
        "scope_hash",
    }
)

_LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}


def hash_id(value: int | str, *, kind: str) -> str:
    """Irreversible identity hash for logs/metrics/reports (S4-LOG-02).

    Never used for storage; databases keep raw integers. The hash is not
    enumeration-proof (QQ id space is small) so it only ever appears in
    observability outputs, never as an identifier.
    """
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]
    return f"{kind}_{digest}"


def new_request_id() -> str:
    return secrets.token_hex(8)


def current_request_id() -> str:
    """The active request id from the correlation scope, or a fresh one when
    called outside any request context (tests, background jobs). Span trees
    share this value as their ``trace_id`` (S4-TRACE-01)."""
    return _log_context.get().get("request_id") or new_request_id()


def get_logger(name: str) -> logging.Logger:
    """Return a standard-library logger managed by the facade."""
    return logging.getLogger(name)


def record_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    """Log one structured event: ``event`` is the short event name; pass
    ``message=`` in fields for the human-readable line (defaults to the event
    name). Whitelisted fields become JSON keys; non-whitelisted keys are
    silently dropped."""
    extra_fields = {key: value for key, value in fields.items() if key in _ALLOWED_KEYS}
    extra_fields.setdefault("event", event)
    if "message" not in extra_fields:
        extra_fields["message"] = event
    logger.log(level, extra_fields["message"], extra={"extra_fields": extra_fields})


def record_error(component: str, category: str, *, message: str = "") -> None:
    """Record one classified error (S4-METRIC-09): the error counter plus a
    sanitized structured event. ``component`` is the failing subsystem,
    ``category`` reuses ``classify_exception``/``classify_send_error`` results
    where available. Imported lazily to keep the facade cycle-free."""
    from qq_bot.observability import metrics

    metrics.ERRORS.labels(component, category).inc()
    record_event(
        get_logger("qq_bot.observability"),
        logging.ERROR,
        "error_recorded",
        message=message or f"error recorded: component={component}, category={category}",
        category=category,
    )


class LogContext:
    """contextvars-based correlation scope for one message (S4-LOG-01).

    Entering merges request/group/user identifiers into the current context;
    leaving restores the previous scope. Nested scopes keep the outer fields.
    """

    def __init__(
        self,
        *,
        request_id: str,
        group_id: int | None = None,
        user_id: int | None = None,
    ) -> None:
        self._token: contextvars.Token[dict[str, str]] | None = None
        data: dict[str, str] = {"request_id": request_id}
        if group_id is not None:
            data["group_hash"] = hash_id(group_id, kind="group")
        if user_id is not None:
            data["user_hash"] = hash_id(user_id, kind="user")
        self._data = data

    def __enter__(self) -> "LogContext":
        merged = {**_log_context.get(), **self._data}
        self._token = _log_context.set(merged)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._token is not None:
            _log_context.reset(self._token)


class JsonFormatter(logging.Formatter):
    """Single-line JSON formatter with a fixed key whitelist (S4-LOG-03/04).

    Output keys come from the contextvars scope and the record's
    ``extra_fields`` (set by ``record_event``); anything outside the
    whitelist is never serialized.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in _log_context.get().items():
            if key in _ALLOWED_KEYS:
                payload[key] = value
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update({k: v for k, v in extra.items() if k in _ALLOWED_KEYS})
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


_stdlib_handler: logging.Handler | None = None
_loguru_handler_id: int | None = None


def install_logging(*, log_format: str = "text", log_level: str = "INFO") -> None:
    """Apply the configured format/level to stdlib logging and (for JSON)
    to NoneBot's loguru logger. Idempotent and re-configurable; safe to call
    at startup after ``nonebot.init()``.

    With ``log_format=json`` both ``qq_bot.*`` stdlib loggers and the loguru
    ``nonebot`` logger emit JSON lines in the same process (S4-LOG-05); the
    loguru JSON formatter merges the same contextvars scope as the stdlib
    formatter so correlation fields stay consistent across both paths.
    """
    global _stdlib_handler, _loguru_handler_id
    level = _LOG_LEVELS.get(log_level, logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    if _stdlib_handler is not None:
        root.removeHandler(_stdlib_handler)
        _stdlib_handler = None
    if _loguru_handler_id is not None:
        _remove_loguru_sink(_loguru_handler_id)
        _loguru_handler_id = None

    if log_format != "json":
        # Plain readable text: ensure a basic handler exists (no-op when the
        # app already configured one) so qq_bot.* logs are visible.
        logging.basicConfig(level=level)
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    _stdlib_handler = handler
    _loguru_handler_id = _install_loguru_json(log_level)


def _remove_loguru_sink(handler_id: int) -> None:
    try:
        from loguru import logger as loguru_logger
    except Exception:
        return
    try:
        loguru_logger.remove(handler_id)
    except Exception:
        pass


def _install_loguru_json(log_level: str) -> int | None:
    """Replace NoneBot's default loguru sink with a JSON-line sink that also
    reads the facade contextvars scope (so plugin lines carry request_id)."""
    try:
        from loguru import logger as loguru_logger
    except Exception:
        return None

    level_name = log_level if log_level in _LOG_LEVELS else "INFO"

    def json_format(record: dict[str, Any]) -> str:
        payload: dict[str, Any] = {
            "ts": record["time"].strftime("%Y-%m-%dT%H:%M:%S%z"),
            "level": record["level"].name,
            "logger": record["name"],
            "message": str(record["message"]),
        }
        for key, value in _log_context.get().items():
            if key in _ALLOWED_KEYS:
                payload[key] = value
        for key, value in record["extra"].items():
            if key in _ALLOWED_KEYS:
                payload[key] = value
        # loguru treats a callable format's return as a format template and
        # runs format_map over it; escape braces so the JSON is emitted as-is.
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return text.replace("{", "{{").replace("}", "}}")

    def level_filter(record: dict[str, Any]) -> bool:
        override = record["extra"].get("nonebot_log_level")
        effective = (
            override if isinstance(override, str) and override in _LOG_LEVELS else level_name
        )
        return record["level"].no >= _LOG_LEVELS[effective]

    loguru_logger.remove()
    return loguru_logger.add(
        sys.stdout,
        level=0,
        diagnose=False,
        filter=level_filter,
        format=json_format,
    )
