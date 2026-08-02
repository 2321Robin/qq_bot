"""Quality gate tests (S3-QUALITY) — fixtures and tmp dirs only, offline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qq_bot.config import BotSettings
from qq_bot.datapipeline.manifest import RefreshManifest, load_manifest
from qq_bot.datapipeline.quality import (
    SKILL_FIELD_MISSING_KEYS,
    GateResult,
    run_quality_gates,
    skill_field_missing_rates,
)
from qq_bot.datapipeline.schemas import PetDetail

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "data_pipeline"
DETAILS_DIR = FIXTURE_ROOT / "details"
MANIFESTS_DIR = FIXTURE_ROOT / "manifests"

VALID_FILES = [f"{n}-测试宠物{chr(ord('A') + i)}.json" for i, n in enumerate(range(101, 107))]


def _validated() -> dict[str, PetDetail]:
    return {
        path.name: PetDetail.model_validate(json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(DETAILS_DIR.glob("1*.json"))
        if path.name != "107-BadPet.json"
    }


def _previous() -> RefreshManifest:
    return load_manifest(MANIFESTS_DIR / "previous.json")


def _settings(**overrides: object) -> BotSettings:
    return BotSettings(**overrides)


def _detail(number: str, name: str, **changes: object) -> PetDetail:
    raw = {
        "name": name,
        "source_url": f"https://example.com/pets/{number}",
        "attributes": ["合成属性"],
        "evolution_condition": "无法进化",
        "total_race_value": 123,
        "profile": {"编号": number},
        "stats": {"生命": 50, "物攻": 50, "魔攻": 50, "物防": 50, "魔防": 50, "速度": 50},
        "skills": [],
        "metadata": {"parser_version": 6, "generated_at": "2026-01-15T08:00:00+00:00"},
        "evolution": {"from": [], "to": [], "evolution_condition": "无法进化"},
    }
    raw.update(changes)
    return PetDetail.model_validate(raw)


def _gates_by_name(results: list[GateResult]) -> dict[str, GateResult]:
    return {result.name: result for result in results}


def test_baseline_six_six_passes_all_gates() -> None:
    results = run_quality_gates(
        _validated(),
        previous=_previous(),
        settings=_settings(data_min_records=6),
        quarantine_nonempty=False,
    )
    assert all(result.passed for result in results)
    assert [result.name for result in results] == sorted(result.name for result in results)


def test_gate_order_is_sorted_by_name() -> None:
    results = run_quality_gates(
        _validated(),
        previous=_previous(),
        settings=_settings(data_min_records=6),
        quarantine_nonempty=False,
    )
    assert [result.name for result in results] == [
        "dangling_edges",
        "new_number_gaps",
        "quarantine_empty",
        "record_count_floor",
        "record_net_drop",
        "skill_key_missing_rate",
        "stats_complete_rate",
        "total_race_rate",
    ]


def test_record_count_floor_pass_and_fail() -> None:
    validated = _validated()
    results = _gates_by_name(
        run_quality_gates(
            validated,
            previous=_previous(),
            settings=_settings(data_min_records=6),
            quarantine_nonempty=False,
        )
    )
    assert results["record_count_floor"].passed
    assert results["record_count_floor"].current == 6.0
    results = _gates_by_name(
        run_quality_gates(
            validated,
            previous=_previous(),
            settings=_settings(data_min_records=7),
            quarantine_nonempty=False,
        )
    )
    assert not results["record_count_floor"].passed
    assert results["record_count_floor"].hard


def test_record_net_drop_against_previous() -> None:
    validated = _validated()
    results = _gates_by_name(
        run_quality_gates(
            validated,
            previous=_previous(),
            settings=_settings(data_max_record_drop=1),
            quarantine_nonempty=False,
        )
    )
    assert results["record_net_drop"].passed
    assert results["record_net_drop"].current == 0.0

    dropped = {name: detail for name, detail in validated.items() if name != "101-测试宠物A.json"}
    results = _gates_by_name(
        run_quality_gates(
            dropped,
            previous=_previous(),
            settings=_settings(data_max_record_drop=0),
            quarantine_nonempty=False,
        )
    )
    assert not results["record_net_drop"].passed
    assert results["record_net_drop"].current == 1.0
    assert results["record_net_drop"].threshold == 0.0


def test_record_net_drop_no_baseline_passes() -> None:
    results = _gates_by_name(
        run_quality_gates(
            _validated(), previous=None, settings=_settings(), quarantine_nonempty=False
        )
    )
    assert results["record_net_drop"].passed
    assert results["record_net_drop"].current == 0.0


def test_new_number_gaps_delta_vs_previous() -> None:
    validated = _validated()
    results = _gates_by_name(
        run_quality_gates(
            validated, previous=_previous(), settings=_settings(), quarantine_nonempty=False
        )
    )
    assert results["new_number_gaps"].passed
    assert results["new_number_gaps"].current == 0.0

    # Current {101..103, 105, 106}: gap at 104 -> 1 new gap vs baseline's 0.
    gapped = {name: detail for name, detail in validated.items() if name != "104-测试宠物D.json"}
    results = _gates_by_name(
        run_quality_gates(
            gapped, previous=_previous(), settings=_settings(), quarantine_nonempty=False
        )
    )
    assert not results["new_number_gaps"].passed
    assert results["new_number_gaps"].current == 1.0


def test_new_number_gaps_no_baseline_passes() -> None:
    results = _gates_by_name(
        run_quality_gates(
            _validated(), previous=None, settings=_settings(), quarantine_nonempty=False
        )
    )
    assert results["new_number_gaps"].passed


def test_new_number_gaps_empty_numbers_fails() -> None:
    results = _gates_by_name(
        run_quality_gates({}, previous=None, settings=_settings(), quarantine_nonempty=False)
    )
    assert not results["new_number_gaps"].passed
    assert results["new_number_gaps"].current == float("inf")


def test_stats_complete_rate_threshold_injectable() -> None:
    validated = _validated()
    results = _gates_by_name(
        run_quality_gates(
            validated,
            previous=_previous(),
            settings=_settings(data_min_stats_complete_rate=0.9),
            quarantine_nonempty=False,
        )
    )
    assert results["stats_complete_rate"].passed
    assert results["stats_complete_rate"].current == 1.0

    stripped = dict(validated)
    incomplete = _detail("101", "测试宠物A", stats={"物攻": 50, "魔攻": 50})
    stripped["101-测试宠物A.json"] = incomplete
    results = _gates_by_name(
        run_quality_gates(
            stripped,
            previous=_previous(),
            settings=_settings(data_min_stats_complete_rate=0.9),
            quarantine_nonempty=False,
        )
    )
    assert not results["stats_complete_rate"].passed
    assert results["stats_complete_rate"].current == pytest.approx(5 / 6)


def test_total_race_rate_pass_and_fail() -> None:
    validated = _validated()
    results = _gates_by_name(
        run_quality_gates(
            validated, previous=_previous(), settings=_settings(), quarantine_nonempty=False
        )
    )
    assert results["total_race_rate"].passed

    stripped = dict(validated)
    stripped["101-测试宠物A.json"] = _detail("101", "测试宠物A", total_race_value=None)
    results = _gates_by_name(
        run_quality_gates(
            stripped, previous=_previous(), settings=_settings(), quarantine_nonempty=False
        )
    )
    assert not results["total_race_rate"].passed
    assert results["total_race_rate"].current == pytest.approx(5 / 6)


def test_dangling_edges_pass_and_fail() -> None:
    validated = _validated()
    results = _gates_by_name(
        run_quality_gates(
            validated, previous=_previous(), settings=_settings(), quarantine_nonempty=False
        )
    )
    assert results["dangling_edges"].passed
    assert results["dangling_edges"].current == 0.0

    dangling = dict(validated)
    dangling["101-测试宠物A.json"] = _detail(
        "101",
        "测试宠物A",
        evolution={
            "from": [],
            "to": [
                {"source": "测试宠物A", "target": "不存在精灵", "condition": "等级达到40级"},
                {"source": "测试宠物A", "target": "测试宠物B", "condition": "等级达到40级"},
            ],
            "evolution_condition": "无法进化",
        },
    )
    results = _gates_by_name(
        run_quality_gates(
            dangling, previous=_previous(), settings=_settings(), quarantine_nonempty=False
        )
    )
    assert not results["dangling_edges"].passed
    assert results["dangling_edges"].current == 1.0


def test_skill_key_missing_rate() -> None:
    validated = _validated()
    rows = [
        {
            "等级": "LV1",
            "技能": "技能A",
            "耗能": "1",
            "类型": "魔攻",
            "威力": "60",
            "效果": "效果A",
        },
        {"等级": "LV2", "技能": "", "耗能": "2", "类型": "物攻", "威力": "80", "效果": "效果B"},
        {
            "等级": "LV3",
            "技能": "技能C",
            "耗能": "3",
            "类型": "魔攻",
            "威力": "90",
            "效果": "效果C",
        },
        {"等级": "LV4", "技能": "", "耗能": "4", "类型": "物攻", "威力": "70", "效果": ""},
    ]
    augmented = dict(validated)
    augmented["101-测试宠物A.json"] = _detail(
        "101", "测试宠物A", skills=[{"source": "技能", "rows": rows}]
    )
    # 101's own 2 rows are replaced by the 4-row group: 2 missing of 8 total.
    total = sum(len(group.rows) for detail in augmented.values() for group in detail.skills)
    assert total == 8
    results = _gates_by_name(
        run_quality_gates(
            augmented,
            previous=_previous(),
            settings=_settings(data_max_skill_key_missing_rate=0.5),
            quarantine_nonempty=False,
        )
    )
    assert results["skill_key_missing_rate"].current == pytest.approx(2 / total)
    assert results["skill_key_missing_rate"].passed

    results = _gates_by_name(
        run_quality_gates(
            augmented,
            previous=_previous(),
            settings=_settings(data_max_skill_key_missing_rate=0.0),
            quarantine_nonempty=False,
        )
    )
    assert not results["skill_key_missing_rate"].passed


def test_skill_key_missing_rate_empty_rows_is_zero() -> None:
    validated = _validated()
    results = _gates_by_name(
        run_quality_gates(
            validated, previous=_previous(), settings=_settings(), quarantine_nonempty=False
        )
    )
    assert results["skill_key_missing_rate"].current == 0.0


def test_quarantine_gate_pass_and_fail() -> None:
    validated = _validated()
    results = _gates_by_name(
        run_quality_gates(
            validated, previous=_previous(), settings=_settings(), quarantine_nonempty=False
        )
    )
    assert results["quarantine_empty"].passed
    assert results["quarantine_empty"].current == 0.0

    results = _gates_by_name(
        run_quality_gates(
            validated, previous=_previous(), settings=_settings(), quarantine_nonempty=True
        )
    )
    assert not results["quarantine_empty"].passed
    assert results["quarantine_empty"].current == 1.0
    assert results["quarantine_empty"].hard


def test_skill_field_missing_rates_non_blocking_stats() -> None:
    rows = [
        {
            "等级": "LV1",
            "技能": "技能A",
            "耗能": "1",
            "类型": "魔攻",
            "威力": "60",
            "效果": "效果A",
        },
        {"等级": "", "技能": "技能B", "耗能": "", "类型": "", "威力": "", "效果": ""},
    ]
    solo = {
        "101-测试宠物A.json": _detail("101", "测试宠物A", skills=[{"source": "技能", "rows": rows}])
    }
    rates = skill_field_missing_rates(solo)
    assert set(rates) == set(SKILL_FIELD_MISSING_KEYS)
    for key in SKILL_FIELD_MISSING_KEYS:
        assert rates[key] == pytest.approx(0.5)
