from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any


LEVEL_CONDITION_RE = re.compile(r"^(?:(?:等级|升至)\s*)?(\d+)\s*级?(?:进化)?$")
ATTRIBUTE_BATTLE_RE = re.compile(
    r"^(?:打败|击败)([一二两三四五六七八九十\d]+)(?:只|个)?(.+?系)(?:精灵|宠物)?(?:进化)?$"
)
EVOLUTION_EDGE_KEYS = ("source", "target", "condition", "raw_condition", "forward_text", "backward_text")
FINAL_EVOLUTION_NOTES = {"无法进化", "暂无普通等级进化条件。"}

def normalize_evolution_condition(condition: str) -> str:
    """Normalize a raw evolution condition into a reusable edge condition."""
    text = _string_value(condition)
    if not text:
        return ""

    level_match = LEVEL_CONDITION_RE.fullmatch(text)
    if level_match:
        return f"升至{level_match.group(1)}级"

    battle_match = ATTRIBUTE_BATTLE_RE.fullmatch(text)
    if battle_match:
        count = _chinese_count_to_digit(battle_match.group(1))
        attribute = battle_match.group(2)
        return f"击败{count}个{attribute}精灵"

    return text


def build_evolution_edge(source: str, target: str, condition: str) -> dict[str, str]:
    """Build a normalized edge between two pet names."""
    source_name = _string_value(source)
    target_name = _string_value(target)
    raw_condition = _string_value(condition)
    normalized_condition = normalize_evolution_condition(raw_condition)
    edge = {
        "source": source_name,
        "target": target_name,
        "condition": normalized_condition,
        "raw_condition": raw_condition,
    }
    edge["forward_text"] = evolution_forward_text(edge)
    edge["backward_text"] = evolution_backward_text(edge)
    return edge


def evolution_forward_text(edge: dict[str, Any]) -> str:
    target = _string_value(edge.get("target"))
    condition = _string_value(edge.get("condition"))
    if condition:
        return f"{condition}可进化为{target}"
    return f"可进化为{target}"


def evolution_backward_text(edge: dict[str, Any]) -> str:
    source = _string_value(edge.get("source"))
    condition = _string_value(edge.get("condition"))
    if not condition:
        return f"可由{source}进化得"
    if condition.endswith("进化"):
        return f"可由{source}{condition}得"
    return f"可由{source}{condition}进化得"


def legacy_evolution_condition(edge: dict[str, Any]) -> str:
    """Return the historic scalar wording used by parsed detail records."""
    source = _string_value(edge.get("source"))
    condition = _string_value(edge.get("condition"))
    if not source:
        return condition
    if not condition:
        return f"由{source}进化"
    if condition.startswith("升至") and condition.endswith("级"):
        return f"由{source}等级{condition[len('升至') :]}进化"
    if condition.startswith("击败") and condition.endswith("精灵"):
        return f"由{source}{condition}进化"
    return f"由{source}{condition}"


