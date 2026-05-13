from pathlib import Path

from qq_bot.services.roco_skills import (
    SkillRecord,
    find_skills,
    format_skill_query_result,
    group_skill_variants,
    load_skill_records,
)


def test_load_skill_records_reads_detail_directory() -> None:
    records = load_skill_records(Path("data/roco_pet_details"))

    assert len(records) >= 100
    assert any(record.name == "闪光" and record.pet_name == "迪莫" for record in records)
    assert all(record.name for record in records)


def test_load_skill_records_maps_dimo_skill_fields() -> None:
    records = load_skill_records(Path("data/roco_pet_details"))
    skill = next(record for record in records if record.pet_name == "迪莫" and record.name == "闪光")

    assert skill.level == "LV1"
    assert skill.energy == "1"
    assert skill.category == "魔攻"
    assert skill.power == "60"
    assert skill.effect == "✦对敌方精灵造成魔法伤害。"


def test_find_skills_prefers_exact_matches() -> None:
    records = [
        SkillRecord(
            name="闪光",
            level="LV1",
            energy="1",
            category="魔攻",
            power="60",
            effect="造成魔法伤害。",
            pet_name="迪莫",
        ),
        SkillRecord(
            name="闪光冲击",
            level="LV27",
            energy="3",
            category="物攻",
            power="100",
            effect="造成物理伤害。",
            pet_name="迪莫",
        ),
    ]

    matches = find_skills(records, "闪光")

    assert [match.name for match in matches] == ["闪光"]


def test_find_skills_uses_substring_when_exact_match_is_missing() -> None:
    records = [
        SkillRecord(
            name="闪光冲击",
            level="LV27",
            energy="3",
            category="物攻",
            power="100",
            effect="造成物理伤害。",
            pet_name="迪莫",
        )
    ]

    assert find_skills(records, "闪光") == records
    assert find_skills(records, "强力闪光冲击") == records


def test_group_skill_variants_groups_identical_details_and_deduplicates_pets() -> None:
    records = [
        SkillRecord("闪光", "LV1", "1", "魔攻", "60", "造成魔法伤害。", "迪莫"),
        SkillRecord("闪光", "LV1", "1", "魔攻", "60", "造成魔法伤害。", "迪莫"),
        SkillRecord("闪光", "LV1", "1", "魔攻", "60", "造成魔法伤害。", "圣光迪莫"),
    ]

    variants = group_skill_variants(records)

    assert len(variants) == 1
    assert variants[0].name == "闪光"
    assert variants[0].pet_names == ["迪莫", "圣光迪莫"]


def test_group_skill_variants_keeps_different_details_separate() -> None:
    records = [
        SkillRecord("闪光", "LV1", "1", "魔攻", "60", "造成魔法伤害。", "迪莫"),
        SkillRecord("闪光", "LV5", "2", "魔攻", "80", "造成更高魔法伤害。", "圣光迪莫"),
    ]

    variants = group_skill_variants(records)

    assert len(variants) == 2
    assert variants[0].pet_names == ["迪莫"]
    assert variants[1].pet_names == ["圣光迪莫"]


def test_format_skill_query_result_handles_usage_and_missing_skill() -> None:
    assert format_skill_query_result("", []) == "用法：/技能 闪光"
    assert format_skill_query_result("不存在", []) == "本地技能表暂时没有收录“不存在”。"


def test_format_skill_query_result_includes_details_and_pet_names() -> None:
    records = [
        SkillRecord("闪光", "LV1", "1", "魔攻", "60", "造成魔法伤害。", "迪莫"),
        SkillRecord("闪光", "LV1", "1", "魔攻", "60", "造成魔法伤害。", "圣光迪莫"),
    ]

    text = format_skill_query_result("闪光", records)

    assert "技能：闪光" in text
    assert "等级：LV1" in text
    assert "耗能：1" in text
    assert "类型：魔攻" in text
    assert "威力：60" in text
    assert "效果：造成魔法伤害。" in text
    assert "可用精灵：迪莫、圣光迪莫" in text


def test_format_skill_query_result_limits_long_pet_lists() -> None:
    records = [
        SkillRecord("撞击", "LV1", "1", "物攻", "40", "造成物理伤害。", f"精灵{i}")
        for i in range(1, 22)
    ]

    text = format_skill_query_result("撞击", records)

    assert "精灵1、精灵2" in text
    assert "精灵21" not in text
    assert "等 21 只" in text


def test_format_skill_query_result_limits_many_variants() -> None:
    records = [
        SkillRecord(f"闪光{i}", "LV1", "1", "魔攻", str(50 + i), f"造成第{i}种伤害。", "迪莫")
        for i in range(1, 13)
    ]

    text = format_skill_query_result("闪光", records)

    assert "技能：闪光1" in text
    assert "技能：闪光10" in text
    assert "技能：闪光11" not in text
    assert "另有 2 个匹配结果未显示" in text
