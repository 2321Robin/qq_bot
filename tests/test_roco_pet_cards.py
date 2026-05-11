from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from qq_bot.services.roco_pet_cards import (
    _evolution_chain_layout,
    _evolution_steps,
    _evolution_tokens,
    _fit_font_to_width,
    _format_evolution_chain,
    generate_pet_card_files,
    pet_art_path,
    pet_card_path,
    render_pet_card_png,
)
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


def test_render_pet_card_png_uses_local_pet_art(tmp_path: Path) -> None:
    record = PetRecord(
        name="迪莫",
        aliases=[],
        number="001",
        attributes=["光"],
        stage="最终形态",
        evolution_chain=["迪莫"],
        evolution_condition="无法进化。",
        source_url="https://wiki.biligame.com/rocom/%E8%BF%AA%E8%8E%AB",
    )
    art_path = tmp_path / "001-迪莫.png"
    Image.new("RGBA", (96, 96), (255, 0, 255, 255)).save(art_path)

    image = Image.open(BytesIO(render_pet_card_png(record, asset_directory=tmp_path)))

    assert image.getpixel((118, 106)) == (255, 0, 255)


def test_render_pet_card_png_uses_local_bwiki_icons(tmp_path: Path) -> None:
    record = PetRecord(
        name="迪莫",
        aliases=[],
        number="001",
        attributes=["光"],
        stage="最终形态",
        evolution_chain=["迪莫"],
        evolution_condition="无法进化。",
        source_url="https://wiki.biligame.com/rocom/%E8%BF%AA%E8%8E%AB",
        height_weight="5.5~7KG",
        body_length="0.54~0.78M",
        favorite_partner="最好的伙伴",
        stats={"hp": 120},
    )
    icon_dir = tmp_path / "icons"
    icon_dir.mkdir()
    Image.new("RGBA", (26, 26), (255, 0, 255, 255)).save(icon_dir / "attribute-光.png")
    Image.new("RGBA", (24, 24), (0, 255, 255, 255)).save(icon_dir / "stat-hp.png")

    image = Image.open(BytesIO(render_pet_card_png(record, asset_directory=tmp_path)))

    assert image.getpixel((462, 86)) == (255, 0, 255)
    assert image.getpixel((101, 458)) == (0, 255, 255)


def test_fit_font_to_width_keeps_long_pet_name_before_attribute() -> None:
    image = Image.new("RGB", (700, 765))
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("C:/Windows/Fonts/msyhbd.ttc", size=32)

    fitted = _fit_font_to_width(draw, "魔力猫", font, 120)
    bbox = draw.textbbox((0, 0), "魔力猫", font=fitted)

    assert bbox[2] - bbox[0] <= 120


def test_format_evolution_chain_uses_full_chain() -> None:
    record = PetRecord(
        name="魔力猫",
        aliases=[],
        number="004",
        attributes=["草"],
        stage="最终形态",
        evolution_chain=["喵喵", "喵呜", "魔力猫"],
        evolution_condition="由喵呜32级进化；常规图鉴形态为最终形态。",
        source_url="https://wiki.biligame.com/rocom/魔力猫",
    )

    assert _format_evolution_chain(record) == "喵喵 → 喵呜 → 魔力猫"


def test_evolution_steps_extracts_conditions_for_arrows() -> None:
    record = PetRecord(
        name="魔力猫",
        aliases=[],
        number="004",
        attributes=["草"],
        stage="最终形态",
        evolution_chain=["喵喵", "喵呜", "魔力猫"],
        evolution_condition="由喵呜32级进化；常规图鉴形态为最终形态。",
        source_url="https://wiki.biligame.com/rocom/魔力猫",
    )

    assert _evolution_steps(record) == [("喵喵", "喵呜", "16级"), ("喵呜", "魔力猫", "32级")]


def test_evolution_tokens_put_conditions_on_matching_arrows() -> None:
    image = Image.new("RGB", (700, 765))
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("C:/Windows/Fonts/msyhbd.ttc", size=20)
    record = PetRecord(
        name="魔力猫",
        aliases=[],
        number="004",
        attributes=["草"],
        stage="最终形态",
        evolution_chain=["喵喵", "喵呜", "魔力猫"],
        evolution_condition="由喵呜32级进化；常规图鉴形态为最终形态。",
        source_url="https://wiki.biligame.com/rocom/魔力猫",
    )

    tokens = _evolution_tokens(draw, record, font, max_width=360)

    arrows = [token for token in tokens if token[0] == "arrow"]
    assert arrows == [("arrow", "→", "16级"), ("arrow", "→", "32级")]


def test_evolution_chain_layout_uses_dynamic_box_and_arrow_width() -> None:
    image = Image.new("RGB", (700, 765))
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("C:/Windows/Fonts/msyhbd.ttc", size=20)
    record = PetRecord(
        name="魔力猫",
        aliases=[],
        number="004",
        attributes=["草"],
        stage="最终形态",
        evolution_chain=["喵喵", "喵呜", "魔力猫"],
        evolution_condition="由喵呜32级进化；常规图鉴形态为最终形态。",
        source_url="https://wiki.biligame.com/rocom/魔力猫",
    )

    box, placements = _evolution_chain_layout(draw, record, font, max_width=448)

    assert box[0] > 126
    assert box[2] < 574
    assert [placement[0] for placement in placements] == ["name", "arrow", "name", "arrow", "name"]
    assert [placement[2] for placement in placements if placement[0] == "arrow"] == [48, 48]


def test_generate_pet_card_files_writes_png_to_directory(tmp_path: Path) -> None:
    record = PetRecord(
        name="迪莫",
        aliases=["小迪莫"],
        number="001",
        attributes=["光"],
        stage="最终形态",
        evolution_chain=["迪莫"],
        evolution_condition="图鉴显示为最终形态；暂无普通等级进化条件。",
        source_url="https://example.com/dimo",
    )

    paths = generate_pet_card_files([record], tmp_path)

    assert paths == [tmp_path / "001-迪莫.png"]
    assert paths[0].read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_pet_card_path_sanitizes_file_name(tmp_path: Path) -> None:
    record = PetRecord(
        name='测/试:宠*物?',
        aliases=[],
        number="9/9",
        attributes=[],
        stage="Ⅰ阶",
        evolution_chain=["测试宠物"],
        evolution_condition="暂无普通等级进化条件。",
        source_url="https://example.com/pet",
    )

    assert pet_card_path(record, tmp_path) == tmp_path / "9_9-测_试_宠_物_.png"


def test_pet_art_path_sanitizes_file_name(tmp_path: Path) -> None:
    record = PetRecord(
        name='测/试:宠*物?',
        aliases=[],
        number="9/9",
        attributes=[],
        stage="Ⅰ阶",
        evolution_chain=["测试宠物"],
        evolution_condition="暂无普通等级进化条件。",
        source_url="https://example.com/pet",
    )

    assert pet_art_path(record, tmp_path) == tmp_path / "9_9-测_试_宠_物_.png"
