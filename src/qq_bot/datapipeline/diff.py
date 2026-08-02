"""Refresh diff report (S3-DIFF)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from qq_bot.datapipeline.manifest import RefreshManifest
from qq_bot.datapipeline.quality import GateResult
from qq_bot.datapipeline.schemas import PetDetail


def row_signature(row: dict) -> str:
    keys = ("等级", "技能", "耗能", "类型", "威力", "效果")
    return "|".join(str(row.get(k) or "").strip() for k in keys)


def edge_signature(edge: dict) -> str:
    return "|".join(str(edge.get(k) or "").strip() for k in ("source", "target", "condition"))


def _edge_text(edge: dict) -> str:
    return (
        f"{str(edge.get('source') or '').strip()} → "
        f"{str(edge.get('target') or '').strip()}（{str(edge.get('condition') or '').strip()}）"
    )


def _row_key(row: dict) -> str:
    # Identity fields: 等级 + 技能.  A changed 耗能/类型/威力/效果 with the same
    # key counts as "modified" rather than remove+add (S3-DIFF-03).
    return "|".join(str(row.get(k) or "").strip() for k in ("等级", "技能"))


class FormsDiff(BaseModel):
    model_config = ConfigDict(extra="forbid")
    added: list[str] = Field(default_factory=list)  # 附 URL 的条目见 added_urls
    modified: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    added_urls: dict[str, str] = Field(default_factory=dict)


class SkillsDiff(BaseModel):
    model_config = ConfigDict(extra="forbid")
    added_rows: int = 0
    modified_rows: int = 0
    removed_rows: int = 0
    details: list[str] = Field(default_factory=list)  # 每文件摘要行
    computed: bool = True  # False when no previous snapshot was available


class EvolutionDiff(BaseModel):
    model_config = ConfigDict(extra="forbid")
    added_edges: list[str] = Field(default_factory=list)
    modified_edges: list[str] = Field(default_factory=list)
    removed_edges: list[str] = Field(default_factory=list)
    needs_confirmation: list[str] = Field(default_factory=list)  # added ∪ modified
    computed: bool = True  # False when no previous snapshot was available


class NumbersDiff(BaseModel):
    model_config = ConfigDict(extra="forbid")
    new: list[str] = Field(default_factory=list)
    gone: list[str] = Field(default_factory=list)
    new_gaps: list[str] = Field(default_factory=list)


class RefreshDiff(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = 1
    generated_at: datetime
    previous_hash: str | None
    current_hash: str
    forms: FormsDiff
    skills: SkillsDiff
    evolution: EvolutionDiff
    numbers: NumbersDiff
    gates: list[dict[str, Any]] = Field(default_factory=list)
    quarantine: dict[str, str] = Field(default_factory=dict)
    gate_failed: bool = False
    allow_quarantine: bool = False  # S3-SCHEMA-04: 记录 override
    skill_field_missing: dict[str, float] = Field(default_factory=dict)  # S3-QUALITY-06


def _snapshot_detail(previous_files_dir: Path, filename: str) -> dict[str, Any]:
    path = previous_files_dir / filename
    if not path.exists():
        return {}
    import json

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _old_rows(previous_files_dir: Path, filename: str) -> list[dict[str, Any]]:
    detail = _snapshot_detail(previous_files_dir, filename)
    return [
        row
        for group in detail.get("skills", [])
        if isinstance(group, dict)
        for row in group.get("rows", [])
        if isinstance(row, dict)
    ]


def _old_edges(previous_files_dir: Path, filename: str) -> list[dict[str, Any]]:
    detail = _snapshot_detail(previous_files_dir, filename)
    evolution = detail.get("evolution", {})
    if not isinstance(evolution, dict):
        return []
    edges: list[dict[str, Any]] = []
    for key in ("from", "to"):
        for edge in evolution.get(key, []):
            if isinstance(edge, dict):
                edges.append(edge)
    return edges


def _new_rows(detail: PetDetail) -> list[dict[str, Any]]:
    return [row.model_dump(by_alias=True) for group in detail.skills for row in group.rows]


def _new_edges(detail: PetDetail) -> list[dict[str, Any]]:
    return [edge.model_dump() for edge in [*detail.evolution.from_, *detail.evolution.to]]


def _compare_rows(
    old_rows: list[dict[str, Any]], new_rows: list[dict[str, Any]]
) -> tuple[int, int, int]:
    old_by_key = {_row_key(row): row for row in old_rows}
    new_by_key = {_row_key(row): row for row in new_rows}
    added = sum(1 for key in new_by_key if key not in old_by_key)
    removed = sum(1 for key in old_by_key if key not in new_by_key)
    modified = sum(
        1
        for key in new_by_key
        if key in old_by_key and row_signature(old_by_key[key]) != row_signature(new_by_key[key])
    )
    return added, modified, removed


def _compare_edges(
    old_edges: list[dict[str, Any]], new_edges: list[dict[str, Any]]
) -> tuple[list[str], list[str], list[str]]:
    old_by_sig = {edge_signature(edge): edge for edge in old_edges}
    new_by_sig = {edge_signature(edge): edge for edge in new_edges}
    old_by_pair = {(e.get("source"), e.get("target")): e for e in old_edges}
    new_by_pair = {(e.get("source"), e.get("target")): e for e in new_edges}
    # A changed condition produces a new signature AND the same (source, target)
    # pair; it counts as modified, not added (S3-DIFF-04).
    added = [
        e
        for sig, e in new_by_sig.items()
        if sig not in old_by_sig and (e.get("source"), e.get("target")) not in old_by_pair
    ]
    removed = [
        e
        for sig, e in old_by_sig.items()
        if sig not in new_by_sig and (e.get("source"), e.get("target")) not in new_by_pair
    ]
    modified = [
        e
        for pair, e in new_by_pair.items()
        if pair in old_by_pair and edge_signature(old_by_pair[pair]) != edge_signature(e)
    ]
    return (
        [_edge_text(e) for e in added],
        [_edge_text(e) for e in modified],
        [_edge_text(e) for e in removed],
    )


def _numbers_of(details: dict[str, PetDetail]) -> set[int]:
    return {
        int(detail.profile.get("编号", ""))
        for detail in details.values()
        if detail.profile.get("编号", "").isdigit()
    }


def _old_numbers(previous: RefreshManifest) -> set[int]:
    return {
        int(name.split("-", 1)[0]) for name in previous.entries if name.split("-", 1)[0].isdigit()
    }


def _gap_numbers(numbers: set[int]) -> set[int]:
    if not numbers:
        return set()
    return {i for i in range(min(numbers), max(numbers) + 1) if i not in numbers}


def compute_diff(
    previous: RefreshManifest | None,
    current: RefreshManifest,
    validated: dict[str, PetDetail],
    previous_files_dir: Path | None = None,
    *,
    gates: list[GateResult] | None = None,
    quarantine: dict[str, str] | None = None,
    allow_quarantine: bool = False,
    skill_field_missing: dict[str, float] | None = None,
) -> RefreshDiff:
    """Diff by filename/hash (forms), row signature (skills) and edge signature (evolution).

    previous_files_dir 为刷新前旧文件快照目录（Task 7 的 staging/previous/）；
    None 时技能/进化行级变化标记为 not_computed，形态级仍精确。
    """
    old_names = set(previous.entries) if previous is not None else set()
    new_names = set(current.entries)
    added = sorted(new_names - old_names)
    removed = sorted(old_names - new_names)
    if previous is None:
        modified: list[str] = []
    else:
        modified = sorted(
            name
            for name in old_names & new_names
            if previous.entries[name].sha256 != current.entries[name].sha256
        )
    added_urls = {
        name: current.entries[name].source_url for name in added if name in current.entries
    }

    snapshot_available = previous_files_dir is not None
    skills = SkillsDiff(computed=snapshot_available)
    evolution = EvolutionDiff(computed=snapshot_available)
    if snapshot_available:
        for filename in [*modified, *added]:
            new_rows = _new_rows(validated[filename]) if filename in validated else []
            old_rows = _old_rows(previous_files_dir, filename) if filename in old_names else []
            added_rows, modified_rows, removed_rows = _compare_rows(old_rows, new_rows)
            skills.added_rows += added_rows
            skills.modified_rows += modified_rows
            skills.removed_rows += removed_rows
            skills.details.append(
                f"{filename}: +{added_rows} ~{modified_rows} -{removed_rows} 技能"
            )

            new_edges = _new_edges(validated[filename]) if filename in validated else []
            old_edges = _old_edges(previous_files_dir, filename) if filename in old_names else []
            added_edges, modified_edges, removed_edges = _compare_edges(old_edges, new_edges)
            evolution.added_edges.extend(added_edges)
            evolution.modified_edges.extend(modified_edges)
            evolution.removed_edges.extend(removed_edges)
    evolution.needs_confirmation = [*evolution.added_edges, *evolution.modified_edges]

    new_numbers = _numbers_of(validated)
    old_number_set = _old_numbers(previous) if previous is not None else set()
    new_gap_set = _gap_numbers(new_numbers) - _gap_numbers(old_number_set)
    numbers = NumbersDiff(
        new=sorted(str(number) for number in new_numbers - old_number_set),
        gone=sorted(str(number) for number in old_number_set - new_numbers),
        new_gaps=sorted(str(number) for number in new_gap_set),
    )

    gate_records = [
        {
            "name": result.name,
            "passed": result.passed,
            "current": result.current,
            "threshold": result.threshold,
            "hard": result.hard,
        }
        for result in (gates or [])
    ]
    gate_failed = any(not result.passed for result in (gates or []))

    return RefreshDiff(
        generated_at=datetime.now().astimezone(),
        previous_hash=previous.dataset_hash if previous is not None else None,
        current_hash=current.dataset_hash,
        forms=FormsDiff(added=added, modified=modified, removed=removed, added_urls=added_urls),
        skills=skills,
        evolution=evolution,
        numbers=numbers,
        gates=gate_records,
        quarantine=quarantine or {},
        gate_failed=gate_failed,
        allow_quarantine=allow_quarantine,
        skill_field_missing=skill_field_missing or {},
    )


def render_markdown(diff: RefreshDiff) -> str:
    """按「新增 N 个形态、修改 M 条技能、Z 条进化关系待确认」格式输出可读报告。"""
    lines: list[str] = []
    lines.append(f"# 数据刷新报告 {diff.generated_at.isoformat()}")
    lines.append("")
    added_count = len(diff.forms.added)
    modified_forms_count = len(diff.forms.modified)
    removed_count = len(diff.forms.removed)
    lines.append(
        f"新增 {added_count} 个形态、修改 {modified_forms_count} 个、删除 {removed_count} 个"
    )
    if diff.skills.computed:
        lines.append(
            f"修改 {diff.skills.modified_rows} 条技能"
            f"（新增 {diff.skills.added_rows} 条、删除 {diff.skills.removed_rows} 条）"
        )
    else:
        lines.append("修改 0 条技能（行级变化未计算：缺少旧文件快照）")
    if diff.evolution.computed:
        lines.append(f"{len(diff.evolution.needs_confirmation)} 条进化关系待确认")
    else:
        lines.append("0 条进化关系待确认（边级变化未计算：缺少旧文件快照）")
    lines.append("")
    if diff.forms.added:
        lines.append("## 新增形态")
        for name in diff.forms.added:
            url = diff.forms.added_urls.get(name, "")
            lines.append(f"- {name}（{url}）")
        lines.append("")
    if diff.evolution.needs_confirmation:
        lines.append("## 待确认进化关系")
        lines.extend(f"- {edge}" for edge in diff.evolution.needs_confirmation)
        lines.append("")
    if diff.evolution.removed_edges:
        lines.append("## 删除的进化边")
        lines.extend(f"- {edge}" for edge in diff.evolution.removed_edges)
        lines.append("")
    if diff.numbers.new or diff.numbers.gone or diff.numbers.new_gaps:
        lines.append("## 编号变化")
        if diff.numbers.new:
            lines.append(f"- 新增编号：{', '.join(diff.numbers.new)}")
        if diff.numbers.gone:
            lines.append(f"- 消失编号：{', '.join(diff.numbers.gone)}")
        if diff.numbers.new_gaps:
            lines.append(f"- 新缺口：{', '.join(diff.numbers.new_gaps)}")
        lines.append("")
    if diff.gates:
        lines.append("## 质量门禁")
        for gate in diff.gates:
            status = "通过" if gate["passed"] else "失败"
            lines.append(
                f"- {gate['name']}: {status}（当前 {gate['current']} / 阈值 {gate['threshold']}）"
            )
        lines.append("")
    if diff.quarantine:
        lines.append("## Quarantine")
        for filename, error in sorted(diff.quarantine.items()):
            lines.append(f"- {filename}: {error}")
        lines.append("")
    if diff.skill_field_missing:
        lines.append("## 技能字段缺失率（不阻断）")
        for key, rate in sorted(diff.skill_field_missing.items()):
            lines.append(f"- {key}: {rate:.3f}")
        lines.append("")
    return "\n".join(lines) + "\n"
