from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from qq_bot.services.roco_pets import PetRecord


DEFAULT_CARD_DIR = Path("data/roco_pet_cards")
DEFAULT_ASSET_DIR = Path("data/roco_assets")
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
FOOTER = "QQ 群机器人生成 · 数据来源：BWiki"

STAT_ROWS = [
    ("hp", "生命"),
    ("physical_attack", "物攻"),
    ("magic_attack", "魔攻"),
    ("physical_defense", "物防"),
    ("magic_defense", "魔防"),
    ("speed", "速度"),
]
STAT_ICON_FILES = {
    "hp": "stat-hp.png",
    "physical_attack": "stat-physical-attack.png",
    "magic_attack": "stat-magic-attack.png",
    "physical_defense": "stat-physical-defense.png",
    "magic_defense": "stat-magic-defense.png",
    "speed": "stat-speed.png",
}


def pet_card_path(record: PetRecord, directory: Path = DEFAULT_CARD_DIR) -> Path:
    return directory / f"{_safe_filename(record.number)}-{_safe_filename(record.name)}.png"


def pet_art_path(record: PetRecord, directory: Path = DEFAULT_ASSET_DIR) -> Path:
    return directory / f"{_safe_filename(record.number)}-{_safe_filename(record.name)}.png"


def generate_pet_card_files(
    records: list[PetRecord],
    directory: Path = DEFAULT_CARD_DIR,
) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for record in records:
        path = pet_card_path(record, directory)
        path.write_bytes(render_pet_card_png(record))
        paths.append(path)
    return paths


def render_pet_card_png(record: PetRecord, asset_directory: Path = DEFAULT_ASSET_DIR) -> bytes:
    image = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)

    title_font = _load_font(32, bold=True)
    large_font = _load_font(30, bold=True)
    normal_font = _load_font(24, bold=True)
    normal_bold_font = _load_font(24, bold=True)
    small_font = _load_font(20, bold=True)
    small_bold_font = _load_font(20, bold=True)

    _rounded(draw, (25, 25, 675, 341), 44, PANEL)
    _rounded(draw, (46, 46, 654, 166), 60, PILL)
    _draw_avatar(image, draw, record, title_font, asset_directory)
    _draw_top_info(image, draw, record, title_font, small_font, small_bold_font, asset_directory)
    _draw_description(image, draw, record, normal_font, small_font, small_bold_font, asset_directory)

    _rounded(draw, (25, 361, 675, 681), 44, PANEL)
    _draw_stats(image, draw, record, large_font, normal_bold_font, asset_directory)

    _center_text(draw, FOOTER, (350, 727), small_font, MUTED)

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _draw_avatar(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    record: PetRecord,
    font: ImageFont.ImageFont,
    asset_directory: Path,
) -> None:
    center = (118, 106)
    radius = 48
    art = _load_pet_art(record, asset_directory)
    if art is not None:
        art.thumbnail((112, 112), Image.Resampling.LANCZOS)
        x = center[0] - art.width // 2
        y = center[1] - art.height // 2
        image.paste(art, (x, y), art)
        return

    attribute = record.attributes[0] if record.attributes else "?"
    draw.ellipse(
        (center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius),
        fill=_attribute_color(attribute),
        outline=MUTED,
        width=3,
    )
    _center_text(draw, record.name[:1] or "?", center, font, TEXT)


def _draw_top_info(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    record: PetRecord,
    title_font: ImageFont.ImageFont,
    small_font: ImageFont.ImageFont,
    small_bold_font: ImageFont.ImageFont,
    asset_directory: Path,
) -> None:
    _rounded(draw, (186, 68, 279, 101), 16, ORANGE)
    _center_text(draw, _value(record.number), (233, 84), small_bold_font, "#202326")
    _vcenter_text(draw, (292, 63, 363, 105), record.name or "未知", title_font, TEXT)

    attr_text = "、".join(record.attributes) if record.attributes else "未知"
    attr_box = (376, 72, 456, 100)
    _rounded(draw, attr_box, 14, "#34383d")
    attr_icon = _load_icon(asset_directory, f"attribute-{record.attributes[0]}.png") if record.attributes else None
    _draw_icon_text(image, draw, attr_box, attr_icon, attr_text[:2], small_bold_font, TEXT, icon_size=(24, 24), gap=7)

    weight_box = (185, 112, 335, 142)
    _rounded(draw, weight_box, 15, PILL)
    weight_icon = _load_icon(asset_directory, "body-weight.png")
    _draw_icon_text(
        image,
        draw,
        weight_box,
        weight_icon,
        _value(record.height_weight),
        small_font,
        MUTED,
        fallback_icon_text="重",
    )

    height_box = (354, 112, 534, 142)
    _rounded(draw, height_box, 15, PILL)
    height_icon = _load_icon(asset_directory, "body-height.png")
    _draw_icon_text(
        image,
        draw,
        height_box,
        height_icon,
        _value(record.body_length),
        small_font,
        MUTED,
        fallback_icon_text="长",
    )


def _draw_description(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    record: PetRecord,
    normal_font: ImageFont.ImageFont,
    small_font: ImageFont.ImageFont,
    small_bold_font: ImageFont.ImageFont,
    asset_directory: Path,
) -> None:
    _rounded(draw, (46, 190, 654, 256), 33, PILL)
    trait_box = (70, 164, 220, 198)
    _rounded(draw, trait_box, 17, GOLD)
    trait_icon = _load_icon(asset_directory, "trait-best-partner.png")
    _draw_icon_text(
        image,
        draw,
        trait_box,
        trait_icon,
        _value(record.favorite_partner),
        small_bold_font,
        "#202326",
        icon_size=(24, 24),
    )
    _draw_wrapped_vcenter_text(draw, (78, 190, 625, 256), _value(record.description), normal_font, TEXT, line_spacing=4)
    _rounded(draw, (316, 278, 385, 312), 17, PILL, outline=ORANGE)
    _center_text(draw, record.name or "未知", (350, 295), small_font, TEXT)


