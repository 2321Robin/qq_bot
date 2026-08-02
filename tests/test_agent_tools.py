"""Roco tool tests (S2-TOOL-05..09) — fixture data, no real datasets."""

from __future__ import annotations

import asyncio
from pathlib import Path

from qq_bot.agent.models import AgentScope
from qq_bot.agent.registry import ToolContext, ToolRegistry
from qq_bot.agent.tools.roco import (
    FindSkillIntersectionInput,
    GetEvolutionRoutesInput,
    LookupPetInput,
    create_roco_tools,
    register_roco_tools,
)
from qq_bot.services.roco_pets import load_pet_records
from qq_bot.services.roco_skills import load_skill_records

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "roco_pet_details"


def _fixture_registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_roco_tools(
        registry,
        pet_records=tuple(load_pet_records(FIXTURE_DIR)),
        skill_records=tuple(load_skill_records(FIXTURE_DIR)),
    )
    registry.validate()
    return registry


def _context() -> ToolContext:
    return ToolContext(scope=AgentScope(group_id="g1", user_id="u1"))


def _execute(registry: ToolRegistry, name: str, arguments: dict[str, object]):
    spec = registry.get(name)
    assert spec is not None
    return asyncio.run(spec.execute(arguments, _context()))


def test_lookup_pet_by_name() -> None:
    result = _execute(_fixture_registry(), "lookup_pet", {"query": "TestPetB"})
    assert result.status == "ok"
    assert len(result.evidence) == 1
    evidence = result.evidence[0]
    assert evidence.id == "L1"
    assert evidence.source_type == "local"
    assert evidence.title == "TestPetB"
    assert evidence.facts["number"] == "010"
    assert evidence.url is None  # local sources never fabricate URLs


def test_lookup_pet_by_number_and_alias() -> None:
    registry = _fixture_registry()
    by_number = _execute(registry, "lookup_pet", {"query": "012"})
    assert by_number.status == "ok"
    assert by_number.evidence[0].facts["name"] == "TestPetD"
    by_alias = _execute(registry, "lookup_pet", {"query": "TestPetShell"})
    assert by_alias.status == "ok"
    assert by_alias.evidence[0].facts["name"] == "TestPetShell（Initial Form）"


def test_lookup_pet_unknown_returns_not_found_with_empty_evidence() -> None:
    result = _execute(_fixture_registry(), "lookup_pet", {"query": "TestPetZ"})
    assert result.status == "not_found"
    assert result.evidence == ()


def test_lookup_pet_rejects_invalid_arguments() -> None:
    registry = _fixture_registry()
    too_long = _execute(registry, "lookup_pet", {"query": "x" * 101})
    assert too_long.status == "invalid_argument"
    empty = _execute(registry, "lookup_pet", {"query": ""})
    assert empty.status == "invalid_argument"
    extra = _execute(registry, "lookup_pet", {"query": "A", "scope": "g1"})
    assert extra.status == "invalid_argument"


def test_lookup_pet_input_schema_is_strict() -> None:
    schema = LookupPetInput.model_json_schema()
    assert schema["additionalProperties"] is False


def test_skill_intersection_positive_and_negative() -> None:
    registry = _fixture_registry()
    positive = _execute(registry, "find_skill_intersection", {"skills": ["测试技能A", "测试技能B"]})
    assert positive.status == "ok"
    facts = positive.evidence[0].facts
    assert facts["intersection"] == ["TestPetA"]
    # fixture roster has no pet mastering 测试技能E -> missing-skill not_found
    missing = _execute(registry, "find_skill_intersection", {"skills": ["测试技能A", "测试技能E"]})
    assert missing.status == "not_found"


def test_skill_intersection_missing_skill_is_not_found() -> None:
    result = _execute(
        _fixture_registry(), "find_skill_intersection", {"skills": ["测试技能A", "测试技能ZZZ"]}
    )
    assert result.status == "not_found"
    assert result.warnings


def test_skill_intersection_validates_counts_and_lengths() -> None:
    registry = _fixture_registry()
    one = _execute(registry, "find_skill_intersection", {"skills": ["测试技能A"]})
    assert one.status == "invalid_argument"
    six = _execute(
        registry,
        "find_skill_intersection",
        {"skills": ["s1", "s2", "s3", "s4", "s5", "s6"]},
    )
    assert six.status == "invalid_argument"
    long_skill = _execute(registry, "find_skill_intersection", {"skills": ["x" * 51, "y"]})
    assert long_skill.status == "invalid_argument"
    bad_limit = _execute(
        registry,
        "find_skill_intersection",
        {"skills": ["测试技能A", "测试技能B"], "max_results": 11},
    )
    assert bad_limit.status == "invalid_argument"
    ok_limit = _execute(
        registry,
        "find_skill_intersection",
        {"skills": ["测试技能A", "测试技能B"], "max_results": 10},
    )
    assert ok_limit.status == "ok"
    schema = FindSkillIntersectionInput.model_json_schema()
    assert schema["additionalProperties"] is False


def test_evolution_routes_reuse_legacy_derivation() -> None:
    result = _execute(_fixture_registry(), "get_evolution_routes", {"query": "TestPetB"})
    assert result.status == "ok"
    facts = result.evidence[0].facts
    assert any("TestPetB -> TestPetC" in edge for edge in facts["edges"])
    assert any("等级16" in edge for edge in facts["edges"])
    routes = facts["routes"]
    assert ["TestPetB", "TestPetC"] in routes
    # fixture TestPetA declares no evolution link, so the route root is B


def test_evolution_routes_unknown_pet_not_found() -> None:
    result = _execute(_fixture_registry(), "get_evolution_routes", {"query": "TestPetZ"})
    assert result.status == "not_found"


def test_evolution_routes_input_schema() -> None:
    schema = GetEvolutionRoutesInput.model_json_schema()
    assert schema["additionalProperties"] is False


def test_roco_tools_output_is_json_serializable() -> None:
    import json

    registry = _fixture_registry()
    result = _execute(registry, "lookup_pet", {"query": "TestPetA"})
    payload = json.loads(result.model_dump_json())
    assert payload["tool"] == "lookup_pet"
    assert payload["status"] == "ok"


def test_roco_tool_route_allowed_sets() -> None:
    specs = {spec.name: spec for spec in create_roco_tools()}
    assert specs["lookup_pet"].contains_untrusted is False
    assert specs["find_skill_intersection"].allowed_routes == frozenset({"local_knowledge"})
    assert specs["lookup_pet"].allowed_routes == frozenset({"local_knowledge", "web_search"})
    assert specs["get_evolution_routes"].allowed_routes == frozenset(
        {"local_knowledge", "web_search"}
    )
