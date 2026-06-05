import json
import importlib
from io import BytesIO
from pathlib import Path
from urllib.error import URLError

from PIL import Image, ImageDraw, ImageFont

from qq_bot.services.roco_pet_cards import (
    _evolution_chain_layout,
    _evolution_steps,
    _evolution_tokens,
    _evolution_name_text_position,
    _fetch_bytes,
    _fetch_text,
    _attribute_display_items,
    _attribute_pill_box,
    _trait_pill_box,
    _trait_description_text_box,
    _fit_name_lines_to_box,
    _fit_font_to_width,
    _fit_icon_text_font_to_box,
    _fit_wrapped_text_to_box,
    _format_evolution_chain,
    _format_attribute_text,
    ensure_attribute_icon_assets,
    ensure_pet_art_assets,
    generate_pet_card_files,
    load_pet_records_from_details,
    pet_art_path,
    pet_card_path,
    render_pet_card_png,
)
from qq_bot.services.roco_pets import EvolutionRelation, PetRecord


def _tiny_png_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGBA", (1, 1), (255, 0, 255, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


class _FakeResponse:
    def __init__(self, body: bytes, charset: str | None = None) -> None:
        self._body = body
        self.headers = self
        self._charset = charset

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def get_content_charset(self) -> str | None:
        return self._charset

    def read(self) -> bytes:
        return self._body


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

    magenta_pixels = sum(
        1
        for y in range(72, 100)
        for x in range(454, 632)
        if image.getpixel((x, y)) == (255, 0, 255)
    )
    assert magenta_pixels > 450
    assert image.getpixel((101, 458)) == (0, 255, 255)


def test_render_pet_card_png_draws_all_attribute_icons(tmp_path: Path) -> None:
    record = PetRecord(
        name="双系宠物",
        aliases=[],
        number="999",
        attributes=["光", "火"],
        stage="最终形态",
        evolution_chain=["双系宠物"],
        evolution_condition="无法进化。",
        source_url="https://wiki.biligame.com/rocom/test",
    )
    icon_dir = tmp_path / "icons"
    icon_dir.mkdir()
    Image.new("RGBA", (26, 26), (255, 0, 255, 255)).save(icon_dir / "attribute-光.png")
    Image.new("RGBA", (26, 26), (0, 255, 255, 255)).save(icon_dir / "attribute-火.png")

    image = Image.open(BytesIO(render_pet_card_png(record, asset_directory=tmp_path))).convert("RGB")

    magenta_pixels = sum(
        1
        for y in range(72, 100)
        for x in range(454, 632)
        if image.getpixel((x, y)) == (255, 0, 255)
    )
    cyan_pixels = sum(
        1
        for y in range(72, 100)
        for x in range(454, 632)
        if image.getpixel((x, y)) == (0, 255, 255)
    )
    assert magenta_pixels > 450
    assert cyan_pixels > 450


def test_multi_attribute_text_is_drawn_next_to_each_icon(tmp_path: Path) -> None:
    record = PetRecord(
        name="双系宠物",
        aliases=[],
        number="999",
        attributes=["光", "火"],
        stage="最终形态",
        evolution_chain=["双系宠物"],
        evolution_condition="无法进化。",
        source_url="https://wiki.biligame.com/rocom/test",
    )
    icon_dir = tmp_path / "icons"
    icon_dir.mkdir()
    Image.new("RGBA", (26, 26), (255, 0, 255, 255)).save(icon_dir / "attribute-光.png")
    Image.new("RGBA", (26, 26), (0, 255, 255, 255)).save(icon_dir / "attribute-火.png")

    image = Image.open(BytesIO(render_pet_card_png(record, asset_directory=tmp_path))).convert("RGB")
    attr_pixels = [
        x
        for y in range(72, 100)
        for x in range(454, 632)
        if image.getpixel((x, y)) in {(255, 0, 255), (0, 255, 255)}
    ]

    assert min(x for x in attr_pixels if image.getpixel((x, 86)) != (0, 255, 255)) < 505
    assert max(x for x in attr_pixels if image.getpixel((x, 86)) != (255, 0, 255)) > 510


