"""Agent protocol models (S2-ROUTE-01/02, S2-TOOL-03/04, S2-EVID-01..03,
S2-AGENT-02).

All models are Pydantic with ``extra="forbid"``; anything the model layer
rejects must never reach the runtime. Tool calls never carry group/user
scope — scope is injected server-side (``AgentScope``) at execution time.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from typing import Any, Literal, TypeAlias
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EvidenceSource = Literal["local", "web", "memory"]
ClaimKind = Literal["factual", "conversational"]
ToolStatus = Literal["ok", "not_found", "invalid_argument", "unavailable", "timeout", "denied"]

_ID_MAX = 128
_NAME_MAX = 64
_ARGS_MAX_KEYS = 32
_PROMPT_MAX = 2000
_FINISH_REASON_MAX = 64
_URL_MAX = 2048

_EVIDENCE_PREFIX = {"local": "L", "web": "W", "memory": "M"}


class RouteKind(str, Enum):
    """The four supported route kinds (strict; no other values exist)."""

    LOCAL_KNOWLEDGE = "local_knowledge"
    WEB_SEARCH = "web_search"
    CHAT_MEMORY = "chat_memory"
    DIRECT_CHAT = "direct_chat"


class ReasonCode(str, Enum):
    """Stable, machine-readable reason codes for route decisions."""

    EXPLICIT_COMMAND = "explicit_command"
    STRUCTURED_CLASSIFIER = "structured_classifier"
    LOW_CONFIDENCE = "low_confidence"
    CLARIFY = "clarify"
    RULE_FALLBACK = "rule_fallback"
    CAPABILITY_ERROR = "capability_error"


class FailureCode(str, Enum):
    """Stable failure codes surfaced by the orchestrator to callers."""

    ROUND_LIMIT = "round_limit"
    CALL_LIMIT = "call_limit"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    BUDGET_INSUFFICIENT = "budget_insufficient"
    VERIFICATION_FAILED = "verification_failed"
    TOOL_DENIED = "tool_denied"
    INTERNAL_ERROR = "internal_error"


def _is_json_safe(value: Any) -> bool:
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, list):
        return all(_is_json_safe(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_safe(item) for key, item in value.items())
    return False


class RouteDecision(BaseModel):
    """Server-side route decision. ``allowed_tools`` is always derived by the
    server policy — model output is never trusted with a tool list."""

    model_config = ConfigDict(extra="forbid")

    primary_route: RouteKind
    confidence: float = Field(ge=0.0, le=1.0)
    reason_code: ReasonCode
    needs_clarification: bool = False
    secondary_route: RouteKind | None = None
    allowed_tools: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _secondary_differs_from_primary(self) -> "RouteDecision":
        if self.secondary_route is not None and self.secondary_route == self.primary_route:
            raise ValueError("secondary_route must differ from primary_route")
        return self

    @field_validator("allowed_tools")
    @classmethod
    def _tools_are_clean_names(cls, tools: tuple[str, ...]) -> tuple[str, ...]:
        for tool in tools:
            if not tool or len(tool) > _NAME_MAX:
                raise ValueError(f"invalid tool name: {tool!r}")
        return tools


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(max_length=_ID_MAX)
    name: str = Field(max_length=_NAME_MAX)
    arguments: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _name_is_identifier(cls, value: str) -> str:
        if not value or any(char in value for char in " \t\r\n"):
            raise ValueError(f"tool name must be a bare identifier: {value!r}")
        return value

    @field_validator("arguments")
    @classmethod
    def _arguments_are_json_safe(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > _ARGS_MAX_KEYS:
            raise ValueError(f"arguments exceed {_ARGS_MAX_KEYS} keys")
        if not _is_json_safe(value):
            raise ValueError("arguments contain non-JSON-safe values")
        return value


class Evidence(BaseModel):
    """One piece of retrieved evidence. ID prefix matches the source type;
    uniqueness within a request is enforced by the EvidenceStore (S2-EVID-04)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(max_length=_ID_MAX)
    source_type: EvidenceSource
    title: str = ""
    facts: dict[str, Any] = Field(default_factory=dict)
    url: str | None = None

    @model_validator(mode="after")
    def _prefix_matches_source(self) -> "Evidence":
        expected = _EVIDENCE_PREFIX[self.source_type]
        if not self.id.startswith(expected) or len(self.id) < 2:
            raise ValueError(
                f"evidence id {self.id!r} must start with {expected!r} "
                f"for source {self.source_type}"
            )
        return self

    @field_validator("facts")
    @classmethod
    def _facts_are_json_safe(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not _is_json_safe(value):
            raise ValueError("facts contain non-JSON-safe values")
        return value

    @field_validator("url")
    @classmethod
    def _url_limited(cls, value: str | None) -> str | None:
        if value is not None:
            if len(value) > _URL_MAX:
                raise ValueError("evidence url too long")
            parts = urlsplit(value)
            if parts.scheme not in ("http", "https") or not parts.netloc:
                raise ValueError(f"evidence url must be http(s) with a host: {value!r}")
        return value


class ToolResult(BaseModel):
    """Normalized tool output. JSON-serializable by construction; never
    contains stack traces, file paths, or API keys."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    tool: str = Field(max_length=_NAME_MAX)
    status: ToolStatus
    evidence: tuple[Evidence, ...] = ()
    warnings: tuple[str, ...] = ()
    truncated: bool = False

    @field_validator("schema_version")
    @classmethod
    def _schema_version_is_one(cls, value: int) -> int:
        if value != 1:
            raise ValueError("only ToolResult schema_version 1 is supported")
        return value


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)
    kind: ClaimKind = "factual"
    evidence_ids: tuple[str, ...] = ()


class GroundedAnswer(BaseModel):
    """Verified final answer: every factual claim references evidence."""

    model_config = ConfigDict(extra="forbid")

    claims: tuple[Claim, ...] = ()
    closing: str | None = Field(default=None, max_length=500)


class SafeFailure(BaseModel):
    """Terminal failure surfaced to the user with a stable code."""

    model_config = ConfigDict(extra="forbid")

    code: FailureCode
    message: str = Field(min_length=1, max_length=500)


AgentOutcome: TypeAlias = GroundedAnswer | SafeFailure


class AgentScope(BaseModel):
    """Server-injected scope. Tool arguments never contain these values —
    the orchestrator injects them at execution time."""

    model_config = ConfigDict(extra="forbid")

    group_id: str | None = Field(default=None, max_length=_ID_MAX)
    user_id: str = Field(max_length=_ID_MAX)
    can_use_chat_memory: bool = False


class AgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=_PROMPT_MAX)
    scope: AgentScope
    route: RouteDecision
    deadline: datetime


class NormalizedResponse(BaseModel):
    """Normalized model turn output (S2-AGENT-01/02). Tool call arguments are
    validated JSON; anything else is an explicit error, never a silent text
    reply. Missing usage is None — callers mark it estimated."""

    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(default=None, max_length=8000)
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str = Field(default="", max_length=_FINISH_REASON_MAX)
    usage: dict[str, int] | None = None

    @field_validator("usage")
    @classmethod
    def _usage_keys_known(cls, value: dict[str, int] | None) -> dict[str, int] | None:
        if value is not None:
            unknown = set(value) - {"prompt_tokens", "completion_tokens", "total_tokens"}
            if unknown:
                raise ValueError(f"unknown usage keys: {sorted(unknown)}")
        return value


class ProviderCapabilities(BaseModel):
    """What the provider is configured to support (S2-AGENT-07)."""

    model_config = ConfigDict(extra="forbid")

    tools: bool = True
    structured_output: bool = True
    usage: bool = True


def tool_arguments_json(tool_call: ToolCall) -> str:
    """Canonical JSON for the call cache (S2-AGENT-06: json.dumps sort_keys)."""
    return json.dumps(tool_call.arguments, sort_keys=True, ensure_ascii=False)
