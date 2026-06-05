from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any


DEFAULT_PET_DETAIL_DIR = Path(__file__).resolve().parents[3] / "data" / "roco_pet_details"

STAT_KEY_MAP = {
    "生命": "hp",
    "物攻": "physical_attack",
    "魔攻": "magic_attack",
    "物防": "physical_defense",
    "魔防": "magic_defense",
    "速度": "speed",
}

DETAIL_ALIAS_MAP = {
    "喵喵": ["草系御三家"],
    "魔力猫": ["魔力喵"],
    "火花": ["火系御三家"],
    "水蓝蓝": ["水系御三家"],
}

DETAIL_EVOLUTION_CHAIN_MAP = {
    "喵喵": ["喵喵", "喵呜", "魔力猫"],
    "喵呜": ["喵喵", "喵呜", "魔力猫"],
    "魔力猫": ["喵喵", "喵呜", "魔力猫"],
    "火花": ["火花", "焰火", "火神"],
    "焰火": ["火花", "焰火", "火神"],
    "火神": ["火花", "焰火", "火神"],
    "水蓝蓝": ["水蓝蓝", "波波拉", "水灵"],
    "波波拉": ["水蓝蓝", "波波拉", "水灵"],
    "水灵": ["水蓝蓝", "波波拉", "水灵"],
}


@dataclass(frozen=True)
class EvolutionRelation:
    source: str
    target: str
    condition: str
    text: str


@dataclass(frozen=True)
class PetRecord:
    name: str
    aliases: list[str]
    number: str
    attributes: list[str]
    stage: str
    evolution_chain: list[str]
    evolution_condition: str
    source_url: str
    height_weight: str = ""
    body_length: str = ""
    favorite_partner: str = ""
    description: str = ""
    race_value: int | None = None
    stats: dict[str, int] | None = None
    evolution_from: list[EvolutionRelation] = field(default_factory=list)
    evolution_to: list[EvolutionRelation] = field(default_factory=list)


def load_pet_records(path: Path = DEFAULT_PET_DETAIL_DIR) -> list[PetRecord]:
    if path.is_dir():
        records: list[PetRecord] = []
        for detail_path in sorted(path.glob("*.json")):
            item = json.loads(detail_path.read_text(encoding="utf-8"))
            records.append(_record_from_detail(item))
        return [_with_detail_compatibility(record) for record in records]

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("roco pet data must be a list")
    return [_record_from_item(item) for item in data]


@lru_cache(maxsize=1)
def get_pet_records() -> tuple[PetRecord, ...]:
    return tuple(load_pet_records())


def find_pet(records: list[PetRecord] | tuple[PetRecord, ...], query: str) -> PetRecord | None:
    cleaned_query = query.strip()
    if not cleaned_query:
        return None

    for candidate_number in _query_number_candidates(cleaned_query):
        for record in records:
            if _same_number(record.number, candidate_number):
                return record

    direct_match = _best_direct_name_match(records, cleaned_query)
    if direct_match is not None:
        return direct_match

    chain_match = _best_chain_match(records, cleaned_query)
    if chain_match is not None:
        return chain_match
    return None


def format_pet_query_result(query: str, records: list[PetRecord] | tuple[PetRecord, ...]) -> str:
    cleaned_query = query.strip()
    if not cleaned_query:
        return "用法：/精灵 迪莫 或 /洛克 迪莫"

    record = find_pet(records, cleaned_query)
    if record is None:
        return f"本地图鉴暂时没有收录“{cleaned_query}”。"

    return format_pet_record(record)


def format_pet_record(record: PetRecord) -> str:
    return "\n".join(
        [
            record.name,
            f"编号：{_value_or_unknown(record.number)}",
            f"属性：{_list_or_unknown(record.attributes)}",
            f"阶段：{_value_or_unknown(record.stage)}",
            f"进化链：{_list_or_unknown(record.evolution_chain, separator=' -> ')}",
            f"进化条件：{_value_or_unknown(record.evolution_condition)}",
            f"来源：{_value_or_unknown(record.source_url)}",
        ]
    )


