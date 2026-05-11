from __future__ import annotations

import re
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
EVOLUTION_ARROW_WIDTH = 48
EVOLUTION_TOKEN_GAP = 18
EVOLUTION_BOX_PADDING = 18


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
    name_box = (292, 63, 420, 105)
    fitted_title_font = _fit_font_to_width(draw, record.name or "未知", title_font, name_box[2] - name_box[0])
    _vcenter_text(draw, name_box, record.name or "未知", fitted_title_font, TEXT)

    attr_text = "、".join(record.attributes) if record.attributes else "未知"
    attr_box = (438, 72, 518, 100)
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
    _draw_evolution_chain(draw, record, small_font, small_bold_font)


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


def _draw_evolution_chain(
    draw: ImageDraw.ImageDraw,
    record: PetRecord,
    name_font: ImageFont.ImageFont,
    condition_font: ImageFont.ImageFont,
) -> None:
    chain_font = _fit_chain_font(draw, record, name_font, 448 - EVOLUTION_BOX_PADDING * 2)
    chain_box, placements = _evolution_chain_layout(draw, record, chain_font, max_width=448)

    _rounded(draw, chain_box, 17, PILL, outline=ORANGE)

    for kind, text, width, x, condition in placements:
        token_box = (x, chain_box[1], x + width, chain_box[3])
        if kind == "arrow":
            arrow_center_x = x + width // 2
            _draw_evolution_arrow(draw, arrow_center_x, 303)
            _draw_evolution_condition(draw, condition, arrow_center_x, condition_font)
        else:
            _vcenter_text(draw, token_box, text, chain_font, TEXT)


def _evolution_chain_layout(
    draw: ImageDraw.ImageDraw,
    record: PetRecord,
    font: ImageFont.ImageFont,
    *,
    max_width: int,
) -> tuple[tuple[int, int, int, int], list[tuple[str, str, int, int, str]]]:
    content_max_width = max_width - EVOLUTION_BOX_PADDING * 2
    tokens = _evolution_tokens(draw, record, font, max_width=content_max_width)
    token_widths = [_evolution_token_width(draw, token, font) for token in tokens]
    content_width = sum(token_widths) + EVOLUTION_TOKEN_GAP * max(0, len(tokens) - 1)
    box_width = min(max_width, content_width + EVOLUTION_BOX_PADDING * 2)
    left = round((CARD_WIDTH - box_width) / 2)
    chain_box = (left, 286, left + box_width, 320)
    x = left + round((box_width - content_width) / 2)
    placements = []

    for token, width in zip(tokens, token_widths, strict=True):
        kind, text, condition = token
        placements.append((kind, text, width, x, condition))
        x += width + EVOLUTION_TOKEN_GAP

    return chain_box, placements


def _draw_evolution_condition(
    draw: ImageDraw.ImageDraw,
    condition: str,
    arrow_center_x: int,
    condition_font: ImageFont.ImageFont,
) -> None:
    if not condition:
        return
    label_box = (arrow_center_x - 36, 260, arrow_center_x + 36, 283)
    label_font = _fit_font_to_width(draw, condition, condition_font, label_box[2] - label_box[0] - 10, min_size=12)
    _rounded(draw, label_box, 11, ORANGE)
    _center_text(draw, condition, (arrow_center_x, 271), label_font, "#202326")


def _fit_chain_font(
    draw: ImageDraw.ImageDraw,
    record: PetRecord,
    font: ImageFont.ImageFont,
    max_width: int,
) -> ImageFont.ImageFont:
    if not isinstance(font, ImageFont.FreeTypeFont):
        return font
    for size in range(font.size, 13, -1):
        candidate = font.font_variant(size=size)
        tokens = _evolution_tokens(draw, record, candidate, max_width=max_width)
        widths = [_evolution_token_width(draw, token, candidate) for token in tokens]
        if sum(widths) + EVOLUTION_TOKEN_GAP * max(0, len(tokens) - 1) <= max_width:
            return candidate
    return font.font_variant(size=14)


