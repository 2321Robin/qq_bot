"""Refresh diff report tests (S3-DIFF) — fixtures and tmp dirs only, offline."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from qq_bot.datapipeline.diff import (
    RefreshDiff,
    compute_diff,
    edge_signature,
    render_markdown,
    row_signature,
)
from qq_bot.datapipeline.manifest import RefreshManifest, build_manifest, load_manifest
from qq_bot.datapipeline.quality import GateResult
from qq_bot.datapipeline.schemas import PetDetail

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "data_pipeline"
DETAILS_DIR = FIXTURE_ROOT / "details"
MANIFESTS_DIR = FIXTURE_ROOT / "manifests"

_REFRESHED_AT = "2026-02-02T00:00:00+00:00"


def _copy_fixtures(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for path in sorted(DETAILS_DIR.glob("1*.json")):
        if path.name == "107-BadPet.json":
            continue
        shutil.copy2(path, dest / path.name)


def _validated(details_dir: Path) -> dict[str, PetDetail]:
    return {
        path.name: PetDetail.model_validate(json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(details_dir.glob("*.json"))
    }


def _previous() -> RefreshManifest:
    return load_manifest(MANIFESTS_DIR / "previous.json")


def _build(details_dir: Path, previous: RefreshManifest | None) -> RefreshManifest:
    from datetime import datetime

    return build_manifest(
        details_dir,
        index_url="https://example.com/pets/index",
        refreshed_at=datetime.fromisoformat(_REFRESHED_AT),
        parser_version=6,
        previous=previous,
    )


def test_row_signature_normalizes_whitespace() -> None:
    assert row_signature({"等级": " 技能 ", "技能": "技能A"}) == row_signature(
        {"等级": "技能", "技能": "技能A"}
    )


def test_row_signature_changes_with_content() -> None:
    assert row_signature({"技能": "技能A", "效果": "效果A"}) != row_signature(
        {"技能": "技能A", "效果": "效果B"}
    )


def test_edge_signature_condition_change() -> None:
    assert edge_signature({"source": "A", "target": "B", "condition": "16级"}) != edge_signature(
        {"source": "A", "target": "B", "condition": "32级"}
    )


def _changed_scenario(tmp_path: Path) -> tuple[Path, Path, dict[str, PetDetail]]:
    """101 gains one skill row (row-level modification), 102 is removed,
    108 is added.  Snapshot dir holds the original 6 files."""
    new_dir = tmp_path / "new"
    snapshot_dir = tmp_path / "previous_files"
    _copy_fixtures(new_dir)
    _copy_fixtures(snapshot_dir)

    modified = json.loads((new_dir / "101-测试宠物A.json").read_text(encoding="utf-8"))
    # Same (等级, 技能) key, different 效果 -> one modified row.
    modified["skills"][0]["rows"][0]["效果"] = "✦追加效果。"
    (new_dir / "101-测试宠物A.json").write_text(
        json.dumps(modified, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (new_dir / "102-测试宠物B.json").unlink()
    added = json.loads((new_dir / "103-测试宠物C.json").read_text(encoding="utf-8"))
    added["name"] = "测试宠物G"
    added["profile"]["编号"] = "108"
    added["evolution"] = {"from": [], "to": [], "evolution_condition": "无法进化"}
    (new_dir / "108-测试宠物G.json").write_text(
        json.dumps(added, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return new_dir, snapshot_dir, _validated(new_dir)


def test_compute_diff_add_remove_modify(tmp_path: Path) -> None:
    new_dir, snapshot_dir, validated = _changed_scenario(tmp_path)
    previous = _previous()
    current = _build(new_dir, _previous())
    diff = compute_diff(previous, current, validated, previous_files_dir=snapshot_dir)

    assert diff.forms.added == ["108-测试宠物G.json"]
    assert diff.forms.removed == ["102-测试宠物B.json"]
    assert diff.forms.modified == ["101-测试宠物A.json"]
    assert diff.forms.added_urls == {"108-测试宠物G.json": "https://example.com/pets/103"}
    # 101's edited row keeps its (等级, 技能) key -> modified; 108 contributes
    # one added row.  102's removal leaves a numbering gap at 102.
    assert diff.skills.added_rows == 1
    assert diff.skills.modified_rows == 1
    assert diff.skills.removed_rows == 0
    assert diff.skills.computed
    assert "101-测试宠物A.json" in " ".join(diff.skills.details)
    assert diff.evolution.needs_confirmation == []
    assert diff.numbers.new == ["108"]
    assert diff.numbers.gone == ["102"]
    assert diff.numbers.new_gaps == ["102", "107"]


def test_compute_diff_edge_condition_change_goes_to_confirmation(tmp_path: Path) -> None:
    new_dir = tmp_path / "new"
    snapshot_dir = tmp_path / "previous_files"
    _copy_fixtures(new_dir)
    _copy_fixtures(snapshot_dir)

    modified = json.loads((new_dir / "101-测试宠物A.json").read_text(encoding="utf-8"))
    modified["evolution"]["to"][0]["condition"] = "等级达到50级"
    (new_dir / "101-测试宠物A.json").write_text(
        json.dumps(modified, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    validated = _validated(new_dir)
    current = _build(new_dir, _previous())
    diff = compute_diff(_previous(), current, validated, previous_files_dir=snapshot_dir)

    assert diff.forms.modified == ["101-测试宠物A.json"]
    assert diff.evolution.modified_edges == ["测试宠物A → 测试宠物B（等级达到50级）"]
    assert diff.evolution.needs_confirmation == ["测试宠物A → 测试宠物B（等级达到50级）"]
    assert "待确认" in render_markdown(diff)
    assert "测试宠物A → 测试宠物B" in render_markdown(diff)


def test_compute_diff_no_previous_all_added(tmp_path: Path) -> None:
    new_dir = tmp_path / "new"
    _copy_fixtures(new_dir)
    validated = _validated(new_dir)
    current = _build(new_dir, None)
    diff = compute_diff(None, current, validated, previous_files_dir=None)

    assert len(diff.forms.added) == 6
    assert diff.forms.removed == []
    assert diff.forms.modified == []
    assert all(name in diff.forms.added_urls for name in diff.forms.added)
    assert not diff.skills.computed
    assert not diff.evolution.computed
    assert diff.evolution.needs_confirmation == []
    assert sorted(diff.numbers.new) == ["101", "102", "103", "104", "105", "106"]
    assert diff.numbers.gone == []
    assert diff.previous_hash is None


def test_gate_failed_report_still_generated(tmp_path: Path) -> None:
    new_dir = tmp_path / "new"
    _copy_fixtures(new_dir)
    validated = _validated(new_dir)
    current = _build(new_dir, _previous())
    gates = [
        GateResult("record_count_floor", False, 6.0, 500.0, hard=True),
        GateResult("quarantine_empty", True, 0.0, 0.0, hard=True),
    ]
    diff = compute_diff(
        None,
        current,
        validated,
        gates=gates,
        quarantine={"107-BadPet.json": "ValidationError: stats"},
    )
    assert diff.gate_failed
    assert diff.quarantine == {"107-BadPet.json": "ValidationError: stats"}
    assert "record_count_floor: 失败" in render_markdown(diff)
    assert "Quarantine" in render_markdown(diff)


def test_fetch_errors_recorded_in_report(tmp_path: Path) -> None:
    new_dir, snapshot_dir, validated = _changed_scenario(tmp_path)
    current = _build(new_dir, _previous())
    diff = compute_diff(
        _previous(),
        current,
        validated,
        previous_files_dir=snapshot_dir,
        errors=[("999-神秘宠物.json", "HTTP 567")],
    )
    assert diff.errors == [{"name": "999-神秘宠物.json", "reason": "HTTP 567"}]
    assert "抓取失败（阻断发布）" in render_markdown(diff)
    assert "HTTP 567" in render_markdown(diff)
    restored = RefreshDiff.model_validate(json.loads(diff.model_dump_json()))
    assert restored.errors == diff.errors


def test_filter_targets_by_report_keeps_only_failed(tmp_path: Path) -> None:
    from qq_bot.datapipeline.publish import _filter_targets_by_report

    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "errors": [
                    {"name": "绅士鸡", "reason": "URLError: WAF block"},
                    {"name": "武者鸡", "reason": "URLError: WAF block"},
                    {"name": "不在索引", "reason": "URLError: gone"},
                ]
            }
        ),
        encoding="utf-8",
    )
    targets = [
        ("绅士鸡", "https://x/绅士鸡", {"精灵编号": "101"}),
        ("武者鸡", "https://x/武者鸡", {"精灵编号": "102"}),
        ("健康宠物", "https://x/健康宠物", {"精灵编号": "103"}),
    ]
    kept = _filter_targets_by_report(targets, report)
    assert [t[0] for t in kept] == ["绅士鸡", "武者鸡"]

    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"errors": []}), encoding="utf-8")
    assert _filter_targets_by_report(targets, empty) == []


def test_diff_json_round_trip(tmp_path: Path) -> None:
    new_dir, snapshot_dir, validated = _changed_scenario(tmp_path)
    current = _build(new_dir, _previous())
    diff = compute_diff(_previous(), current, validated, previous_files_dir=snapshot_dir)
    restored = RefreshDiff.model_validate(json.loads(diff.model_dump_json()))
    assert restored == diff
    assert restored.forms.added == ["108-测试宠物G.json"]