def test_render_pet_card_png_falls_back_for_missing_attribute_icon(tmp_path: Path) -> None:
    record = PetRecord(
        name="鸭吉吉国王",
        aliases=[],
        number="011",
        attributes=["普通"],
        stage="最终形态",
        evolution_chain=["鸭吉吉国王"],
        evolution_condition="无法进化。",
        source_url="https://wiki.biligame.com/rocom/鸭吉吉国王",
    )

    image = Image.open(BytesIO(render_pet_card_png(record, asset_directory=tmp_path))).convert("RGB")

    orange_pixels = sum(
        1
        for y in range(72, 100)
        for x in range(438, 518)
        if image.getpixel((x, y)) == (216, 116, 43)
    )
    assert orange_pixels > 20


def test_render_pet_card_png_does_not_draw_trait_icon(tmp_path: Path) -> None:
    record = PetRecord(
        name="迪莫",
        aliases=[],
        number="001",
        attributes=["光"],
        stage="最终形态",
        evolution_chain=["迪莫"],
        evolution_condition="无法进化。",
        source_url="https://wiki.biligame.com/rocom/%E8%BF%AA%E8%8E%AB",
        favorite_partner="最好的伙伴",
        description="造成克制伤害后，获得攻防速+20%。",
    )
    icon_dir = tmp_path / "icons"
    icon_dir.mkdir()
    Image.new("RGBA", (24, 24), (255, 0, 255, 255)).save(icon_dir / "trait-best-partner.png")

    image = Image.open(BytesIO(render_pet_card_png(record, asset_directory=tmp_path))).convert("RGB")

    assert image.getpixel((83, 181)) != (255, 0, 255)


def test_fit_font_to_width_keeps_long_pet_name_before_attribute() -> None:
    image = Image.new("RGB", (700, 765))
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("C:/Windows/Fonts/msyhbd.ttc", size=32)

    fitted = _fit_font_to_width(draw, "鸭吉吉国王", font, 140, min_size=18)
    bbox = draw.textbbox((0, 0), "鸭吉吉国王", font=fitted)

    assert bbox[2] - bbox[0] <= 140


def test_pet_name_uses_uniform_font_size_and_wraps_before_attributes() -> None:
    image = Image.new("RGB", (700, 765))
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("C:/Windows/Fonts/msyhbd.ttc", size=20)
    name_box = (292, 57, 444, 111)
    attr_box = (454, 70, 530, 102)

    fitted_font, lines = _fit_name_lines_to_box(draw, "鸭吉吉（紧实的样子）", font, name_box)

    assert fitted_font.size == 20
    assert len(lines) >= 2
    assert all(name_box[0] + draw.textbbox((0, 0), line, font=fitted_font)[2] <= attr_box[0] - 10 for line in lines)


def test_pet_name_never_shrinks_below_uniform_size() -> None:
    image = Image.new("RGB", (700, 765))
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("C:/Windows/Fonts/msyhbd.ttc", size=20)
    name_box = (292, 57, 444, 111)

    fitted_font, lines = _fit_name_lines_to_box(draw, "石之帕蔻（卷卷尾巴的样子）", font, name_box)

    assert fitted_font.size == 20
    assert len(lines) >= 2


def test_number_pill_is_compact_for_number_text() -> None:
    number_box = (196, 69, 270, 99)

    assert number_box[2] - number_box[0] == 74
    assert number_box[3] - number_box[1] == 30


def test_011_title_has_gap_before_attribute_box() -> None:
    image = Image.new("RGB", (700, 765))
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("C:/Windows/Fonts/msyhbd.ttc", size=20)
    name_box = (292, 57, 444, 111)
    attr_box = (454, 70, 530, 102)

    fitted, lines = _fit_name_lines_to_box(draw, "鸭吉吉国王", font, name_box)

    assert fitted.size == 20
    assert all(name_box[0] + draw.textbbox((0, 0), line, font=fitted)[2] <= attr_box[0] - 10 for line in lines)


