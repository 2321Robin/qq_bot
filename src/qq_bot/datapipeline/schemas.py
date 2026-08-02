"""Pydantic contracts for Roco pet detail data (S3-SCHEMA)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

STAT_KEYS = {"生命", "物攻", "魔攻", "物防", "魔防", "速度"}
NUMBER_RE = r"^\d{3}$"


class SkillRow(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    level: str = Field(default="", validation_alias="等级", serialization_alias="等级")
    name: str = Field(default="", validation_alias="技能", serialization_alias="技能")
    energy: str = Field(default="", validation_alias="耗能", serialization_alias="耗能")
    category: str = Field(default="", validation_alias="类型", serialization_alias="类型")
    power: str = Field(default="", validation_alias="威力", serialization_alias="威力")
    effect: str = Field(default="", validation_alias="效果", serialization_alias="效果")

    @field_validator("level", "name", "energy", "category", "power", "effect")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


class SkillGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1)
    rows: list[SkillRow] = Field(default_factory=list)


class EvolutionEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    target: str
    condition: str = ""
    raw_condition: str = ""
    forward_text: str = ""
    backward_text: str = ""
    text: str = ""

    @field_validator("source", "target")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("edge source/target must be non-empty")
        return value

    @model_validator(mode="after")
    def _distinct(self) -> "EvolutionEdge":
        if self.source == self.target:
            raise ValueError("self-loop evolution edge")
        return self


class EvolutionBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_: list[EvolutionEdge] = Field(
        default_factory=list, validation_alias="from", serialization_alias="from"
    )
    to: list[EvolutionEdge] = Field(default_factory=list)
    evolution_condition: str = ""


class DetailMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parser_version: int = Field(ge=1)
    generated_at: datetime


class PetDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    name: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    attributes: list[str] = Field(default_factory=list)
    evolution_condition: str = ""
    total_race_value: int | None = Field(default=None, ge=0)
    profile: dict[str, str] = Field(default_factory=dict)
    stats: dict[str, int] = Field(default_factory=dict)
    skills: list[SkillGroup] = Field(default_factory=list)
    metadata: DetailMetadata
    evolution: EvolutionBlock = Field(default_factory=EvolutionBlock)

    @field_validator("stats")
    @classmethod
    def _stats_keys(cls, value: dict[str, int]) -> dict[str, int]:
        unknown = set(value) - STAT_KEYS
        if unknown:
            raise ValueError(f"unknown stat keys: {sorted(unknown)}")
        if any(v < 0 for v in value.values()):
            raise ValueError("negative stat value")
        return value

    @field_validator("profile")
    @classmethod
    def _profile_number(cls, value: dict[str, str]) -> dict[str, str]:
        number = (value.get("编号") or "").strip()
        if not number:
            raise ValueError("profile.编号 is required")
        if not number.isdigit() or len(number) != 3:
            raise ValueError(f"profile.编号 must be a 3-digit string, got {number!r}")
        return value

    @field_validator("name", "source_url")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("field must be non-empty")
        return value
