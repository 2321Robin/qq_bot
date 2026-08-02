"""Task 9: prebuilt n-gram index build + query (S3-INDEX-01..03), offline."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from qq_bot.datapipeline.index import RocoSearchIndex, build_index, grams
from qq_bot.datapipeline.manifest import load_manifest
from qq_bot.datapipeline.validation import validate_detail_file

DETAILS_DIR = Path("tests/fixtures/data_pipeline/details")
INVALID_DIR = Path("tests/fixtures/data_pipeline/invalid")
MANIFEST_PATH = Path("tests/fixtures/data_pipeline/manifests/previous.json")


def _validated() -> dict[str, object]:
    validated: dict[str, object] = {}
    for path in sorted(DETAILS_DIR.glob("*.json")):
        detail = validate_detail_file(path)
        if detail is not None:
            validated[path.name] = detail
    return validated


def _build(tmp_path: Path) -> Path:
    index_path = tmp_path / "index.sqlite3"
    manifest = load_manifest(MANIFEST_PATH)
    build_index(DETAILS_DIR, _validated(), index_path, manifest)
    return index_path


def test_grams_bigrams_unigrams_and_empty() -> None:
    assert grams("迪莫") == [("迪莫", 0)]
    assert grams("光") == [("光", 0)]  # single char degrades to unigram
    assert grams("") == []
    assert grams("!!!") == []
    # Mixed CJK + ASCII: position is over the surviving char list.
    assert grams("A光B") == [("A光", 0), ("光B", 1)]


def test_build_is_deterministic_byte_identical(tmp_path: Path) -> None:
    first = _build(tmp_path)
    second = _build(tmp_path)
    assert first.read_bytes() == second.read_bytes()


def test_build_writes_dataset_hash_meta(tmp_path: Path) -> None:
    index_path = _build(tmp_path)
    con = sqlite3.connect(index_path)
    try:
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    finally:
        con.close()
    manifest = load_manifest(MANIFEST_PATH)
    assert meta["dataset_hash"] == manifest.dataset_hash
    assert meta["schema_version"] == "1"
    assert meta["built_at"] == manifest.refreshed_at.isoformat()


def test_search_pets_rankings(tmp_path: Path) -> None:
    index = RocoSearchIndex.open(_build(tmp_path))
    assert index is not None
    # Two-char name and substring hits.
    by_name = {h["name"]: h for h in index.search_pets("宠物")}
    assert "测试宠物A" in by_name
    # Number hits work too (编号 grams are indexed).
    hits = index.search_pets("101")
    assert hits and hits[0]["number"] == "101"
    # Exact name outranks substring coverage.
    exact = index.search_pets("测试宠物A")
    assert exact[0] == {"number": "101", "name": "测试宠物A"}
    # No match -> empty.
    assert index.search_pets("不存在的精灵") == []
    # Deterministic ordering across two calls.
    assert index.search_pets("测试宠物") == index.search_pets("测试宠物")


def test_search_skills(tmp_path: Path) -> None:
    index = RocoSearchIndex.open(_build(tmp_path))
    assert index is not None
    hits = index.search_skills("测试技能A")
    assert hits
    assert {"name": "测试技能A", "pet_name": "测试宠物A"} in hits
    assert index.search_skills("完全不存在") == []


def test_open_rejects_corrupt_or_foreign_index(tmp_path: Path) -> None:
    index_path = _build(tmp_path)
    # Corrupt the schema_version meta.
    con = sqlite3.connect(index_path)
    try:
        con.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
        con.commit()
    finally:
        con.close()
    assert RocoSearchIndex.open(index_path) is None
    # A random file is not an index at all.
    junk = tmp_path / "junk.sqlite3"
    junk.write_text("not sqlite", encoding="utf-8")
    assert RocoSearchIndex.open(junk) is None
    assert RocoSearchIndex.open(tmp_path / "missing.sqlite3") is None


def test_build_skips_invalid_details(tmp_path: Path) -> None:
    # Same per-file filtering the build script applies: invalid files are
    # excluded from the index, valid ones are indexed.
    import shutil

    details_dir = tmp_path / "details"
    details_dir.mkdir()
    for path in sorted(DETAILS_DIR.glob("*.json")):
        shutil.copy2(path, details_dir / path.name)
    shutil.copy2(INVALID_DIR / "107-BadPet.json", details_dir / "107-BadPet.json")
    validated: dict[str, object] = {}
    skipped: list[str] = []
    for path in sorted(details_dir.glob("*.json")):
        detail = validate_detail_file(path)
        if detail is None:
            skipped.append(path.name)
        else:
            validated[path.name] = detail
    assert skipped == ["107-BadPet.json"]
    assert len(validated) == 6

    manifest = load_manifest(MANIFEST_PATH)
    index_path = tmp_path / "index.sqlite3"
    build_index(details_dir, validated, index_path, manifest)
    index = RocoSearchIndex.open(index_path)
    assert index is not None
    assert index.search_pets("BadPet") == []
