from pathlib import Path

from qq_bot.services.roco_pets import (
    PetRecord,
    find_pet,
    format_pet_record,
    format_pet_query_result,
    load_pet_records,
)


def test_load_pet_records_reads_local_json_data() -> None:
    records = load_pet_records(Path("data/roco_pets.json"))

    assert len(records) >= 5
    assert any(record.name == "迪莫" for record in records)


def test_load_pet_records_reads_card_fields() -> None:
    records = load_pet_records(Path("data/roco_pets.json"))
    dimo = next(record for record in records if record.name == "迪莫")

    assert dimo.height_weight == "5.5~7KG"
    assert dimo.body_length == "0.54~0.78M"
    assert dimo.favorite_partner == "最好的伙伴"
    assert dimo.description == "造成翼制伤害后，获得攻防速+20%，并回复2能量"
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
