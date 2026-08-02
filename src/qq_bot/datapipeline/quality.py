"""Quality gates with regression checks against the previous manifest (S3-QUALITY)."""

from __future__ import annotations

from dataclasses import dataclass

from qq_bot.config import BotSettings
from qq_bot.datapipeline.manifest import RefreshManifest
from qq_bot.datapipeline.schemas import STAT_KEYS, PetDetail


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    current: float
    threshold: float
    hard: bool


def number_of(detail: PetDetail) -> str:
    return detail.profile.get("编号", "")


def _numbers_of(validated: dict[str, PetDetail]) -> set[int]:
    return {int(number_of(d)) for d in validated.values() if number_of(d).isdigit()}


def _gate_record_count(validated: dict[str, PetDetail], settings: BotSettings) -> GateResult:
    current = float(len(validated))
    return GateResult(
        "record_count_floor",
        current >= settings.data_min_records,
        current,
        float(settings.data_min_records),
        hard=True,
    )


def _gate_net_drop(
    validated: dict[str, PetDetail],
    previous: RefreshManifest | None,
    settings: BotSettings,
) -> GateResult:
    if previous is None:
        return GateResult(
            "record_net_drop", True, 0.0, float(settings.data_max_record_drop), hard=True
        )
    drop = max(0, len(previous.entries) - len(validated))
    return GateResult(
        "record_net_drop",
        drop <= settings.data_max_record_drop,
        float(drop),
        float(settings.data_max_record_drop),
        hard=True,
    )


def _gate_number_gaps(
    validated: dict[str, PetDetail],
    previous: RefreshManifest | None,
    settings: BotSettings,
) -> GateResult:
    current_numbers = _numbers_of(validated)
    if not current_numbers:
        return GateResult(
            "new_number_gaps",
            False,
            float("inf"),
            float(settings.data_max_new_number_gaps),
            hard=True,
        )
    new_gaps = _gap_count(current_numbers)
    if previous is not None:
        old_gaps = _gap_count(_old_numbers(previous))
        new_gaps = max(0, new_gaps - old_gaps)
    return GateResult(
        "new_number_gaps",
        new_gaps <= settings.data_max_new_number_gaps,
        float(new_gaps),
        float(settings.data_max_new_number_gaps),
        hard=True,
    )


def _gap_count(numbers: set[int]) -> int:
    if not numbers:
        return 0
    return sum(1 for i in range(min(numbers), max(numbers) + 1) if i not in numbers)


def _old_numbers(previous: RefreshManifest) -> set[int]:
    # 上一份 manifest 不含 profile，用文件名前缀（`001-名称.json`）恢复编号
    return {
        int(name.split("-", 1)[0]) for name in previous.entries if name.split("-", 1)[0].isdigit()
    }


def _gate_stats_rate(validated: dict[str, PetDetail], settings: BotSettings) -> GateResult:
    complete = sum(1 for d in validated.values() if STAT_KEYS <= set(d.stats))
    rate = complete / len(validated) if validated else 0.0
    return GateResult(
        "stats_complete_rate",
        rate >= settings.data_min_stats_complete_rate,
        rate,
        settings.data_min_stats_complete_rate,
        hard=True,
    )


def _gate_total_race_rate(validated: dict[str, PetDetail], settings: BotSettings) -> GateResult:
    complete = sum(1 for d in validated.values() if d.total_race_value is not None)
    rate = complete / len(validated) if validated else 0.0
    return GateResult(
        "total_race_rate",
        rate >= settings.data_min_total_race_rate,
        rate,
        settings.data_min_total_race_rate,
        hard=True,
    )


def _gate_dangling_edges(validated: dict[str, PetDetail], settings: BotSettings) -> GateResult:
    known = {d.name for d in validated.values()}
    dangling = 0
    for detail in validated.values():
        for edge in [*detail.evolution.from_, *detail.evolution.to]:
            if edge.source not in known or edge.target not in known:
                dangling += 1
    return GateResult(
        "dangling_edges",
        dangling <= settings.data_max_dangling_edges,
        float(dangling),
        float(settings.data_max_dangling_edges),
        hard=True,
    )


def _gate_skill_keys(validated: dict[str, PetDetail], settings: BotSettings) -> GateResult:
    total = 0
    missing = 0
    for detail in validated.values():
        for group in detail.skills:
            for row in group.rows:
                total += 1
                if not row.name:
                    missing += 1
    rate = missing / total if total else 0.0
    return GateResult(
        "skill_key_missing_rate",
        rate <= settings.data_max_skill_key_missing_rate,
        rate,
        settings.data_max_skill_key_missing_rate,
        hard=True,
    )


SKILL_FIELD_MISSING_KEYS = ("等级", "耗能", "类型", "威力", "效果")


def skill_field_missing_rates(validated: dict[str, PetDetail]) -> dict[str, float]:
    """Non-blocking missing rates for the non-key skill fields (S3-QUALITY-06).

    Computed per field across every skill row; the plan omits these from the
    gate set and the JSON diff schema, so the refresh summary/report surface
    them separately.
    """
    totals: dict[str, int] = {key: 0 for key in SKILL_FIELD_MISSING_KEYS}
    missing: dict[str, int] = {key: 0 for key in SKILL_FIELD_MISSING_KEYS}
    for detail in validated.values():
        for group in detail.skills:
            for row in group.rows:
                values = {
                    "等级": row.level,
                    "耗能": row.energy,
                    "类型": row.category,
                    "威力": row.power,
                    "效果": row.effect,
                }
                for key in SKILL_FIELD_MISSING_KEYS:
                    totals[key] += 1
                    if not values[key]:
                        missing[key] += 1
    return {
        key: (missing[key] / totals[key] if totals[key] else 0.0)
        for key in SKILL_FIELD_MISSING_KEYS
    }


def _gate_quarantine(quarantine_nonempty: bool) -> GateResult:
    return GateResult(
        "quarantine_empty",
        not quarantine_nonempty,
        1.0 if quarantine_nonempty else 0.0,
        0.0,
        hard=True,
    )


def run_quality_gates(
    validated: dict[str, PetDetail],
    *,
    previous: RefreshManifest | None,
    settings: BotSettings,
    quarantine_nonempty: bool,
) -> list[GateResult]:
    """Return gates in fixed name order (S3-QUALITY-01/08)."""
    return sorted(
        [
            _gate_record_count(validated, settings),
            _gate_net_drop(validated, previous, settings),
            _gate_number_gaps(validated, previous, settings),
            _gate_stats_rate(validated, settings),
            _gate_total_race_rate(validated, settings),
            _gate_dangling_edges(validated, settings),
            _gate_skill_keys(validated, settings),
            _gate_quarantine(quarantine_nonempty),
        ],
        key=lambda g: g.name,
    )
