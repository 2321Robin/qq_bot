from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from qq_bot.services.roco_pet_cards import (
    _fit_font_to_width,
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
