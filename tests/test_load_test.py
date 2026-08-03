"""Synthetic load test script tests (S4-LOAD-01..06).

All offline: the script's own imports touch nothing but local modules and
fixtures, and we additionally prove no network by patching httpx to raise.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from scripts.run_load_test import LoadTestReport, main


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any accidental network call fails the run loudly."""

    def boom(*args, **kwargs):
        raise AssertionError("load test must never touch the network")

    monkeypatch.setattr(httpx.AsyncClient, "post", boom)
    monkeypatch.setattr(httpx.AsyncClient, "get", boom)
    monkeypatch.setattr(httpx.AsyncClient, "request", boom)


@pytest.mark.asyncio
async def test_cli_runs_offline_small(no_network) -> None:
    exit_code = await _run(["--cases", "10", "--concurrency", "2", "--no-write"])
    assert exit_code == 0


def test_main_returns_zero_without_writing(no_network, capsys) -> None:
    exit_code = main(["--cases", "2", "--concurrency", "1", "--no-write"])
    assert exit_code == 0
    out = capsys.readouterr().out
    report = json.loads(out[out.index("{") :])
    _assert_report_shape(report)


def test_main_writes_json_and_markdown_reports(no_network, tmp_path: Path) -> None:
    exit_code = main(["--cases", "2", "--concurrency", "1", "--output-dir", str(tmp_path)])
    assert exit_code == 0
    files = sorted(tmp_path.glob("loadtest-*.json")) + sorted(tmp_path.glob("loadtest-*.md"))
    assert len(files) == 2
    json_file = next(tmp_path.glob("loadtest-*.json"))
    payload = json.loads(json_file.read_text(encoding="utf-8"))
    _assert_report_shape(payload)
    # Pydantic round-trip: the on-disk report satisfies the fixed schema
    LoadTestReport.model_validate(payload)
    md = next(tmp_path.glob("loadtest-*.md")).read_text(encoding="utf-8")
    assert "合成负载" in md
    assert "PostgreSQL：不引入" in md
    assert "Redis：不引入" in md


def _assert_report_shape(report: dict) -> None:
    assert report["schema_version"] == 1
    assert report["mode"] == "synthetic"
    assert report["created_at"]
    assert report["env"]["concurrency"] >= 1
    names = {scenario["name"] for scenario in report["scenarios"]}
    assert names == {"local_knowledge", "web_search", "chat_memory", "direct_chat", "mixed"}
    for scenario in report["scenarios"]:
        assert scenario["cases"] == scenario["ok"] + scenario["errors"]
        assert scenario["e2e_p50_ms"] >= 0
        assert scenario["e2e_p95_ms"] >= 0
        assert scenario["throughput_req_per_s"] > 0
        assert scenario["phases"], "per-phase P50/P95 must be present"
        for phase, timing in scenario["phases"].items():
            assert timing["p50_ms"] >= 0 and timing["p95_ms"] >= 0
    assert report["conclusion"]["postgresql_recommended"] is False
    assert report["conclusion"]["redis_recommended"] is False
    assert len(report["conclusion"]["trigger_conditions"]) >= 3
    assert report["disclaimer"]


async def _run(args: list[str]) -> int:
    import asyncio

    return await asyncio.to_thread(main, args)
