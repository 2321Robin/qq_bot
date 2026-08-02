"""Refresh manifest: per-file hashes, dataset hash, verification (S3-MANIFEST)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class LicenseInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    claim: str
    attribution_required: bool
    commercial_use: bool
    redistribution: str
    game_assets: str


class ManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_url: str
    sha256: str
    size: int
    parser_version: int
    fetched_at: datetime
    etag: str | None = None
    last_modified: str | None = None


class RefreshManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = 1
    refreshed_at: datetime
    index_url: str
    license: LicenseInfo
    entries: dict[str, ManifestEntry]
    dataset_hash: str
    previous_hash: str | None = None
    checks: dict[str, float] = {}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_dataset_hash(hashes: dict[str, str]) -> str:
    """Deterministic: sorted filenames, concatenated sha256, re-hash (S3-MANIFEST-02)."""
    joined = "".join(hashes[name] for name in sorted(hashes))
    return hashlib.sha256(joined.encode("ascii")).hexdigest()


def build_manifest(
    details_dir: Path,
    *,
    index_url: str,
    refreshed_at: datetime,
    parser_version: int,
    previous: RefreshManifest | None,
    etags: dict[str, str | None] | None = None,
) -> RefreshManifest:
    entries: dict[str, ManifestEntry] = {}
    for path in sorted(details_dir.glob("*.json")):
        detail = json.loads(path.read_text(encoding="utf-8"))
        metadata = detail.get("metadata") or {}
        old = previous.entries.get(path.name) if previous else None
        source_url = str(detail.get("source_url") or "") or (old.source_url if old else "")
        entries[path.name] = ManifestEntry(
            source_url=source_url,
            sha256=file_sha256(path),
            size=path.stat().st_size,
            parser_version=int(metadata.get("parser_version") or parser_version),
            fetched_at=refreshed_at,
            etag=etags.get(path.name) if etags else None,
            last_modified=old.last_modified if old else None,
        )
    hashes = {name: entry.sha256 for name, entry in entries.items()}
    return RefreshManifest(
        refreshed_at=refreshed_at,
        index_url=index_url,
        license=LicenseInfo(
            source="BWiki (wiki.biligame.com/rocom)",
            claim="CC BY-NC-SA 4.0",
            attribution_required=True,
            commercial_use=False,
            redistribution="private-only",
            game_assets="proprietary",
        ),
        entries=entries,
        dataset_hash=compute_dataset_hash(hashes),
        previous_hash=previous.dataset_hash if previous else None,
    )


def load_manifest(path: Path) -> RefreshManifest:
    return RefreshManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))


def write_manifest_atomic(manifest: RefreshManifest, path: Path) -> None:
    """Write via temp file + os.replace so failure never leaves a partial latest.json."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def verify_manifest(manifest: RefreshManifest, details_dir: Path) -> list[str]:
    """Return a list of inconsistencies; empty means consistent (S3-MANIFEST-05)."""
    problems: list[str] = []
    disk = {path.name: file_sha256(path) for path in details_dir.glob("*.json")}
    for name, entry in manifest.entries.items():
        if name not in disk:
            problems.append(f"missing on disk: {name}")
        elif disk[name] != entry.sha256:
            problems.append(f"hash mismatch: {name}")
    for name in sorted(set(disk) - set(manifest.entries)):
        problems.append(f"untracked file: {name}")
    actual = compute_dataset_hash({n: e.sha256 for n, e in manifest.entries.items()})
    if actual != manifest.dataset_hash:
        problems.append(f"dataset_hash mismatch: {actual} != {manifest.dataset_hash}")
    return problems