def normalize_pet_detail_directory(directory: Path) -> list[Path]:
    """Write normalized evolution relations into every detail JSON file in a directory."""
    paths = sorted(Path(directory).glob("*.json"))
    details = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    normalize_pet_details(details)
    for path, detail in zip(paths, details, strict=True):
        path.write_text(json.dumps(detail, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return paths


def normalize_pet_details(details: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach normalized evolution relations to each detail."""
    names = [_string_value(detail.get("name")) for detail in details]
    global_edges = _collect_global_evolution_edges(details, names)
    edges = _dedupe_edges([
        *global_edges,
        *_infer_sourceless_evolution_edges(details, global_edges),
    ])
    incoming: dict[str, list[dict[str, str]]] = {}
    outgoing: dict[str, list[dict[str, str]]] = {}
    for edge in edges:
        outgoing.setdefault(edge["source"], []).append(edge)
        incoming.setdefault(edge["target"], []).append(edge)

    for detail in details:
        name = _string_value(detail.get("name"))
        incoming_relations = [_relation_from_edge(edge, edge["backward_text"]) for edge in incoming.get(name, [])]
        outgoing_relations = [_relation_from_edge(edge, edge["forward_text"]) for edge in outgoing.get(name, [])]
        evolution_condition = _complete_evolution_condition(
            incoming_relations,
            outgoing_relations,
            _string_value(detail.get("evolution_condition")),
        )
        detail["evolution_condition"] = evolution_condition
        detail["evolution"] = {
            "from": incoming_relations,
            "to": outgoing_relations,
            "evolution_condition": evolution_condition,
        }
    return details


def _collect_global_evolution_edges(details: list[dict[str, Any]], names: list[str]) -> list[dict[str, str]]:
    edges: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    def add_edge(edge: dict[str, str] | None) -> None:
        if edge is None:
            return
        source = edge["source"]
        target = edge["target"]
        condition = edge["condition"]
        if not source or not target:
            return
        key = (source, target, condition)
        if key in seen:
            return
        seen.add(key)
        edges.append(edge)

    for detail in details:
        for raw_edge in _list_value(detail.get("evolution_edges")):
            add_edge(_edge_from_mapping(raw_edge))

    for detail in details:
        evolution = detail.get("evolution")
        if isinstance(evolution, dict):
            for raw_relation in _list_value(evolution.get("from")):
                add_edge(_edge_from_mapping(raw_relation))
            for raw_relation in _list_value(evolution.get("to")):
                add_edge(_edge_from_mapping(raw_relation))

    for detail in details:
        target = _string_value(detail.get("name"))
        if not target:
            continue
        for segment in _condition_segments(_string_value(detail.get("evolution_condition"))):
            add_edge(_edge_from_compat_condition(target, segment, names))
    return _dedupe_edges(edges)


def _infer_sourceless_evolution_edges(
    details: list[dict[str, Any]], seed_edges: list[dict[str, str]]
) -> list[dict[str, str]]:
    edges: list[dict[str, str]] = []
    incoming: dict[str, list[dict[str, str]]] = {}
    for edge in seed_edges:
        incoming.setdefault(edge["target"], []).append(edge)

    for index, detail in enumerate(details):
        target = _string_value(detail.get("name"))
        condition = _string_value(detail.get("evolution_condition"))
        if not target or not _needs_source_inference(condition):
            continue
        if incoming.get(target):
            continue

        source = _infer_previous_source(details, index, incoming)
        if not source:
            continue
        edge = build_evolution_edge(source, target, condition)
        edges.append(edge)
        incoming.setdefault(target, []).append(edge)
    return edges


def _infer_previous_source(
    details: list[dict[str, Any]],
    index: int,
    incoming: dict[str, list[dict[str, str]]],
) -> str:
    current_condition = _string_value(details[index].get("evolution_condition"))
    previous = _previous_named_detail(details, index)
    if previous is None:
        return ""

    previous_name = _string_value(previous.get("name"))
    previous_condition = _string_value(previous.get("evolution_condition"))
    previous_incoming = incoming.get(previous_name, [])
    if (
        previous_incoming
        and _is_branch_condition(current_condition)
        and _is_branch_condition(previous_condition)
    ):
        return previous_incoming[0]["source"]
    return previous_name


def _previous_named_detail(details: list[dict[str, Any]], index: int) -> dict[str, Any] | None:
    for previous_index in range(index - 1, -1, -1):
        detail = details[previous_index]
        if _string_value(detail.get("name")):
            return detail
    return None


def _needs_source_inference(condition: str) -> bool:
    text = _string_value(condition)
    if not text or text in FINAL_EVOLUTION_NOTES:
        return False
    if text.startswith(("由", "可由")) or "可进化为" in text or "；" in text or ";" in text:
        return False
    return True


def _is_branch_condition(condition: str) -> bool:
    text = _string_value(condition)
    if not text:
        return False
    return (
        bool(ATTRIBUTE_BATTLE_RE.fullmatch(text))
        or text.endswith("血脉")
        or text.startswith("撞击")
    )


def _dedupe_edges(edges: list[dict[str, str]]) -> list[dict[str, str]]:
    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for edge in edges:
        key = (edge["source"], edge["target"], edge["condition"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(edge)
    return deduped


def _edge_from_mapping(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    source = _string_value(value.get("source") or value.get("source_name"))
    target = _string_value(value.get("target") or value.get("target_name"))
    raw_condition = _string_value(value.get("raw_condition") or value.get("condition"))
    if not source or not target:
        return None
    return build_evolution_edge(source, target, raw_condition)


def _edge_from_compat_condition(target: str, condition: str, names: list[str]) -> dict[str, str] | None:
    if not condition.startswith("由"):
        return None

    sources = sorted((name for name in names if name and name != target), key=len, reverse=True)
    for source in sources:
        prefix = f"由{source}"
        if not condition.startswith(prefix):
            continue
        remainder = condition[len(prefix) :].strip()
        if not remainder:
            return None
        raw_condition = _compat_raw_condition(remainder)
        return build_evolution_edge(source, target, raw_condition)
    return None


def _compat_raw_condition(remainder: str) -> str:
    text = remainder.strip()
    if not text:
        return ""
    if LEVEL_CONDITION_RE.fullmatch(text) and text.endswith("进化"):
        return text.removesuffix("进化")
    if ATTRIBUTE_BATTLE_RE.fullmatch(text) and text.endswith("进化"):
        return text.removesuffix("进化")
    return text


def _condition_segments(condition: str) -> list[str]:
    return [segment.strip() for segment in re.split(r"[；;]", condition) if segment.strip()]


def _relation_from_edge(edge: dict[str, str], text: str) -> dict[str, str]:
    relation = {key: edge[key] for key in EVOLUTION_EDGE_KEYS}
    relation["text"] = text
    return relation


def _complete_evolution_condition(
    incoming_relations: list[dict[str, str]],
    outgoing_relations: list[dict[str, str]],
    fallback: str,
) -> str:
    parts = [relation["text"] for relation in incoming_relations]
    parts.extend(relation["text"] for relation in outgoing_relations)
    return "；".join(parts) if parts else fallback


def _list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _string_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _chinese_count_to_digit(value: str) -> str:
    if value.isdigit():
        return value
    numbers = {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    if value == "十":
        return "10"
    if "十" in value:
        left, _, right = value.partition("十")
        tens = numbers.get(left, 1) if left else 1
        ones = numbers.get(right, 0) if right else 0
        return str(tens * 10 + ones)
    number = numbers.get(value)
    return str(number) if number is not None else value
