from __future__ import annotations

from collections.abc import Sequence

from qq_bot.services.roco_pets import PetRecord, find_pet, get_pet_records
from qq_bot.services.roco_skills import (
    SkillRecord,
    find_skills,
    format_skill_query_result,
    get_skill_records,
)

ROCO_SUBJECT_MARKERS = ("洛克", "王国", "精灵", "宠物", "图鉴")
PET_FIELD_MARKERS = ("编号", "属性", "系别", "种族", "身高", "体重", "阶段", "简介")
SKILL_TOPIC_MARKERS = ("技能", "学习", "威力", "耗能")
MAX_CONTEXT_NAMES = 30
MAX_CONTEXT_SKILLS = 5
MAX_PET_SKILLS = 30


def build_roco_context(
    prompt: str,
    *,
    pet_records: Sequence[PetRecord] | None = None,
    skill_records: Sequence[SkillRecord] | None = None,
) -> str:
    cleaned_prompt = prompt.strip()
    if not cleaned_prompt:
        return ""

    has_roco_subject = _contains_any(cleaned_prompt, ROCO_SUBJECT_MARKERS)
    has_evolution_topic = "进化" in cleaned_prompt
    has_pet_field_topic = _contains_any(cleaned_prompt, PET_FIELD_MARKERS)
    has_skill_topic = (
        _contains_any(cleaned_prompt, SKILL_TOPIC_MARKERS)
        or (
            has_roco_subject
            and ("会" in cleaned_prompt or "能" in cleaned_prompt or "效果" in cleaned_prompt)
        )
    ) and not has_pet_field_topic
    if not (has_roco_subject or has_evolution_topic or has_pet_field_topic or has_skill_topic):
        return ""

    active_skill_records: Sequence[SkillRecord] = ()
    skill_names: list[str] = []
    if has_skill_topic:
        active_skill_records = skill_records if skill_records is not None else get_skill_records()
        skill_names = _skill_names_in_prompt(cleaned_prompt, active_skill_records)
        if len(skill_names) >= 2 and _asks_for_skill_intersection(cleaned_prompt):
            return _format_skill_intersection_context(skill_names, active_skill_records)
        if skill_names and not has_evolution_topic:
            return _format_skill_context(skill_names, active_skill_records)

    active_pet_records = pet_records if pet_records is not None else get_pet_records()
    pet_record = find_pet(active_pet_records, cleaned_prompt)

    if has_evolution_topic:
        if pet_record is None:
            if has_roco_subject:
                return _format_missing_context(cleaned_prompt, "进化")
            return ""
        return _format_evolution_context(pet_record, active_pet_records)

    if has_skill_topic:
        if pet_record is not None:
            return _format_pet_skill_context(pet_record, active_skill_records)
        if has_roco_subject:
            return _format_missing_context(cleaned_prompt, "技能")

    if pet_record is not None and (has_roco_subject or has_pet_field_topic):
        return _format_pet_context(pet_record)

    if has_roco_subject and (has_pet_field_topic or has_skill_topic):
        return _format_missing_context(cleaned_prompt, "本地资料")

    return ""


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _skill_names_in_prompt(prompt: str, records: Sequence[SkillRecord]) -> list[str]:
    unique_names = {record.name for record in records if record.name}
    names = [name for name in unique_names if name in prompt]
    names.sort(key=lambda name: (prompt.find(name), -len(name)))

    selected: list[str] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        selected.append(name)
        seen.add(name)
        if len(selected) >= MAX_CONTEXT_SKILLS:
            break
    return selected


def _asks_for_skill_intersection(prompt: str) -> bool:
    return (
        ("既" in prompt and "又" in prompt)
        or "同时" in prompt
        or "都" in prompt
        or "共同" in prompt
    )


def _format_skill_intersection_context(
    skill_names: list[str], records: Sequence[SkillRecord]
) -> str:
    selected_names = skill_names[:MAX_CONTEXT_SKILLS]
    pets_by_skill = {
        skill_name: {
            record.pet_name
            for record in records
            if record.name == skill_name and record.pet_name
        }
        for skill_name in selected_names
    }
    pet_sets = list(pets_by_skill.values())
    common_pets = set.intersection(*pet_sets) if pet_sets else set()

    lines = [
        "问题类型：技能交集",
        f"匹配技能：{'、'.join(selected_names)}",
        f"同时可学习精灵：{_format_pet_names(common_pets, records)}",
    ]
    for skill_name in selected_names:
        lines.append(f"{skill_name} 可学习精灵：{_format_pet_names(pets_by_skill[skill_name], records)}")
    lines.append("回答时只能依据以上本地技能表；没有交集就说明本地数据没有记录。")
    return "\n".join(lines)


