from pathlib import Path

from qq_bot.services.roco_pets import (
    PetRecord,
    find_pet,
    format_pet_record,
    format_pet_query_result,
    load_pet_records,
)


def test_load_pet_records_reads_detail_json_directory() -> None:
    records = load_pet_records(Path("data/roco_pet_details"))

    assert len(records) >= 40
    assert any(record.name == "迪莫" for record in records)
    assert all(record.name for record in records)


def test_load_pet_records_maps_detail_fields_to_pet_record() -> None:
    records = load_pet_records(Path("data/roco_pet_details"))
    dimo = next(record for record in records if record.name == "迪莫")

    assert dimo.number == "001"
    assert dimo.attributes == ["光"]
    assert dimo.stage == "未知"
    assert dimo.evolution_chain == ["迪莫"]
    assert dimo.evolution_condition == "无法进化"
    assert dimo.source_url == "https://wiki.biligame.com/rocom/%E8%BF%AA%E8%8E%AB"
    assert dimo.height_weight == "5.5~7KG"
    assert dimo.body_length == "0.54~0.78M"
    assert dimo.race_value == 582
    assert dimo.stats == {
        "hp": 120,
        "physical_attack": 80,
        "magic_attack": 80,
        "physical_defense": 105,
        "magic_defense": 105,
        "speed": 92,
    }


def test_find_pet_matches_name_alias_and_substring() -> None:
    records = [
        PetRecord(
            name="迪莫",
            aliases=["小迪莫"],
            number="001",
            attributes=["光"],
            stage="初始",
            evolution_chain=["迪莫", "圣光迪莫"],
            evolution_condition="参与主线获得；后续形态按活动或任务开放。",
            source_url="https://example.com/dimo",
        )
    ]

    assert find_pet(records, "迪莫") is records[0]
    assert find_pet(records, "小迪莫") is records[0]
    assert find_pet(records, "圣光") is records[0]
    assert find_pet(records, "不存在") is None


def test_load_pet_records_derives_alias_from_parenthesized_detail_name() -> None:
    records = load_pet_records(Path("data/roco_pet_details"))
    full_form = find_pet(records, "咔咔壳（本来的样子）")

    assert full_form is not None
    assert full_form.name == "咔咔壳（本来的样子）"
    assert "咔咔壳" in full_form.aliases


def test_load_pet_records_preserves_detail_aliases() -> None:
    records = load_pet_records(Path("data/roco_pet_details"))

    assert find_pet(records, "草系御三家") is find_pet(records, "喵喵")
    assert find_pet(records, "火系御三家") is find_pet(records, "火花")
    assert find_pet(records, "水系御三家") is find_pet(records, "水蓝蓝")
    assert find_pet(records, "魔力喵") is find_pet(records, "魔力猫")


def test_load_pet_records_preserves_detail_evolution_chain() -> None:
    records = load_pet_records(Path("data/roco_pet_details"))
    miaomiao = find_pet(records, "喵喵")

    assert miaomiao is not None
    assert miaomiao.evolution_chain == ["喵喵", "喵呜", "魔力猫"]


def test_format_pet_record_includes_evolution_condition_and_source() -> None:
    record = PetRecord(
        name="迪莫",
        aliases=["小迪莫"],
        number="001",
        attributes=["光"],
        stage="初始",
        evolution_chain=["迪莫", "圣光迪莫"],
        evolution_condition="参与主线获得；后续形态按活动或任务开放。",
        source_url="https://example.com/dimo",
    )

    text = format_pet_record(record)

    assert "迪莫" in text
    assert "编号：001" in text
    assert "属性：光" in text
    assert "进化链：迪莫 -> 圣光迪莫" in text
    assert "进化条件：参与主线获得；后续形态按活动或任务开放。" in text
    assert "来源：https://example.com/dimo" in text


def test_format_pet_record_still_works_without_card_fields() -> None:
    record = PetRecord(
        name="测试宠物",
        aliases=[],
        number="999",
        attributes=["光"],
        stage="Ⅰ阶",
        evolution_chain=["测试宠物"],
        evolution_condition="暂无普通等级进化条件。",
        source_url="https://example.com/pet",
    )

    text = format_pet_record(record)

    assert "测试宠物" in text
    assert "编号：999" in text
    assert "进化条件：暂无普通等级进化条件。" in text


def test_format_pet_query_result_handles_usage_and_missing_record() -> None:
    records: list[PetRecord] = []

    assert "用法" in format_pet_query_result("", records)
    assert "本地图鉴暂时没有收录" in format_pet_query_result("不存在", records)
