from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import replace
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw, ImageFont

from qq_bot.services.roco_pets import PetRecord


DEFAULT_CARD_DIR = Path("data/roco_pet_cards")
DEFAULT_ASSET_DIR = Path("data/roco_assets")
DEFAULT_DETAIL_DIR = Path("data/roco_pet_details")
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
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
FETCH_ATTEMPTS = 3
FETCH_RETRY_DELAYS = (0.25, 0.75)

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
EVOLUTION_NAME_PADDING = 12


def pet_card_path(record: PetRecord, directory: Path = DEFAULT_CARD_DIR) -> Path:
    return directory / f"{_safe_filename(record.number)}-{_safe_filename(record.name)}.png"


def pet_art_path(record: PetRecord, directory: Path = DEFAULT_ASSET_DIR) -> Path:
    return directory / f"{_safe_filename(record.number)}-{_safe_filename(record.name)}.png"


def generate_pet_card_files(
    records: list[PetRecord],
    directory: Path | None = None,
    *,
    output_directory: Path | None = None,
    asset_directory: Path = DEFAULT_ASSET_DIR,
) -> list[Path]:
    if directory is not None and output_directory is not None:
        raise ValueError("directory and output_directory cannot both be provided")
    directory = output_directory or directory or DEFAULT_CARD_DIR
    directory.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for record in records:
        path = pet_card_path(record, directory)
        path.write_bytes(render_pet_card_png(record, asset_directory=asset_directory))
        paths.append(path)
    return paths


def load_pet_records_from_details(directory: Path = DEFAULT_DETAIL_DIR) -> list[PetRecord]:
    records: list[PetRecord] = []
    explicit_chain_names: set[str] = set()
    for path in sorted(directory.glob("*.json")):
        try:
            detail = json.loads(path.read_text(encoding="utf-8"))
            detail = _dict_value(detail)
            record = _pet_record_from_detail(detail)
            if _string_detail_list(detail.get("evolution_chain")):
                explicit_chain_names.add(record.name)
            records.append(record)
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            print(f"Skipped {path}: {exc}", file=sys.stderr)
    return _derive_detail_evolution_data(records, explicit_chain_names)


def _pet_record_from_detail(detail: dict[str, Any]) -> PetRecord:
    profile = _dict_value(detail.get("profile"))
    name = _string_detail_value(detail.get("name"))
    number = _string_detail_value(profile.get("编号"))
    attributes = _string_detail_list(detail.get("attributes")) or _split_detail_values(profile.get("系别"))
    stats = _card_stats_from_detail_stats(_dict_value(detail.get("stats")))
    evolution_chain = _string_detail_list(detail.get("evolution_chain")) or [name]

    return PetRecord(
        name=name,
        aliases=_string_detail_list(detail.get("aliases")),
        number=number,
        attributes=attributes,
        stage=_first_string(profile, "阶段", "形态"),
        evolution_chain=[part for part in evolution_chain if part],
        evolution_condition=_string_detail_value(detail.get("evolution_condition")),
        source_url=_string_detail_value(detail.get("source_url")),
        height_weight=_first_string(profile, "体重", "重量"),
        body_length=_first_string(profile, "体长", "身高", "长度"),
        favorite_partner=_first_string(profile, "最佳拍档", "推荐搭档", "拍档"),
        description=_first_string(profile, "简介", "描述", "精灵介绍"),
        race_value=_optional_detail_int(detail.get("total_race_value")),
        stats=stats,
    )


