"""Parsers for Roco BWiki pet detail pages."""

from __future__ import annotations

from datetime import UTC, datetime
from html.parser import HTMLParser
import re
from typing import Any

from qq_bot.services.roco_evolution import build_evolution_edge, legacy_evolution_condition


STAT_HEADERS = {"精力", "生命", "物攻", "魔攻", "物防", "魔防", "速度", "HP", "hp"}
SKILL_HEADING_MARKERS = ("技能", "本身就有", "技能石", "血脉", "转血脉", "学习")
CAPTURE_CLASSES = {
    "rocom_sprite_grament_name",
    "rocom_sprite_grament_attributes_text",
    "rocom_sprite_info_total",
    "rocom_sprite_info_qualification_name",
    "rocom_sprite_info_qualification_value",
    "rocom_evolution_data",
    "rocom_spirit_evolution_level_num",
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
    "rocom_sprite_skillDamage": "耗能",
    "rocom_sprite_skillType": "类型",
    "rocom_sprite_skill_power": "威力",
    "rocom_sprite_skillContent": "效果",
}
PARSER_VERSION = 6


class _BwikiParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.headings: list[tuple[str, str]] = []
        self.tables: list[dict[str, Any]] = []
        self.class_texts: dict[str, list[str]] = {class_name: [] for class_name in CAPTURE_CLASSES}
        self.skill_rows: list[dict[str, str]] = []
        self.physique_items: list[dict[str, str]] = []
        self.evolution_edges: list[dict[str, str]] = []
        self._evolution_box_stack: list[dict[str, Any]] = []
        self._anchor_stack: list[dict[str, Any]] = []
        self.visible_texts: list[str] = []
        self._current_tag = ""
        self._text_parts: list[str] = []
        self._current_heading = ""
        self._table_stack: list[dict[str, Any]] = []
        self._row_stack: list[list[dict[str, str]]] = []
        self._cell_stack: list[dict[str, Any]] = []
        self._element_stack: list[dict[str, Any]] = []
        self._skill_stack: list[dict[str, str]] = []
        self._physique_stack: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        class_names = _class_names(attrs)
        captures = [class_name for class_name in class_names if class_name in CAPTURE_CLASSES]
        skill_box = "rocom_sprite_skill_box" in class_names
        evolution_box = "rocom_spirit_evolution_box" in class_names
        physique_item = tag == "li" and any(
            "rocom_sprite_info_physique" in element["classes"] for element in self._element_stack
        )
        self._element_stack.append(
            {
                "tag": tag,
                "classes": class_names,
                "captures": [{"class": class_name, "text_parts": []} for class_name in captures],
                "skill_box": skill_box,
                "evolution_box": evolution_box,
                "physique_item": physique_item,
            }
        )
        if evolution_box:
            self._evolution_box_stack.append({"sources": [], "targets": [], "conditions": []})
        if physique_item:
            self._physique_stack.append({"label": "", "text_parts": []})
        if tag == "a":
            self._anchor_stack.append({"attrs": attrs, "text_parts": []})
        elif tag == "img" and self._physique_stack:
            label = _attr_value(attrs, "alt")
            if label:
                self._physique_stack[-1]["label"] = label
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
        if tag == "a" and self._anchor_stack:
            self._capture_evolution_anchor()

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
                    if (
                        self._evolution_box_stack
                        and class_name == "rocom_spirit_evolution_level_num"
                    ):
                        self._evolution_box_stack[-1]["conditions"].append(text)
            if element["skill_box"] and self._skill_stack:
                skill_row = self._skill_stack.pop()
                if skill_row:
                    self.skill_rows.append(skill_row)
            if element["evolution_box"] and self._evolution_box_stack:
                box = self._evolution_box_stack.pop()
                self.evolution_edges.extend(_build_evolution_edges_from_box(box))
            if element["physique_item"] and self._physique_stack:
                physique_item = self._physique_stack.pop()
                text = _normalize_text("".join(physique_item["text_parts"]))
                if text:
                    self.physique_items.append({"label": physique_item["label"], "value": text})

    def handle_data(self, data: str) -> None:
        text = _normalize_text(data)
        if text:
            self.visible_texts.append(text)
        for element in self._element_stack:
            for capture in element["captures"]:
                capture["text_parts"].append(data)
        if self._anchor_stack:
            self._anchor_stack[-1]["text_parts"].append(data)
        if self._cell_stack:
            self._cell_stack[-1]["text_parts"].append(data)
        elif self._current_tag:
            self._text_parts.append(data)
        if self._physique_stack:
            self._physique_stack[-1]["text_parts"].append(data)

    def _capture_evolution_anchor(self) -> None:
        anchor = self._anchor_stack.pop()
        title = _attr_value(anchor["attrs"], "title") or _normalize_text(
            "".join(anchor["text_parts"])
        )
        if not title or not self._evolution_box_stack:
            return
        if any("rocom_spirit_evolution_1" in element["classes"] for element in self._element_stack):
            self._evolution_box_stack[-1]["sources"].append(title)
        if any("rocom_spirit_evolution_2" in element["classes"] for element in self._element_stack):
            self._evolution_box_stack[-1]["targets"].append(title)