def test_attribute_pill_box_is_compact_for_single_attribute() -> None:
    image = Image.new("RGB", (700, 765))
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("C:/Windows/Fonts/msyhbd.ttc", size=20)

    single_box = _attribute_pill_box(draw, ["光"], font)
    double_box = _attribute_pill_box(draw, ["光", "火"], font)

    assert single_box == (454, 70, 530, 102)
    assert double_box[2] - double_box[0] > single_box[2] - single_box[0]
    assert double_box[2] <= 632


def test_format_attribute_text_removes_empty_delimiters() -> None:
    assert _format_attribute_text(["普通", "", "翼"]) == "普通、翼"
    assert _format_attribute_text(["普通、", "翼"]) == "普通、翼"
    assert _format_attribute_text([]) == "未知"


def test_attribute_display_items_do_not_join_multi_attributes() -> None:
    assert _attribute_display_items(["光", "火"]) == ["光", "火"]
    assert _attribute_display_items(["光、火"]) == ["光", "火"]
    assert _attribute_display_items([]) == ["未知"]


def test_fit_wrapped_text_to_box_keeps_long_trait_description_inside_box() -> None:
    image = Image.new("RGB", (700, 765))
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("C:/Windows/Fonts/msyhbd.ttc", size=24)
    text = "鸭吉吉国王的种族资质大幅增加，能耗为1的技能威力+50%。"

    fitted_font, lines = _fit_wrapped_text_to_box(draw, text, font, (78, 204, 625, 250), line_spacing=4)
    total_height = sum(draw.textbbox((0, 0), line, font=fitted_font)[3] - draw.textbbox((0, 0), line, font=fitted_font)[1] for line in lines)
    total_height += 4 * max(0, len(lines) - 1)

    assert total_height <= 46
    assert all(draw.textbbox((0, 0), line, font=fitted_font)[2] <= 547 for line in lines)


def test_trait_description_box_keeps_bottom_padding_for_two_lines() -> None:
    outer_box = (46, 190, 654, 256)
    description_text_box = _trait_description_text_box()

    assert outer_box[3] - description_text_box[3] >= 6


def test_trait_label_text_fits_inside_pill() -> None:
    image = Image.new("RGB", (700, 765))
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("C:/Windows/Fonts/msyhbd.ttc", size=20)
    trait_box = (70, 164, 220, 198)
    text = "“国王”的威严"

    fitted = _fit_icon_text_font_to_box(draw, text, font, trait_box, icon_width=0, gap=8, padding=20)
    bbox = draw.textbbox((0, 0), text, font=fitted)

    assert bbox[2] - bbox[0] <= trait_box[2] - trait_box[0] - 20


def test_trait_pill_box_width_tracks_trait_name() -> None:
    image = Image.new("RGB", (700, 765))
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("C:/Windows/Fonts/msyhbd.ttc", size=20)

    short_box = _trait_pill_box(draw, "破空", font)
    long_box = _trait_pill_box(draw, "“国王”的威严", font)

    assert short_box == (70, 164, 138, 198)
    assert long_box[0] == 70
    assert long_box[1:] == (164, 220, 198)
    assert long_box[2] - long_box[0] > short_box[2] - short_box[0]


def test_trait_label_and_description_boxes_do_not_overlap() -> None:
    trait_box = (70, 164, 220, 198)
    description_text_box = (78, 204, 625, 256)

    assert description_text_box[1] >= trait_box[3] + 6