def _dict_value(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("detail field must be an object")
    return value


def _string_detail_value(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        return str(value).strip()
    return value.strip()


def _string_detail_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        return []
    return [_string_detail_value(part) for part in value if _string_detail_value(part)]


def _split_detail_values(value: Any) -> list[str]:
    text = _string_detail_value(value)
    if not text:
        return []
    return [part for part in re.split(r"[、,，/ ]+", text) if part]


def _first_string(profile: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = _string_detail_value(profile.get(key))
        if value:
            return value
    return ""


def _optional_detail_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    text = _string_detail_value(value)
    return int(text) if text.isdigit() else None


def _card_stats_from_detail_stats(stats: dict[str, Any]) -> dict[str, int] | None:
    mapped: dict[str, int] = {}
    for source_key, target_key in {
        "生命": "hp",
        "精力": "hp",
        "HP": "hp",
        "hp": "hp",
        "物攻": "physical_attack",
        "魔攻": "magic_attack",
        "物防": "physical_defense",
        "魔防": "magic_defense",
        "速度": "speed",
    }.items():
        value = _optional_detail_int(stats.get(source_key))
        if value is not None:
            mapped[target_key] = value
    return mapped or None


def _derive_detail_evolution_data(records: list[PetRecord], explicit_chain_names: set[str]) -> list[PetRecord]:
    names = [record.name for record in records if record.name]
    predecessors: dict[str, str] = {}
    outgoing: dict[str, list[str]] = {}
    for record in records:
        predecessor = _evolution_predecessor_from_condition(record.evolution_condition, record.name, names)
        if predecessor is not None:
            predecessors[record.name] = predecessor
            outgoing.setdefault(predecessor, []).append(record.name)

    derived_records: list[PetRecord] = []
    for record in records:
        chain = record.evolution_chain
        if record.name not in explicit_chain_names:
            chain = _derived_evolution_chain(record.name, predecessors, outgoing) or [record.name]
        stage = record.stage or _stage_from_chain(record.name, chain, outgoing)
        derived_records.append(replace(record, evolution_chain=chain, stage=stage))
    return derived_records


def _evolution_predecessor_from_condition(condition: str, record_name: str, names: list[str]) -> str | None:
    if "由" not in condition or "进化" not in condition:
        return None
    for name in sorted(names, key=len, reverse=True):
        if name != record_name and f"由{name}" in condition:
            return name
    return None


def _derived_evolution_chain(name: str, predecessors: dict[str, str], outgoing: dict[str, list[str]]) -> list[str]:
    chain = [name]
    seen = {name}
    while chain[0] in predecessors and predecessors[chain[0]] not in seen:
        predecessor = predecessors[chain[0]]
        chain.insert(0, predecessor)
        seen.add(predecessor)
    while chain[-1] in outgoing and len(outgoing[chain[-1]]) == 1 and outgoing[chain[-1]][0] not in seen:
        next_name = outgoing[chain[-1]][0]
        chain.append(next_name)
        seen.add(next_name)
    return chain if len(chain) > 1 else []


def _stage_from_chain(name: str, chain: list[str], outgoing: dict[str, list[str]]) -> str:
    if len(chain) <= 1:
        return ""
    try:
        index = chain.index(name)
    except ValueError:
        return ""
    if name not in outgoing and index == len(chain) - 1:
        return "最终形态"
    if index == 0:
        return "Ⅰ阶"
    if index == 1:
        return "Ⅱ阶"
    return "Ⅲ阶" if name in outgoing else "最终形态"


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


def ensure_pet_art_assets(
    records: list[PetRecord] | tuple[PetRecord, ...],
    asset_directory: Path = DEFAULT_ASSET_DIR,
    fetch_html_func=None,
    fetch_bytes_func=None,
) -> dict[str, int]:
    fetch_html = fetch_html_func or _fetch_text
    fetch_bytes = fetch_bytes_func or _fetch_bytes
    asset_directory.mkdir(parents=True, exist_ok=True)
    result = {"existing": 0, "fetched": 0, "failed": 0}

    for record in records:
        path = pet_art_path(record, asset_directory)
        if path.exists():
            result["existing"] += 1
            continue
        try:
            html = fetch_html(record.source_url)
            image_url = _find_pet_art_url(record, html)
            if not image_url:
                raise ValueError("no pet image URL found")
            image_bytes = fetch_bytes(image_url)
            _validate_image_bytes(image_bytes)
            path.write_bytes(image_bytes)
            result["fetched"] += 1
        except Exception as exc:  # noqa: BLE001 - report per-record failures and continue.
            result["failed"] += 1
            print(f"Failed to fetch art for {record.name}: {exc}", file=sys.stderr)

    return result


def ensure_attribute_icon_assets(
    records: list[PetRecord] | tuple[PetRecord, ...],
    asset_directory: Path = DEFAULT_ASSET_DIR,
    fetch_html_func=None,
    fetch_bytes_func=None,
) -> dict[str, int]:
    fetch_html = fetch_html_func or _fetch_text
    fetch_bytes = fetch_bytes_func or _fetch_bytes
    icon_directory = asset_directory / "icons"
    icon_directory.mkdir(parents=True, exist_ok=True)
    result = {"existing": 0, "fetched": 0, "failed": 0}

    missing_attributes = _missing_attributes(records, icon_directory)
    source_urls = _source_urls_by_attribute(records)
    for attribute in missing_attributes:
        path = icon_directory / f"attribute-{attribute}.png"
        if path.exists():
            result["existing"] += 1
            continue
        try:
            html = fetch_html(source_urls[attribute])
            image_url = _find_attribute_icon_url(attribute, source_urls[attribute], html)
            if not image_url:
                raise ValueError("no attribute icon URL found")
            image_bytes = fetch_bytes(image_url)
            _validate_image_bytes(image_bytes)
            path.write_bytes(image_bytes)
            result["fetched"] += 1
        except Exception as exc:  # noqa: BLE001 - report per-attribute failures and continue.
            result["failed"] += 1
            print(f"Failed to fetch attribute icon for {attribute}: {exc}", file=sys.stderr)

    return result


def _missing_attributes(records: list[PetRecord] | tuple[PetRecord, ...], icon_directory: Path) -> list[str]:
    attributes: list[str] = []
    for record in records:
        for attribute in record.attributes:
            for value in _attribute_values(attribute):
                if value not in attributes and not (icon_directory / f"attribute-{value}.png").exists():
                    attributes.append(value)
    return attributes


def _source_urls_by_attribute(records: list[PetRecord] | tuple[PetRecord, ...]) -> dict[str, str]:
    urls: dict[str, str] = {}
    for record in records:
        for attribute in record.attributes:
            for value in _attribute_values(attribute):
                urls.setdefault(value, record.source_url)
    return urls


def _find_attribute_icon_url(attribute: str, base_url: str, html: str) -> str:
    parser = _AttributeIconParser(attribute, base_url)
    parser.feed(html)
    return parser.best_url


class _AttributeIconParser(HTMLParser):
    def __init__(self, attribute: str, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.attribute = attribute
        self.base_url = base_url
        self.best_url = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "img" or self.best_url:
            return
        values = {key: value or "" for key, value in attrs}
        searchable = " ".join([values.get("alt", ""), values.get("title", ""), values.get("src", "")])
        if "属性" not in searchable or self.attribute not in searchable:
            return
        src = values.get("src") or values.get("data-src") or values.get("data-original")
        if src:
            self.best_url = urljoin(self.base_url, src)


def _fetch_text(url: str) -> str:
    request = Request(url, headers=BROWSER_HEADERS)
    with _urlopen_with_retries(request, url) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def _fetch_bytes(url: str) -> bytes:
    request = Request(url, headers=BROWSER_HEADERS)
    with _urlopen_with_retries(request, url) as response:
        return response.read()


def _urlopen_with_retries(request: Request, url: str):
    last_error: Exception | None = None
    for attempt in range(FETCH_ATTEMPTS):
        try:
            return urlopen(request, timeout=30)
        except (HTTPError, URLError, OSError) as exc:
            last_error = exc
            if attempt == FETCH_ATTEMPTS - 1:
                break
            _retry_sleep(FETCH_RETRY_DELAYS[min(attempt, len(FETCH_RETRY_DELAYS) - 1)])
    raise RuntimeError(f"failed to fetch {url} after {FETCH_ATTEMPTS} attempts: {last_error}") from last_error


def _retry_sleep(seconds: float) -> None:
    time.sleep(seconds)


def _validate_image_bytes(image_bytes: bytes) -> None:
    with Image.open(BytesIO(image_bytes)) as image:
        image.verify()


def _find_pet_art_url(record: PetRecord, html: str) -> str:
    parser = _PetArtParser(record.name, record.source_url)
    parser.feed(html)
    return parser.best_url


class _PetArtParser(HTMLParser):
    def __init__(self, pet_name: str, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.pet_name = pet_name
        self.base_url = base_url
        self.best_url = ""
        self._best_score = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "img":
            return
        values = {key: value or "" for key, value in attrs}
        src = values.get("src") or values.get("data-src") or values.get("data-original")
        if not src:
            return
        alt = values.get("alt", "")
        title = values.get("title", "")
        class_name = values.get("class", "")
        searchable = " ".join([alt, title, class_name, src])
        score = 0
        if "宠物" in searchable:
            score += 2
        if "立绘" in searchable:
            score += 2
        if "页面" in searchable:
            score += 1
        if self.pet_name and self.pet_name in searchable:
            score += 1
        if score > self._best_score:
            self._best_score = score
            self.best_url = urljoin(self.base_url, src)


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
    _rounded(draw, (196, 69, 270, 99), 15, ORANGE)
    _center_text(draw, _value(record.number), (233, 84), small_bold_font, "#202326")
    name_box = (292, 57, 444, 111)
    name_font = title_font.font_variant(size=20) if isinstance(title_font, ImageFont.FreeTypeFont) else title_font
    fitted_title_font, name_lines = _fit_name_lines_to_box(draw, record.name or "未知", name_font, name_box)
    _draw_name_lines(draw, name_box, name_lines, fitted_title_font)

    attr_text = _format_attribute_text(record.attributes)
    attr_box = _attribute_pill_box(draw, record.attributes, small_bold_font)
    _rounded(draw, attr_box, 14, "#34383d")
    _draw_attribute_icons_text(image, draw, attr_box, record.attributes, attr_text, small_bold_font, asset_directory)

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
    trait_text = _value(record.favorite_partner)
    trait_box = _trait_pill_box(draw, trait_text, small_bold_font)
    _rounded(draw, trait_box, 17, GOLD)
    _draw_icon_text(
        image,
        draw,
        trait_box,
        None,
        trait_text,
        small_bold_font,
        "#202326",
        icon_size=(24, 24),
    )
    _draw_wrapped_vcenter_text(draw, (78, 204, 625, 256), _value(record.description), normal_font, TEXT, line_spacing=4)
    _draw_evolution_chain(draw, record, small_font, small_bold_font)


def _draw_attribute_icons_text(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    attributes: list[str],
    text: str,
    font: ImageFont.ImageFont,
    asset_directory: Path,
) -> None:
    values = _attribute_display_items(attributes)
    icon_size = (26, 26)
    text_gap = 5
    group_gap = 10
    fitted_font = _fit_attribute_group_font(draw, values, font, box, icon_size[0], text_gap, group_gap)
    text_boxes = [draw.textbbox((0, 0), value, font=fitted_font) for value in values]
    text_widths = [bbox[2] - bbox[0] for bbox in text_boxes]
    content_width = sum(icon_size[0] + text_gap + width for width in text_widths) + group_gap * max(0, len(values) - 1)
    x = box[0] + (box[2] - box[0] - content_width) / 2
    center_y = box[1] + (box[3] - box[1]) / 2

    for value, text_bbox, text_width in zip(values, text_boxes, text_widths, strict=True):
        icon = _load_icon(asset_directory, f"attribute-{value}.png")
        if icon is not None:
            _paste_icon(image, icon, (int(x), int(center_y - icon_size[1] / 2)), icon_size)
        else:
            _center_text(draw, value[:1], (int(x + icon_size[0] / 2), int(center_y)), fitted_font, ORANGE)
        x += icon_size[0] + text_gap
        text_height = text_bbox[3] - text_bbox[1]
        text_y = center_y - text_height / 2 - text_bbox[1]
        draw.text((x - text_bbox[0], text_y), value, fill=TEXT, font=fitted_font)
        x += text_width + group_gap


def _fit_name_lines_to_box(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    box: tuple[int, int, int, int],
    *,
    min_size: int = 16,
) -> tuple[ImageFont.ImageFont, list[str]]:
    max_width = box[2] - box[0]
    return font, _balanced_name_lines(draw, text, font, max_width)


def _balanced_name_lines(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    if _text_width(draw, text, font) <= max_width:
        return [text]
    breakpoints = [index for index, char in enumerate(text) if char in "（(】]」』"]
    candidates: list[list[str]] = []
    for breakpoint in breakpoints:
        left = text[:breakpoint]
        right = text[breakpoint:]
        if left and right:
            candidates.append([left, right])
    candidates.append(_wrap_text(draw, text, font, max_width))
    fitting = [lines for lines in candidates if all(_text_width(draw, line, font) <= max_width for line in lines)]
    if fitting:
        return min(fitting, key=lambda lines: (len(lines), max(_text_width(draw, line, font) for line in lines)))
    return candidates[-1]


def _draw_name_lines(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    lines: list[str],
    font: ImageFont.ImageFont,
) -> None:
    line_spacing = 1
    line_boxes = [draw.textbbox((0, 0), line, font=font) for line in lines]
    line_heights = [bbox[3] - bbox[1] for bbox in line_boxes]
    total_height = sum(line_heights) + line_spacing * max(0, len(lines) - 1)
    y = box[1] + (box[3] - box[1] - total_height) / 2
    for line, bbox, height in zip(lines, line_boxes, line_heights, strict=True):
        draw.text((box[0] - bbox[0], y - bbox[1]), line, fill=TEXT, font=font)
        y += height + line_spacing


def _text_lines_height(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    font: ImageFont.ImageFont,
    *,
    line_spacing: int,
) -> int:
    return sum(draw.textbbox((0, 0), line, font=font)[3] - draw.textbbox((0, 0), line, font=font)[1] for line in lines) + line_spacing * max(0, len(lines) - 1)


def _attribute_pill_box(
    draw: ImageDraw.ImageDraw,
    attributes: list[str],
    font: ImageFont.ImageFont,
) -> tuple[int, int, int, int]:
    values = _attribute_display_items(attributes)
    width = _attribute_group_content_width(draw, values, font, icon_width=26, text_gap=5, group_gap=10) + 16
    width = max(76, min(178, width))
    return (454, 70, 454 + width, 102)


def _trait_pill_box(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
) -> tuple[int, int, int, int]:
    width = _text_width(draw, text, font) + 28
    width = max(68, min(150, width))
    return (70, 164, 70 + width, 198)


def _attribute_display_items(attributes: list[str]) -> list[str]:
    values = _attribute_values(*attributes)
    return values or ["未知"]


def _fit_attribute_group_font(
    draw: ImageDraw.ImageDraw,
    values: list[str],
    font: ImageFont.ImageFont,
    box: tuple[int, int, int, int],
    icon_width: int,
    text_gap: int,
    group_gap: int,
    *,
    min_size: int = 10,
) -> ImageFont.ImageFont:
    max_width = box[2] - box[0] - 12
    if not isinstance(font, ImageFont.FreeTypeFont):
        return font
    for size in range(font.size, min_size - 1, -1):
        candidate = font.font_variant(size=size)
        width = _attribute_group_content_width(draw, values, candidate, icon_width, text_gap, group_gap)
        if width <= max_width:
            return candidate
    return font.font_variant(size=min_size)


def _attribute_group_content_width(
    draw: ImageDraw.ImageDraw,
    values: list[str],
    font: ImageFont.ImageFont,
    icon_width: int,
    text_gap: int,
    group_gap: int,
) -> int:
    text_width = sum(_text_width(draw, value, font) for value in values)
    return len(values) * (icon_width + text_gap) + text_width + group_gap * max(0, len(values) - 1)


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
    chain_font = _fit_chain_font(draw, record, name_font, 560 - EVOLUTION_BOX_PADDING * 2)
    chain_box, placements = _evolution_chain_layout(draw, record, chain_font, max_width=560)

    for kind, text, width, x, condition in placements:
        token_box = (x, chain_box[1], x + width, chain_box[3])
        if kind == "arrow":
            arrow_center_x = x + width // 2
            _draw_evolution_arrow(draw, arrow_center_x, 303)
            _draw_evolution_condition(draw, condition, arrow_center_x, condition_font)
        else:
            _rounded(draw, token_box, 17, PILL, outline=ORANGE)
            draw.text(_evolution_name_text_position(draw, token_box, text, chain_font), text, fill=TEXT, font=chain_font)


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
    for size in range(font.size, 9, -1):
        candidate = font.font_variant(size=size)
        tokens = _evolution_tokens(draw, record, candidate, max_width=max_width)
        widths = [_evolution_token_width(draw, token, candidate) for token in tokens]
        if sum(widths) + EVOLUTION_TOKEN_GAP * max(0, len(tokens) - 1) <= max_width:
            return candidate
    return font.font_variant(size=10)


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


def _evolution_name_text_position(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    text: str,
    font: ImageFont.ImageFont,
) -> tuple[float, float]:
    bbox = draw.textbbox((0, 0), text, font=font)
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    x = box[0] + (box[2] - box[0] - width) / 2 - bbox[0]
    y = box[1] + (box[3] - box[1] - height) / 2 - bbox[1]
    return x, y


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

    return tokens


def _evolution_token_width(
    draw: ImageDraw.ImageDraw,
    token: tuple[str, str, str],
    font: ImageFont.ImageFont,
) -> int:
    kind, text, _ = token
    if kind == "arrow":
        return EVOLUTION_ARROW_WIDTH
    return _text_width(draw, text, font) + EVOLUTION_NAME_PADDING * 2


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
    font, lines = _fit_wrapped_text_to_box(draw, text, font, box, line_spacing=line_spacing)
    line_boxes = [draw.textbbox((0, 0), line, font=font) for line in lines]
    line_heights = [bbox[3] - bbox[1] for bbox in line_boxes]
    total_height = sum(line_heights) + line_spacing * max(0, len(lines) - 1)
    y = box[1] + (box[3] - box[1] - total_height) / 2
    for line, bbox, height in zip(lines, line_boxes, line_heights, strict=True):
        draw.text((box[0] - bbox[0], y - bbox[1]), line, fill=fill, font=font)
        y += height + line_spacing


def _fit_wrapped_text_to_box(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    box: tuple[int, int, int, int],
    *,
    line_spacing: int = 4,
    min_size: int = 16,
) -> tuple[ImageFont.ImageFont, list[str]]:
    max_width = box[2] - box[0]
    max_height = box[3] - box[1]
    if not isinstance(font, ImageFont.FreeTypeFont):
        return font, _wrap_text(draw, text, font, max_width)

    for size in range(font.size, min_size - 1, -1):
        candidate = font.font_variant(size=size)
        lines = _wrap_text(draw, text, candidate, max_width)
        line_boxes = [draw.textbbox((0, 0), line, font=candidate) for line in lines]
        line_heights = [bbox[3] - bbox[1] for bbox in line_boxes]
        total_height = sum(line_heights) + line_spacing * max(0, len(lines) - 1)
        if total_height <= max_height:
            return candidate, lines
    fitted = font.font_variant(size=min_size)
    return fitted, _wrap_text(draw, text, fitted, max_width)


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


def _format_attribute_text(attributes: list[str]) -> str:
    values = _attribute_values(*attributes)
    return "、".join(values) if values else "未知"


def _attribute_values(*attributes: str) -> list[str]:
    values: list[str] = []
    for attribute in attributes:
        values.extend(part for part in re.split(r"[、,，/ ]+", attribute.strip("、,，/ ")) if part)
    return values


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
    font = _fit_icon_text_font_to_box(draw, text, font, box, icon_width=icon_size[0] if icon is not None or fallback_icon_text else 0, gap=gap)
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


def _fit_icon_text_font_to_box(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    box: tuple[int, int, int, int],
    *,
    icon_width: int,
    gap: int,
    padding: int = 20,
    min_size: int = 14,
) -> ImageFont.ImageFont:
    max_width = box[2] - box[0] - padding - icon_width - (gap if icon_width else 0)
    if not isinstance(font, ImageFont.FreeTypeFont):
        return font
    for size in range(font.size, min_size - 1, -1):
        candidate = font.font_variant(size=size)
        bbox = draw.textbbox((0, 0), text, font=candidate)
        if bbox[2] - bbox[0] <= max_width:
            return candidate
    return font.font_variant(size=min_size)


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