def _format_skill_context(skill_names: list[str], records: Sequence[SkillRecord]) -> str:
    parts: list[str] = ["问题类型：技能资料"]
    for skill_name in skill_names[:MAX_CONTEXT_SKILLS]:
        matches = find_skills(records, skill_name)
        if matches:
            parts.append(format_skill_query_result(skill_name, records))
        else:
            parts.append(f"本地技能表暂时没有收录“{skill_name}”。")
    parts.append("回答时只能依据以上本地技能表；本地资料没有记录时要直接说明。")
    return "\n\n".join(parts)


def _format_pet_skill_context(record: PetRecord, records: Sequence[SkillRecord]) -> str:
    matching_records = [skill for skill in records if skill.pet_name == record.name]
    if not matching_records:
        return "\n".join(
            [
                "问题类型：精灵技能",
                f"匹配精灵：{record.name}",
                "本地技能表暂时没有收录该精灵的技能记录。",
                "回答时说明本地数据没有记录，不要凭模型记忆补全。",
            ]
        )

    lines = ["问题类型：精灵技能", f"匹配精灵：{record.name}", "本地技能记录："]
    shown_records = matching_records[:MAX_PET_SKILLS]
    for skill in shown_records:
        lines.append(
            f"- {skill.name}：等级{_value_or_unknown(skill.level)}，"
            f"耗能{_value_or_unknown(skill.energy)}，"
            f"类型{_value_or_unknown(skill.category)}，威力{_value_or_unknown(skill.power)}，"
            f"效果{_value_or_unknown(skill.effect)}"
        )
    if len(matching_records) > len(shown_records):
        lines.append(f"另有 {len(matching_records) - len(shown_records)} 条技能记录未显示。")
    lines.append("回答时只能依据以上本地技能表；本地资料没有记录时要直接说明。")
    return "\n".join(lines)


def _format_evolution_context(record: PetRecord, records: Sequence[PetRecord]) -> str:
    predecessors = _evolution_predecessors(record, records)
    successors = _evolution_successors(record, records)
    lines = [
        "问题类型：进化",
        f"匹配精灵：{record.name}",
        f"编号：{_value_or_unknown(record.number)}",
        f"属性：{_format_list(record.attributes)}",
        f"阶段：{_value_or_unknown(record.stage)}",
        f"进化链：{_format_chain(record, successors)}",
        f"进化条件：{_value_or_unknown(record.evolution_condition)}",
    ]

    if record.evolution_from:
        lines.append("来源进化：")
        lines.extend(f"- {relation.text}" for relation in record.evolution_from if relation.text)
    elif predecessors:
        lines.append(f"上一形态：{'、'.join(predecessor.name for predecessor in predecessors)}")
    elif not _condition_has_predecessor(record.evolution_condition, records):
        lines.append("上一形态：本地记录未写明")

    if record.evolution_to:
        lines.append("后续进化：")
        lines.extend(f"- {relation.text}" for relation in record.evolution_to if relation.text)
    elif successors:
        lines.append("后续进化：")
        for successor in successors:
            condition = _value_or_unknown(successor.evolution_condition)
            lines.append(f"- {successor.name}：{condition}")
    else:
        lines.append("后续进化：本地记录未写明")

    if record.source_url:
        lines.append(f"来源：{record.source_url}")
    lines.append("回答时只能依据以上本地图鉴字段；字段为空或未写明就说明本地数据没有记录。")
    return "\n".join(lines)


def _format_pet_context(record: PetRecord) -> str:
    lines = [
        "问题类型：精灵资料",
        f"匹配精灵：{record.name}",
        f"编号：{_value_or_unknown(record.number)}",
        f"属性：{_format_list(record.attributes)}",
        f"阶段：{_value_or_unknown(record.stage)}",
        f"进化链：{_format_list(record.evolution_chain, separator=' -> ')}",
        f"进化条件：{_value_or_unknown(record.evolution_condition)}",
    ]
    if record.race_value is not None:
        lines.append(f"种族值总和：{record.race_value}")
    if record.height_weight:
        lines.append(f"身高体重：{record.height_weight}")
    if record.body_length:
        lines.append(f"体长：{record.body_length}")
    if record.description:
        lines.append(f"简介：{record.description}")
    if record.source_url:
        lines.append(f"来源：{record.source_url}")
    lines.append("回答时只能依据以上本地图鉴字段；字段为空就说明本地数据没有记录。")
    return "\n".join(lines)


