"""Evaluation dataset schemas (S2-EVAL-01..05).

The public dataset only contains synthetic entity data (MIT fixtures plus a
deterministic synthetic extension roster); wiki-derived ``data/`` content is
never redistributed (DATA_LICENSE.md). All JSONL cases are validated through
``EvalCase`` and the dataset-level checks in :func:`load_dataset`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SplitName = Literal["dev", "test", "private"]
EvidenceType = Literal["local", "web", "memory"]

ALLOWED_EVIDENCE_TYPES: tuple[str, ...] = ("local", "web", "memory")


class EvalRoute(str, Enum):
    """The four canonical routes (mirrors the agent RouteKind values)."""

    LOCAL_KNOWLEDGE = "local_knowledge"
    WEB_SEARCH = "web_search"
    CHAT_MEMORY = "chat_memory"
    DIRECT_CHAT = "direct_chat"


class EvalCase(BaseModel):
    """One evaluation case; unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")

    id: str
    split: SplitName
    prompt: str
    tags: list[str] = Field(default_factory=list)
    expected_route: EvalRoute
    allowed_tools: list[str] = Field(default_factory=list)
    required_facts: list[str] = Field(default_factory=list)
    forbidden_facts: list[str] = Field(default_factory=list)
    expected_refusal: bool = False
    expected_evidence_types: list[EvidenceType] = Field(default_factory=list)
    freshness_required: bool = False

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("case id must not be empty")
        return value

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("case prompt must not be empty")
        return value

    @field_validator("expected_evidence_types")
    @classmethod
    def validate_evidence_types(cls, value: list[EvidenceType]) -> list[EvidenceType]:
        for entry in value:
            if entry not in ALLOWED_EVIDENCE_TYPES:
                raise ValueError(
                    f"expected_evidence_types only allows {ALLOWED_EVIDENCE_TYPES!r}, got {entry!r}"
                )
        return value


class DatasetManifest(BaseModel):
    """Stable fingerprint of a frozen dataset (S2-EVAL-05/06)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    dataset_hash: str
    case_count: int
    split_counts: dict[str, int]


def canonical_case_json(case: EvalCase) -> str:
    """Deterministic canonical JSON for one case (sorted keys, no spaces)."""
    return json.dumps(
        case.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def compute_dataset_hash(cases: Sequence[EvalCase]) -> str:
    """SHA-256 over the canonical JSON of cases sorted by id (S2-EVAL-06)."""
    canonical = "\n".join(canonical_case_json(case) for case in sorted(cases, key=lambda c: c.id))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_manifest(cases: Sequence[EvalCase]) -> DatasetManifest:
    split_counts: dict[str, int] = {}
    for case in cases:
        split_counts[case.split] = split_counts.get(case.split, 0) + 1
    return DatasetManifest(
        dataset_hash=compute_dataset_hash(cases),
        case_count=len(cases),
        split_counts=split_counts,
    )


class DatasetValidationError(ValueError):
    """Raised when a dataset fails structural validation."""


def validate_dataset(cases: Sequence[EvalCase]) -> None:
    """Dataset-level checks: unique ids and non-empty case list."""
    if not cases:
        raise DatasetValidationError("dataset must not be empty")
    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise DatasetValidationError(f"duplicate case id {case.id!r}")
        seen.add(case.id)


def load_dataset(path: str | Path) -> tuple[list[EvalCase], DatasetManifest]:
    """Load and fully validate a JSONL dataset (S2-EVAL-03/06)."""
    dataset_path = Path(path)
    if not dataset_path.exists():
        raise FileNotFoundError(f"dataset file not found: {dataset_path}")

    cases: list[EvalCase] = []
    for line_number, line in enumerate(dataset_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetValidationError(f"invalid JSON on line {line_number}: {exc.msg}") from exc
        cases.append(EvalCase.model_validate(raw))

    validate_dataset(cases)
    return cases, build_manifest(cases)


def write_dataset(cases: Sequence[EvalCase], path: str | Path) -> DatasetManifest:
    """Write a dataset in deterministic order (sorted by id) and return its
    manifest. The test split is frozen after the first release; new failure
    samples go into the next dataset version instead of editing expecteds."""
    validate_dataset(cases)
    dataset_path = Path(path)
    dataset_path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(cases, key=lambda case: case.id)
    dataset_path.write_text(
        "\n".join(
            json.dumps(case.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
            for case in ordered
        )
        + "\n",
        encoding="utf-8",
    )
    return build_manifest(ordered)