def test_ensure_attribute_icon_assets_downloads_missing_icons(tmp_path: Path) -> None:
    record = PetRecord(
        name="鸭吉吉国王",
        aliases=[],
        number="011",
        attributes=["普通"],
        stage="最终形态",
        evolution_chain=["鸭吉吉国王"],
        evolution_condition="无法进化。",
        source_url="https://wiki.biligame.com/rocom/鸭吉吉国王",
    )
    html = '<img alt="图标 宠物 属性 普通.png" src="/images/rocom/attribute-normal.png">'
    requested_urls: list[str] = []

    def fetch_bytes(url: str) -> bytes:
        requested_urls.append(url)
        return _tiny_png_bytes()

    result = ensure_attribute_icon_assets(
        [record],
        asset_directory=tmp_path,
        fetch_html_func=lambda url: html,
        fetch_bytes_func=fetch_bytes,
    )

    assert result == {"existing": 0, "fetched": 1, "failed": 0}
    assert requested_urls == ["https://wiki.biligame.com/images/rocom/attribute-normal.png"]
    assert Image.open(tmp_path / "icons" / "attribute-普通.png").size == (1, 1)


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


def test_evolution_steps_keeps_intimacy_condition_for_arrows() -> None:
    record = PetRecord(
        name="岚鸟",
        aliases=[],
        number="020",
        attributes=["翼"],
        stage="最终形态",
        evolution_chain=["雪绒鸟", "冬羽雀", "岚鸟"],
        evolution_condition="亲密度进化为冬羽雀，30级进化为岚鸟。",
        source_url="https://wiki.biligame.com/rocom/岚鸟（本来的样子）",
    )

    assert _evolution_steps(record) == [("雪绒鸟", "冬羽雀", "亲密度"), ("冬羽雀", "岚鸟", "30级")]


def test_evolution_steps_prefers_structured_edge_condition_for_arrows() -> None:
    record = PetRecord(
        name="小雪人",
        aliases=[],
        number="354",
        attributes=["冰"],
        stage="Ⅰ阶",
        evolution_chain=["小雪人", "雪怪"],
        evolution_condition="达到40级并释放15次滚雪球技能可进化为雪怪",
        source_url="https://wiki.biligame.com/rocom/小雪人",
        evolution_to=[
            EvolutionRelation(
                source="小雪人",
                target="雪怪",
                condition="达到40级并释放15次滚雪球技能",
                text="达到40级并释放15次滚雪球技能可进化为雪怪",
            )
        ],
    )

    assert _evolution_steps(record) == [("小雪人", "雪怪", "达到40级并释放15次滚雪球技能")]


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


def test_evolution_chain_layout_reserves_individual_name_boxes() -> None:
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

    _, placements = _evolution_chain_layout(draw, record, font, max_width=448)

    for kind, text, width, _, _ in placements:
        if kind == "arrow":
            assert width == 48
        else:
            text_width = draw.textbbox((0, 0), text, font=font)[2]
            assert width >= text_width + 24


def test_evolution_name_text_position_centers_text_in_box() -> None:
    image = Image.new("RGB", (700, 765))
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("C:/Windows/Fonts/msyhbd.ttc", size=20)
    box = (300, 286, 400, 320)
    text = "魔力猫"

    x, y = _evolution_name_text_position(draw, box, text, font)
    bbox = draw.textbbox((0, 0), text, font=font)
    rendered_left = x + bbox[0]
    rendered_right = x + bbox[2]
    rendered_top = y + bbox[1]
    rendered_bottom = y + bbox[3]

    assert abs(((rendered_left + rendered_right) / 2) - ((box[0] + box[2]) / 2)) < 1
    assert abs(((rendered_top + rendered_bottom) / 2) - ((box[1] + box[3]) / 2)) < 1


