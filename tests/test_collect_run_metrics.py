"""Offline tests for scripts/collect_run_metrics.py (S5-METRICS-01..03).

Fully offline: synthetic Prometheus exposition text + a temp SQLite database.
Covers report schema validity, aggregation correctness, hashed dimensions
(no raw ids in the report), not_observed semantics, and --no-write.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts.collect_run_metrics import (
    MetricsReport,
    aggregate_db,
    aggregate_metrics,
    compute_uptime_days,
    load_state,
    main,
    parse_metrics_text,
    percentile,
)

SYNTHETIC_METRICS = """\
# HELP qq_bot_messages_total Processed messages
# TYPE qq_bot_messages_total counter
qq_bot_messages_total{kind="ai_prompt"} 4
qq_bot_messages_total{kind="plain"} 1
# HELP qq_bot_commands_total Executed commands
# TYPE qq_bot_commands_total counter
qq_bot_commands_total{command="配额"} 2
qq_bot_commands_total{command="最近故障"} 1
# HELP qq_bot_ai_requests_total Model requests
# TYPE qq_bot_ai_requests_total counter
qq_bot_ai_requests_total{provider="primary",result="ok"} 4
# HELP qq_bot_span_duration_seconds Span latency
# TYPE qq_bot_span_duration_seconds histogram
qq_bot_span_duration_seconds_bucket{phase="msg.total",le="0.1"} 2
qq_bot_span_duration_seconds_bucket{phase="msg.total",le="0.5"} 3
qq_bot_span_duration_seconds_bucket{phase="msg.total",le="1.0"} 4
qq_bot_span_duration_seconds_bucket{phase="msg.total",le="+Inf"} 4
qq_bot_span_duration_seconds_count{phase="msg.total"} 4
qq_bot_span_duration_seconds_sum{phase="msg.total"} 1.2
# HELP qq_bot_search_requests_total Web searches
# TYPE qq_bot_search_requests_total counter
qq_bot_search_requests_total{result="ok"} 3
# HELP qq_bot_retry_total Actual retries (attempt>=2)
# TYPE qq_bot_retry_total counter
qq_bot_retry_total{dependency="ai"} 1
# HELP qq_bot_provider_fallback_total Primary->fallback switches
# TYPE qq_bot_provider_fallback_total counter
qq_bot_provider_fallback_total{provider="primary"} 2
# HELP qq_bot_errors_total Classified errors
# TYPE qq_bot_errors_total counter
qq_bot_errors_total{component="ai",category="transient"} 3
# HELP qq_bot_tokens_total Provider-reported tokens
# TYPE qq_bot_tokens_total counter
qq_bot_tokens_total{kind="prompt",model="glm-4-flash",estimated="false"} 420
qq_bot_tokens_total{kind="completion",model="glm-4-flash",estimated="false"} 53
# HELP qq_bot_cost_usd_total Estimated cost in USD
# TYPE qq_bot_cost_usd_total counter
qq_bot_cost_usd_total{model="glm-4-flash",status="actual"} 0
# HELP qq_bot_quota_denied_total Quota rejections
# TYPE qq_bot_quota_denied_total counter
qq_bot_quota_denied_total{scope_type="group",reason="rate"} 2
"""


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "chat_memory.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, group_id INTEGER,"
        " user_id INTEGER, is_ai_prompt INTEGER, message_text TEXT, ai_reply TEXT,"
        " created_at TEXT)"
    )
    for group_id, user_id in [(1001, 2001), (1001, 2002), (1002, 2001)]:
        conn.execute(
            "INSERT INTO chat_messages (group_id, user_id, is_ai_prompt, message_text,"
            " created_at) VALUES (?, ?, 1, 'x', '2026-08-03T00:00:00+00:00')",
            (group_id, user_id),
        )
    conn.execute(
        "CREATE TABLE quota_usage (scope_type TEXT, scope_id INTEGER, day TEXT,"
        " requests INTEGER, tokens INTEGER, cost_usd REAL, updated_at TEXT,"
        " PRIMARY KEY (scope_type, scope_id, day))"
    )
    conn.execute(
        "INSERT INTO quota_usage VALUES ('group', 1001, '2026-08-03', 2, 473, 0.0,"
        " '2026-08-03T05:00:00+00:00')"
    )
    conn.execute(
        "CREATE TABLE quota_events (id INTEGER PRIMARY KEY, at TEXT, scope_type TEXT,"
        " scope_id INTEGER, kind TEXT, reason TEXT, detail TEXT)"
    )
    conn.execute(
        "INSERT INTO quota_events (at, scope_type, scope_id, kind, reason, detail)"
        " VALUES ('2026-08-03T05:00:00+00:00', 'group', 1001, 'rate_denied', 'rate', 'x')"
    )
    conn.commit()
    conn.close()
    return path


def test_parse_metrics_text_skips_help_type_created() -> None:
    families = parse_metrics_text(SYNTHETIC_METRICS)
    assert families["qq_bot_messages_total"][("kind=ai_prompt",)] == 4.0
    # _created lines are excluded
    assert not any(name.endswith("_created") for name in families)
    assert "HELP" not in families


def test_percentile_interpolates_within_bucket() -> None:
    buckets = [(0.1, 2.0), (0.5, 3.0), (1.0, 4.0)]
    assert percentile(buckets, 4.0, 0.50) == pytest.approx(0.1)
    assert percentile(buckets, 4.0, 0.95) == pytest.approx(0.9)
    assert percentile(buckets, 0.0, 0.5) is None
    assert percentile([], 4.0, 0.5) is None


def test_aggregate_metrics_counts_and_hashes(db: Path) -> None:
    db_agg = aggregate_db(db)
    report = aggregate_metrics(parse_metrics_text(SYNTHETIC_METRICS), db_agg)
    # schema
    assert report.schema_version == 1
    assert report.captured_at
    # messages
    assert report.messages["metric_total"] == 5
    assert report.messages["by_kind"] == {"ai_prompt": 4, "plain": 1}
    assert report.messages["stored_total"] == 3
    assert report.messages["groups"]["count"] == 2
    assert report.messages["users"]["count"] == 2
    # commands
    assert report.commands["total"] == 3
    assert report.commands["by_command"] == {"配额": 2, "最近故障": 1}
    # ai + latency
    assert report.ai["requests"] == 4
    assert report.ai["latency_source"] == "span.msg.total"
    assert report.ai["e2e_p50_ms"] == pytest.approx(100.0)
    assert report.ai["e2e_p95_ms"] == pytest.approx(900.0)
    # search: triggered observed, no_result honestly absent
    assert report.search["triggered"] == 3
    assert "search.no_result" in report.not_observed
    # reliability / tokens / cost / quota
    assert report.reliability == {"fallbacks": 2, "retries": 1, "errors": 3}
    assert report.tokens["prompt"] == 420
    assert report.tokens["completion"] == 53
    assert report.tokens["total"] == 473
    assert report.cost_usd["total"] == 0.0
    assert report.quota["denied_total"] == 2
    assert report.quota["by_reason"] == {"rate": 2}
    assert report.quota["usage"]["requests"] == 2
    assert report.quota["usage"]["tokens"] == 473
    assert report.quota["usage"]["events"] == 1
    assert "quota.denied" not in report.not_observed
    assert "tokens" not in report.not_observed


def test_aggregate_metrics_hashed_dimensions_never_leak_ids(db: Path) -> None:
    db_agg = aggregate_db(db)
    report = aggregate_metrics(parse_metrics_text(SYNTHETIC_METRICS), db_agg)
    blob = json.dumps(report.model_dump(), ensure_ascii=False)
    assert "1001" not in blob
    assert "2001" not in blob
    assert "2002" not in blob
    groups = report.messages["groups"]
    users = report.messages["users"]
    assert len(groups["hash"]) == 64
    assert groups["hash"] != users["hash"]


def test_not_observed_semantics_when_metrics_empty(db: Path) -> None:
    db_agg = aggregate_db(db)
    report = aggregate_metrics({}, db_agg)
    for field in (
        "messages",
        "commands",
        "ai.requests",
        "ai.e2e_latency",
        "search.triggered",
        "reliability.fallbacks",
        "tokens",
        "cost_usd",
        "quota.denied",
    ):
        assert field in report.not_observed, field
    # db-derived counts still present
    assert report.messages["stored_total"] == 3
    # nothing fabricated as zero
    assert "total" not in report.cost_usd
    assert "requests" not in report.ai


def test_aggregate_db_quota_tables_absent_still_returns_counts(db: Path) -> None:
    db_agg = aggregate_db(db)
    assert db_agg["quota"]["events"] == 1


def test_compute_uptime_days_from_state(tmp_path: Path) -> None:
    state = {"schema_version": 1, "first_seen": "2026-08-01T00:00:00+00:00"}
    from datetime import UTC, datetime

    now = datetime(2026, 8, 3, 0, 0, tzinfo=UTC)
    assert compute_uptime_days(state, now) == 2.0
    assert compute_uptime_days({}, now) is None


def test_load_state_returns_empty_for_missing_or_broken(tmp_path: Path) -> None:
    assert load_state(tmp_path / "missing.json") == {}
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert load_state(broken) == {}


def test_main_no_write_prints_report_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], db: Path
) -> None:
    metrics_file = tmp_path / "metrics.txt"
    metrics_file.write_text(SYNTHETIC_METRICS, encoding="utf-8")
    out_dir = tmp_path / "reports"
    rc = main(
        [
            "--metrics-file",
            str(metrics_file),
            "--db",
            str(db),
            "--output-dir",
            str(out_dir),
            "--no-write",
        ]
    )
    assert rc == 0
    captured = json.loads(capsys.readouterr().out)
    assert captured["schema_version"] == 1
    assert captured["messages"]["metric_total"] == 5
    assert not out_dir.exists()
    assert not (out_dir / "metrics-state.json").exists()


def test_main_writes_report_and_state(tmp_path: Path, db: Path) -> None:
    metrics_file = tmp_path / "metrics.txt"
    metrics_file.write_text(SYNTHETIC_METRICS, encoding="utf-8")
    out_dir = tmp_path / "reports"
    rc = main(
        [
            "--metrics-file",
            str(metrics_file),
            "--db",
            str(db),
            "--output-dir",
            str(out_dir),
        ]
    )
    assert rc == 0
    reports = sorted(out_dir.glob("metrics-[0-9]*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["schema_version"] == 1
    assert report["uptime_days"] == 0.0  # first collection
    state = json.loads((out_dir / "metrics-state.json").read_text(encoding="utf-8"))
    assert state["first_seen"] == state["last_collection"]

    # second collection accumulates uptime (simulate elapsed time)
    from datetime import UTC, datetime, timedelta

    state_path = out_dir / "metrics-state.json"
    older = json.loads(state_path.read_text(encoding="utf-8"))
    older["first_seen"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    state_path.write_text(json.dumps(older, ensure_ascii=False), encoding="utf-8")
    rc = main(
        [
            "--metrics-file",
            str(metrics_file),
            "--db",
            str(db),
            "--output-dir",
            str(out_dir),
        ]
    )
    assert rc == 0
    report2 = json.loads(
        sorted(out_dir.glob("metrics-[0-9]*.json"))[-1].read_text(encoding="utf-8")
    )
    assert report2["uptime_days"] == pytest.approx(2.0, abs=0.01)


def test_main_both_sources_fail_exits_nonzero(tmp_path: Path) -> None:
    rc = main(
        [
            "--metrics-file",
            str(tmp_path / "nope.txt"),
            "--db",
            str(tmp_path / "nope.sqlite3"),
            "--output-dir",
            str(tmp_path),
            "--no-write",
        ]
    )
    assert rc == 1


def test_report_schema_is_pydantic_stable() -> None:
    report = MetricsReport(
        captured_at="2026-08-03T00:00:00+00:00",
        source={"metrics_ok": False, "db_ok": False},
        not_observed=["metrics"],
    )
    data = report.model_dump()
    assert data["schema_version"] == 1
    assert isinstance(data, dict)
