"""Prebuilt n-gram inverted index over pet/skill data (S3-INDEX-01)."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from qq_bot.datapipeline.manifest import RefreshManifest
from qq_bot.datapipeline.schemas import PetDetail

INDEX_SCHEMA_VERSION = "1"
_CJK = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbfA-Za-z0-9]")


def grams(text: str) -> list[tuple[str, int]]:
    """Bigram postings with position; single surviving char degrades to unigram."""
    chars = [c for c in text if _CJK.match(c)]
    if not chars:
        return []
    if len(chars) == 1:
        return [(chars[0], 0)]
    return [(chars[i] + chars[i + 1], i) for i in range(len(chars) - 1)]


def build_index(
    details_dir: Path,
    validated: dict[str, PetDetail],
    index_path: Path,
    manifest: RefreshManifest,
) -> int:
    """Rebuild the index from validated details; returns the skill row count."""
    del details_dir  # validated details are authoritative; dir kept for the signature
    index_path.parent.mkdir(parents=True, exist_ok=True)
    if index_path.exists():
        index_path.unlink()
    con = sqlite3.connect(index_path)
    try:
        con.executescript(
            """
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE pet_records(
                record_id INTEGER PRIMARY KEY, number TEXT NOT NULL,
                name TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '');
            CREATE TABLE pet_ngrams(
                gram TEXT NOT NULL, record_id INTEGER NOT NULL, pos INTEGER NOT NULL);
            CREATE INDEX pet_ngrams_gram ON pet_ngrams(gram, record_id);
            CREATE TABLE skill_rows(
                skill_id INTEGER PRIMARY KEY, name TEXT NOT NULL, pet_name TEXT NOT NULL);
            CREATE TABLE skill_ngrams(
                gram TEXT NOT NULL, skill_id INTEGER NOT NULL, pos INTEGER NOT NULL);
            CREATE INDEX skill_ngrams_gram ON skill_ngrams(gram, skill_id);
            """
        )
        con.execute("INSERT INTO meta VALUES('schema_version', ?)", (INDEX_SCHEMA_VERSION,))
        con.execute("INSERT INTO meta VALUES('dataset_hash', ?)", (manifest.dataset_hash,))
        con.execute("INSERT INTO meta VALUES('built_at', ?)", (manifest.refreshed_at.isoformat(),))
        skill_id = 0
        for record_id, (filename, detail) in enumerate(sorted(validated.items())):
            number = detail.profile.get("编号", "")
            name = detail.name
            aliases = " ".join(detail.profile.get("别名", "").replace("、", " ").split())
            con.execute(
                "INSERT INTO pet_records VALUES(?,?,?,?)",
                (record_id, number, name, aliases),
            )
            for text, table in (
                (name, "pet_ngrams"),
                (aliases, "pet_ngrams"),
                (number, "pet_ngrams"),
            ):
                for gram, pos in grams(text):
                    con.execute(f"INSERT INTO {table} VALUES(?,?,?)", (gram, record_id, pos))
            for group in detail.skills:
                for row in group.rows:
                    if not row.name:
                        continue
                    con.execute("INSERT INTO skill_rows VALUES(?,?,?)", (skill_id, row.name, name))
                    for gram, pos in grams(row.name):
                        con.execute("INSERT INTO skill_ngrams VALUES(?,?,?)", (gram, skill_id, pos))
                    skill_id += 1
        con.commit()
    finally:
        con.close()
    return skill_id


def build_search_index(
    details_dir: Path,
    index_path: Path,
    manifest_path: Path | None = None,
) -> int:
    """Validate the details dir and rebuild the index from the given manifest.

    Invalid files are skipped (they never enter the index); the manifest path
    defaults to ``<details_dir>/../manifests/latest.json``.
    """
    from qq_bot.datapipeline.manifest import load_manifest
    from qq_bot.datapipeline.validation import validate_detail_file

    if manifest_path is None:
        manifest_path = details_dir.parent / "manifests" / "latest.json"
    manifest = load_manifest(manifest_path)
    validated: dict[str, PetDetail] = {}
    for path in sorted(details_dir.glob("*.json")):
        detail = validate_detail_file(path)
        if detail is not None:
            validated[path.name] = detail
    return build_index(details_dir, validated, index_path, manifest)


class RocoSearchIndex:
    """Read-only index handle; deterministic ranking (S3-INDEX-02/03)."""

    def __init__(self, con: sqlite3.Connection) -> None:
        self._con = con

    @classmethod
    def open(cls, path: Path) -> RocoSearchIndex | None:
        try:
            uri = path.resolve().as_uri() + "?mode=ro"
            con = sqlite3.connect(uri, uri=True)
            row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None or row[0] != INDEX_SCHEMA_VERSION:
                con.close()
                return None
            return cls(con)
        except sqlite3.Error:
            return None

    def search_pets(self, query: str, limit: int = 20) -> list[dict[str, str]]:
        """Deterministic ranking: exact > prefix > n-gram coverage, then (number, name)."""
        query_grams = grams(query)
        if not query_grams:
            return []
        gram_list = [g for g, _ in query_grams]
        placeholders = ",".join("?" for _ in gram_list)
        rows = self._con.execute(
            f"""
            SELECT r.number, r.name, COUNT(DISTINCT n.gram) AS matched
            FROM pet_ngrams n
            JOIN pet_records r ON r.record_id = n.record_id
            WHERE n.gram IN ({placeholders})
            GROUP BY r.record_id
            ORDER BY matched DESC, r.number ASC, r.name ASC
            LIMIT ?
            """,
            (*gram_list, limit * 4),
        ).fetchall()
        coverage = len(set(gram_list))
        results: list[dict[str, str]] = []
        for number, name, matched in rows:
            if matched < coverage and len(results) >= limit:
                continue
            score = 0
            if name == query or number == query:
                score = 3
            elif name.startswith(query):
                score = 2
            elif matched / coverage >= 0.5:
                score = 1
            if score:
                results.append({"number": number, "name": name})
            if len(results) >= limit:
                break
        return results

    def search_skills(self, query: str, limit: int = 20) -> list[dict[str, str]]:
        query_grams = grams(query)
        if not query_grams:
            return []
        gram_list = [g for g, _ in query_grams]
        placeholders = ",".join("?" for _ in gram_list)
        rows = self._con.execute(
            f"""
            SELECT s.name, s.pet_name, COUNT(DISTINCT n.gram) AS matched
            FROM skill_ngrams n
            JOIN skill_rows s ON s.skill_id = n.skill_id
            WHERE n.gram IN ({placeholders})
            GROUP BY s.skill_id
            ORDER BY matched DESC, s.pet_name ASC, s.name ASC
            LIMIT ?
            """,
            (*gram_list, limit * 4),
        ).fetchall()
        coverage = len(set(gram_list))
        results: list[dict[str, str]] = []
        for name, pet_name, matched in rows:
            if matched < coverage and len(results) >= limit:
                continue
            if name == query or name.startswith(query) or matched / coverage >= 0.5:
                results.append({"name": name, "pet_name": pet_name})
            if len(results) >= limit:
                break
        return results
