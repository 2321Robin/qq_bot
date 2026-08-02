"""Directory-level validation and quarantine tests (S3-SCHEMA-03/07)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qq_bot.datapipeline.validation import validate_detail_file, validate_directory

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "data_pipeline" / "details"
INVALID_DIR = Path(__file__).parent / "fixtures" / "data_pipeline" / "invalid"


def test_valid_fixture_file_returns_detail(tmp_path: Path) -> None:
    source = FIXTURE_DIR / "101-测试宠物A.json"
    working = tmp_path / "details"
    working.mkdir()
    target = working / source.name
    target.write_bytes(source.read_bytes())
    detail = validate_detail_file(target)
    assert detail is not None
    assert detail.name == "测试宠物A"
    assert target.exists()  # untouched


def test_invalid_detail_file_returns_none_without_moving(tmp_path: Path) -> None:
    working = tmp_path / "details"
    working.mkdir()
    target = working / "107-BadPet.json"
    target.write_bytes((INVALID_DIR / "107-BadPet.json").read_bytes())
    detail = validate_detail_file(target)
    assert detail is None
    # validate_detail_file is read-only; directory-level moves belong to
    # validate_directory (S3-SCHEMA-03).
    assert target.exists()


def test_validate_directory_moves_invalid_file_with_error_json(tmp_path: Path) -> None:
    details = tmp_path / "details"
    quarantine = tmp_path / "quarantine"
    details.mkdir()
    for name in ("101-测试宠物A.json", "107-BadPet.json"):
        if name == "107-BadPet.json":
            source = INVALID_DIR / name
        else:
            source = FIXTURE_DIR / name
        (details / name).write_bytes(source.read_bytes())

    result = validate_directory(details, quarantine)

    assert set(result.ok) == {"101-测试宠物A.json"}
    assert set(result.quarantined) == {"107-BadPet.json"}
    assert not (details / "107-BadPet.json").exists()
    assert (quarantine / "107-BadPet.json").exists()
    error_path = quarantine / "107-BadPet.error.json"
    assert error_path.exists()
    error_payload = json.loads(error_path.read_text(encoding="utf-8"))
    assert error_payload["file"] == "107-BadPet.json"
    assert "unknown stat keys" in error_payload["error"]


def test_validate_directory_creates_quarantine_dir_automatically(tmp_path: Path) -> None:
    details = tmp_path / "details"
    details.mkdir()
    (details / "107-BadPet.json").write_bytes((INVALID_DIR / "107-BadPet.json").read_bytes())
    quarantine = tmp_path / "nested" / "quarantine"
    result = validate_directory(details, quarantine)
    assert set(result.quarantined) == {"107-BadPet.json"}
    assert quarantine.is_dir()
    assert (quarantine / "107-BadPet.error.json").exists()


def test_validate_directory_empty_dir_returns_empty_result(tmp_path: Path) -> None:
    details = tmp_path / "empty_details"
    details.mkdir()
    result = validate_directory(details, tmp_path / "quarantine")
    assert result.ok == {}
    assert result.quarantined == {}


def test_validate_directory_keeps_valid_files_in_place(tmp_path: Path) -> None:
    details = tmp_path / "details"
    quarantine = tmp_path / "quarantine"
    details.mkdir()
    names = ("101-测试宠物A.json", "102-测试宠物B.json", "103-测试宠物C.json")
    for name in names:
        (details / name).write_bytes((FIXTURE_DIR / name).read_bytes())
    result = validate_directory(details, quarantine)
    assert set(result.ok) == set(names)
    assert result.quarantined == {}
    assert sorted(p.name for p in details.iterdir()) == sorted(names)
    assert not any(quarantine.iterdir())


@pytest.mark.parametrize(
    "name",
    [
        "101-测试宠物A.json",
        "102-测试宠物B.json",
        "103-测试宠物C.json",
        "104-测试宠物D.json",
        "105-测试宠物E.json",
        "106-测试宠物F.json",
    ],
)
def test_all_valid_fixtures_pass_directory_validation(name: str, tmp_path: Path) -> None:
    details = tmp_path / "details"
    quarantine = tmp_path / "quarantine"
    details.mkdir()
    for fixture in sorted(FIXTURE_DIR.glob("*.json")):
        (details / fixture.name).write_bytes(fixture.read_bytes())
    (details / "107-BadPet.json").write_bytes((INVALID_DIR / "107-BadPet.json").read_bytes())
    result = validate_directory(details, quarantine)
    # 107-BadPet.json is the single invalid fixture file.
    assert set(result.ok) == {
        "101-测试宠物A.json",
        "102-测试宠物B.json",
        "103-测试宠物C.json",
        "104-测试宠物D.json",
        "105-测试宠物E.json",
        "106-测试宠物F.json",
    }
    assert set(result.quarantined) == {"107-BadPet.json"}
