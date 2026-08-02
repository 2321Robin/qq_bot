"""Manifest build/verify tests (S3-MANIFEST-01..06)."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from qq_bot.datapipeline.manifest import (
    build_manifest,
    compute_dataset_hash,
    file_sha256,
    load_manifest,
    verify_manifest,
    write_manifest_atomic,
)
from qq_bot.datapipeline.validation import validate_directory

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "data_pipeline"
FIXTURE_DETAILS = FIXTURE_DIR / "details"
FIXTURE_MANIFEST = FIXTURE_DIR / "manifests" / "previous.json"

FIXED_TIME = datetime(2026, 2, 1, tzinfo=timezone.utc)


def _copy_valid_details(tmp_path: Path) -> Path:
    """Copy committed fixtures, quarantine 107-BadPet.json, keep the six valid files."""
    staging = tmp_path / "details"
    shutil.copytree(FIXTURE_DETAILS, staging)
    validate_directory(staging, tmp_path / "quarantine")
    return staging


def _build(tmp_path: Path, *, previous=None) -> tuple[Path, object]:
    details = _copy_valid_details(tmp_path)
    manifest = build_manifest(
        details,
        index_url="https://example.com/pets/index",
        refreshed_at=FIXED_TIME,
        parser_version=6,
        previous=previous,
    )
    return details, manifest


def test_dataset_hash_deterministic(tmp_path: Path) -> None:
    _, first = _build(tmp_path / "one")
    _, second = _build(tmp_path / "two")
    assert first.dataset_hash == second.dataset_hash


def test_dataset_hash_changes_when_file_changes(tmp_path: Path) -> None:
    details, manifest = _build(tmp_path)
    before = manifest.dataset_hash
    target = details / "104-测试宠物D.json"
    raw = json.loads(target.read_text(encoding="utf-8"))
    raw["profile"]["简介"] = "被修改的合成测试数据。"
    target.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rebuilt = build_manifest(
        details,
        index_url="https://example.com/pets/index",
        refreshed_at=FIXED_TIME,
        parser_version=6,
        previous=None,
    )
    assert rebuilt.dataset_hash != before


def test_verify_manifest_detects_modified_file(tmp_path: Path) -> None:
    details, manifest = _build(tmp_path)
    target = details / "104-测试宠物D.json"
    target.write_text(target.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    problems = verify_manifest(manifest, details)
    assert any("hash mismatch: 104-测试宠物D.json" in p for p in problems)


def test_verify_manifest_detects_deleted_file(tmp_path: Path) -> None:
    details, manifest = _build(tmp_path)
    (details / "104-测试宠物D.json").unlink()
    problems = verify_manifest(manifest, details)
    assert any("missing on disk: 104-测试宠物D.json" in p for p in problems)


def test_verify_manifest_detects_untracked_file(tmp_path: Path) -> None:
    details, manifest = _build(tmp_path)
    (details / "999-外来宠物.json").write_text("{}", encoding="utf-8")
    problems = verify_manifest(manifest, details)
    assert any("untracked file: 999-外来宠物.json" in p for p in problems)


def test_verify_manifest_detects_tampered_dataset_hash(tmp_path: Path) -> None:
    details, manifest = _build(tmp_path)
    manifest.dataset_hash = "0" * 64
    problems = verify_manifest(manifest, details)
    assert any(p.startswith("dataset_hash mismatch") for p in problems)


def test_verify_manifest_clean_manifest_returns_empty(tmp_path: Path) -> None:
    details, manifest = _build(tmp_path)
    assert verify_manifest(manifest, details) == []


def test_write_manifest_atomic_roundtrip(tmp_path: Path) -> None:
    _, manifest = _build(tmp_path)
    target = tmp_path / "nested" / "latest.json"
    write_manifest_atomic(manifest, target)
    loaded = load_manifest(target)
    assert loaded.model_dump() == manifest.model_dump()


def test_previous_hash_points_to_old_dataset_hash(tmp_path: Path) -> None:
    details, first = _build(tmp_path)
    second = build_manifest(
        details,
        index_url="https://example.com/pets/index",
        refreshed_at=FIXED_TIME,
        parser_version=6,
        previous=first,
    )
    assert second.previous_hash == first.dataset_hash
    assert first.previous_hash is None


def test_manifest_rejects_unknown_top_level_key(tmp_path: Path) -> None:
    _, manifest = _build(tmp_path)
    raw = json.loads(manifest.model_dump_json())
    raw["hacker"] = True
    target = tmp_path / "latest.json"
    target.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    try:
        load_manifest(target)
        raise AssertionError("expected ValidationError")
    except Exception as exc:  # noqa: BLE001 - pydantic ValidationError
        assert exc.__class__.__name__ == "ValidationError"


def test_manifest_entries_carry_required_fields(tmp_path: Path) -> None:
    _, manifest = _build(tmp_path)
    entry = manifest.entries["101-测试宠物A.json"]
    assert entry.source_url == "https://example.com/pets/101"
    assert entry.parser_version == 6
    assert len(entry.sha256) == 64
    assert entry.size > 0
    assert entry.fetched_at == FIXED_TIME
    assert manifest.license.attribution_required is True
    assert manifest.license.commercial_use is False
    assert manifest.license.redistribution == "private-only"


def test_fixture_manifest_is_current(tmp_path: Path) -> None:
    """Rebuild the committed previous.json; it must match byte-for-byte (fixture guard)."""
    details = _copy_valid_details(tmp_path)
    rebuilt = build_manifest(
        details,
        index_url="https://example.com/pets/index",
        refreshed_at=FIXED_TIME,
        parser_version=6,
        previous=None,
    )
    committed = FIXTURE_MANIFEST.read_text(encoding="utf-8")
    assert rebuilt.model_dump_json(indent=2) + "\n" == committed
    # Sanity: the invalid fixture file must never be part of the manifest.
    assert "107-BadPet" not in committed


def test_compute_dataset_hash_is_sha256_of_sorted_concat() -> None:
    import hashlib

    hashes = {"b": "11" * 32, "a": "22" * 32}
    expected = hashlib.sha256(("22" * 32 + "11" * 32).encode("ascii")).hexdigest()
    assert compute_dataset_hash(hashes) == expected


def test_file_sha256_matches_hashlib_direct() -> None:
    path = FIXTURE_DETAILS / "101-测试宠物A.json"
    import hashlib

    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    assert file_sha256(path) == expected