def _build_evolution_edges_from_box(box: dict[str, Any]) -> list[dict[str, str]]:
    sources = [source for source in box.get("sources", []) if source]
    targets = [target for target in box.get("targets", []) if target]
    conditions = [condition for condition in box.get("conditions", []) if condition]
    if not sources or not targets:
        return []

    edge_count = max(len(sources), len(targets), len(conditions), 1)
    edges: list[dict[str, str]] = []
    for index in range(edge_count):
        source = _indexed_or_single(sources, index)
        target = _indexed_or_single(targets, index)
        if source and target:
            edges.append(
                build_evolution_edge(source, target, _indexed_or_single(conditions, index))
            )
    return edges


def _indexed_or_single(values: list[str], index: int) -> str:
    if index < len(values):
        return values[index]
    if len(values) == 1:
        return values[0]
    return ""


def parse_pet_detail(source_url: str, html: str) -> dict[str, Any]:
    """Parse a BWiki pet detail page into normalized pet detail data."""
    if _looks_like_raw_pet_template(html):
        return _parse_raw_pet_detail(source_url, html)
    if 'class="sprite-titlename"' in html or "sprite-info-attrlist" in html:
        return _parse_sprite_detail(source_url, html)

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
    physique = _parse_component_physique(parser)
    evolution_edges = parser.evolution_edges
    component_evolution_condition = _parse_component_evolution_condition(
        evolution_edges, _extract_name(parser)
    )
    div_evolution_condition = (
        _first_class_text(parser, "rocom_evolution_data") or component_evolution_condition
    )
    for key, value in _parse_component_profile(parser, div_attributes, physique).items():
        profile.setdefault(key, value)
    for key, value in _parse_component_trait(parser).items():
        profile.setdefault(key, value)
    table_evolution_condition = profile.get("进化条件", "")
    profile.pop("进化条件", None)
    if not stats:
        stats.update(_parse_component_stats(parser))
    if not skills:
        skills.extend(_parse_component_skills(parser))

    attributes = _split_values(profile.get("系别", "")) or div_attributes
    evolution_condition = table_evolution_condition or div_evolution_condition
    total_race_value = _parse_total_race_value(parser, stats)

    return {
        "name": _extract_name(parser),
        "source_url": source_url,
        "attributes": attributes,
        "evolution_condition": evolution_condition,
        "evolution_edges": evolution_edges,
        "total_race_value": total_race_value,
        "profile": profile,
        "stats": stats,
        "skills": skills,
        "metadata": {
            "parser_version": PARSER_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
        },
    }


def _looks_like_raw_pet_template(source: str) -> bool:
    text = source.lstrip()
    return text.startswith("{{精灵") or "{{精灵信息" in text[:200]


