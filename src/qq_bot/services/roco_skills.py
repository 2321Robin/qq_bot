from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


DEFAULT_PET_DETAIL_DIR = Path(__file__).resolve().parents[3] / "data" / "roco_pet_details"
MAX_PET_NAMES_PER_VARIANT = 20
MAX_SKILL_VARIANTS_PER_QUERY = 10


@dataclass(frozen=True)
class SkillRecord:
    name: str
    level: str
    energy: str
    category: str
    power: str
    effect: str
    pet_name: str


@dataclass
class SkillVariant:
    name: str
    level: str
    energy: str
    category: str
    power: str
    effect: str
    pet_names: list[str]


def load_skill_records(path: Path = DEFAULT_PET_DETAIL_DIR) -> list[SkillRecord]:
    records: list[SkillRecord] = []
    for detail_path in sorted(path.glob("*.json")):
        detail = json.loads(detail_path.read_text(encoding="utf-8"))
        if not isinstance(detail, dict):
            raise ValueError("roco pet detail records must be objects")

        pet_name = _optional_string(detail, "name")
        skills = detail.get("skills", [])
        if not isinstance(skills, list):
            continue

        for group in skills:
            if not isinstance(group, dict):
                continue
            rows = group.get("rows", [])
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                record = _record_from_row(row, pet_name)
                if record.name:
                    records.append(record)
    return records


@lru_cache(maxsize=1)
def get_skill_records() -> tuple[SkillRecord, ...]:
    return tuple(load_skill_records())


def find_skills(
    records: list[SkillRecord] | tuple[SkillRecord, ...], query: str
) -> list[SkillRecord]:
    cleaned_query = query.strip()
    if not cleaned_query:
        return []

    exact_matches = [record for record in records if record.name == cleaned_query]
    if exact_matches:
        return exact_matches

    return [
        record
        for record in records
        if cleaned_query in record.name or record.name in cleaned_query
    ]


def group_skill_variants(
    records: list[SkillRecord] | tuple[SkillRecord, ...]
) -> list[SkillVariant]:
    variants: list[SkillVariant] = []
    variant_by_key: dict[tuple[str, str, str, str, str], SkillVariant] = {}
    for record in records:
        key = (
            record.name,
            record.energy,
            record.category,
            record.power,
            record.effect,
        )
        variant = variant_by_key.get(key)
        if variant is None:
            variant = SkillVariant(record.name, "", record.energy, record.category, record.power, record.effect, pet_names=[])
            variant_by_key[key] = variant
            variants.append(variant)
        if record.pet_name and record.pet_name not in variant.pet_names:
            variant.pet_names.append(record.pet_name)
    return variants


def format_skill_query_result(
    query: str, records: list[SkillRecord] | tuple[SkillRecord, ...]
) -> str:
    cleaned_query = query.strip()
    if not cleaned_query:
        return "用法：/技能 闪光"

    matches = find_skills(records, cleaned_query)
    if not matches:
        return f"本地技能表暂时没有收录“{cleaned_query}”。"

    variants = group_skill_variants(matches)
    shown_variants = variants[:MAX_SKILL_VARIANTS_PER_QUERY]
    parts = [_format_variant(variant) for variant in shown_variants]
    hidden_count = len(variants) - len(shown_variants)
    if hidden_count > 0:
        parts.append(f"另有 {hidden_count} 个匹配结果未显示")
    return "\n\n".join(parts)


def _record_from_row(row: dict[Any, Any], pet_name: str) -> SkillRecord:
    return SkillRecord(
        name=_optional_string(row, "技能"),
        level=_optional_string(row, "等级"),
        energy=_optional_string(row, "耗能"),
        category=_optional_string(row, "类型"),
        power=_optional_string(row, "威力"),
        effect=_optional_string(row, "效果"),
        pet_name=pet_name,
    )


def _optional_string(item: dict[Any, Any], key: str) -> str:
    value = item.get(key, "")
    return value.strip() if isinstance(value, str) else ""


def _format_variant(variant: SkillVariant) -> str:
    return "\n".join(
        [
            f"技能：{variant.name}",
            f"耗能：{_value_or_unknown(variant.energy)}",
            f"类型：{_value_or_unknown(variant.category)}",
            f"威力：{_value_or_unknown(variant.power)}",
            f"效果：{_value_or_unknown(variant.effect)}",
            f"可用精灵：{_format_pet_names(variant.pet_names)}",
        ]
    )


def _format_pet_names(pet_names: list[str]) -> str:
    shown_names = pet_names[:MAX_PET_NAMES_PER_VARIANT]
    text = "、".join(shown_names) if shown_names else "未知"
    if len(pet_names) > MAX_PET_NAMES_PER_VARIANT:
        text += f" 等 {len(pet_names)} 只"
    return text


def _value_or_unknown(value: str) -> str:
    return value or "未知"
