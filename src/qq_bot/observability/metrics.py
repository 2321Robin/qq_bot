"""Prometheus metric definitions (S4-METRIC).

One instrumentation point feeds metrics, spans and logs through the
observability facade. Every label value is an aggregate, a category or a
hash — never a raw group/user id, message body or prompt (S4-PRIV-01).

When ``metrics_enabled`` is false the registry exposes zero-cost no-op
stand-ins, so a disabled build never touches prometheus_client
(S4-METRIC-12). Metrics are reached through the module attribute (``metrics
.MESSAGES``), which resolves through ``__getattr__`` so the swap is live.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

_Metric = Counter | Gauge | Histogram


class _NoopMetric:
    """Zero-cost stand-in used while metrics are disabled.

    Accepts the prometheus_client call surface (``labels``/``inc``/``set``/
    ``observe``) but does nothing.
    """

    __slots__ = ()

    def labels(self, *_args: object, **_kwargs: object) -> "_NoopMetric":
        return self

    def inc(self, _amount: float = 1) -> None:
        return None

    def set(self, _value: float) -> None:
        return None

    def observe(self, _amount: float) -> None:
        return None


_SPECS: dict[str, tuple[type[_Metric], tuple[object, ...]]] = {}
_REAL: dict[str, _Metric] = {}
_METRICS: dict[str, _Metric] = {}


def _define(name: str, kind: type[_Metric], *args: object) -> None:
    """Create the real Prometheus object once (registry registration is
    permanent); ``_METRICS`` holds the live object, swapped at runtime."""
    _SPECS[name] = (kind, args)
    _REAL[name] = kind(*args)
    _METRICS[name] = _REAL[name]


_define("MESSAGES", Counter, "qq_bot_messages_total", "Processed messages", ["kind"])
_define(
    "COMMANDS",
    Counter,
    "qq_bot_commands_total",
    "Executed commands",
    ["command"],
)
_define(
    "AI_REQUESTS",
    Counter,
    "qq_bot_ai_requests_total",
    "Model requests",
    ["provider", "result"],
)
_define(
    "AI_DURATION",
    Histogram,
    "qq_bot_ai_request_duration_seconds",
    "Model request latency",
    ["provider"],
)
_define(
    "SEARCH_REQUESTS",
    Counter,
    "qq_bot_search_requests_total",
    "Web searches",
    ["result"],
)
_define(
    "SEARCH_GATE",
    Counter,
    "qq_bot_search_gate_total",
    "LLM search-intent gate verdicts (S11-SEARCH-01)",
    ["result"],
)
_define("SEARCH_DURATION", Histogram, "qq_bot_search_duration_seconds", "Web search latency")
_define(
    "RETRIES",
    Counter,
    "qq_bot_retry_total",
    "Actual retries (attempt>=2)",
    ["dependency"],
)
_define(
    "FALLBACKS",
    Counter,
    "qq_bot_provider_fallback_total",
    "Primary->fallback switches",
    ["provider"],
)
_define(
    "ERRORS",
    Counter,
    "qq_bot_errors_total",
    "Classified errors",
    ["component", "category"],
)
_define(
    "CIRCUIT_INFO",
    Gauge,
    "qq_bot_circuit_breaker_info",
    "Breaker state (1=active)",
    ["name", "state"],
)
_define(
    "CIRCUIT_TRANSITIONS",
    Counter,
    "qq_bot_circuit_breaker_transitions_total",
    "State transitions",
    ["name", "to_state"],
)
_define("SEND_RESULTS", Counter, "qq_bot_send_total", "QQ send results", ["category"])
_define(
    "TOKENS",
    Counter,
    "qq_bot_tokens_total",
    "Provider-reported tokens",
    ["kind", "model", "estimated"],
)
_define(
    "COST_USD",
    Counter,
    "qq_bot_cost_usd_total",
    "Estimated cost in USD",
    ["model", "status"],
)
_define(
    "QUOTA_DENIED",
    Counter,
    "qq_bot_quota_denied_total",
    "Quota rejections",
    ["scope_type", "reason"],
)
_define("AGENT_OUTCOMES", Counter, "qq_bot_agent_outcome_total", "Agent results", ["code"])
_define(
    "AUTO_CHAT",
    Counter,
    "qq_bot_auto_chat_total",
    "Auto chat pipeline decisions",
    ["stage", "result"],
)
_define(
    "AUTO_CHAT_PENDING",
    Gauge,
    "qq_bot_auto_chat_pending_batches",
    "Pending auto-chat collection batches",
)
_define(
    "ROUTES",
    Counter,
    "qq_bot_route_total",
    "Route decisions",
    ["route", "reason_code"],
)
_define("SPAN_DURATION", Histogram, "qq_bot_span_duration_seconds", "Span latency", ["phase"])
_define(
    "SCHEDULER_SENDS",
    Counter,
    "qq_bot_scheduler_send_total",
    "Scheduled send results",
    ["result"],
)
_define(
    "SCHEDULED_REDELIVER",
    Counter,
    "qq_bot_scheduled_redeliver_total",
    "Scheduled send redeliveries",
    ["job", "result"],
)
_define(
    "SCHEDULED_JOBS_TOTAL",
    Counter,
    "qq_bot_scheduled_jobs_total",
    "Scheduled job runs by job type and result",
    ["job", "result"],
)
_define(
    "REPORT_SECTIONS_TOTAL",
    Counter,
    "qq_bot_report_sections_total",
    "Life report section outcomes",
    ["section", "result"],
)
_define(
    "REPORT_LLM_TOTAL",
    Counter,
    "qq_bot_report_llm_total",
    "Life report LLM polish outcomes",
    ["section", "result"],
)
_define(
    "BRIEFING_FEED_TOTAL",
    Counter,
    "qq_bot_briefing_feed_total",
    "AI briefing feed outcomes",
    ["result"],
)
_define(
    "BRIEFING_LLM_TOTAL",
    Counter,
    "qq_bot_briefing_llm_total",
    "AI briefing compose outcomes",
    ["result"],
)
_define(
    "EXPRESSION_TOTAL",
    Counter,
    "qq_bot_expression_total",
    "Expression learning pipeline results",
    ["stage", "result"],
)
_define(
    "JARGON_TOTAL",
    Counter,
    "qq_bot_jargon_total",
    "Jargon learning pipeline results",
    ["stage", "result"],
)
_define(
    "EXPRESSION_ACTIVE",
    Gauge,
    "qq_bot_expression_active",
    "Active expression templates per group",
    ["group_hash"],
)
_define(
    "AUTO_CHAT_INTEREST",
    Gauge,
    "qq_bot_auto_chat_interest",
    "Current group chat interest level",
    ["group_hash"],
)
_define(
    "AUTO_CHAT_REACTIONS",
    Counter,
    "qq_bot_auto_chat_reactions_total",
    "Auto chat reaction outcomes",
    ["result"],
)
_define(
    "PERSON_OBSERVE_TOTAL",
    Counter,
    "qq_bot_person_observe_total",
    "Person observation card pipeline results",
    ["stage", "result"],
)
_define(
    "PERSON_OBSERVE_CARDS",
    Gauge,
    "qq_bot_person_observe_cards",
    "Active observation cards per group",
    ["group_hash"],
)
_define(
    "AUTO_CHAT_STICKERS",
    Counter,
    "qq_bot_auto_chat_stickers_total",
    "Auto chat sticker send outcomes",
    ["result"],
)
_define(
    "AI_VISION",
    Counter,
    "qq_bot_ai_vision_total",
    "Vision request accounting (S11-VISION-01): request/degraded/fallback_text/dropped",
    ["result"],
)
_define(
    "QA_EXPRESSIONS",
    Counter,
    "qq_bot_qa_expressions_total",
    "QA reply sticker/face marker outcomes (S11-VISION-01)",
    ["kind", "result"],
)


def set_metrics_enabled(enabled: bool) -> None:
    """Enable or disable instrumentation (S4-METRIC-12).

    When disabled, every metric is swapped for a zero-cost no-op so the
    disabled path never touches prometheus_client. Call once at startup,
    before the first message is processed.
    """
    for name in _SPECS:
        _METRICS[name] = _REAL[name] if enabled else _NoopMetric()


def __getattr__(name: str) -> _Metric:
    try:
        return _METRICS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
