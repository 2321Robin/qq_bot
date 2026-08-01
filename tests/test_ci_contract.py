"""CI workflow and pre-commit configuration contract tests (S1-CI-01..03, S1-PRE)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_FLOATING_REFS = {"main", "master", "latest", "HEAD", "dev", "develop"}


def _read_yaml(relative: str) -> dict:
    path = ROOT / relative
    assert path.exists(), f"missing {relative}"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _workflow() -> dict:
    return _read_yaml(".github/workflows/ci.yml")


def test_workflow_has_expected_triggers() -> None:
    workflow = _workflow()
    # PyYAML 1.x parses the YAML key `on:` as boolean True
    on = workflow.get("on") or workflow.get(True)
    assert on is not None
    assert "pull_request" in on
    assert "push" in on
    assert "workflow_dispatch" in on


def test_workflow_minimal_permissions_and_concurrency() -> None:
    workflow = _workflow()
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] is True


def test_every_job_has_a_timeout() -> None:
    jobs = _workflow()["jobs"]
    assert jobs
    for name, job in jobs.items():
        assert job.get("timeout-minutes"), f"job {name!r} has no timeout-minutes"


def test_test_job_matrix_covers_311_and_312() -> None:
    test_job = _workflow()["jobs"]["test"]
    matrix = test_job["strategy"]["matrix"]
    assert matrix["python-version"] == ["3.11", "3.12"]
    assert test_job["strategy"]["fail-fast"] is False


def test_quality_job_runs_formatters_linters_and_hooks() -> None:
    runs = " ".join(step.get("run", "") for step in _workflow()["jobs"]["quality"]["steps"])
    assert "pre_commit run --all-files" in runs
    assert "ruff check ." in runs
    assert "ruff format --check ." in runs


def test_test_job_runs_pytest_and_branch_coverage_on_one_version() -> None:
    steps = _workflow()["jobs"]["test"]["steps"]
    runs = [step.get("run", "") for step in steps]
    assert any("pytest -q" in run for run in runs)
    coverage = [step for step in steps if "--cov=qq_bot" in step.get("run", "")]
    assert coverage, "no coverage step in test job"
    coverage_run = coverage[0]["run"]
    assert "--cov-branch" in coverage_run
    assert "--cov-report=term-missing" in coverage_run
    assert "--cov-report=xml" in coverage_run
    # coverage must run on a single baseline version, not the whole matrix
    assert coverage[0].get("if") not in (None, "")


def test_coverage_artifact_is_uploaded() -> None:
    steps = _workflow()["jobs"]["test"]["steps"]
    uploads = [
        step for step in steps if step.get("uses", "").startswith("actions/upload-artifact@")
    ]
    assert uploads, "no coverage artifact upload"
    assert "coverage.xml" in uploads[0].get("with", {}).get("path", "")


def test_security_job_runs_redacted_gitleaks_full_history() -> None:
    steps = _workflow()["jobs"]["security"]["steps"]
    runs = " ".join(step.get("run", "") for step in steps)
    assert "gitleaks" in runs
    assert "--redact" in runs
    assert "--no-banner" in runs
    assert steps[0]["with"]["fetch-depth"] == 0
    # the security job must not upload raw (unredacted) scan reports
    for step in steps:
        assert "upload-artifact" not in step.get("uses", "")


def test_container_job_builds_and_smoke_tests_image() -> None:
    runs = " ".join(step.get("run", "") for step in _workflow()["jobs"]["container"]["steps"])
    assert "docker build" in runs
    assert "docker compose config" in runs
    assert "/healthz" in runs
    assert "/readyz" in runs
    # non-root check must compare the image user against root
    assert "Config.User" in runs
    assert '!= "root"' in runs


def test_third_party_actions_pinned_to_full_sha() -> None:
    for job in _workflow()["jobs"].values():
        for step in job["steps"]:
            uses = step.get("uses")
            if not uses:
                continue
            _, _, ref = uses.partition("@")
            assert ref, f"action {uses!r} has no pinned revision"
            assert _SHA40.match(ref), f"action {uses!r} is not pinned to a 40-char SHA"


def test_pre_commit_config_has_required_hooks() -> None:
    config = _read_yaml(".pre-commit-config.yaml")
    hook_ids = {hook["id"] for repo in config["repos"] for hook in repo["hooks"]}
    required = {
        "ruff",
        "ruff-format",
        "trailing-whitespace",
        "end-of-file-fixer",
        "check-yaml",
        "check-toml",
        "check-merge-conflict",
        "check-added-large-files",
        "detect-private-key",
    }
    assert required <= hook_ids


def test_pre_commit_revisions_are_pinned() -> None:
    config = _read_yaml(".pre-commit-config.yaml")
    for repo in config["repos"]:
        if repo.get("repo") == "local":
            continue
        rev = repo.get("rev", "")
        assert rev, f"repo {repo.get('repo')!r} has no pinned rev"
        assert rev not in _FLOATING_REFS


def test_pre_commit_local_gitleaks_hook_is_redacted() -> None:
    config = _read_yaml(".pre-commit-config.yaml")
    local = [repo for repo in config["repos"] if repo.get("repo") == "local"]
    assert local, "no local gitleaks hook"
    entries = [hook.get("entry", "") for repo in local for hook in repo["hooks"]]
    gitleaks = [entry for entry in entries if "gitleaks" in entry]
    assert gitleaks
    assert "--redact" in gitleaks[0]
    assert "--pre-commit" in gitleaks[0]


def test_pyproject_declares_dev_deps_and_coverage_config() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for dependency in ("aiosqlite", "tenacity", "pytest-cov", "pre-commit", "pyyaml"):
        assert dependency in text, f"missing dev/runtime dependency {dependency}"
    assert 'source = ["qq_bot"]' in text
    assert "branch = true" in text
    assert "relative_files = true" in text
    assert "show_missing = true" in text
    assert "fail_under = " in text, "coverage fail_under must be set from the real baseline"
    assert "omit" not in text, "core modules must not be excluded from coverage"


def test_gitignore_covers_coverage_and_container_outputs() -> None:
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for entry in (".coverage", "coverage.xml", "htmlcov/", "container-data/"):
        assert entry in text
