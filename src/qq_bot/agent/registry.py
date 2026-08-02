"""Tool Registry (S2-TOOL-01..04, S2-TOOL-06).

Every tool is a ``ToolSpec`` registered in a ``ToolRegistry``; the
orchestrator only ever calls tools through the registry, and the model never
receives execution privileges — only schema descriptions.

The registry deliberately offers no persistent-write tools; long-term memory
writes go through command handlers only (Task 12).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ConfigDict

from qq_bot.agent.models import AgentScope, RouteKind, ToolResult


class ToolUnavailableError(Exception):
    """Infrastructure failure inside a tool (mapped to status ``unavailable``)."""


class InvalidArgumentError(Exception):
    """Executed with invalid arguments (mapped to status ``invalid_argument``)."""


@dataclass(frozen=True)
class ToolContext:
    """Server-injected execution context. The model never supplies these
    values; group/user scope comes from ``AgentScope`` and evidence ids
    derive from the request-wide ``evidence_index`` (S2-AGENT-05,
    S2-EVID-02: ids are unique within one request)."""

    scope: AgentScope
    evidence_index: int = 0


ToolExecutor = Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult] | ToolResult]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    allowed_routes: frozenset[RouteKind] = field(default_factory=frozenset)
    max_results: int = 5
    contains_untrusted: bool = False
    timeout_seconds: float = 10.0
    executor: ToolExecutor | None = None

    def to_json_schema(self) -> dict[str, Any]:
        schema = self.input_model.model_json_schema()
        schema["additionalProperties"] = False
        return schema

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """Validate arguments, run the executor under the timeout, and map
        failures to stable statuses. Never leaks stack traces or raw
        exception text into results."""
        if self.executor is None:
            return ToolResult(tool=self.name, status="unavailable", warnings=("not registered",))
        try:
            parsed = self.input_model.model_validate(arguments)
        except Exception:
            return ToolResult(tool=self.name, status="invalid_argument", warnings=("参数校验失败",))
        try:
            outcome = self.executor(parsed.model_dump(), context)
            if asyncio.iscoroutine(outcome):
                return await asyncio.wait_for(outcome, timeout=self.timeout_seconds)
            return outcome
        except asyncio.TimeoutError:
            return ToolResult(tool=self.name, status="timeout", warnings=("执行超时",))
        except InvalidArgumentError:
            return ToolResult(tool=self.name, status="invalid_argument", warnings=("参数非法",))
        except ToolUnavailableError:
            return ToolResult(tool=self.name, status="unavailable", warnings=("服务暂不可用",))
        except Exception:
            return ToolResult(tool=self.name, status="unavailable", warnings=("执行异常",))


class ToolRegistryError(ValueError):
    """Startup validation failed for one or more registered tools."""


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ToolRegistryError(f"duplicate tool name: {spec.name}")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def names_for(self, routes: frozenset[RouteKind] | set[RouteKind]) -> list[str]:
        return [spec.name for spec in self._specs.values() if spec.allowed_routes & routes]

    def schemas_for(self, routes: frozenset[RouteKind] | set[RouteKind]) -> list[dict[str, Any]]:
        return [
            spec.to_json_schema() for spec in self._specs.values() if spec.allowed_routes & routes
        ]

    def validate(self) -> None:
        """Startup check: every spec must be executable and schema-able;
        allowed_routes must be non-empty; names must be unique identifiers."""
        problems: list[str] = []
        for spec in self._specs.values():
            if not spec.name or any(char in spec.name for char in " \t\r\n"):
                problems.append(f"{spec.name or '<empty>'}: invalid tool name")
            if not spec.allowed_routes:
                problems.append(f"{spec.name}: allowed_routes is empty")
            if spec.executor is None:
                problems.append(f"{spec.name}: missing executor")
            if spec.input_model is None:
                problems.append(f"{spec.name}: missing input_model")
            else:
                try:
                    schema = spec.to_json_schema()
                    if schema.get("additionalProperties") is not False:
                        problems.append(f"{spec.name}: schema allows extra properties")
                except Exception as exc:
                    problems.append(f"{spec.name}: schema generation failed ({exc})")
            if spec.max_results < 1:
                problems.append(f"{spec.name}: max_results must be >= 1")
            if spec.timeout_seconds <= 0:
                problems.append(f"{spec.name}: timeout_seconds must be > 0")
        if problems:
            raise ToolRegistryError("; ".join(problems))


class StrictInputModel(BaseModel):
    """Base class for tool input models: no extra fields ever reach tools."""

    model_config = ConfigDict(extra="forbid")
