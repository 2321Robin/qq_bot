from qq_bot.services.roco_pet_cards import render_pet_card_png
from qq_bot.services.roco_pets import PetRecord


def test_render_pet_card_png_returns_png_bytes() -> None:
    record = PetRecord(
        name="迪莫",
        aliases=["小迪莫"],
        number="001",
        attributes=["光"],
        stage="最终形态",
        evolution_chain=["迪莫"],
        evolution_condition="图鉴显示为最终形态；暂无普通等级进化条件。",
        source_url="https://example.com/dimo",
        height_weight="5.5~7KG",
        body_length="0.54~0.78M",
        favorite_partner="最好的伙伴",
        description="造成翼制伤害后，获得攻防速+20%，并回复2能量",
        race_value=582,
        stats={
            "hp": 120,
            "physical_attack": 80,
            "magic_attack": 80,
            "physical_defense": 105,
            "magic_defense": 105,
            "speed": 92,
        },
    )

    image = render_pet_card_png(record)

    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(image) > 1_000


def test_render_pet_card_png_handles_missing_optional_fields() -> None:
    record = PetRecord(
        name="测试宠物",
        aliases=[],
        number="999",
        attributes=[],
        stage="Ⅰ阶",
        evolution_chain=["测试宠物"],
        evolution_condition="暂无普通等级进化条件。",
        source_url="https://example.com/pet",
    )

    image = render_pet_card_png(record)

    assert image.startswith(b"\x89PNG\r\n\x1a\n")
