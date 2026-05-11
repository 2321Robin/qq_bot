"""Parsers for Roco BWiki pet detail pages."""

from __future__ import annotations

from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any


STAT_HEADERS = {"精力", "物攻", "魔攻", "物防", "魔防", "速度", "HP", "hp"}
SKILL_HEADING_MARKERS = ("技能", "本身就有", "技能石", "血脉", "转血脉", "学习")
CAPTURE_CLASSES = {
    "rocom_sprite_grament_name",
    "rocom_sprite_grament_attributes_text",
    "rocom_sprite_info_qualification_name",
    "rocom_sprite_info_qualification_value",
    "rocom_evolution_data",
    "rocom_sprite_skill_level",
    "rocom_sprite_skillName",
    "rocom_sprite_skillDamage",
    "rocom_sprite_skillType",
    "rocom_sprite_skill_power",
    "rocom_sprite_skillContent",
}
SKILL_FIELD_CLASSES = {
    "rocom_sprite_skill_level": "等级",
    "rocom_sprite_skillName": "技能",
    "rocom_sprite_skillDamage": "星级",
    "rocom_sprite_skillType": "类型",
    "rocom_sprite_skill_power": "威力",
    "rocom_sprite_skillContent": "效果",
}


class _BwikiParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.headings: list[tuple[str, str]] = []
        self.tables: list[dict[str, Any]] = []
        self.class_texts: dict[str, list[str]] = {class_name: [] for class_name in CAPTURE_CLASSES}
        self.skill_rows: list[dict[str, str]] = []
        self._current_tag = ""
        self._text_parts: list[str] = []
        self._current_heading = ""
        self._table_stack: list[dict[str, Any]] = []
        self._row_stack: list[list[dict[str, str]]] = []
        self._cell_stack: list[dict[str, Any]] = []
        self._element_stack: list[dict[str, Any]] = []
        self._skill_stack: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        class_names = _class_names(attrs)
        captures = [class_name for class_name in class_names if class_name in CAPTURE_CLASSES]
        skill_box = "rocom_sprite_skill_box" in class_names
        self._element_stack.append(
            {
                "tag": tag,
                "captures": [{"class": class_name, "text_parts": []} for class_name in captures],
                "skill_box": skill_box,
            }
        )
        if skill_box:
            self._skill_stack.append({})

        if tag in {"title", "h1", "h2", "h3", "h4"}:
            self._current_tag = tag
            self._text_parts = []
        elif tag == "table":
            self._table_stack.append({"heading": self._current_heading, "rows": []})
        elif tag == "tr" and self._table_stack:
            self._row_stack.append([])
        elif tag in {"th", "td"} and self._row_stack:
            self._cell_stack.append({"tag": tag, "text_parts": []})

    def handle_endtag(self, tag: str) -> None:
        if tag in {"title", "h1", "h2", "h3", "h4"} and self._current_tag == tag:
            text = _normalize_text("".join(self._text_parts))
            if tag == "title":
                self.title = text
            elif text:
                self.headings.append((tag, text))
                self._current_heading = text
            self._current_tag = ""
            self._text_parts = []
        elif tag in {"th", "td"} and self._cell_stack:
            cell = self._cell_stack.pop()
            text = _normalize_text("".join(cell["text_parts"]))
            if self._cell_stack:
                self._cell_stack[-1]["text_parts"].append(text)
            if self._row_stack:
                self._row_stack[-1].append({"tag": cell["tag"], "text": text})
        elif tag == "tr" and self._table_stack and self._row_stack:
            row = self._row_stack.pop()
            if row:
                self._table_stack[-1]["rows"].append(row)
        elif tag == "table" and self._table_stack:
            self.tables.append(self._table_stack.pop())

        if self._element_stack:
            element = self._element_stack.pop()
            for capture in element["captures"]:
                text = _normalize_text("".join(capture["text_parts"]))
                if text:
                    class_name = capture["class"]
                    self.class_texts[class_name].append(text)
                    if self._skill_stack and class_name in SKILL_FIELD_CLASSES:
                        self._skill_stack[-1][SKILL_FIELD_CLASSES[class_name]] = text
            if element["skill_box"] and self._skill_stack:
                skill_row = self._skill_stack.pop()
                if skill_row:
                    self.skill_rows.append(skill_row)

    def handle_data(self, data: str) -> None:
        for element in self._element_stack:
            for capture in element["captures"]:
                capture["text_parts"].append(data)
        if self._cell_stack:
            self._cell_stack[-1]["text_parts"].append(data)
        elif self._current_tag:
            self._text_parts.append(data)


