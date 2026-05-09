from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from qq_bot.services.roco_pets import PetRecord


CARD_WIDTH = 700
CARD_HEIGHT = 765
BACKGROUND = "#2c2f33"
PANEL = "#3b3f44"
PILL = "#202326"
TEXT = "#f4f4f4"
MUTED = "#c9ccd1"
ORANGE = "#d8742b"
GOLD = "#ffad2f"
BAR_BG = "#1c1f22"

STAT_ROWS = [
    ("hp", "生命"),
    ("physical_attack", "物攻"),
    ("magic_attack", "魔攻"),
    ("physical_defense", "物防"),
    ("magic_defense", "魔防"),
    ("speed", "速度"),
]


def render_pet_card_png(record: PetRecord) -> bytes:
    image = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)

    title_font = _load_font(32)
    large_font = _load_font(30)
    normal_font = _load_font(24)
    small_font = _load_font(20)

    _rounded(draw, (25, 25, 675, 341), 44, PANEL)
    _rounded(draw, (46, 46, 654, 166), 60, PILL)
    _draw_avatar(draw, record, title_font)
    _draw_top_info(draw, record, title_font, small_font)
    _draw_description(draw, record, normal_font, small_font)

    _rounded(draw, (25, 361, 675, 681), 44, PANEL)
    _draw_stats(draw, record, large_font, normal_font)

    draw.text((134, 712), "生成自群机器人 @小呱呱", fill=TEXT, font=small_font)
    draw.text((380, 712), "数据来源：roco.cn", fill=TEXT, font=small_font)

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _draw_avatar(draw: ImageDraw.ImageDraw, record: PetRecord, font: ImageFont.ImageFont) -> None:
    center = (118, 106)
    radius = 48
    attribute = record.attributes[0] if record.attributes else "?"
    draw.ellipse(
        (center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius),
        fill=_attribute_color(attribute),
        outline=MUTED,
        width=3,
    )
    _center_text(draw, record.name[:1] or "?", center, font, TEXT)


def _draw_top_info(
    draw: ImageDraw.ImageDraw,
    record: PetRecord,
    title_font: ImageFont.ImageFont,
    small_font: ImageFont.ImageFont,
) -> None:
    _rounded(draw, (186, 68, 279, 101), 16, ORANGE)
    _center_text(draw, _value(record.number), (233, 84), small_font, "#202326")
    draw.text((292, 67), record.name or "未知", fill=TEXT, font=title_font)

    attr_text = "、".join(record.attributes) if record.attributes else "未知"
    _rounded(draw, (376, 72, 433, 98), 13, "#34383d")
    _center_text(draw, attr_text[:2], (404, 85), small_font, TEXT)

    _rounded(draw, (185, 112, 329, 141), 15, PILL)
    draw.text((205, 115), "重", fill=ORANGE, font=small_font)
    draw.text((236, 115), _value(record.height_weight), fill=MUTED, font=small_font)

    _rounded(draw, (346, 112, 526, 141), 15, PILL)
    draw.text((367, 115), "长", fill=ORANGE, font=small_font)
    draw.text((410, 115), _value(record.body_length), fill=MUTED, font=small_font)


def _draw_description(
    draw: ImageDraw.ImageDraw,
    record: PetRecord,
    normal_font: ImageFont.ImageFont,
    small_font: ImageFont.ImageFont,
) -> None:
    _rounded(draw, (64, 164, 180, 194), 15, GOLD)
    _center_text(draw, _value(record.favorite_partner), (122, 179), small_font, "#202326")
    _rounded(draw, (46, 184, 654, 252), 34, PILL)
    draw.text((78, 211), _value(record.description), fill=TEXT, font=normal_font)
    _rounded(draw, (316, 274, 385, 308), 17, PILL, outline=ORANGE)
    _center_text(draw, record.name or "未知", (350, 291), small_font, TEXT)


def _draw_stats(
    draw: ImageDraw.ImageDraw,
    record: PetRecord,
    large_font: ImageFont.ImageFont,
    normal_font: ImageFont.ImageFont,
) -> None:
    _rounded(draw, (47, 383, 653, 425), 21, PILL)
    draw.text((76, 391), "种族值", fill=TEXT, font=normal_font)
    race_value = str(record.race_value) if record.race_value is not None else "未知"
    draw.text((564, 386), race_value, fill=GOLD, font=large_font)

    stats = record.stats or {}
    max_value = 180
    y = 445
    for key, label in STAT_ROWS:
        value = stats.get(key)
        display_value = str(value) if value is not None else "未知"
        draw.text((78, y - 4), label, fill=TEXT, font=normal_font)
        _rounded(draw, (161, y, 569, y + 22), 11, BAR_BG)
        if value is not None and value > 0:
            width = max(12, min(408, int(408 * value / max_value)))
            _rounded(draw, (161, y, 161 + width, y + 22), 11, ORANGE)
        draw.text((585, y - 6), display_value, fill=MUTED, font=normal_font)
        y += 38


def _rounded(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int, int, int],
    radius: int,
    fill: str,
    *,
    outline: str | None = None,
) -> None:
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=2 if outline else 1)


def _center_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    center: tuple[int, int],
    font: ImageFont.ImageFont,
    fill: str,
) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    draw.text((center[0] - width / 2, center[1] - height / 2 - 2), text, fill=fill, font=font)


def _value(value: str | None) -> str:
    return value or "未知"


def _attribute_color(attribute: str) -> str:
    return {
        "光": "#4f8edc",
        "草": "#4a9b5d",
        "火": "#d86a38",
        "水": "#3f84c8",
    }.get(attribute, "#64748b")


def _load_font(size: int) -> ImageFont.ImageFont:
    candidates = [
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()
