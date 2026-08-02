"""Tool Registry tests (S2-TOOL-01..04)."""

from __future__ import annotations

import asyncio

import pytest

from qq_bot.agent.models import AgentScope, RouteKind, ToolResult
from qq_bot.agent.registry import (
    StrictInputModel,
    ToolContext,
    ToolRegistry,
    ToolRegistryError,
    ToolSpec,
)
from pydantic import Field


class EchoInput(StrictInputModel):
    value: str = Field(min_length=1, max_length=10)


class EmptyInput(StrictInputModel):
    pass


def _spec(**overrides: object) -> ToolSpec:
    fields: dict[str, object] = {
        "name": "echo",
        "description": "echo tool",
        "input_model": EchoInput,
        "allowed_routes": frozenset({RouteKind.LOCAL_KNOWLEDGE}),
        "max_results": 5,
        "contains_untrusted": False,
        "timeout_seconds": 5.0,
        "executor": lambda arguments, context: ToolResult(tool="echo", status="ok", evidence=()),
    }
    fields.update(overrides)
    return ToolSpec(**fields)  # type: ignore[arg-type]


def _scope() -> AgentScope:
    return AgentScope(group_id="g1", user_id="u1", can_use_chat_memory=True)


def _context() -> ToolContext:
    return ToolContext(scope=_scope())


def test_schema_rejects_extra_properties() -> None:
    schema = _spec().to_json_schema()
    assert schema["additionalProperties"] is False
    assert "value" in schema["properties"]


def test_registry_register_get_and_listing() -> None:
    registry = ToolRegistry()
    spec = _spec()
    registry.register(spec)
    assert registry.get("echo") is spec
    assert registry.names_for(frozenset({RouteKind.LOCAL_KNOWLEDGE})) == ["echo"]
    assert registry.names_for(frozenset({RouteKind.WEB_SEARCH})) == []
    schemas = registry.schemas_for(frozenset({RouteKind.LOCAL_KNOWLEDGE}))
    assert schemas == [spec.to_json_schema()]


def test_registry_rejects_duplicate_names() -> None:
    registry = ToolRegistry()
    registry.register(_spec())
    with pytest.raises(ToolRegistryError, match="duplicate"):
        registry.register(_spec())


def test_validate_rejects_empty_routes_and_missing_executor() -> None:
    registry = ToolRegistry()
    registry.register(_spec(allowed_routes=frozenset()))
    registry.register(_spec(name="noexec", executor=None))
    with pytest.raises(ToolRegistryError, match="allowed_routes is empty"):
        registry.validate()
    registry2 = ToolRegistry()
    registry2.register(_spec(name="noexec", executor=None))
    with pytest.raises(ToolRegistryError, match="missing executor"):
        registry2.validate()


def test_validate_rejects_missing_input_model() -> None:
    registry = ToolRegistry()
    registry.register(_spec(input_model=None))  # type: ignore[arg-type]
    with pytest.raises(ToolRegistryError, match="missing input_model"):
        registry.validate()


def test_validate_rejects_invalid_name_and_limits() -> None:
    registry = ToolRegistry()
    registry.register(_spec(name="bad name"))
    with pytest.raises(ToolRegistryError, match="invalid tool name"):
        registry.validate()
    registry2 = ToolRegistry()
    registry2.register(_spec(max_results=0))
    with pytest.raises(ToolRegistryError, match="max_results"):
        registry2.validate()
    registry3 = ToolRegistry()
    registry3.register(_spec(timeout_seconds=0))
    with pytest.raises(ToolRegistryError, match="timeout_seconds"):
        registry3.validate()


def test_execute_validates_arguments() -> None:
    result = asyncio.run(_spec().execute({"value": "ok"}, _context()))
    assert result.status == "ok"
    invalid = asyncio.run(_spec().execute({"value": "x" * 100}, _context()))
    assert invalid.status == "invalid_argument"
    extra = asyncio.run(_spec().execute({"value": "ok", "extra": 1}, _context()))
    assert extra.status == "invalid_argument"


def test_execute_maps_timeout() -> None:
    async def slow(arguments: dict[str, object], context: ToolContext) -> ToolResult:
        await asyncio.sleep(10)
        return ToolResult(tool="echo", status="ok")

    spec = _spec(executor=slow, timeout_seconds=0.05)
    result = asyncio.run(spec.execute({"value": "ok"}, _context()))
    assert result.status == "timeout"


def test_execute_maps_infra_errors_to_unavailable_without_leaking() -> None:
    from qq_bot.agent.registry import ToolUnavailableError

    def boom(arguments: dict[str, object], context: ToolContext) -> ToolResult:
        raise ToolUnavailableError("redis is down")

    spec = _spec(executor=boom)
    result = asyncio.run(spec.execute({"value": "ok"}, _context()))
    assert result.status == "unavailable"
    assert "redis" not in result.model_dump_json()

    def raw_boom(arguments: dict[str, object], context: ToolContext) -> ToolResult:
        raise RuntimeError("/secret/path/key.txt")

    spec2 = _spec(executor=raw_boom)
    result2 = asyncio.run(spec2.execute({"value": "ok"}, _context()))
    assert result2.status == "unavailable"
    assert "/secret/path/key.txt" not in result2.model_dump_json()


def test_sync_executor_is_wrapped() -> None:
    def sync(arguments: dict[str, object], context: ToolContext) -> ToolResult:
        return ToolResult(tool="echo", status="ok")

    result = asyncio.run(_spec(executor=sync).execute({"value": "ok"}, _context()))
    assert result.status == "ok"


def test_missing_executor_returns_unavailable_not_crash() -> None:
    spec = _spec(executor=None)
    result = asyncio.run(spec.execute({"value": "ok"}, _context()))
    assert result.status == "unavailable"
