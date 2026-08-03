"""Run metrics collection (S5-METRICS-01..03, zero new dependencies).

Fetches the /metrics exposition (or a previously exported metrics text),
aggregates the chat_memory.sqlite3 tables, and writes one daily report
``metrics-YYYYmmdd.json`` plus the cumulative ``metrics-state.json``
(first_seen drives ``uptime_days``).

Honesty rules (S5-METRICS-01): only values actually observed are reported;
anything without an observation is ``null``/absent and its field name is
listed in ``not_observed``. Never fabricate zeros.

Usage:
    python scripts/collect_run_metrics.py                                        # defaults
    python scripts/collect_run_metrics.py --metrics-file metrics.txt --no-write
    python scripts/collect_run_metrics.py --db container-data/run-validate/chat_memory.sqlite3 --output-dir data/reports
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pydantic import BaseModel, Field  # noqa: E402

SCHEMA_VERSION = 1
STATE_FILENAME = "metrics-state.json"
REPORT_PREFIX = "metrics-"

# ---------------------------------------------------------------------------
# Prometheus text exposition parsing (subset sufficient for our families)
# ---------------------------------------------------------------------------

_LINE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?P<labels>\{.*?\})?\s+(?P<value>[-+]?[0-9.eE+]+)\s*$"
)
_LABELS = re.compile(
    r'([a-zA-Z_][a-zA-Z0-9_]*)=\"((?:[^"\\\\]|\\\\.)*)\"|([a-zA-Z_][a-zA-Z0-9_]*)=([-+]?[0-9.eE+]+)'
)


def _parse_labels(label_text: str) -> tuple[str, ...]:
    if not label_text:
        return ()
    labels: list[str] = []
    for m in _LABELS.finditer(label_text):
        if m.group(1) is not None:
            labels.append(m.group(1) + "=" + m.group(2))
        else:
            labels.append(m.group(3) + "=" + m.group(4))
    return tuple(sorted(labels))


def parse_metrics_text(text: str) -> dict[str, dict[tuple[str, ...], float]]:
    """Parse an exposition into {family_name: {label_tuple: value}}.

    Histogram bucket/count/sum lines are kept as their own families
    (``name_bucket``, ``name_count``, ``name_sum``); ``*_created`` lines and
    HELP/TYPE lines are ignored.
    """
    families: dict[str, dict[tuple[str, ...], float]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE.match(line)
        if m is None:
            continue
        name = m.group("name")
        if name.endswith("_created"):
            continue
        try:
            value = float(m.group("value"))
        except ValueError:
            continue
        families.setdefault(name, {})[_parse_labels(m.group("labels"))] = value
    return families


def histogram_buckets(
    families: dict[str, dict[tuple[str, ...], float]], family: str
) -> tuple[list[tuple[float, float]], float | None]:
    """Return (sorted (upper_bound, cumulative_count) pairs, count_total)."""
    pairs: list[tuple[float, float]] = []
    count: float | None = None
    for name, samples in families.items():
        if name == family + "_bucket":
            for labels, value in samples.items():
                bound = float(labels[0].split("=", 1)[1]) if labels else float("inf")
                pairs.append((bound, value))
        elif name == family + "_count":
            count = sum(samples.values())
    pairs.sort(key=lambda pair: pair[0])
    return pairs, count


def percentile(buckets: list[tuple[float, float]], count: float, p: float) -> float | None:
    """Linear interpolation over cumulative histogram buckets."""
    if count <= 0 or not buckets:
        return None
    target = p * count
    prev_bound, prev_cum = 0.0, 0.0
    for bound, cum in buckets:
        if cum >= target:
            if cum <= prev_cum:
                return float(bound)
            return prev_bound + (bound - prev_bound) * (target - prev_cum) / (cum - prev_cum)
        prev_bound, prev_cum = bound, cum
    return None


def _family_total(families: dict[str, dict[tuple[str, ...], float]], name: str) -> float | None:
    samples = families.get(name)
    if samples is None:
        return None
    return sum(samples.values())


def _split_labels(labels: tuple[str, ...]) -> dict[str, str]:
    return dict(item.split("=", 1) for item in labels)


# ---------------------------------------------------------------------------
# SQLite aggregation (counts + hashes only, never raw ids)
# ---------------------------------------------------------------------------


def _sha256_of_ids(ids: list[int]) -> str:
    payload = "\n".join(str(i) for i in sorted(set(ids))).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def aggregate_db(db_path: Path) -> dict[str, Any]:
    """Read-only aggregation of chat_messages/quota tables. Raises when the
    database cannot be opened (caller marks the report db_ok=False)."""
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        group_rows = [r[0] for r in conn.execute("SELECT DISTINCT group_id FROM chat_messages")]
        user_rows = [r[0] for r in conn.execute("SELECT DISTINCT user_id FROM chat_messages")]
        message_count = conn.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0]
        quota = {"requests": None, "tokens": None, "cost_usd": None, "events": None}
        try:
            row = conn.execute(
                "SELECT SUM(requests), SUM(tokens), SUM(cost_usd) FROM quota_usage"
            ).fetchone()
            if row[0] is not None:
                quota["requests"] = int(row[0])
                quota["tokens"] = int(row[1] or 0)
                quota["cost_usd"] = float(row[2] or 0.0)
        except sqlite3.Error:
            pass
        try:
            quota["events"] = int(conn.execute("SELECT COUNT(*) FROM quota_events").fetchone()[0])
        except sqlite3.Error:
            pass
        return {
            "stored_total": int(message_count),
            "groups": {"count": len(group_rows), "hash": _sha256_of_ids(group_rows)},
            "users": {"count": len(user_rows), "hash": _sha256_of_ids(user_rows)},
            "quota": quota,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# State (first_seen) handling
# ---------------------------------------------------------------------------


def load_state(state_path: Path) -> dict[str, Any]:
    if state_path.is_file():
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(data.get("first_seen"), str):
                return data
        except (OSError, ValueError):
            pass
    return {}


def compute_uptime_days(state: dict[str, Any], now: datetime) -> float | None:
    first_seen = state.get("first_seen")
    if not first_seen:
        return None
    try:
        first = datetime.fromisoformat(first_seen)
    except ValueError:
        return None
    return round((now - first).total_seconds() / 86400.0, 3)


# ---------------------------------------------------------------------------
# Report schema (schema_version=1)
# ---------------------------------------------------------------------------


class MetricsReport(BaseModel):
    schema_version: int = Field(default=SCHEMA_VERSION, frozen=True)
    captured_at: str
    source: dict[str, Any]
    uptime_days: float | None = None
    messages: dict[str, Any] = Field(default_factory=dict)
    commands: dict[str, Any] = Field(default_factory=dict)
    ai: dict[str, Any] = Field(default_factory=dict)
    search: dict[str, Any] = Field(default_factory=dict)
    reliability: dict[str, Any] = Field(default_factory=dict)
    tokens: dict[str, Any] = Field(default_factory=dict)
    cost_usd: dict[str, Any] = Field(default_factory=dict)
    quota: dict[str, Any] = Field(default_factory=dict)
    not_observed: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Aggregation over parsed metrics
# ---------------------------------------------------------------------------


def aggregate_metrics(
    families: dict[str, dict[tuple[str, ...], float]], db_agg: dict[str, Any] | None
) -> MetricsReport:
    now = datetime.now(UTC)
    not_observed: list[str] = []
    source = {"metrics_ok": True}

    # messages
    messages: dict[str, Any] = {}
    total = _family_total(families, "qq_bot_messages_total")
    by_kind: dict[str, int] = {}
    for labels, value in families.get("qq_bot_messages_total", {}).items():
        kind = _split_labels(labels).get("kind", "?")
        by_kind[kind] = int(value)
    if total is None:
        not_observed.append("messages")
    else:
        messages["metric_total"] = int(total)
        messages["by_kind"] = by_kind

    # commands
    commands: dict[str, Any] = {}
    by_command: dict[str, int] = {}
    for labels, value in families.get("qq_bot_commands_total", {}).items():
        by_command[_split_labels(labels).get("command", "?")] = int(value)
    if by_command:
        commands["total"] = sum(by_command.values())
        commands["by_command"] = by_command
    else:
        not_observed.append("commands")

    # AI requests + e2e latency
    ai: dict[str, Any] = {}
    requests = _family_total(families, "qq_bot_ai_requests_total")
    by_result: dict[str, int] = {}
    for labels, value in families.get("qq_bot_ai_requests_total", {}).items():
        by_result[_split_labels(labels).get("result", "?")] = int(value)
    if requests is None:
        not_observed.append("ai.requests")
    else:
        ai["requests"] = int(requests)
        ai["by_result"] = by_result

    latency_source: str | None = None
    p50 = p95 = None
    # msg.total phase is the end-to-end span (S4-TRACE-01)
    per_phase: dict[str, list[tuple[float, float]]] = {}
    for labels, value in families.get("qq_bot_span_duration_seconds_bucket", {}).items():
        split = _split_labels(labels)
        phase = split.get("phase", "?")
        bound = float(split.get("le", "inf"))
        per_phase.setdefault(phase, []).append((bound, value))
    for pairs in per_phase.values():
        pairs.sort(key=lambda pair: pair[0])
    if "msg.total" in per_phase:
        phase_count = sum(
            v
            for labels, v in families.get("qq_bot_span_duration_seconds_count", {}).items()
            if _split_labels(labels).get("phase") == "msg.total"
        )
        p50 = percentile(per_phase["msg.total"], phase_count, 0.50)
        p95 = percentile(per_phase["msg.total"], phase_count, 0.95)
        latency_source = "span.msg.total"
    else:
        buckets2, count2 = histogram_buckets(families, "qq_bot_ai_request_duration_seconds")
        if buckets2 and count2:
            p50 = percentile(buckets2, count2, 0.50)
            p95 = percentile(buckets2, count2, 0.95)
            latency_source = "ai_request_duration"
    if p50 is None or p95 is None:
        not_observed.append("ai.e2e_latency")
    else:
        ai["e2e_p50_ms"] = round(p50 * 1000.0, 1)
        ai["e2e_p95_ms"] = round(p95 * 1000.0, 1)
        ai["latency_source"] = latency_source

    # search
    search: dict[str, Any] = {}
    search_total = _family_total(families, "qq_bot_search_requests_total")
    search_by_result: dict[str, int] = {}
    for labels, value in families.get("qq_bot_search_requests_total", {}).items():
        search_by_result[_split_labels(labels).get("result", "?")] = int(value)
    if search_total is None:
        not_observed.append("search.triggered")
    else:
        search["triggered"] = int(search_total)
        search["by_result"] = search_by_result
    # no metric for empty-result searches exists; report honestly
    not_observed.append("search.no_result")

    # reliability
    reliability: dict[str, Any] = {}
    fallbacks = _family_total(families, "qq_bot_provider_fallback_total")
    retries = _family_total(families, "qq_bot_retry_total")
    errors = _family_total(families, "qq_bot_errors_total")
    if fallbacks is None:
        not_observed.append("reliability.fallbacks")
    else:
        reliability["fallbacks"] = int(fallbacks)
    if retries is None:
        not_observed.append("reliability.retries")
    else:
        reliability["retries"] = int(retries)
    if errors is None:
        not_observed.append("reliability.errors")
    else:
        reliability["errors"] = int(errors)

    # tokens / cost
    tokens: dict[str, Any] = {}
    prompt = completion = 0.0
    by_model: dict[str, dict[str, int]] = {}
    token_samples = families.get("qq_bot_tokens_total")
    if token_samples is None:
        not_observed.append("tokens")
    else:
        for labels, value in token_samples.items():
            split = _split_labels(labels)
            kind = split.get("kind", "?")
            model = split.get("model", "?")
            if kind == "prompt":
                prompt += value
            elif kind == "completion":
                completion += value
            by_model.setdefault(model, {}).setdefault(kind, 0)
            by_model[model][kind] += int(value)
        tokens["prompt"] = int(prompt)
        tokens["completion"] = int(completion)
        tokens["total"] = int(prompt + completion)
        tokens["by_model"] = by_model

    cost: dict[str, Any] = {}
    cost_samples = families.get("qq_bot_cost_usd_total")
    if cost_samples is None:
        not_observed.append("cost_usd")
    else:
        cost["total"] = round(sum(cost_samples.values()), 6)
        cost["by_status"] = {
            _split_labels(labels).get("status", "?"): int(value)
            for labels, value in cost_samples.items()
        }

    # quota
    quota: dict[str, Any] = {}
    denied = _family_total(families, "qq_bot_quota_denied_total")
    by_reason: dict[str, int] = {}
    for labels, value in families.get("qq_bot_quota_denied_total", {}).items():
        by_reason[_split_labels(labels).get("reason", "?")] = int(value)
    if denied is None:
        not_observed.append("quota.denied")
    else:
        quota["denied_total"] = int(denied)
        quota["by_reason"] = by_reason
    if db_agg is not None:
        quota["usage"] = db_agg["quota"]
        quota["usage_observed"] = any(v is not None for v in db_agg["quota"].values())

    # db-derived message aggregates
    if db_agg is not None:
        messages["stored_total"] = db_agg["stored_total"]
        messages["groups"] = db_agg["groups"]
        messages["users"] = db_agg["users"]

    return MetricsReport(
        captured_at=now.isoformat(),
        source=source,
        messages=messages,
        commands=commands,
        ai=ai,
        search=search,
        reliability=reliability,
        tokens=tokens,
        cost_usd=cost,
        quota=quota,
        not_observed=sorted(set(not_observed)),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _fetch_metrics(endpoint: str, metrics_file: str | None) -> str:
    if metrics_file:
        return Path(metrics_file).read_text(encoding="utf-8")
    import httpx

    response = httpx.get(endpoint, timeout=10.0)
    response.raise_for_status()
    return response.text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--endpoint", default="http://127.0.0.1:8081/metrics")
    parser.add_argument("--metrics-file", default=None, help="read a saved /metrics text export")
    parser.add_argument("--db", default="data/chat_memory.sqlite3")
    parser.add_argument("--output-dir", default="data/reports")
    parser.add_argument("--no-write", action="store_true", help="print the report only")
    args = parser.parse_args(argv)

    metrics_text: str | None = None
    metrics_error: str | None = None
    try:
        metrics_text = _fetch_metrics(args.endpoint, args.metrics_file)
    except Exception as exc:  # noqa: BLE001 - report the failure, keep going
        metrics_error = f"{type(exc).__name__}: {exc}"

    db_agg: dict[str, Any] | None = None
    db_error: str | None = None
    db_path = Path(args.db)
    if db_path.is_file():
        try:
            db_agg = aggregate_db(db_path)
        except Exception as exc:  # noqa: BLE001
            db_error = f"{type(exc).__name__}: {exc}"
    else:
        db_error = f"database not found: {args.db}"

    not_observed: list[str] = []
    source: dict[str, Any] = {
        "metrics_ok": metrics_text is not None,
        "db_ok": db_agg is not None,
        "endpoint": args.endpoint if not args.metrics_file else None,
        "metrics_file": args.metrics_file,
        "db": str(db_path),
    }
    if metrics_text is None:
        source["metrics_error"] = metrics_error
        not_observed.append("metrics")
    if db_agg is None:
        source["db_error"] = db_error
        not_observed.append("db")

    report = (
        aggregate_metrics(parse_metrics_text(metrics_text), db_agg)
        if metrics_text is not None
        else MetricsReport(
            captured_at=datetime.now(UTC).isoformat(),
            source=source,
            not_observed=["metrics"],
        )
    )
    report.source = source

    # uptime from state
    output_dir = Path(args.output_dir)
    state_path = output_dir / STATE_FILENAME
    state = load_state(state_path) if (not args.no_write or output_dir.is_dir()) else {}
    now = datetime.now(UTC)
    if args.no_write:
        report.uptime_days = compute_uptime_days(state, now) if state else None
    else:
        if not state.get("first_seen"):
            state = {"schema_version": SCHEMA_VERSION, "first_seen": now.isoformat()}
        report.uptime_days = compute_uptime_days(state, now)
        output_dir.mkdir(parents=True, exist_ok=True)
        state["last_collection"] = now.isoformat()
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        report_path = output_dir / f"{REPORT_PREFIX}{now.strftime('%Y%m%d')}.json"
        report_path.write_text(
            json.dumps(report.model_dump(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    report.not_observed = sorted(set(report.not_observed + not_observed))
    print(json.dumps(report.model_dump(), ensure_ascii=False, indent=2))
    if metrics_text is None and db_agg is None:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
