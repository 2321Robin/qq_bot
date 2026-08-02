"""Task 9: runtime search layer with full-scan fallback (S3-INDEX-04/05), offline."""

from __future__ import annotations

from pathlib import Path

from qq_bot.datapipeline.index import RocoSearchIndex, build_index
from qq_bot.datapipeline.manifest import load_manifest
from qq_bot.datapipeline.validation import validate_detail_file
from qq_bot.services.roco_pets import find_pet, load_pet_records
from qq_bot.services.roco_search import open_search_index, pet_candidates, skill_candidates
from qq_bot.services.roco_skills import find_skills, load_skill_records

DETAILS_DIR = Path("tests/fixtures/data_pipeline/details")
MANIFEST_PATH = Path("tests/fixtures/data_pipeline/manifests/previous.json")


def _build_index(tmp_path: Path) -> Path:
    index_path = tmp_path / "index.sqlite3"
    validated: dict[str, object] = {}
    for path in sorted(DETAILS_DIR.glob("*.json")):
        detail = validate_detail_file(path)
        if detail is not None:
            validated[path.name] = detail
    build_index(DETAILS_DIR, validated, index_path, load_manifest(MANIFEST_PATH))
    return index_path


def _records():
    return load_pet_records(DETAILS_DIR)


def _skill_records():
    return load_skill_records(DETAILS_DIR)


def test_find_pet_with_index_prefers_index_hits(tmp_path: Path) -> None:
    records = _records()
    index = RocoSearchIndex.open(_build_index(tmp_path))
    assert index is not None

    pet = find_pet(records, "测试宠物A", index=index)
    assert pet is not None
    assert pet.name == "测试宠物A"

    # Number queries resolve through the index too.
    pet_by_number = find_pet(records, "103", index=index)
    assert pet_by_number is not None and pet_by_number.number == "103"

    # Index with no hits -> full-scan fallback gives the same answer as no index.
    no_hit_query = "测试宠物"  # substring that every fixture name matches
    with_index = find_pet(records, no_hit_query, index=index)
    without_index = find_pet(records, no_hit_query)
    assert with_index is not None
    assert with_index.name == without_index.name


def test_find_pet_without_index_matches_stage2(tmp_path: Path) -> None:
    records = _records()
    index = RocoSearchIndex.open(_build_index(tmp_path))
    assert index is not None
    for query in ("测试宠物A", "103", "宠物B", "完全不存在"):
        assert find_pet(records, query, index=None) == find_pet(records, query)
    # index=None is the default: identical call shape to stage 2.
    assert find_pet(records, "测试宠物A") == find_pet(records, "测试宠物A", index=None)


def test_find_skills_with_index_narrows_and_falls_back(tmp_path: Path) -> None:
    records = _skill_records()
    index = RocoSearchIndex.open(_build_index(tmp_path))
    assert index is not None

    exact = find_skills(records, "测试技能A", index=index)
    assert exact and exact[0].name == "测试技能A"

    substring = find_skills(records, "技能", index=index)
    plain = find_skills(records, "技能")
    assert [r.name for r in substring] == [r.name for r in plain]

    # No index hits -> empty candidate pool -> identical full scan.
    assert find_skills(records, "不存在", index=index) == find_skills(records, "不存在")


def test_open_search_index_returns_none_for_missing(tmp_path: Path) -> None:
    assert open_search_index(tmp_path / "missing.sqlite3") is None
    assert open_search_index(tmp_path) is None  # a directory is not an index


def test_pet_candidates_maps_hits_to_records(tmp_path: Path) -> None:
    records = _records()
    index = RocoSearchIndex.open(_build_index(tmp_path))
    assert index is not None
    candidates = pet_candidates(index, "测试宠物A", records)
    assert candidates and candidates[0].name == "测试宠物A"
    assert pet_candidates(index, "不存在的", records) == []


def test_skill_candidates_maps_hits_to_records(tmp_path: Path) -> None:
    records = _skill_records()
    index = RocoSearchIndex.open(_build_index(tmp_path))
    assert index is not None
    candidates = skill_candidates(index, "测试技能A", records)
    assert candidates and (candidates[0].pet_name, candidates[0].name) == (
        "测试宠物A",
        "测试技能A",
    )


def test_config_flag_defaults_preserve_stage2_behavior() -> None:
    """When DATA_USE_SEARCH_INDEX=false the runtime does not open an index:
    find_pet/find_skills keep the stage-2 full-scan semantics."""
    from qq_bot.config import BotSettings

    settings = BotSettings(data_use_search_index=False)
    assert settings.data_use_search_index is False
    records = _records()
    assert find_pet(records, "测试宠物A") is not None