def _record_from_item(item: Any) -> PetRecord:
    if not isinstance(item, dict):
        raise ValueError("roco pet records must be objects")
    return PetRecord(
        name=_string_value(item, "name"),
        aliases=_string_list(item, "aliases"),
        number=_string_value(item, "number"),
        attributes=_string_list(item, "attributes"),
        stage=_string_value(item, "stage"),
        evolution_chain=_string_list(item, "evolution_chain"),
        evolution_condition=_string_value(item, "evolution_condition"),
        source_url=_string_value(item, "source_url"),
        height_weight=_string_value(item, "height_weight"),
        body_length=_string_value(item, "body_length"),
        favorite_partner=_string_value(item, "favorite_partner"),
        description=_string_value(item, "description"),
        race_value=_optional_int(item, "race_value"),
        stats=_stats_value(item),
        evolution_from=[],
        evolution_to=[],
    )


def _record_from_detail(item: Any) -> PetRecord:
    if not isinstance(item, dict):
        raise ValueError("roco pet detail records must be objects")

    profile = _dict_value(item, "profile")
    name = _string_value(item, "name")
    number = _string_from_mapping(profile, "编号")
    attributes = _string_list(item, "attributes")
    evolution = _dict_value(item, "evolution")

    return PetRecord(
        name=name,
        aliases=_detail_aliases(item, name),
        number=number,
        attributes=attributes,
        stage=_string_from_mapping(profile, "阶段") or "未知",
        evolution_chain=_detail_evolution_chain(item, name),
        evolution_condition=_detail_evolution_condition(item),
        source_url=_string_value(item, "source_url"),
        height_weight=_string_from_mapping(profile, "体重") or _string_from_mapping(profile, "身高体重"),
        body_length=_string_from_mapping(profile, "体长"),
        favorite_partner=(
            _string_from_mapping(profile, "最好的伙伴")
            or _string_from_mapping(profile, "最佳拍档")
        ),
        description=_string_from_mapping(profile, "简介") or _first_skill_effect(item),
        race_value=_optional_int(item, "total_race_value"),
        stats=_detail_stats_value(item),
        evolution_from=_detail_evolution_relations(evolution, "from"),
        evolution_to=_detail_evolution_relations(evolution, "to"),
    )


def _with_detail_compatibility(record: PetRecord) -> PetRecord:
    aliases = [*record.aliases]
    for alias in DETAIL_ALIAS_MAP.get(record.name, []):
        if alias not in aliases:
            aliases.append(alias)

    return replace(
        record,
        aliases=aliases,
        evolution_chain=DETAIL_EVOLUTION_CHAIN_MAP.get(record.name, record.evolution_chain),
    )


def _detail_evolution_chain(item: dict[str, Any], name: str) -> list[str]:
    chain = _string_list(item, "evolution_chain")
    return chain or ([name] if name else [])


def _detail_evolution_condition(item: dict[str, Any]) -> str:
    evolution = _dict_value(item, "evolution")
    condition = _string_from_mapping(evolution, "evolution_condition")
    return condition or _string_value(item, "evolution_condition")


def _detail_evolution_relations(evolution: dict[str, Any], key: str) -> list[EvolutionRelation]:
    raw_relations = evolution.get(key, [])
    if not isinstance(raw_relations, list):
        return []

    relations: list[EvolutionRelation] = []
    for raw_relation in raw_relations:
        if not isinstance(raw_relation, dict):
            continue
        relation = EvolutionRelation(
            source=_string_from_mapping(raw_relation, "source"),
            target=_string_from_mapping(raw_relation, "target"),
            condition=_string_from_mapping(raw_relation, "condition"),
            text=_string_from_mapping(raw_relation, "text"),
        )
        if relation.source or relation.target or relation.text:
            relations.append(relation)
    return relations


