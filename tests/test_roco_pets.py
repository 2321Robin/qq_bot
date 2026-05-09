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


def test_format_pet_query_result_handles_usage_and_missing_record() -> None:
    records: list[PetRecord] = []

    assert "用法" in format_pet_query_result("", records)
    assert "本地图鉴暂时没有收录" in format_pet_query_result("不存在", records)