def _parse_raw_pet_detail(source_url: str, source: str) -> dict[str, Any]:
    fields = _parse_raw_template_fields(source)
    stats = _parse_raw_stats(fields)
    attributes = _raw_attributes(fields)
    profile = _raw_profile(fields, attributes)
    skills = _raw_skill_groups(fields)

    return {
        "name": fields.get("精灵名称", ""),
        "source_url": _without_raw_action(source_url),
        "attributes": attributes,
        "evolution_condition": fields.get("进化条件", ""),
        "evolution_edges": [],
        "total_race_value": sum(stats.values()) if len(stats) >= 6 else None,
        "profile": profile,
        "stats": stats,
        "skills": skills,
        "metadata": {
            "parser_version": PARSER_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
        },
    }


def _parse_raw_template_fields(source: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    current_key = ""
    current_parts: list[str] = []

    def flush() -> None:
        nonlocal current_key, current_parts
        if current_key:
            fields[current_key] = _normalize_raw_value("\n".join(current_parts))
        current_key = ""
        current_parts = []

    for line in source.splitlines():
        stripped_line = line.strip()
        if stripped_line.startswith("|") and "=" in stripped_line:
            flush()
            key, value = stripped_line[1:].split("=", maxsplit=1)
            current_key = key.strip()
            current_parts = [value]
        elif stripped_line.startswith("}}"):
            flush()
        elif current_key:
            current_parts.append(stripped_line)
    flush()
    return fields


def _normalize_raw_value(value: str) -> str:
    return value.strip().replace("&nbsp;", " ")


def _parse_raw_stats(fields: dict[str, str]) -> dict[str, int]:
    stats: dict[str, int] = {}
    for key in ("生命", "物攻", "魔攻", "物防", "魔防", "速度"):
        try:
            stats[key] = int(fields.get(key, ""))
        except ValueError:
            continue
    return stats


def _raw_attributes(fields: dict[str, str]) -> list[str]:
    return _dedupe_values(
        value
        for value in (fields.get("主属性", ""), fields.get("2属性", ""))
        if value and value != "-"
    )


def _raw_profile(fields: dict[str, str], attributes: list[str]) -> dict[str, str]:
    profile: dict[str, str] = {}
    if fields.get("精灵初阶名称"):
        profile["初阶"] = fields["精灵初阶名称"]
    if fields.get("精灵阶段"):
        profile["阶段"] = fields["精灵阶段"]
    if fields.get("精灵类型"):
        profile["类型"] = fields["精灵类型"]
    if attributes:
        profile["系别"] = "、".join(attributes)
    if fields.get("体型"):
        profile["体长"] = _append_unit(fields["体型"], "M")
    if fields.get("重量"):
        profile["体重"] = _append_unit(fields["重量"], "KG")
    if fields.get("特性"):
        profile["最佳拍档"] = fields["特性"]
        profile["最好的伙伴"] = fields["特性"]
    if fields.get("特性描述"):
        profile["简介"] = fields["特性描述"]
    if fields.get("精灵描述"):
        profile["精灵描述"] = fields["精灵描述"]
    return profile


def _append_unit(value: str, unit: str) -> str:
    text = value.strip()
    if not text or text.upper().endswith(unit):
        return text
    return f"{text}{unit}"


def _raw_skill_groups(fields: dict[str, str]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    _append_raw_skill_group(groups, "技能", fields.get("技能", ""), fields.get("技能解锁等级", ""))
    _append_raw_skill_group(groups, "血脉技能", fields.get("血脉技能", ""), "")
    _append_raw_skill_group(groups, "技能石", fields.get("可学技能石", ""), "")
    return groups


def _append_raw_skill_group(
    groups: list[dict[str, Any]], source: str, names_value: str, levels_value: str
) -> None:
    names = _split_csv(names_value)
    if not names:
        return
    levels = _split_csv(levels_value)
    rows: list[dict[str, str]] = []
    for index, name in enumerate(names):
        row = {
            "等级": _raw_skill_level(levels[index] if index < len(levels) else ""),
            "技能": name,
            "耗能": "",
            "类型": "",
            "威力": "",
            "效果": "",
        }
        rows.append(row)
    groups.append({"source": source, "rows": rows})


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip() and part.strip() != "-"]


def _raw_skill_level(value: str) -> str:
    text = value.strip()
    if not text or text == "-":
        return ""
    return f"LV{text}" if text.isdigit() else text


def _without_raw_action(source_url: str) -> str:
    return re.sub(r"([?&])action=raw&?", lambda match: match.group(1), source_url).rstrip("?&")


def _normalize_text(text: str) -> str:
    return " ".join(text.split())


def _class_names(attrs: list[tuple[str, str | None]]) -> set[str]:
    for name, value in attrs:
        if name == "class" and value:
            return set(value.split())
    return set()


def _attr_value(attrs: list[tuple[str, str | None]], key: str) -> str:
    for name, value in attrs:
        if name == key and value:
            return value
    return ""


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
    attributes: list[str] = []
    for value in parser.class_texts["rocom_sprite_grament_attributes_text"]:
        attributes.extend(_split_values(value))
    return _dedupe_values(attributes)


def _parse_component_evolution_condition(
    evolution_edges: list[dict[str, str]], current_name: str
) -> str:
    for edge in evolution_edges:
        if edge["target"] == current_name:
            return legacy_evolution_condition(edge)
    return ""


def _parse_component_profile(
    parser: _BwikiParser,
    attributes: list[str],
    physique: dict[str, str],
) -> dict[str, str]:
    profile: dict[str, str] = {}
    grament_name = _first_class_text(parser, "rocom_sprite_grament_name")
    if grament_name:
        number = grament_name.split(maxsplit=1)[0]
        if number:
            profile["编号"] = number
    if attributes:
        profile["系别"] = "、".join(attributes)
    profile.update(physique)
    return profile


def _parse_component_physique(parser: _BwikiParser) -> dict[str, str]:
    physique: dict[str, str] = {}
    for item in parser.physique_items:
        label = item["label"]
        value = item["value"].replace(" ", "")
        if not re.search(r"\d", value):
            continue
        if ("身高" in label or "体长" in label) and value:
            physique["体长"] = value
        elif "体重" in label and value:
            physique["体重"] = value
    return physique


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


def _parse_component_trait(parser: _BwikiParser) -> dict[str, str]:
    for index, text in enumerate(parser.visible_texts):
        if text != "特性":
            continue
        values: list[str] = []
        for candidate in parser.visible_texts[index + 1 :]:
            if candidate in {"精灵属性", "进化链", "克制表"}:
                break
            if candidate.startswith("等级:") or candidate in {
                "选择性格",
                "生命:",
                "速度:",
                "物攻:",
                "物防:",
                "魔攻:",
                "魔防:",
            }:
                break
            values.append(candidate)
        if len(values) >= 2:
            description = values[2] if len(values) >= 3 and values[1] == values[0] else values[1]
            return {"最佳拍档": values[0], "简介": description}
    return {}


def _parse_total_race_value(parser: _BwikiParser, stats: dict[str, int]) -> int | None:
    for text in parser.class_texts["rocom_sprite_info_total"]:
        if "种族值" not in text:
            continue
        match = re.search(r"\d+", text)
        if match:
            return int(match.group())
    if len(stats) >= 6:
        return sum(stats.values())
    return None


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


def _dedupe_values(values: list[str]) -> list[str]:
    unique_values: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique_values.append(value)
    return unique_values


# --- current BWiki sprite template (post-2026-07 migration) ---
# BWiki migrated detail pages from the rocom_sprite_* widget template to a
# sprite-* template.  _BwikiParser cannot read those pages, so parse_pet_detail
# dispatches on page structure and _BwikiSpriteParser parses the new template
# into the same normalized PetDetail schema.

_SPRITE_TYPE_ICON_RE = re.compile(r"属性\s*([^.]+?)\s*\.png$")
_SPRITE_SKILL_LEVEL_RE = re.compile(r"Lv\.?\s*(\d+)", re.IGNORECASE)
_SPRITE_TYPE_HIGHLIGHT = "受击伤害增加"


class _BwikiSpriteParser(HTMLParser):
    """Streaming parser for the current BWiki sprite template.

    Captures the pieces that map onto the normalized PetDetail schema: the
    title block (编号/名称), the stats list, the trait (最佳拍档/简介),
    the own attributes (属性克制 header row), physique, skill cards and the
    evolution chain sections.

    Captures are keyed to element ids rather than nesting depth: the template
    lays out sibling spans at the same depth (e.g. sprite-info-attrname next
    to sprite-info-attrnum), so depth bookkeeping would finalize sibling
    captures early.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_block: list[str] = []
        self.attrsum_text = ""
        self.attr_pairs: list[tuple[str, str]] = []
        self.trait_name = ""
        self.trait_desc = ""
        self.own_types: list[str] = []
        self.physique: dict[str, str] = {}
        self.skill_cards: list[dict[str, Any]] = []
        self.evolve_nodes: list[dict[str, str]] = []
        self._elem_stack: list[int] = []
        self._next_elem = 0
        self._caps: list[dict[str, Any]] = []
        self._pending_attr_name = ""

    def _new_elem(self) -> int:
        elem_id = self._next_elem
        self._next_elem += 1
        self._elem_stack.append(elem_id)
        return elem_id

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = {
            token for key, value in attrs if key == "class" and value for token in value.split()
        }
        elem = self._new_elem()
        if "sprite-titlename" in classes:
            self._caps.append({"kind": "title", "texts": [], "elem": elem})
        elif "sprite-info-desc" in classes:
            self._caps.append({"kind": "desc", "texts": [], "elem": elem})
        elif "sprite-info-attrsum" in classes:
            self._caps.append({"kind": "attrsum", "texts": [], "elem": elem})
        elif "sprite-info-attrname" in classes:
            self._caps.append({"kind": "attrname", "texts": [], "elem": elem})
        elif "sprite-info-attrnum" in classes:
            self._caps.append({"kind": "attrnum", "texts": [], "elem": elem})
        elif "sprite-trait-name" in classes:
            self._caps.append({"kind": "trait_name", "texts": [], "elem": elem})
        elif "sprite-trait-desc" in classes:
            self._caps.append({"kind": "trait_desc", "texts": [], "elem": elem})
        elif "sprite-info-type" in classes:
            self._caps.append({"kind": "type", "events": [], "elem": elem})
        elif "sprite_type" in classes and dict(attrs).get("title"):
            self._caps.append({"kind": "sprite_type", "alts": [], "elem": elem})
        elif tag == "div" and "skill-single" in classes:
            self._caps.append(
                {
                    "kind": "skill",
                    "elem": elem,
                    "name": "",
                    "typelist_events": [],
                    "desc": "",
                    "source": "",
                }
            )
        elif "skill-name" in classes:
            self._caps.append({"kind": "skill_name", "texts": [], "elem": elem})
        elif "skill-head-typelist" in classes:
            self._caps.append({"kind": "typelist", "events": [], "elem": elem})
        elif "skill-desc-atk" in classes:
            self._caps.append({"kind": "skill_desc", "texts": [], "elem": elem})
        elif "skill-source" in classes:
            self._caps.append({"kind": "skill_source", "texts": [], "elem": elem})
        elif "sprite-evolve-section" in classes:
            self._caps.append({"kind": "evolve", "node": "", "cond_texts": [], "elem": elem})
        if tag == "img":
            alt = dict(attrs).get("alt") or ""
            match = re.match(r"信息图标\s*(身高|体长|体重)\.png$", alt)
            if match:
                # the value lives in sibling spans of the img's parent block
                parent = self._elem_stack[-2] if len(self._elem_stack) > 1 else elem
                key = "体长" if match.group(1) == "身高" else match.group(1)
                self._caps.append({"kind": "physique", "key": key, "texts": [], "elem": parent})
            self._dispatch("alt", alt)
        if tag == "div" and "sprite-evolve-btn" in classes:
            for cap in reversed(self._caps):
                if cap["kind"] == "evolve" and not cap["node"]:
                    cap["node"] = dict(attrs).get("data-link") or ""
                    break
        if tag in ("span", "div") and "sprite-evolve-cond" in classes:
            for cap in reversed(self._caps):
                if cap["kind"] == "evolve" and "cond_elem" not in cap:
                    cap["cond_elem"] = elem
                    break

    def handle_data(self, data: str) -> None:
        self._dispatch("text", data)

    def handle_endtag(self, tag: str) -> None:
        closed = self._elem_stack.pop()
        remaining: list[dict[str, Any]] = []
        for cap in self._caps:
            if cap.get("elem") == closed:
                self._finalize(cap)
            else:
                remaining.append(cap)
        self._caps = remaining

    def _dispatch(self, kind: str, value: str) -> None:
        for cap in self._caps:
            cap_kind = cap["kind"]
            if cap_kind == "type":
                cap["events"].append((kind, value))
            elif cap_kind == "typelist":
                cap["events"].append((kind, value))
            elif cap_kind == "sprite_type" and kind == "alt":
                match = _SPRITE_TYPE_ICON_RE.search(value)
                if match:
                    cap["alts"].append(match.group(1))
            elif cap_kind == "evolve":
                cond_elem = cap.get("cond_elem")
                if cond_elem is not None and cond_elem in self._elem_stack:
                    if kind == "text":
                        cap["cond_texts"].append(value)
            elif kind == "text":
                texts = cap.get("texts")
                if texts is not None:
                    texts.append(value)

    def _finalize(self, cap: dict[str, Any]) -> None:
        kind = cap["kind"]
        if kind == "title":
            self.title_block = [_normalize_text(t) for t in cap["texts"] if _normalize_text(t)]
        elif kind == "desc":
            text = _normalize_text("".join(cap["texts"]))
            if text:
                self.physique["精灵描述"] = text
        elif kind == "attrsum":
            self.attrsum_text = _normalize_text("".join(cap["texts"]))
        elif kind == "attrname":
            self._pending_attr_name = _normalize_text("".join(cap["texts"])).rstrip(":：")
        elif kind == "attrnum":
            value = _normalize_text("".join(cap["texts"]))
            if self._pending_attr_name and value.isdigit():
                self.attr_pairs.append((self._pending_attr_name, value))
                self._pending_attr_name = ""
        elif kind == "trait_name":
            self.trait_name = _normalize_text("".join(cap["texts"]))
        elif kind == "trait_desc":
            self.trait_desc = _normalize_text("".join(cap["texts"]))
        elif kind == "type":
            for event_kind, value in cap["events"]:
                if event_kind == "text" and _SPRITE_TYPE_HIGHLIGHT in value:
                    break
                if event_kind == "alt":
                    match = _SPRITE_TYPE_ICON_RE.search(value)
                    if match:
                        self.own_types.append(match.group(1))
        elif kind == "sprite_type":
            self.own_types.extend(cap["alts"])
        elif kind == "skill_name":
            for other in reversed(self._caps):
                if other["kind"] == "skill":
                    other["name"] = _normalize_text("".join(cap["texts"]))
                    break
        elif kind == "physique":
            self.physique[cap["key"]] = _normalize_text("".join(cap["texts"]))
        elif kind == "typelist":
            for other in reversed(self._caps):
                if other["kind"] == "skill":
                    other["typelist_events"] = cap["events"]
                    break
        elif kind == "skill_desc":
            for other in reversed(self._caps):
                if other["kind"] == "skill":
                    other["desc"] = _normalize_text("".join(cap["texts"]))
                    break
        elif kind == "skill_source":
            for other in reversed(self._caps):
                if other["kind"] == "skill":
                    other["source"] = _normalize_text("".join(cap["texts"]))
                    break
        elif kind == "skill":
            self.skill_cards.append(
                {
                    "name": cap["name"],
                    "typelist_events": cap["typelist_events"],
                    "desc": cap["desc"],
                    "source": cap["source"],
                }
            )
        elif kind == "evolve":
            condition = _normalize_text("".join(cap["cond_texts"]))
            if cap["node"]:
                self.evolve_nodes.append({"node": cap["node"], "condition": condition})


def _sprite_skill_rows(cards: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Turn captured skill cards into normalized skill rows (no 系别 column)."""
    rows: list[dict[str, str]] = []
    for card in cards:
        name = card["name"]
        if not name:
            continue
        energy = ""
        category = ""
        power = ""
        seen_label = False
        label_count = 0
        value_texts: list[str] = []
        for event_kind, value in card["typelist_events"]:
            if event_kind == "text":
                text = _normalize_text(value)
                if not text:
                    continue
                if text in ("耗能", "分类", "系别", "威力") and label_count < 4:
                    label_count += 1
                    seen_label = True
                    continue
                value_texts.append(text)
            elif seen_label and event_kind == "alt":
                match = re.search(r"类别\s*(.+?)(?:\.png)?$", value)
                if match:
                    category = match.group(1)
        if value_texts:
            energy = value_texts[0]
            power = value_texts[-1]
        level = ""
        match = _SPRITE_SKILL_LEVEL_RE.search(card["source"])
        if match:
            level = f"LV{match.group(1)}"
        rows.append(
            {
                "等级": level,
                "技能": name,
                "耗能": energy,
                "类型": category,
                "威力": power,
                "效果": card["desc"],
            }
        )
    return rows


def _sprite_evolution_edges(nodes: list[dict[str, str]]) -> list[dict[str, str]]:
    """Pair consecutive evolve-section nodes into edges.

    The section list can contain several branches sharing a prefix (e.g.
    喵喵▶喵呜▶魔力猫▶叶冕魔力猫 then 喵喵▶喵呜▶魔力猫▶武斗酷猫), so the
    node list is split into chains at repeated nodes; each node's condition
    describes the transition from the previous node (empty for a chain head).
    Edges from shared prefixes are deduplicated.
    """
    chains: list[list[str]] = []
    current: list[str] = []
    for node in nodes:
        name = node["node"]
        if name in current:
            chains.append(current)
            split = current.index(name)
            current = current[:split] + [name]
        else:
            current.append(name)
    if current:
        chains.append(current)

    edges: list[dict[str, str]] = []
    seen_edges: set[tuple[str, str, str]] = set()
    for chain in chains:
        node_conditions = {node["node"]: node["condition"] for node in nodes}
        for previous, target in zip(chain, chain[1:]):
            condition = node_conditions.get(target, "")
            edge = build_evolution_edge(previous, target, condition)
            key = (edge["source"], edge["target"], edge["condition"])
            if edge["source"] and edge["target"] and key not in seen_edges:
                seen_edges.add(key)
                edges.append(edge)
    return edges


def _parse_sprite_detail(source_url: str, html: str) -> dict[str, Any]:
    """Parse a detail page rendered with the current sprite template."""
    parser = _BwikiSpriteParser()
    parser.feed(html)

    title = parser.title_block
    number = title[0] if title else ""
    name = title[1] if len(title) > 1 else ""
    types = _dedupe_values(parser.own_types)

    stats = {stat_name: int(value) for stat_name, value in parser.attr_pairs}
    total_race_value: int | None = None
    match = re.search(r"(\d+)", parser.attrsum_text)
    if match:
        total_race_value = int(match.group(1))

    profile: dict[str, str] = {"编号": number, "系别": "、".join(types)}
    if parser.physique:
        profile.update(parser.physique)
    if parser.trait_name:
        profile["最佳拍档"] = parser.trait_name
    if parser.trait_desc:
        profile["简介"] = parser.trait_desc

    skill_rows = _sprite_skill_rows(parser.skill_cards)
    skills = [{"source": "技能", "rows": skill_rows}] if skill_rows else []

    edges = _sprite_evolution_edges(parser.evolve_nodes)
    evolution_condition = edges[0]["forward_text"] if edges else ""

    return {
        "name": name,
        "source_url": source_url,
        "attributes": types,
        "evolution_condition": evolution_condition,
        "evolution_edges": edges,
        "total_race_value": total_race_value,
        "profile": profile,
        "stats": stats,
        "skills": skills,
        "metadata": {
            "parser_version": PARSER_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
        },
    }