def _format_missing_context(prompt: str, question_type: str) -> str:
    return "\n".join(
        [
            f"问题类型：{question_type}",
            f"本地洛克王国资料暂时没有找到与“{prompt}”匹配的精灵或技能记录。",
            "回答时说明本地数据没有记录，不要凭模型记忆补全。",
        ]
    )


def _evolution_predecessors(record: PetRecord, records: Sequence[PetRecord]) -> list[PetRecord]:
    predecessors: list[PetRecord] = []
    for relation in record.evolution_from:
        predecessor = _find_pet_by_name(records, relation.source)
        if predecessor is not None and predecessor not in predecessors:
            predecessors.append(predecessor)

    if record.name in record.evolution_chain:
        index = record.evolution_chain.index(record.name)
        for name in record.evolution_chain[:index]:
            predecessor = _find_pet_by_name(records, name)
            if predecessor is not None and predecessor not in predecessors:
                predecessors.append(predecessor)

    predecessor_name = _condition_predecessor(record.evolution_condition, record.name, records)
    if predecessor_name:
        predecessor = _find_pet_by_name(records, predecessor_name)
        if predecessor is not None and predecessor not in predecessors:
            predecessors.append(predecessor)
    return predecessors


def _evolution_successors(record: PetRecord, records: Sequence[PetRecord]) -> list[PetRecord]:
    successors: list[PetRecord] = []
    seen: set[str] = set()
    for relation in record.evolution_to:
        successor = _find_pet_by_name(records, relation.target)
        if successor is not None and successor.name not in seen:
            successors.append(successor)
            seen.add(successor.name)

    if record.name in record.evolution_chain:
        index = record.evolution_chain.index(record.name)
        for name in record.evolution_chain[index + 1 :]:
            successor = _find_pet_by_name(records, name)
            if successor is not None and successor.name not in seen:
                successors.append(successor)
                seen.add(successor.name)

    for candidate in records:
        if candidate.name == record.name or candidate.name in seen:
            continue
        predecessor = _condition_predecessor(candidate.evolution_condition, candidate.name, records)
        if predecessor == record.name:
            successors.append(candidate)
            seen.add(candidate.name)
            continue
        if record.name in candidate.evolution_chain and candidate.name in candidate.evolution_chain:
            source_index = candidate.evolution_chain.index(record.name)
            candidate_index = candidate.evolution_chain.index(candidate.name)
            if source_index < candidate_index:
                successors.append(candidate)
                seen.add(candidate.name)
    return successors


def _condition_predecessor(
    condition: str, record_name: str, records: Sequence[PetRecord]
) -> str | None:
    if "由" not in condition or "进化" not in condition:
        return None
    names = sorted((record.name for record in records if record.name), key=len, reverse=True)
    for name in names:
        if name != record_name and f"由{name}" in condition:
            return name
    return None


def _condition_has_predecessor(condition: str, records: Sequence[PetRecord]) -> bool:
    return _condition_predecessor(condition, "", records) is not None


def _find_pet_by_name(records: Sequence[PetRecord], name: str) -> PetRecord | None:
    for record in records:
        if record.name == name:
            return record
    return None


def _format_chain(record: PetRecord, successors: list[PetRecord]) -> str:
    if len(record.evolution_chain) > 1:
        return " -> ".join(record.evolution_chain)
    relation_names = _dedupe_names(
        [
            *[relation.source for relation in record.evolution_from if relation.source],
            record.name,
            *[relation.target for relation in record.evolution_to if relation.target],
        ]
    )
    if len(relation_names) > 1:
        return " -> ".join(relation_names)
    if successors:
        return " -> ".join([record.name, *[successor.name for successor in successors]])
    return _format_list(record.evolution_chain, separator=" -> ")


def _dedupe_names(names: Sequence[str]) -> list[str]:
    deduped: list[str] = []
    for name in names:
        if name and name not in deduped:
            deduped.append(name)
    return deduped


def _format_pet_names(pet_names: set[str], records: Sequence[SkillRecord]) -> str:
    if not pet_names:
        return "本地技能表没有记录"
    ordered_names: list[str] = []
    for record in records:
        if record.pet_name in pet_names and record.pet_name not in ordered_names:
            ordered_names.append(record.pet_name)
        if len(ordered_names) >= MAX_CONTEXT_NAMES:
            break
    text = "、".join(ordered_names)
    if len(pet_names) > len(ordered_names):
        text += f" 等 {len(pet_names)} 只"
    return text


def _format_list(values: Sequence[str], *, separator: str = "、") -> str:
    return separator.join(value for value in values if value) or "未知"


def _value_or_unknown(value: str) -> str:
    return value or "未知"