def parse_pet_detail(source_url: str, html: str) -> dict[str, Any]:
    """Parse a BWiki pet detail HTML page into normalized pet detail data."""
    parser = _BwikiParser()
    parser.feed(html)

    profile: dict[str, str] = {}
    stats: dict[str, int] = {}
    skills: list[dict[str, Any]] = []

    for table in parser.tables:
        rows = table["rows"]
        heading = table["heading"]
        headers = _first_row_headers(rows)
        if _is_stats_table(headers):
            stats.update(_parse_stats_table(rows, headers))
        elif _is_skill_table(heading, headers):
            skill_rows = _parse_record_table(rows, headers)
            if skill_rows:
                skills.append({"source": heading or "技能", "rows": _dedupe_records(skill_rows)})
        else:
            profile.update(_parse_profile_table(rows))

    div_attributes = _parse_component_attributes(parser)
    div_evolution_condition = _first_class_text(parser, "rocom_evolution_data")
    for key, value in _parse_component_profile(parser, div_attributes, div_evolution_condition).items():
        profile.setdefault(key, value)
    if not stats:
        stats.update(_parse_component_stats(parser))
    if not skills:
        skills.extend(_parse_component_skills(parser))

    attributes = _split_values(profile.get("系别", "")) or div_attributes
    evolution_condition = profile.get("进化条件", "") or div_evolution_condition

    return {
        "name": _extract_name(parser),
        "source_url": source_url,
        "attributes": attributes,
        "evolution_condition": evolution_condition,
        "profile": profile,
        "stats": stats,
        "skills": skills,
        "metadata": {
            "parser_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
        },
    }


def _normalize_text(text: str) -> str:
    return " ".join(text.split())


def _class_names(attrs: list[tuple[str, str | None]]) -> set[str]:
    for name, value in attrs:
        if name == "class" and value:
            return set(value.split())
    return set()


def _extract_name(parser: _BwikiParser) -> str:
    for tag, text in parser.headings:
        if tag == "h1":
            return text
    if parser.title:
        return parser.title.split(" - ", maxsplit=1)[0].strip()
    return ""


def _first_row_headers(rows: list[list[dict[str, str]]]) -> list[str]:
    if not rows:
        return []
    first_row = rows[0]
    if not first_row or any(cell["tag"] != "th" for cell in first_row):
        return []
    return [cell["text"] for cell in first_row]


def _is_stats_table(headers: list[str]) -> bool:
    return sum(header in STAT_HEADERS for header in headers) >= 3


def _is_skill_table(heading: str, headers: list[str]) -> bool:
    return any("技能" in header for header in headers) or any(
        marker in heading for marker in SKILL_HEADING_MARKERS
    )


def _parse_stats_table(rows: list[list[dict[str, str]]], headers: list[str]) -> dict[str, int]:
    if len(rows) < 2:
        return {}

    values = [cell["text"] for cell in rows[1]]
    stats: dict[str, int] = {}
    for header, value in zip(headers, values, strict=False):
        if header in STAT_HEADERS:
            try:
                stats[header] = int(value)
            except ValueError:
                continue
    return stats


def _parse_record_table(
    rows: list[list[dict[str, str]]], headers: list[str]
) -> list[dict[str, str]]:
    if not headers:
        return []

    records: list[dict[str, str]] = []
    for row in rows[1:]:
        values = [cell["text"] for cell in row]
        record = dict(zip(headers, values, strict=False))
        if record:
            records.append(record)
    return records


def _parse_profile_table(rows: list[list[dict[str, str]]]) -> dict[str, str]:
    profile: dict[str, str] = {}
    for row in rows:
        cells = row[:]
        index = 0
        while index < len(cells):
            if cells[index]["tag"] == "th" and index + 1 < len(cells):
                key = cells[index]["text"]
                value = cells[index + 1]["text"]
                if key:
                    profile[key] = value
                index += 2
            else:
                index += 1
    return profile


def _first_class_text(parser: _BwikiParser, class_name: str) -> str:
    values = parser.class_texts.get(class_name, [])
    return values[0] if values else ""


def _parse_component_attributes(parser: _BwikiParser) -> list[str]:
    return _split_values(_first_class_text(parser, "rocom_sprite_grament_attributes_text"))


def _parse_component_profile(
    parser: _BwikiParser, attributes: list[str], evolution_condition: str
) -> dict[str, str]:
    profile: dict[str, str] = {}
    grament_name = _first_class_text(parser, "rocom_sprite_grament_name")
    if grament_name:
        number = grament_name.split(maxsplit=1)[0]
        if number:
            profile["编号"] = number
    if attributes:
        profile["系别"] = "、".join(attributes)
    if evolution_condition:
        profile["进化条件"] = evolution_condition
    return profile


def _parse_component_stats(parser: _BwikiParser) -> dict[str, int]:
    names = parser.class_texts["rocom_sprite_info_qualification_name"]
    values = parser.class_texts["rocom_sprite_info_qualification_value"]
    stats: dict[str, int] = {}
    for name, value in zip(names, values, strict=False):
        try:
            stats[name] = int(value)
        except ValueError:
            continue
    return stats


def _parse_component_skills(parser: _BwikiParser) -> list[dict[str, Any]]:
    if not parser.skill_rows:
        return []
    return [{"source": "技能", "rows": _dedupe_records(parser.skill_rows)}]


def _dedupe_records(records: list[dict[str, str]]) -> list[dict[str, str]]:
    unique_records: list[dict[str, str]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for record in records:
        key = tuple(record.items())
        if key in seen:
            continue
        seen.add(key)
        unique_records.append(record)
    return unique_records


def _split_values(value: str) -> list[str]:
    if not value:
        return []
    normalized = value.replace("/", "、").replace(",", "、").replace("，", "、")
    return [part.strip() for part in normalized.split("、") if part.strip()]
