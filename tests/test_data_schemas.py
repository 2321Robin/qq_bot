"""Schema contract tests (S3-SCHEMA-01/02): valid details pass, malformed data is rejected."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from qq_bot.datapipeline.schemas import PetDetail, SkillRow

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "data_pipeline" / "details"


def _load_valid_raw() -> dict:
    return {
        "name": "测试宠物A",
        "source_url": "https://example.com/pets/101",
        "attributes": ["合成属性"],
        "evolution_condition": "无法进化",
        "total_race_value": 123,
        "profile": {"编号": "101", "系别": "合成属性", "简介": "合成数据。"},
        "stats": {"生命": 50, "物攻": 50, "魔攻": 50, "物防": 50, "魔防": 50, "速度": 50},
        "skills": [
            {
                "source": "技能",
                "rows": [
                    {
                        "等级": "LV1",
                        "技能": "测试技能A",
                        "耗能": "1",
                        "类型": "魔攻",
                        "威力": "60",
                        "效果": "效果文本。",
                    }
                ],
            }
        ],
        "metadata": {"parser_version": 6, "generated_at": "2026-01-15T08:00:00+00:00"},
        "evolution": {"from": [], "to": [], "evolution_condition": ""},
    }


def test_every_fixture_valid_detail_parses() -> None:
    """All six committed valid fixtures must satisfy the contract (fixture guard)."""
    valid_names = {f"10{i}-测试宠物{chr(ord('A') + i - 1)}.json" for i in range(1, 7)}
    parsed = 0
    for path in sorted(FIXTURE_DIR.glob("*.json")):
        if path.name in valid_names:
            PetDetail.model_validate_json(path.read_text(encoding="utf-8"))
            parsed += 1
    assert parsed == 6


def test_valid_detail_roundtrip_by_alias() -> None:
    detail = PetDetail.model_validate(_load_valid_raw())
    dumped = detail.model_dump(by_alias=True)
    assert dumped["evolution"]["from"] == []
    assert dumped["skills"][0]["rows"][0]["等级"] == "LV1"
    assert dumped["skills"][0]["rows"][0]["技能"] == "测试技能A"


def test_unknown_top_level_key_rejected() -> None:
    raw = _load_valid_raw()
    raw["unexpected"] = True
    with pytest.raises(ValidationError, match="unexpected"):
        PetDetail.model_validate(raw)


def test_unknown_stat_key_rejected() -> None:
    raw = _load_valid_raw()
    raw["stats"]["未知"] = 1
    with pytest.raises(ValidationError, match="unknown stat keys"):
        PetDetail.model_validate(raw)


def test_negative_stat_value_rejected() -> None:
    raw = _load_valid_raw()
    raw["stats"]["速度"] = -1
    with pytest.raises(ValidationError, match="negative stat value"):
        PetDetail.model_validate(raw)


def test_missing_profile_number_rejected() -> None:
    raw = _load_valid_raw()
    del raw["profile"]["编号"]
    with pytest.raises(ValidationError, match="profile.编号 is required"):
        PetDetail.model_validate(raw)


def test_non_three_digit_number_rejected() -> None:
    raw = _load_valid_raw()
    raw["profile"]["编号"] = "42"
    with pytest.raises(ValidationError, match="3-digit"):
        PetDetail.model_validate(raw)


def test_self_loop_evolution_edge_rejected() -> None:
    raw = _load_valid_raw()
    raw["evolution"]["to"] = [{"source": "测试宠物A", "target": "测试宠物A", "condition": "x"}]
    with pytest.raises(ValidationError, match="self-loop"):
        PetDetail.model_validate(raw)


def test_empty_edge_target_rejected() -> None:
    raw = _load_valid_raw()
    raw["evolution"]["to"] = [{"source": "测试宠物A", "target": "  ", "condition": "x"}]
    with pytest.raises(ValidationError, match="non-empty"):
        PetDetail.model_validate(raw)


def test_negative_total_race_value_rejected() -> None:
    raw = _load_valid_raw()
    raw["total_race_value"] = -5
    with pytest.raises(ValidationError):
        PetDetail.model_validate(raw)


def test_skill_row_chinese_key_roundtrip() -> None:
    row = SkillRow.model_validate(
        {"等级": " LV1 ", "技能": "测试技能A", "耗能": "", "类型": "", "威力": "", "效果": ""}
    )
    assert row.level == "LV1"  # stripped
    dumped = row.model_dump(by_alias=True)
    assert set(dumped) == {"等级", "技能", "耗能", "类型", "威力", "效果"}
    assert dumped["等级"] == "LV1"


def test_skill_row_unknown_key_rejected() -> None:
    with pytest.raises(ValidationError, match="extra"):
        SkillRow.model_validate(
            {"等级": "LV1", "技能": "x", "耗能": "", "类型": "", "威力": "", "效果": "", "额外": 1}
        )
