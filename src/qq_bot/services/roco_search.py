"""Runtime fuzzy search over the prebuilt index with full fallback (S3-INDEX-04/05)."""

from __future__ import annotations

from pathlib import Path

from qq_bot.datapipeline.index import RocoSearchIndex


def open_search_index(path: Path) -> RocoSearchIndex | None:
    """None when missing/corrupt/schema mismatch — callers fall back to full scan."""
    return RocoSearchIndex.open(path)


def pet_candidates(index: RocoSearchIndex, query: str, records: tuple | list) -> list:
    """Map index hits back to in-memory records; empty result falls back to full scan."""
    hits = index.search_pets(query)
    by_key = {(r.number, r.name): r for r in records}
    matched = [by_key[(h["number"], h["name"])] for h in hits if (h["number"], h["name"]) in by_key]
    return matched


def skill_candidates(index: RocoSearchIndex, query: str, records: tuple | list) -> list:
    """Map skill index hits back to in-memory records keyed by (pet_name, name)."""
    hits = index.search_skills(query)
    by_key = {(r.pet_name, r.name): r for r in records}
    matched = [
        by_key[(h["pet_name"], h["name"])] for h in hits if (h["pet_name"], h["name"]) in by_key
    ]
    return matched
