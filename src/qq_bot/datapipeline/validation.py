"""Directory-level validation with quarantine handling (S3-SCHEMA-03)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from qq_bot.datapipeline.schemas import PetDetail


@dataclass
class ValidationResult:
    ok: dict[str, PetDetail] = field(default_factory=dict)
    quarantined: dict[str, str] = field(default_factory=dict)  # filename -> error text


def validate_detail_file(path: Path) -> PetDetail | None:
    """Return the validated detail, or None and move the file to quarantine."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return PetDetail.model_validate(raw)
    except ValidationError:
        return None


def validate_directory(details_dir: Path, quarantine_dir: Path) -> ValidationResult:
    """Validate every *.json in details_dir; failures move to quarantine_dir."""
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    result = ValidationResult()
    for path in sorted(details_dir.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            detail = PetDetail.model_validate(raw)
            result.ok[path.name] = detail
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            error_text = f"{type(exc).__name__}: {exc}"
            target = quarantine_dir / path.name
            path.replace(target)
            error_path = quarantine_dir / f"{path.stem}.error.json"
            error_path.write_text(
                json.dumps({"file": path.name, "error": error_text}, ensure_ascii=False, indent=2)
                + "\n",
                encoding="utf-8",
            )
            result.quarantined[path.name] = error_text
    return result