def test_evolution_tokens_keep_full_names_when_chain_is_wide() -> None:
    image = Image.new("RGB", (700, 765))
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("C:/Windows/Fonts/msyhbd.ttc", size=20)
    record = PetRecord(
        name="香草甜甜（蓝莓饰品）",
        aliases=[],
        number="999",
        attributes=["普通"],
        stage="最终形态",
        evolution_chain=["脆筒甜甜", "香草甜甜（蓝莓饰品）"],
        evolution_condition="由脆筒甜甜进化。",
        source_url="https://wiki.biligame.com/rocom/test",
    )

    tokens = _evolution_tokens(draw, record, font, max_width=140)

    names = [text for kind, text, _ in tokens if kind == "name"]
    assert names == ["脆筒甜甜", "香草甜甜（蓝莓饰品）"]
    assert all("…" not in name for name in names)


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


def test_generate_pet_card_files_accepts_output_and_asset_directories(tmp_path: Path) -> None:
    record = PetRecord(
        name="迪莫",
        aliases=[],
        number="001",
        attributes=["光"],
        stage="最终形态",
        evolution_chain=["迪莫"],
        evolution_condition="无法进化",
        source_url="https://example.com/dimo",
    )
    asset_dir = tmp_path / "assets"
    output_dir = tmp_path / "cards"
    pet_art_path(record, asset_dir).parent.mkdir(parents=True, exist_ok=True)
    pet_art_path(record, asset_dir).write_bytes(_tiny_png_bytes())

    paths = generate_pet_card_files([record], output_directory=output_dir, asset_directory=asset_dir)

    assert paths == [output_dir / "001-迪莫.png"]
    image = Image.open(paths[0])
    assert image.size == (700, 765)


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


def test_fetch_text_retries_transient_urlopen_failure(monkeypatch) -> None:
    import qq_bot.services.roco_pet_cards as cards

    attempts = 0
    slept: list[float] = []

    def fake_urlopen(request, timeout):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise URLError("temporary")
        return _FakeResponse("成功".encode(), "utf-8")

    monkeypatch.setattr(cards, "urlopen", fake_urlopen)
    monkeypatch.setattr(cards, "_retry_sleep", slept.append)

    assert _fetch_text("https://example.com/pet") == "成功"
    assert attempts == 2
    assert slept == [0.25]


def test_fetch_bytes_retries_transient_urlopen_failure(monkeypatch) -> None:
    import qq_bot.services.roco_pet_cards as cards

    attempts = 0
    slept: list[float] = []

    def fake_urlopen(request, timeout):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise URLError("temporary")
        return _FakeResponse(b"png-bytes")

    monkeypatch.setattr(cards, "urlopen", fake_urlopen)
    monkeypatch.setattr(cards, "_retry_sleep", slept.append)

    assert _fetch_bytes("https://example.com/pet.png") == b"png-bytes"
    assert attempts == 2
    assert slept == [0.25]


def test_ensure_pet_art_assets_skips_existing_file(tmp_path: Path) -> None:
    record = PetRecord(
        name="迪莫",
        aliases=[],
        number="001",
        attributes=["光"],
        stage="最终形态",
        evolution_chain=["迪莫"],
        evolution_condition="无法进化",
        source_url="https://wiki.biligame.com/rocom/迪莫",
    )
    pet_art_path(record, tmp_path).write_bytes(b"existing")
    calls: list[str] = []

    result = ensure_pet_art_assets(
        [record],
        asset_directory=tmp_path,
        fetch_html_func=lambda url: calls.append(url) or "",
        fetch_bytes_func=lambda url: b"new",
    )

    assert result == {"existing": 1, "fetched": 0, "failed": 0}
    assert calls == []
    assert pet_art_path(record, tmp_path).read_bytes() == b"existing"


def test_ensure_pet_art_assets_downloads_missing_file(tmp_path: Path) -> None:
    record = PetRecord(
        name="迪莫",
        aliases=[],
        number="001",
        attributes=["光"],
        stage="最终形态",
        evolution_chain=["迪莫"],
        evolution_condition="无法进化",
        source_url="https://wiki.biligame.com/rocom/迪莫",
    )
    html = '<img alt="迪莫" src="/rocom/images/1/11/Dimo.png">'

    result = ensure_pet_art_assets(
        [record],
        asset_directory=tmp_path,
        fetch_html_func=lambda url: html,
        fetch_bytes_func=lambda url: _tiny_png_bytes() if url.endswith("/rocom/images/1/11/Dimo.png") else b"",
    )

    assert result == {"existing": 0, "fetched": 1, "failed": 0}
    assert Image.open(pet_art_path(record, tmp_path)).size == (1, 1)