def _dict_value(item: dict[str, Any], key: str) -> dict[str, Any]:
    value = item.get(key, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def _string_from_mapping(item: dict[str, Any], key: str) -> str:
    value = item.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value.strip()


def _derived_aliases(name: str) -> list[str]:
    aliases: list[str] = []
    for marker in ("（", "("):
        if marker in name:
            alias = name.split(marker, 1)[0].strip()
            if alias and alias != name and alias not in aliases:
                aliases.append(alias)
    return aliases


def _detail_aliases(item: dict[str, Any], name: str) -> list[str]:
    aliases = _string_list(item, "aliases")
    for alias in _derived_aliases(name):
        if alias not in aliases:
            aliases.append(alias)
    return aliases


def _detail_stats_value(item: dict[str, Any]) -> dict[str, int] | None:
    raw_stats = item.get("stats")
    if raw_stats is None:
        return None
    if not isinstance(raw_stats, dict):
        raise ValueError("stats must be an object")

    stats: dict[str, int] = {}
    for key, value in raw_stats.items():
        if not isinstance(key, str):
            raise ValueError("stats keys must be strings")
        if not isinstance(value, int):
            raise ValueError("stats values must be integers")
        stats[STAT_KEY_MAP.get(key, key)] = value
    return stats


def _first_skill_effect(item: dict[str, Any]) -> str:
    skills = item.get("skills", [])
    if not isinstance(skills, list):
        return ""
    for group in skills:
        if not isinstance(group, dict):
            continue
        rows = group.get("rows", [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            effect = row.get("效果", "")
            if isinstance(effect, str) and effect.strip():
                return effect.strip()
    return ""


def _string_value(item: dict[str, Any], key: str) -> str:
    value = item.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value.strip()


def _string_list(item: dict[str, Any], key: str) -> list[str]:
    value = item.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list")
    values: list[str] = []
    for part in value:
        if not isinstance(part, str):
            raise ValueError(f"{key} must contain strings")
        cleaned = part.strip()
        if cleaned:
            values.append(cleaned)
    return values


def _optional_int(item: dict[str, Any], key: str) -> int | None:
    value = item.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value


def _stats_value(item: dict[str, Any]) -> dict[str, int] | None:
    value = item.get("stats")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("stats must be an object")

    stats: dict[str, int] = {}
    for key, stat_value in value.items():
        if not isinstance(key, str):
            raise ValueError("stats keys must be strings")
        if not isinstance(stat_value, int):
            raise ValueError("stats values must be integers")
        stats[key] = stat_value
    return stats


def _query_number_candidates(query: str) -> list[str]:
    explicit_matches = re.findall(r"(?:序号|编号|图鉴编号|图鉴|No\.?|NO\.?|no\.?|#)\s*0*(\d{1,3})(?!\d)", query)
    if explicit_matches:
        return [_normalize_number(match) for match in explicit_matches]

    compact = re.sub(r"[\s:：#？?。,.，、!！]", "", query)
    for word in (
        "洛克王国",
        "洛克",
        "王国",
        "世界",
        "精灵",
        "宠物",
        "图鉴编号",
        "图鉴",
        "序号",
        "编号",
        "No.",
        "NO.",
        "no.",
        "No",
        "NO",
        "no",
        "怎么",
        "如何",
        "进化条件",
        "进化",
        "条件",
        "是什么",
        "查询",
        "查",
        "一下",
        "的",
    ):
        compact = compact.replace(word, "")
    if compact.isdigit() and 1 <= len(compact) <= 3:
        return [_normalize_number(compact)]
    return []


def _normalize_number(number: str) -> str:
    return str(int(number)).zfill(3)


def _same_number(record_number: str, query_number: str) -> bool:
    if not record_number or not query_number:
        return False
    try:
        return int(record_number) == int(query_number)
    except ValueError:
        return record_number == query_number


def _best_direct_name_match(records: list[PetRecord] | tuple[PetRecord, ...], query: str) -> PetRecord | None:
    return _best_named_match(records, query, include_chain=False)


def _best_chain_match(records: list[PetRecord] | tuple[PetRecord, ...], query: str) -> PetRecord | None:
    return _best_named_match(records, query, include_chain=True)


def _best_named_match(
    records: list[PetRecord] | tuple[PetRecord, ...],
    query: str,
    *,
    include_chain: bool,
) -> PetRecord | None:
    best: tuple[int, int, int, PetRecord] | None = None
    for index, record in enumerate(records):
        names = [record.name, *record.aliases]
        if include_chain:
            names.extend(record.evolution_chain)
        for name in names:
            if not name:
                continue
            if query == name:
                score = 3
            elif name in query:
                score = 2
            elif query in name:
                score = 1
            else:
                continue
            candidate = (score, len(name), -index, record)
            if best is None or candidate[:3] > best[:3]:
                best = candidate
    return best[3] if best is not None else None

def _value_or_unknown(value: str) -> str:
    return value or "未知"


def _list_or_unknown(values: list[str], *, separator: str = "、") -> str:
    return separator.join(values) if values else "未知"
