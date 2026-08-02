"""Local knowledge tools (S2-TOOL-05..09).

Three read-only tools over the existing roco services. They reuse the legacy
query implementations — no duplicated matching or evolution derivation — and
always return normalized ``ToolResult`` objects.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from qq_bot.agent.models import Evidence, RouteKind, ToolResult
from qq_bot.agent.registry import StrictInputModel, ToolContext, ToolRegistry, ToolSpec
from qq_bot.services.roco_knowledge import (
    build_evolution_edges,
    build_evolution_routes,
)
from qq_bot.services.roco_pets import PetRecord, find_pet, get_pet_records
from qq_bot.services.roco_skills import SkillRecord, get_skill_records, group_skill_variants

MAX_QUERY_LEN = 100
MAX_SKILL_LEN = 50
MAX_SKILLS = 5
MAX_INTERSECTION_RESULTS = 10
MAX_EVIDENCE_RESULTS = 20


class LookupPetInput(StrictInputModel):
    query: Annotated[str, Field(min_length=1, max_length=MAX_QUERY_LEN)]


class FindSkillIntersectionInput(StrictInputModel):
    skills: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=MAX_SKILL_LEN)]],
        Field(min_length=2, max_length=MAX_SKILLS),
    ]
    max_results: Annotated[int, Field(ge=1, le=MAX_INTERSECTION_RESULTS)] = 5


class GetEvolutionRoutesInput(StrictInputModel):
    query: Annotated[str, Field(min_length=1, max_length=MAX_QUERY_LEN)]


def _evidence_for_record(
    record: PetRecord,
    skills: tuple[SkillRecord, ...],
    index: int,
) -> Evidence:
    pet_skills = [skill.name for skill in skills if skill.pet_name == record.name]
    facts: dict[str, object] = {
        "name": record.name,
        "number": record.number,
    }
    if record.aliases:
        facts["aliases"] = record.aliases
    if record.attributes:
        facts["attributes"] = record.attributes
    if record.stage:
        facts["stage"] = record.stage
    if record.race_value is not None:
        facts["race_value"] = record.race_value
    if record.evolution_chain:
        facts["evolution_chain"] = record.evolution_chain
    if record.evolution_condition:
        facts["evolution_condition"] = record.evolution_condition
    if pet_skills:
        facts["skills"] = pet_skills[:MAX_EVIDENCE_RESULTS]
    return Evidence(
        id=f"L{index + 1}",
        source_type="local",
        title=record.name,
        facts=facts,
        url=None,
    )


def create_roco_tools(
    *,
    pet_records: tuple[PetRecord, ...] | list[PetRecord] | None = None,
    skill_records: tuple[SkillRecord, ...] | list[SkillRecord] | None = None,
) -> tuple[ToolSpec, ...]:
    """Build the three local tools over the production caches (default) or an
    injected roster (evaluation fixtures)."""
    pets = tuple(pet_records) if pet_records is not None else get_pet_records()
    skills = tuple(skill_records) if skill_records is not None else get_skill_records()

    async def lookup_pet(arguments: dict[str, object], context: ToolContext) -> ToolResult:
        record = find_pet(pets, str(arguments["query"]))
        if record is None:
            return ToolResult(tool="lookup_pet", status="not_found", evidence=())
        return ToolResult(
            tool="lookup_pet",
            status="ok",
            evidence=(_evidence_for_record(record, skills, context.evidence_index),),
        )

    async def find_skill_intersection(
        arguments: dict[str, object], context: ToolContext
    ) -> ToolResult:
        requested = [str(skill) for skill in arguments["skills"]]
        variants = group_skill_variants(skills)
        variant_by_name = {variant.name: variant for variant in variants}
        hits: dict[str, list[str]] = {}
        missing: list[str] = []
        for skill in requested:
            variant = variant_by_name.get(skill)
            if variant is None:
                missing.append(skill)
            else:
                hits[skill] = variant.pet_names
        if missing:
            return ToolResult(
                tool="find_skill_intersection",
                status="not_found",
                evidence=(),
                warnings=(f"未收录技能：{'、'.join(missing)}",),
            )
        common = set(hits[requested[0]])
        for skill in requested[1:]:
            common &= set(hits[skill])
        if not common:
            return ToolResult(
                tool="find_skill_intersection",
                status="not_found",
                evidence=(),
                warnings=("没有同时掌握这些技能的精灵",),
            )
        limit = int(arguments["max_results"])
        intersection = sorted(common)[:limit]
        facts: dict[str, object] = {
            "skills": requested,
            "hits": {skill: names[:MAX_EVIDENCE_RESULTS] for skill, names in hits.items()},
            "intersection": intersection,
            "count": len(common),
        }
        return ToolResult(
            tool="find_skill_intersection",
            status="ok",
            evidence=(
                Evidence(
                    id=f"L{context.evidence_index + 1}",
                    source_type="local",
                    title="技能交集",
                    facts=facts,
                    url=None,
                ),
            ),
        )

    async def get_evolution_routes(
        arguments: dict[str, object], context: ToolContext
    ) -> ToolResult:
        record = find_pet(pets, str(arguments["query"]))
        if record is None:
            return ToolResult(tool="get_evolution_routes", status="not_found", evidence=())
        routes = build_evolution_routes(record, pets)
        edges = build_evolution_edges(record, pets)
        facts: dict[str, object] = {
            "name": record.name,
            "routes": routes,
            "edges": edges,
            "count": len(routes),
        }
        if record.evolution_condition:
            facts["evolution_condition"] = record.evolution_condition
        return ToolResult(
            tool="get_evolution_routes",
            status="ok",
            evidence=(
                Evidence(
                    id=f"L{context.evidence_index + 1}",
                    source_type="local",
                    title=f"{record.name} 进化路线",
                    facts=facts,
                    url=None,
                ),
            ),
        )

    local_routes = frozenset({RouteKind.LOCAL_KNOWLEDGE, RouteKind.WEB_SEARCH})
    return (
        ToolSpec(
            name="lookup_pet",
            description="按名称、别名或编号查询本地图鉴中的精灵资料。",
            input_model=LookupPetInput,
            allowed_routes=local_routes,
            max_results=1,
            contains_untrusted=False,
            timeout_seconds=5.0,
            executor=lookup_pet,
        ),
        ToolSpec(
            name="find_skill_intersection",
            description="查询同时掌握一组技能的精灵（技能交集，2~5 个技能）。",
            input_model=FindSkillIntersectionInput,
            allowed_routes=frozenset({RouteKind.LOCAL_KNOWLEDGE}),
            max_results=MAX_INTERSECTION_RESULTS,
            contains_untrusted=False,
            timeout_seconds=5.0,
            executor=find_skill_intersection,
        ),
        ToolSpec(
            name="get_evolution_routes",
            description="查询本地图鉴中精灵的完整进化路线与分支条件。",
            input_model=GetEvolutionRoutesInput,
            allowed_routes=local_routes,
            max_results=1,
            contains_untrusted=False,
            timeout_seconds=5.0,
            executor=get_evolution_routes,
        ),
    )


def register_roco_tools(
    registry: ToolRegistry,
    *,
    pet_records: tuple[PetRecord, ...] | list[PetRecord] | None = None,
    skill_records: tuple[SkillRecord, ...] | list[SkillRecord] | None = None,
) -> None:
    for spec in create_roco_tools(pet_records=pet_records, skill_records=skill_records):
        registry.register(spec)