def _draw_stats(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    record: PetRecord,
    large_font: ImageFont.ImageFont,
    normal_bold_font: ImageFont.ImageFont,
    asset_directory: Path,
) -> None:
    _rounded(draw, (47, 383, 653, 425), 21, PILL)
    race_icon = _load_icon(asset_directory, "stat-race.png")
    _draw_icon_text(image, draw, (76, 389, 176, 419), race_icon, "种族值", normal_bold_font, TEXT, icon_size=(22, 22), gap=8)
    race_value = str(record.race_value) if record.race_value is not None else "未知"
    _vcenter_text(draw, (564, 385, 622, 421), race_value, large_font, GOLD)

    stats = record.stats or {}
    max_value = 180
    y = 446
    for key, label in STAT_ROWS:
        value = stats.get(key)
        display_value = str(value) if value is not None else "未知"
        icon = _load_icon(asset_directory, STAT_ICON_FILES[key])
        row_box = (70, y - 4, 170, y + 28)
        _draw_icon_text(image, draw, row_box, icon, label, normal_bold_font, TEXT, icon_size=(22, 22), gap=7, align="left")
        bar_x = 190
        bar_width = 365
        _rounded(draw, (bar_x, y + 1, bar_x + bar_width, y + 23), 11, BAR_BG)
        if value is not None and value > 0:
            width = max(12, min(bar_width, int(bar_width * value / max_value)))
            _rounded(draw, (bar_x, y + 1, bar_x + width, y + 23), 11, ORANGE)
        _vcenter_text(draw, (585, y - 5, 630, y + 27), display_value, normal_bold_font, MUTED)
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
    draw.text((center[0] - width / 2 - bbox[0], center[1] - height / 2 - bbox[1]), text, fill=fill, font=font)


def _vcenter_text(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: str,
) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    height = bbox[3] - bbox[1]
    y = box[1] + (box[3] - box[1] - height) / 2 - bbox[1]
    draw.text((box[0], y), text, fill=fill, font=font)


def _draw_wrapped_vcenter_text(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: str,
    *,
    line_spacing: int = 4,
) -> None:
    lines = _wrap_text(draw, text, font, box[2] - box[0])
    line_boxes = [draw.textbbox((0, 0), line, font=font) for line in lines]
    line_heights = [bbox[3] - bbox[1] for bbox in line_boxes]
    total_height = sum(line_heights) + line_spacing * max(0, len(lines) - 1)
    y = box[1] + (box[3] - box[1] - total_height) / 2
    for line, bbox, height in zip(lines, line_boxes, line_heights, strict=True):
        draw.text((box[0] - bbox[0], y - bbox[1]), line, fill=fill, font=font)
        y += height + line_spacing


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for character in text:
        candidate = f"{current}{character}"
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if current and bbox[2] - bbox[0] > max_width:
            lines.append(current)
            current = character
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or ["未知"]


def _draw_icon_text(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    icon: Image.Image | None,
    text: str,
    font: ImageFont.ImageFont,
    fill: str,
    *,
    icon_size: tuple[int, int] = (18, 18),
    gap: int = 8,
    align: str = "center",
    fallback_icon_text: str | None = None,
) -> None:
    text_bbox = draw.textbbox((0, 0), text, font=font)
    text_width = text_bbox[2] - text_bbox[0]
    text_height = text_bbox[3] - text_bbox[1]
    icon_width = icon_size[0] if icon is not None or fallback_icon_text else 0
    content_width = icon_width + (gap if icon_width else 0) + text_width
    x = box[0] + 10 if align == "left" else box[0] + (box[2] - box[0] - content_width) / 2
    center_y = box[1] + (box[3] - box[1]) / 2

    if icon is not None:
        _paste_icon(image, icon, (int(x), int(center_y - icon_size[1] / 2)), icon_size)
    elif fallback_icon_text:
        _center_text(draw, fallback_icon_text, (int(x + icon_size[0] / 2), int(center_y)), font, ORANGE)

    text_x = x + icon_width + (gap if icon_width else 0)
    text_y = center_y - text_height / 2 - text_bbox[1]
    draw.text((text_x, text_y), text, fill=fill, font=font)


def _value(value: str | None) -> str:
    return value or "未知"


def _attribute_color(attribute: str) -> str:
    return {
        "光": "#4f8edc",
        "草": "#4a9b5d",
        "火": "#d86a38",
        "水": "#3f84c8",
    }.get(attribute, "#64748b")


def _safe_filename(value: str) -> str:
    return "".join(character if character not in '<>:"/\\|?*' else "_" for character in value)


def _load_pet_art(record: PetRecord, directory: Path) -> Image.Image | None:
    path = pet_art_path(record, directory)
    if not path.exists():
        return None
    try:
        return Image.open(path).convert("RGBA")
    except OSError:
        return None


def _load_icon(directory: Path, filename: str) -> Image.Image | None:
    path = directory / "icons" / filename
    if not path.exists():
        return None
    try:
        return Image.open(path).convert("RGBA")
    except OSError:
        return None


def _paste_icon(image: Image.Image, icon: Image.Image, xy: tuple[int, int], size: tuple[int, int]) -> None:
    resized = icon.copy()
    resized.thumbnail(size, Image.Resampling.LANCZOS)
    image.paste(resized, xy, resized)


def _load_font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        Path("C:/Windows/Fonts/msyhbd.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/arialbd.ttf"),
    ] if bold else [
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()