def _draw_evolution_arrow(draw: ImageDraw.ImageDraw, center_x: int, center_y: int) -> None:
    half_width = 24
    shaft_start = center_x - half_width
    shaft_end = center_x + half_width - 8
    draw.line((shaft_start, center_y, shaft_end, center_y), fill=ORANGE, width=5)
    draw.polygon(
        [
            (center_x + half_width, center_y),
            (center_x + half_width - 12, center_y - 8),
            (center_x + half_width - 12, center_y + 8),
        ],
        fill=ORANGE,
    )


def _evolution_tokens(
    draw: ImageDraw.ImageDraw,
    record: PetRecord,
    font: ImageFont.ImageFont,
    *,
    max_width: int,
) -> list[tuple[str, str, str]]:
    chain = record.evolution_chain or [record.name or "未知"]
    steps = _evolution_steps(record)
    tokens: list[tuple[str, str, str]] = []

    for index, name in enumerate(chain):
        tokens.append(("name", name, ""))
        if index >= len(steps):
            continue
        tokens.append(("arrow", "→", steps[index][2]))

    return _compact_evolution_tokens(draw, tokens, font, max_width)


def _compact_evolution_tokens(
    draw: ImageDraw.ImageDraw,
    tokens: list[tuple[str, str, str]],
    font: ImageFont.ImageFont,
    max_width: int,
) -> list[tuple[str, str, str]]:
    widths = [_evolution_token_width(draw, token, font) for token in tokens]
    if sum(widths) + EVOLUTION_TOKEN_GAP * max(0, len(tokens) - 1) <= max_width:
        return tokens

    compacted = []
    for kind, text, condition in tokens:
        if kind == "name" and len(text) > 3:
            compacted.append((kind, f"{text[:2]}…", condition))
        else:
            compacted.append((kind, text, condition))
    return compacted


def _evolution_token_width(
    draw: ImageDraw.ImageDraw,
    token: tuple[str, str, str],
    font: ImageFont.ImageFont,
) -> int:
    kind, text, _ = token
    if kind == "arrow":
        return EVOLUTION_ARROW_WIDTH
    return _text_width(draw, text, font)


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


def _fit_font_to_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
    *,
    min_size: int = 22,
) -> ImageFont.ImageFont:
    bbox = draw.textbbox((0, 0), text, font=font)
    if bbox[2] - bbox[0] <= max_width:
        return font
    if not isinstance(font, ImageFont.FreeTypeFont):
        return font

    for size in range(font.size - 1, min_size - 1, -1):
        fitted = font.font_variant(size=size)
        bbox = draw.textbbox((0, 0), text, font=fitted)
        if bbox[2] - bbox[0] <= max_width:
            return fitted
    return font.font_variant(size=min_size)


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


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


def _format_evolution_chain(record: PetRecord) -> str:
    return " → ".join(record.evolution_chain) if record.evolution_chain else _value(record.name)


def _evolution_steps(record: PetRecord) -> list[tuple[str, str, str]]:
    chain = record.evolution_chain
    if len(chain) < 2:
        return []
    levels = [f"{level}级" for level in re.findall(r"(\d+)级", record.evolution_condition)]
    if len(chain) == 3 and len(levels) == 1:
        levels = ["16级", levels[0]]
    return [(source, target, levels[index] if index < len(levels) else "") for index, (source, target) in enumerate(zip(chain, chain[1:], strict=False))]


def _evolution_step_positions(record: PetRecord) -> list[tuple[str, str, str, int]]:
    steps = _evolution_steps(record)
    if not steps:
        return []
    if len(steps) == 1:
        positions = [350]
    elif len(steps) == 2:
        positions = [292, 408]
    else:
        start = 220
        end = 480
        interval = (end - start) / (len(steps) - 1)
        positions = [round(start + interval * index) for index in range(len(steps))]
    return [(source, target, condition, positions[index]) for index, (source, target, condition) in enumerate(steps)]


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