def test_ensure_pet_art_assets_prefers_pet_art_over_logo(tmp_path: Path) -> None:
    record = PetRecord(
        name="迪莫",
        aliases=[],
        number="001",
        attributes=["光"],
        stage="最终形态",
        evolution_chain=["迪莫"],
        evolution_condition="无法进化",
        source_url="https://wiki.biligame.com/rocom/迪莫",
    )
    html = """
        <img alt="BWiki rocom logo" src="/rocom/logo.png">
        <img alt="页面 宠物 立绘 迪莫 1.png" src="/rocom/images/2/22/页面_宠物_立绘_迪莫_1.png">
    """
    requested_urls: list[str] = []

    def fetch_bytes(url: str) -> bytes:
        requested_urls.append(url)
        return _tiny_png_bytes()

    result = ensure_pet_art_assets(
        [record],
        asset_directory=tmp_path,
        fetch_html_func=lambda url: html,
        fetch_bytes_func=fetch_bytes,
    )

    assert result == {"existing": 0, "fetched": 1, "failed": 0}
    assert requested_urls == ["https://wiki.biligame.com/rocom/images/2/22/页面_宠物_立绘_迪莫_1.png"]


def test_ensure_pet_art_assets_accepts_positional_optional_arguments(tmp_path: Path) -> None:
    record = PetRecord(
        name="迪莫",
        aliases=[],
        number="001",
        attributes=["光"],
        stage="最终形态",
        evolution_chain=["迪莫"],
        evolution_condition="无法进化",
        source_url="https://wiki.biligame.com/rocom/迪莫",
    )
    html = '<img alt="迪莫" src="/rocom/images/1/11/Dimo.png">'

    result = ensure_pet_art_assets(
        [record],
        tmp_path,
        lambda url: html,
        lambda url: _tiny_png_bytes(),
    )

    assert result == {"existing": 0, "fetched": 1, "failed": 0}


def test_ensure_pet_art_assets_rejects_invalid_image_bytes(tmp_path: Path, capsys) -> None:
    record = PetRecord(
        name="迪莫",
        aliases=[],
        number="001",
        attributes=["光"],
        stage="最终形态",
        evolution_chain=["迪莫"],
        evolution_condition="无法进化",
        source_url="https://wiki.biligame.com/rocom/迪莫",
    )

    result = ensure_pet_art_assets(
        [record],
        asset_directory=tmp_path,
        fetch_html_func=lambda url: '<img alt="页面 宠物 立绘 迪莫 1.png" src="/rocom/images/1/11/Dimo.png">',
        fetch_bytes_func=lambda url: b"not an image",
    )

    assert result == {"existing": 0, "fetched": 0, "failed": 1}
    assert not pet_art_path(record, tmp_path).exists()
    assert "Failed to fetch art for 迪莫" in capsys.readouterr().err


def test_ensure_pet_art_assets_reports_download_failure(tmp_path: Path, capsys) -> None:
    record = PetRecord(
        name="迪莫",
        aliases=[],
        number="001",
        attributes=["光"],
        stage="最终形态",
        evolution_chain=["迪莫"],
        evolution_condition="无法进化",
        source_url="https://wiki.biligame.com/rocom/迪莫",
    )

    def fail_bytes(url: str) -> bytes:
        raise URLError("offline")

    result = ensure_pet_art_assets(
        [record],
        asset_directory=tmp_path,
        fetch_html_func=lambda url: '<img alt="迪莫" src="/rocom/images/1/11/Dimo.png">',
        fetch_bytes_func=fail_bytes,
    )

    assert result == {"existing": 0, "fetched": 0, "failed": 1}
    assert not pet_art_path(record, tmp_path).exists()
    assert "Failed to fetch art for 迪莫" in capsys.readouterr().err


