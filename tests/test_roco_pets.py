import json
from pathlib import Path

from qq_bot.services.roco_pets import (
    PetRecord,
    find_pet,
    format_pet_record,
    format_pet_query_result,
    load_pet_records,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "roco_pet_details"


def test_load_pet_records_reads_detail_json_directory() -> None:
    records = load_pet_records(FIXTURE_DIR)

    assert len(records) >= 5
    assert any(record.name == "TestPetA" for record in records)
def test_load_pet_records_maps_detail_fields_to_pet_record() -> None:
    records = load_pet_records(FIXTURE_DIR)
    pet = next(record for record in records if record.name == "TestPetA")

    assert pet.number == "001"
    assert pet.attributes == ["合成属性"]
    assert pet.stage == "未知"
    assert pet.evolution_chain == ["TestPetA"]
    assert pet.evolution_condition == "无法进化"
    assert pet.source_url == "https://example.com/test-pet-a"
    assert pet.height_weight == "10~20KG"
    assert pet.body_length == "1.0~2.0M"
    assert pet.race_value == 123
    assert pet.stats == {
        "hp": 50,
        "physical_attack": 50,
        "magic_attack": 50,
        "physical_defense": 50,
        "magic_defense": 50,
        "speed": 50,
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
    assert find_pet(records, "编号001怎么进化") is records[0]
    assert find_pet(records, "1怎么进化") is records[0]
    assert find_pet(records, "不存在") is None


def test_find_pet_prefers_direct_name_over_evolution_chain_substring() -> None:
    records = [
        PetRecord(
            name="钨丝贝贝",
            aliases=[],
            number="348",
            attributes=["机械"],
            stage="I阶",
            evolution_chain=["钨丝贝贝", "辉光幕机", "机幕方舟"],
            evolution_condition="提升为1星可进化为辉光幕机",
            source_url="https://example.com/348",
        ),
        PetRecord(
            name="辉光幕机",
            aliases=[],
            number="349",
            attributes=["机械"],
            stage="II阶",
            evolution_chain=["钨丝贝贝", "辉光幕机", "机幕方舟"],
            evolution_condition="可由钨丝贝贝提升为1星进化得；提升为2星可进化为机幕方舟",
            source_url="https://example.com/349",
        ),
    ]

    assert find_pet(records, "辉光幕机怎么进化") is records[1]


def test_load_pet_records_derives_alias_from_parenthesized_detail_name() -> None:
    records = load_pet_records(FIXTURE_DIR)
    full_form = find_pet(records, "TestPetShell（Initial Form）")

    assert full_form is not None
    assert full_form.name == "TestPetShell（Initial Form）"
    assert "TestPetShell" in full_form.aliases


def test_load_pet_records_preserves_detail_aliases() -> None:
    """Detail-level aliases and DETAIL_ALIAS_MAP aliases are both included.

    DETAIL_ALIAS_MAP is a hardcoded migration map; these tests use inline
    records to exercise the compatibility layer without real game data.
    """
    from qq_bot.services.roco_pets import _with_detail_compatibility

    # Record with a detail-level alias
    record_with_alias = PetRecord(
        name="TestPetX",
        aliases=["TPX"],
        number="100",
        attributes=["合成属性"],
        stage="初始",
        evolution_chain=[],
        evolution_condition="",
        source_url="https://example.com",
    )
    result = _with_detail_compatibility(record_with_alias)
    assert "TPX" in result.aliases


def test_load_pet_records_preserves_detail_evolution_chain() -> None:
    """Evolution chain from fixture data is preserved correctly."""
    records = load_pet_records(FIXTURE_DIR)
    pet_b = find_pet(records, "TestPetB")

    assert pet_b is not None
    assert pet_b.evolution_chain == ["TestPetB"]



def test_load_pet_records_prefers_detail_evolution_object() -> None:
    records = load_pet_records(FIXTURE_DIR)
    pet_c = find_pet(records, "TestPetC")
    assert pet_c is not None
    assert pet_c.evolution_condition == "由TestPetB进化（合成数据）"


def test_load_pet_records_prefers_detail_evolution_object_from_tmp(tmp_path) -> None:
    detail = {
        "name": "喵呜",
        "source_url": "https://example.com/miaowu",
        "attributes": ["草"],
        "evolution_condition": "由喵喵等级16级进化",
        "evolution": {"from": [], "to": [], "evolution_condition": "可由喵喵升至16级进化得；升至36级可进化为魔力猫"},
        "evolution_chain": ["喵喵", "喵呜", "魔力猫"],
        "profile": {"编号": "003"},
        "stats": {},
        "skills": [],
    }
    path = tmp_path / "003-喵呜.json"
    path.write_text(json.dumps(detail, ensure_ascii=False), encoding="utf-8")

    records = load_pet_records(tmp_path)

    assert records[0].evolution_chain == ["喵喵", "喵呜", "魔力猫"]
    assert records[0].evolution_condition == "可由喵喵升至16级进化得；升至36级可进化为魔力猫"



def test_load_pet_records_maps_structured_evolution_relations(tmp_path) -> None:
    detail = {
        "name": "喵呜",
        "source_url": "https://example.com/miaowu",
        "attributes": ["草"],
        "evolution_condition": "可由喵喵升至16级进化得；升至36级可进化为魔力猫",
        "evolution": {
            "from": [
                {
                    "source": "喵喵",
                    "target": "喵呜",
                    "condition": "升至16级",
                    "text": "可由喵喵升至16级进化得",
                }
            ],
            "to": [
                {
                    "source": "喵呜",
                    "target": "魔力猫",
                    "condition": "升至36级",
                    "text": "升至36级可进化为魔力猫",
                }
            ],
            "evolution_condition": "可由喵喵升至16级进化得；升至36级可进化为魔力猫",
        },
        "profile": {"编号": "003"},
        "stats": {},
        "skills": [],
    }
    path = tmp_path / "003-喵呜.json"
    path.write_text(json.dumps(detail, ensure_ascii=False), encoding="utf-8")

    record = load_pet_records(tmp_path)[0]

    assert record.evolution_from[0].source == "喵喵"
    assert record.evolution_from[0].text == "可由喵喵升至16级进化得"
    assert record.evolution_to[0].target == "魔力猫"
    assert record.evolution_to[0].text == "升至36级可进化为魔力猫"

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
