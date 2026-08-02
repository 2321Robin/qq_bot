"""Refresh orchestration tests (Task 7) — tmp dirs + fake fetcher only, offline."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


from qq_bot.config import BotSettings
from urllib.parse import quote as _quote

from qq_bot.datapipeline.fetch import FetchResponse
from qq_bot.datapipeline.manifest import load_manifest
from qq_bot.datapipeline.publish import RefreshArgs, run_refresh
from qq_bot.datapipeline.validation import validate_directory

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "data_pipeline"
DETAILS_DIR = FIXTURE_ROOT / "details"
INVALID_DIR = FIXTURE_ROOT / "invalid"
MANIFESTS_DIR = FIXTURE_ROOT / "manifests"

VALID_FILES = sorted(
    path.name for path in DETAILS_DIR.glob("1*.json") if path.name != "107-BadPet.json"
)

LENIENT = BotSettings(
    data_min_records=1,
    data_max_record_drop=100,
    data_max_new_number_gaps=100,
    data_min_stats_complete_rate=0.0,
    data_min_total_race_rate=0.0,
    data_max_dangling_edges=100,
    data_max_skill_key_missing_rate=1.0,
)


ROOT = Path(__file__).resolve().parents[1]


def test_cli_online_path_resolves_scripts_package(tmp_path: Path) -> None:
    """The CLI entry must resolve scripts.fetch_roco_pet_detail when run as
    `python scripts/refresh_roco_data.py` (regression: only ROOT/src was on
    sys.path, so online mode died with ModuleNotFoundError before any fetch).

    Uses an unreachable localhost URL: import resolution is proven without
    any network access.
    """
    result = subprocess.run(
        [
            sys.executable,
            "scripts/refresh_roco_data.py",
            "--index-url",
            "http://127.0.0.1:1/index",
            "--details-dir",
            str(DETAILS_DIR),
            "--manifest-dir",
            str(tmp_path / "manifests"),
            "--reports-dir",
            str(tmp_path / "reports"),
            "--quarantine-dir",
            str(tmp_path / "quarantine"),
            "--staging-dir",
            str(tmp_path / "staging"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert "No module named 'scripts.fetch_roco_pet_detail'" not in result.stderr
    assert "Failed to fetch index" in result.stderr
    assert result.returncode == 1


def _raw_template(name: str, number: str = "101", extra: str = "") -> str:
    return f"""{{{{精灵信息
    |精灵名称={name}
    |主属性=光
    |生命=120
    |物攻=80
    |魔攻=80
    |物防=105
    |魔防=105
    |速度=92
    |进化条件=无法进化
    {extra}
    }}}}"""


def _index_html(names: list[tuple[str, str]]) -> str:
    rows = "\n".join(
        f"<tr><td>{name}</td><td>{number}</td><td>光</td></tr>" for name, number in names
    )
    return "<table><tr><th>精灵名称</th><th>精灵编号</th><th>系别</th></tr>" + rows + "</table>"


class FakeFetcher:
    def __init__(self, responses: dict[str, FetchResponse]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def fetch(self, url: str, headers: dict[str, str] | None = None) -> FetchResponse:
        self.calls.append(url)
        return self.responses[url]


def _run(args: RefreshArgs, **flags: object) -> int:
    """run_refresh with RefreshArgs fields as keyword flags."""
    kwargs: dict[str, object] = {}
    for key, value in flags.items():
        if key in {"settings", "fetcher", "publish_hook"}:
            kwargs[key] = value
        else:
            setattr(args, key, value)
    return run_refresh(args, **kwargs)


def _args(tmp_path: Path, **overrides: object) -> RefreshArgs:
    base = {
        "details_dir": tmp_path / "details",
        "manifest_dir": tmp_path / "manifests",
        "reports_dir": tmp_path / "reports",
        "quarantine_dir": tmp_path / "quarantine",
        "staging_dir": tmp_path / ".staging",
        "index_path": tmp_path / "roco_search.sqlite3",
        "index_url": "https://example.com/index",
        "no_normalize": True,
        "no_cards": True,
        "no_index": True,
    }
    base.update(overrides)
    return RefreshArgs(**base)


def _setup(tmp_path: Path) -> RefreshArgs:
    details = tmp_path / "details"
    details.mkdir()
    for path in sorted(DETAILS_DIR.glob("1*.json")):
        if path.name != "107-BadPet.json":
            shutil.copy2(path, details / path.name)
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    shutil.copy2(MANIFESTS_DIR / "previous.json", manifests / "previous.json")
    return _args(tmp_path)


def _latest(tmp_path: Path) -> None:
    manifests = tmp_path / "manifests"
    manifests.mkdir(exist_ok=True)
    shutil.copy2(MANIFESTS_DIR / "previous.json", manifests / "latest.json")


def _report_paths(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "reports").glob("refresh-*.json"))


def _pet_url(name: str) -> str:
    return f"https://wiki.biligame.com/rocom/{_quote(name)}"


def test_full_refresh_chain_publishes_and_reports(tmp_path: Path) -> None:
    args = _setup(tmp_path)
    changed = _raw_template("测试宠物A", "101", extra="|精灵描述=新版描述")
    fetcher = FakeFetcher(
        {
            "https://example.com/index": FetchResponse(
                200,
                {},
                _index_html([(f"测试宠物{chr(ord('A') + i)}", str(101 + i)) for i in range(6)]),
            ),
            _pet_url("测试宠物A"): FetchResponse(200, {"etag": '"v2"'}, changed),
            **{
                _pet_url(f"测试宠物{chr(ord('A') + i)}"): FetchResponse(304, {}, "")
                for i in range(1, 6)
            },
        }
    )
    code = _run(args, fetcher=fetcher, settings=LENIENT)
    assert code == 0

    assert (args.manifest_dir / "latest.json").exists()
    assert (args.manifest_dir / "change_set.json").exists()
    manifest = load_manifest(args.manifest_dir / "latest.json")
    assert "101-测试宠物A.json" in manifest.entries
    assert manifest.checks["record_count_floor"] == 6.0
    assert manifest.checks["record_count_floor_threshold"] == 1.0

    published = json.loads((args.details_dir / "101-测试宠物A.json").read_text(encoding="utf-8"))
    assert published["profile"]["精灵描述"] == "新版描述"

    reports = _report_paths(tmp_path)
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["gate_failed"] is False
    assert report["forms"]["modified"] == ["101-测试宠物A.json"]
    md = reports[0].with_suffix(".md")
    assert md.exists()
    assert "新增 0 个形态" in md.read_text(encoding="utf-8")

    change_set = json.loads((args.manifest_dir / "change_set.json").read_text(encoding="utf-8"))
    assert change_set["modified"] == ["101-测试宠物A.json"]
    assert set(change_set["unchanged"]) == {
        f"{n}-测试宠物{chr(ord('A') + n - 101)}.json" for n in range(102, 107)
    }
    assert change_set["details"]["101-测试宠物A.json"]["skills"]["added"] == 0


def test_all_304_no_writes_and_zero_added_forms(tmp_path: Path) -> None:
    args = _setup(tmp_path)
    fetcher = FakeFetcher(
        {
            "https://example.com/index": FetchResponse(
                200,
                {},
                _index_html([(f"测试宠物{chr(ord('A') + i)}", str(101 + i)) for i in range(6)]),
            ),
            **{
                _pet_url(f"测试宠物{chr(ord('A') + i)}"): FetchResponse(304, {}, "")
                for i in range(6)
            },
        }
    )
    code = _run(args, fetcher=fetcher, settings=LENIENT)
    assert code == 0
    fetch_dir = args.staging_dir / "fetch"
    assert not fetch_dir.exists() or list(fetch_dir.glob("*.json")) == []
    report = json.loads(_report_paths(tmp_path)[0].read_text(encoding="utf-8"))
    assert report["forms"]["added"] == []
    assert report["forms"]["modified"] == []
    change_set = json.loads((args.manifest_dir / "change_set.json").read_text(encoding="utf-8"))
    assert len(change_set["unchanged"]) == 6


def test_corrupted_unchanged_file_blocks_publish(tmp_path: Path) -> None:
    args = _setup(tmp_path)
    _latest(tmp_path)
    # Hash in previous.json is unchanged; content on disk is corrupt.
    (args.details_dir / "101-测试宠物A.json").write_text(
        json.dumps(
            {
                "name": "测试宠物A",
                "source_url": "https://example.com/pets/101",
                "attributes": ["合成属性"],
                "evolution_condition": "无法进化",
                "total_race_value": 123,
                "profile": {"编号": "101"},
                "stats": {"未知": 1},
                "metadata": {"parser_version": 6},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    latest_before = (args.manifest_dir / "latest.json").read_bytes()
    code = _run(args, settings=LENIENT)
    assert code == 1
    assert (args.manifest_dir / "latest.json").read_bytes() == latest_before
    assert (args.manifest_dir / "change_set.json").exists() is False
    report = json.loads(_report_paths(tmp_path)[0].read_text(encoding="utf-8"))
    assert report["gate_failed"] is True
    assert "101-测试宠物A.json" in report["quarantine"]


def test_gate_failure_preserves_latest_and_exits_1(tmp_path: Path) -> None:
    args = _setup(tmp_path)
    _latest(tmp_path)
    latest_before = (args.manifest_dir / "latest.json").read_bytes()
    details_before = {
        path.name: path.read_bytes() for path in sorted(args.details_dir.glob("*.json"))
    }
    # Default thresholds: 6 records < 500 -> record_count_floor fails.
    code = _run(args, settings=BotSettings())
    assert code == 1
    assert (args.manifest_dir / "latest.json").read_bytes() == latest_before
    assert {
        path.name: path.read_bytes() for path in sorted(args.details_dir.glob("*.json"))
    } == details_before
    report = json.loads(_report_paths(tmp_path)[0].read_text(encoding="utf-8"))
    assert report["gate_failed"] is True
    assert any(g["name"] == "record_count_floor" and not g["passed"] for g in report["gates"])


def test_pre_existing_trusted_quarantine_blocks_but_allow_quarantine_passes(
    tmp_path: Path,
) -> None:
    args = _setup(tmp_path)
    args.quarantine_dir.mkdir(exist_ok=True)
    # A file that WAS trusted (tracked in previous.json) sits in quarantine.
    shutil.copy2(DETAILS_DIR / "102-测试宠物B.json", args.quarantine_dir / "102-测试宠物B.json")
    code = _run(args, settings=LENIENT)
    assert code == 1
    assert (args.manifest_dir / "latest.json").exists() is False

    report = json.loads(_report_paths(tmp_path)[0].read_text(encoding="utf-8"))
    assert report["allow_quarantine"] is False
    quarantine_gate = next(g for g in report["gates"] if g["name"] == "quarantine_empty")
    assert quarantine_gate["passed"] is False

    code = _run(args, settings=LENIENT, offline=True, allow_quarantine=True)
    assert code == 0
    assert (args.manifest_dir / "latest.json").exists()
    report = json.loads(_report_paths(tmp_path)[-1].read_text(encoding="utf-8"))
    assert report["allow_quarantine"] is True


def test_allow_quarantine_does_not_waive_other_gates(tmp_path: Path) -> None:
    args = _setup(tmp_path)
    args.quarantine_dir.mkdir(exist_ok=True)
    shutil.copy2(DETAILS_DIR / "102-测试宠物B.json", args.quarantine_dir / "102-测试宠物B.json")
    code = _run(args, settings=BotSettings(), offline=True)
    assert code == 1  # record_count_floor still fails with default thresholds


def test_dry_run_touches_nothing_official(tmp_path: Path) -> None:
    args = _setup(tmp_path)
    details_before = {
        path.name: path.read_bytes() for path in sorted(args.details_dir.glob("*.json"))
    }
    code = _run(args, settings=LENIENT, dry_run=True)
    assert code == 0
    assert {
        path.name: path.read_bytes() for path in sorted(args.details_dir.glob("*.json"))
    } == details_before
    assert (args.manifest_dir / "latest.json").exists() is False
    assert (args.manifest_dir / "change_set.json").exists() is False
    assert len(_report_paths(tmp_path)) == 1  # report still written (S3-DIFF-05)


def test_offline_mode_never_calls_fetcher(tmp_path: Path) -> None:
    args = _setup(tmp_path)
    fetcher = FakeFetcher({})
    code = _run(args, fetcher=fetcher, settings=LENIENT, offline=True)
    assert code == 0
    assert fetcher.calls == []
    assert (args.manifest_dir / "latest.json").exists()
    report = json.loads(_report_paths(tmp_path)[0].read_text(encoding="utf-8"))
    assert len(report["forms"]["added"]) == 0
    assert len(report["forms"]["modified"]) == 0


def test_prune_removed_deletes_only_with_flag(tmp_path: Path) -> None:
    args = _setup(tmp_path)
    manifests = args.manifest_dir
    previous = load_manifest(manifests / "previous.json")
    from qq_bot.datapipeline.manifest import ManifestEntry

    extra = previous.model_copy(deep=True)
    extra.entries["200-远古宠物.json"] = ManifestEntry(
        source_url="https://example.com/pets/200",
        sha256="0" * 64,
        size=0,
        parser_version=6,
        fetched_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    (manifests / "previous.json").write_text(
        extra.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    # A leftover file that the index no longer lists (valid content).
    legacy = json.loads((DETAILS_DIR / "103-测试宠物C.json").read_text(encoding="utf-8"))
    legacy["name"] = "远古宠物"
    legacy["profile"]["编号"] = "200"
    (args.details_dir / "200-远古宠物.json").write_text(
        json.dumps(legacy, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # The index no longer lists 200: all six pet fetches answer 304.
    responses = {
        "https://example.com/index": FetchResponse(
            200,
            {},
            _index_html(
                [
                    ("测试宠物A", "101"),
                    ("测试宠物B", "102"),
                    ("测试宠物C", "103"),
                    ("测试宠物D", "104"),
                    ("测试宠物E", "105"),
                    ("测试宠物F", "106"),
                ]
            ),
        )
    }
    for name in ["测试宠物A", "测试宠物B", "测试宠物C", "测试宠物D", "测试宠物E", "测试宠物F"]:
        responses[_pet_url(name)] = FetchResponse(304, {}, "")
    fetcher = FakeFetcher(responses)

    code = _run(args, fetcher=fetcher, settings=LENIENT)
    assert code == 0
    # Default: removed files are kept on disk; the removal is still reported.
    assert (args.details_dir / "200-远古宠物.json").exists()
    change_set = json.loads((args.manifest_dir / "change_set.json").read_text(encoding="utf-8"))
    assert "200-远古宠物.json" in change_set["removed"]

    (args.details_dir / "200-远古宠物.json").unlink()
    (args.manifest_dir / "latest.json").unlink()
    (args.manifest_dir / "change_set.json").unlink()
    fetcher2 = FakeFetcher(responses)
    code = _run(args, fetcher=fetcher2, settings=LENIENT, prune_removed=True)
    assert code == 0
    assert (args.details_dir / "200-远古宠物.json").exists() is False


def test_offline_detects_removed_from_disk(tmp_path: Path) -> None:
    args = _setup(tmp_path)
    manifests = args.manifest_dir
    previous = load_manifest(manifests / "previous.json")
    from qq_bot.datapipeline.manifest import ManifestEntry

    extra = previous.model_copy(deep=True)
    extra.entries["200-远古宠物.json"] = ManifestEntry(
        source_url="https://example.com/pets/200",
        sha256="0" * 64,
        size=0,
        parser_version=6,
        fetched_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    (manifests / "previous.json").write_text(
        extra.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    code = _run(args, settings=LENIENT, offline=True)
    assert code == 0
    change_set = json.loads((args.manifest_dir / "change_set.json").read_text(encoding="utf-8"))
    assert "200-远古宠物.json" in change_set["removed"]


def test_exception_during_publish_exits_2_and_leaves_dir_untouched(tmp_path: Path) -> None:
    args = _setup(tmp_path)
    details_before = {
        path.name: path.read_bytes() for path in sorted(args.details_dir.glob("*.json"))
    }

    def boom(step: str) -> None:
        raise RuntimeError("injected publish failure")

    code = _run(args, settings=LENIENT, offline=True, publish_hook=boom)
    assert code == 2
    assert {
        path.name: path.read_bytes() for path in sorted(args.details_dir.glob("*.json"))
    } == details_before
    assert (args.manifest_dir / "latest.json").exists() is False


def test_index_fetch_failure_exits_1(tmp_path: Path) -> None:
    args = _setup(tmp_path)
    fetcher = FakeFetcher({"https://example.com/index": FetchResponse(500, {}, "boom")})
    code = _run(args, fetcher=fetcher, settings=LENIENT)
    assert code == 1
    assert (args.manifest_dir / "latest.json").exists() is False


def test_verify_only_checks_consistency(tmp_path: Path) -> None:
    args = _setup(tmp_path)
    assert _run(args, verify_only=True) == 0
    (args.details_dir / "101-测试宠物A.json").write_text("{}", encoding="utf-8")
    assert _run(args, verify_only=True) == 1


def test_publish_with_normalize_stays_consistent(tmp_path: Path) -> None:
    args = _setup(tmp_path)
    code = _run(args, settings=LENIENT, offline=True, no_normalize=False)
    assert code == 0
    manifest = load_manifest(args.manifest_dir / "latest.json")
    from qq_bot.datapipeline.manifest import verify_manifest

    assert verify_manifest(manifest, args.details_dir) == []
    validation = validate_directory(args.details_dir, tmp_path / "quarantine2")
    assert validation.quarantined == {}
    assert len(validation.ok) == 6


def test_107_untracked_bad_file_does_not_block(tmp_path: Path) -> None:
    args = _setup(tmp_path)
    shutil.copy2(INVALID_DIR / "107-BadPet.json", args.details_dir / "107-BadPet.json")
    code = _run(args, settings=LENIENT, offline=True)
    assert code == 0
    # 107 left the official dir for quarantine (S3-SCHEMA-03).
    assert (args.details_dir / "107-BadPet.json").exists() is False
    assert (args.quarantine_dir / "107-BadPet.json").exists()
    assert (args.quarantine_dir / "107-BadPet.error.json").exists()
    report = json.loads(_report_paths(tmp_path)[0].read_text(encoding="utf-8"))
    assert report["gate_failed"] is False
    manifest = load_manifest(args.manifest_dir / "latest.json")
    assert "107-BadPet.json" not in manifest.entries


def test_clear_record_caches_invalidates_lru_cache(monkeypatch: object) -> None:
    """S3-INCR-07: hot-reload hooks exist and clear the lru_caches."""
    from qq_bot.services import roco_pets, roco_skills

    calls = {"pets": 0, "skills": 0}

    def fake_pets(*args: object, **kwargs: object) -> list:
        calls["pets"] += 1
        return []

    def fake_skills(*args: object, **kwargs: object) -> list:
        calls["skills"] += 1
        return []

    roco_pets.clear_record_caches()
    roco_skills.clear_record_caches()
    monkeypatch.setattr(roco_pets, "load_pet_records", fake_pets)
    monkeypatch.setattr(roco_skills, "load_skill_records", fake_skills)

    roco_pets.get_pet_records()
    roco_pets.get_pet_records()
    roco_skills.get_skill_records()
    roco_skills.get_skill_records()
    assert calls == {"pets": 1, "skills": 1}

    roco_pets.clear_record_caches()
    roco_skills.clear_record_caches()
    roco_pets.get_pet_records()
    roco_skills.get_skill_records()
    assert calls == {"pets": 2, "skills": 2}


def test_refresh_never_calls_clear_record_caches(tmp_path: Path, monkeypatch: object) -> None:
    """S3-INCR-07: the refresh pipeline must not invalidate service caches itself."""
    from qq_bot.services import roco_pets, roco_skills

    called: list[str] = []
    monkeypatch.setattr(roco_pets, "clear_record_caches", lambda: called.append("pets"))
    monkeypatch.setattr(roco_skills, "clear_record_caches", lambda: called.append("skills"))

    args = _setup(tmp_path)
    code = _run(args, settings=LENIENT, offline=True)
    assert code == 0
    assert called == []


def test_publish_builds_search_index_when_enabled(tmp_path: Path) -> None:
    args = _setup(tmp_path)
    args.no_index = False
    code = _run(args, settings=LENIENT, offline=True)
    assert code == 0
    assert args.index_path.exists()
    from qq_bot.datapipeline.index import RocoSearchIndex

    index = RocoSearchIndex.open(args.index_path)
    assert index is not None
    hits = index.search_pets("测试宠物A")
    assert hits and hits[0]["name"] == "测试宠物A"