def test_load_pet_records_from_details_maps_detail_json(tmp_path: Path) -> None:
    detail_path = tmp_path / "001-迪莫.json"
    detail_path.write_text(
        json.dumps(
            {
                "name": "迪莫",
                "aliases": ["小迪莫", "圣光迪莫"],
                "source_url": "https://wiki.biligame.com/rocom/迪莫",
                "attributes": ["光"],
                "evolution_chain": ["迪莫幼体", "迪莫"],
                "evolution_condition": "无法进化",
                "total_race_value": 582,
                "profile": {
                    "编号": "001",
                    "阶段": "最终形态",
                    "体长": "0.54~0.78M",
                    "体重": "5.5~7KG",
                    "身高": "0.54~0.78M",
                    "最佳拍档": "最好的伙伴",
                    "简介": "造成翼制伤害后，获得攻防速+20%",
                },
                "stats": {
                    "生命": 120,
                    "物攻": 80,
                    "魔攻": 80,
                    "物防": 105,
                    "魔防": 105,
                    "速度": 92,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    records = load_pet_records_from_details(tmp_path)

    assert len(records) == 1
    assert records[0] == PetRecord(
        name="迪莫",
        aliases=["小迪莫", "圣光迪莫"],
        number="001",
        attributes=["光"],
        stage="最终形态",
        evolution_chain=["迪莫幼体", "迪莫"],
        evolution_condition="无法进化",
        source_url="https://wiki.biligame.com/rocom/迪莫",
        height_weight="5.5~7KG",
        body_length="0.54~0.78M",
        favorite_partner="最好的伙伴",
        description="造成翼制伤害后，获得攻防速+20%",
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


def test_load_pet_records_from_details_skips_invalid_json(tmp_path: Path, capsys) -> None:
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    valid_path = tmp_path / "002-喵喵.json"
    valid_path.write_text(
        json.dumps(
            {
                "name": "喵喵",
                "source_url": "https://wiki.biligame.com/rocom/喵喵",
                "attributes": ["草"],
                "profile": {"编号": "002"},
                "stats": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    records = load_pet_records_from_details(tmp_path)

    assert [(record.name, record.evolution_chain, record.stage) for record in records] == [("喵喵", ["喵喵"], "")]
    captured = capsys.readouterr()
    assert "Skipped" in captured.err
    assert "broken.json" in captured.err


def test_load_pet_records_from_details_skips_non_object_json(tmp_path: Path, capsys) -> None:
    (tmp_path / "array.json").write_text("[]", encoding="utf-8")
    valid_path = tmp_path / "002-喵喵.json"
    valid_path.write_text(
        json.dumps(
            {
                "name": "喵喵",
                "source_url": "https://wiki.biligame.com/rocom/喵喵",
                "attributes": ["草"],
                "profile": {"编号": "002"},
                "stats": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    records = load_pet_records_from_details(tmp_path)

    assert [record.name for record in records] == ["喵喵"]
    captured = capsys.readouterr()
    assert "Skipped" in captured.err
    assert "array.json" in captured.err


def test_load_pet_records_from_details_derives_evolution_chains_and_stages(tmp_path: Path) -> None:
    for number, name, condition in [
        ("002", "喵喵", "初始形态"),
        ("003", "喵呜", "由喵喵等级16级进化"),
        ("004", "魔力猫", "由喵呜32级进化"),
    ]:
        (tmp_path / f"{number}-{name}.json").write_text(
            json.dumps(
                {
                    "name": name,
                    "source_url": f"https://wiki.biligame.com/rocom/{name}",
                    "attributes": ["草"],
                    "evolution_condition": condition,
                    "profile": {"编号": number},
                    "stats": {},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    records = load_pet_records_from_details(tmp_path)

    assert [(record.name, record.evolution_chain, record.stage) for record in records] == [
        ("喵喵", ["喵喵", "喵呜", "魔力猫"], "Ⅰ阶"),
        ("喵呜", ["喵喵", "喵呜", "魔力猫"], "Ⅱ阶"),
        ("魔力猫", ["喵喵", "喵呜", "魔力猫"], "最终形态"),
    ]


def test_load_pet_records_from_details_does_not_pick_arbitrary_branch(tmp_path: Path) -> None:
    for number, name, condition in [
        ("234", "脆筒甜甜", "初始形态"),
        ("235", "香草甜甜", "由脆筒甜甜进化"),
        ("235", "香草甜甜（杨桃饰品）", "由脆筒甜甜进化"),
        ("235", "香草甜甜（蓝莓饰品）", "由脆筒甜甜进化"),
    ]:
        (tmp_path / f"{number}-{name}.json").write_text(
            json.dumps(
                {
                    "name": name,
                    "source_url": f"https://wiki.biligame.com/rocom/{name}",
                    "attributes": ["普通"],
                    "evolution_condition": condition,
                    "profile": {"编号": number},
                    "stats": {},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    records = load_pet_records_from_details(tmp_path)

    by_name = {record.name: record for record in records}
    assert by_name["脆筒甜甜"].evolution_chain == ["脆筒甜甜"]
    assert by_name["脆筒甜甜"].stage == ""
    assert by_name["香草甜甜"].evolution_chain == ["脆筒甜甜", "香草甜甜"]
    assert by_name["香草甜甜"].stage == "最终形态"
    assert by_name["香草甜甜（杨桃饰品）"].evolution_chain == ["脆筒甜甜", "香草甜甜（杨桃饰品）"]
    assert by_name["香草甜甜（蓝莓饰品）"].evolution_chain == ["脆筒甜甜", "香草甜甜（蓝莓饰品）"]


def test_generate_roco_pet_cards_script_loads_assets_and_renders(monkeypatch, capsys) -> None:
    script = importlib.reload(importlib.import_module("scripts.generate_roco_pet_cards"))
    records = [object(), object()]
    calls = {}

    def fake_load(directory: Path):
        calls["load"] = directory
        return records

    def fake_ensure(loaded_records, *, asset_directory: Path):
        calls["ensure"] = (loaded_records, asset_directory)
        return {"downloaded": 1, "cached": 1}

    def fake_ensure_attribute_icons(loaded_records, *, asset_directory: Path):
        calls["ensure_attribute_icons"] = (loaded_records, asset_directory)
        return {"fetched": 1, "existing": 1, "failed": 0}

    def fake_generate(loaded_records, *, output_directory: Path, asset_directory: Path):
        calls["generate"] = (loaded_records, output_directory, asset_directory)
        return [Path("001.png"), Path("002.png")]

    monkeypatch.setattr(script, "load_pet_records_from_details", fake_load)
    monkeypatch.setattr(script, "ensure_attribute_icon_assets", fake_ensure_attribute_icons)
    monkeypatch.setattr(script, "ensure_pet_art_assets", fake_ensure)
    monkeypatch.setattr(script, "generate_pet_card_files", fake_generate)

    script.main()

    detail_dir = script.ROOT / "data" / "roco_pet_details"
    asset_dir = script.ROOT / "data" / "roco_assets"
    card_dir = script.ROOT / "data" / "roco_pet_cards"
    assert calls == {
        "load": detail_dir,
        "ensure_attribute_icons": (records, asset_dir),
        "ensure": (records, asset_dir),
        "generate": (records, card_dir, asset_dir),
    }
    assert capsys.readouterr().out == (
        "Loaded 2 pet record(s); attribute icons {'fetched': 1, 'existing': 1, 'failed': 0}; "
        "assets {'downloaded': 1, 'cached': 1}; generated 2 card(s)\n"
    )
