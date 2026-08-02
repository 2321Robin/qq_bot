"""Incremental fetcher with conditional requests and content-hash skip (S3-INCR)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen

from qq_bot.datapipeline.manifest import RefreshManifest
from qq_bot.datapipeline.schemas import PetDetail
from qq_bot.services.roco_bwiki import parse_pet_detail
from qq_bot.services.roco_evolution import normalize_pet_details

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


@dataclass
class FetchResponse:
    status: int  # 200 / 304 / other
    headers: dict[str, str]  # lowercase keys
    body: str  # empty for 304


class HTTPFetcher(Protocol):
    def fetch(self, url: str, headers: dict[str, str] | None = None) -> FetchResponse: ...


def conditional_headers(etag: str | None, last_modified: str | None) -> dict[str, str]:
    headers: dict[str, str] = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    return headers


def classify(previous_sha256: str | None, response: FetchResponse) -> str:
    """Return 'unchanged' when 304, or when 200 body hash matches the previous entry."""
    if response.status == 304:
        return "unchanged"
    if response.status != 200:
        return "error"
    body_sha = hashlib.sha256(response.body.encode("utf-8")).hexdigest()
    if previous_sha256 is not None and body_sha == previous_sha256:
        return "unchanged"
    return "changed"


# ---- helpers moved from scripts/fetch_roco_pet_detail.py (re-exported there) ----


def _is_raw_page_url(url: str) -> bool:
    return "action=raw" in urlparse(url).query


def raw_page_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc != "wiki.biligame.com" or not parsed.path.startswith("/rocom/"):
        return url
    query = parsed.query
    if "action=raw" in query:
        return url
    query = f"{query}&action=raw" if query else "action=raw"
    return urlunparse(parsed._replace(query=query))


def apply_index_metadata(detail: dict, metadata: dict[str, str]) -> None:
    number = metadata.get("精灵编号", "").strip()
    if number:
        profile = detail.setdefault("profile", {})
        if isinstance(profile, dict):
            profile.setdefault("编号", number)
    total = metadata.get("总种族值", "").strip()
    if total.isdigit() and detail.get("total_race_value") is None:
        detail["total_race_value"] = int(total)


def merge_skill_groups(detail: dict, raw_detail: dict) -> None:
    groups = detail.setdefault("skills", [])
    raw_groups = raw_detail.get("skills", [])
    if not isinstance(groups, list) or not isinstance(raw_groups, list):
        return
    existing_sources = {group.get("source") for group in groups if isinstance(group, dict)}
    for raw_group in raw_groups:
        if not isinstance(raw_group, dict):
            continue
        source = raw_group.get("source")
        if source in existing_sources and _has_skill_effects(groups):
            continue
        groups.append(raw_group)
        existing_sources.add(source)


def _has_skill_effects(groups: list) -> bool:
    for group in groups:
        if not isinstance(group, dict):
            continue
        rows = group.get("rows", [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and row.get("效果"):
                return True
    return False


def contains_bwiki_placeholder(value: str) -> bool:
    return "数据源为BWIKI" in value or "wiki.biligame.com" in value and "数据源" in value


# ---- production fetcher (urllib with curl fallback; network only here) ----

# Windows ships curl.exe in System32; POSIX runners have plain curl.
CURL_BIN = "curl.exe" if os.name == "nt" else "curl"

# BWiki's WAF serves a small CSS-only challenge page (empty <title>) instead of
# the real page once a client trips its rate limits. Real pet pages are much
# larger and carry a real <title>; treat the challenge as a retryable fetch
# error so a transient WAF state can never quarantine valid data (observed in
# real operations: 460 files quarantined with empty names in one run).
_CHALLENGE_MARKERS = ("<title></title>", "box-sizing:border-box")


def looks_like_challenge_page(body: str) -> bool:
    return len(body) < 50000 and all(marker in body for marker in _CHALLENGE_MARKERS)


class UrllibFetcher:
    """Production HTTP fetcher: urllib with browser headers, curl fallback."""

    def fetch(self, url: str, headers: dict[str, str] | None = None) -> FetchResponse:
        request_headers = dict(BROWSER_HEADERS)
        if headers:
            request_headers.update(headers)
        request = Request(url, headers=request_headers)
        try:
            with urlopen(request, timeout=30) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                body = response.read().decode(charset, errors="replace")
                response_headers = {
                    str(key).lower(): str(value) for key, value in response.headers.items()
                }
                if looks_like_challenge_page(body):
                    raise URLError("anti-bot challenge page served (WAF block)")
                return FetchResponse(status=response.status, headers=response_headers, body=body)
        except (HTTPError, URLError, TimeoutError, OSError):
            return self._fetch_with_curl(url, headers)

    def _fetch_with_curl(self, url: str, headers: dict[str, str] | None = None) -> FetchResponse:
        command = [
            CURL_BIN,
            "-L",
            "--retry",
            "2",
            "--max-time",
            "30",
            "-A",
            BROWSER_HEADERS["User-Agent"],
            "-H",
            f"Accept: {BROWSER_HEADERS['Accept']}",
            "-H",
            f"Accept-Language: {BROWSER_HEADERS['Accept-Language']}",
        ]
        if headers:
            for key, value in headers.items():
                command += ["-H", f"{key}: {value}"]
        command.append(url)
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            raise URLError(result.stderr.strip() or f"{CURL_BIN} exited with {result.returncode}")
        if looks_like_challenge_page(result.stdout):
            raise URLError("anti-bot challenge page served (WAF block)")
        return FetchResponse(status=200, headers={}, body=result.stdout)


# ---- incremental loop ----


@dataclass
class FetchOutcome:
    change_set: dict[str, list[str]] = field(  # added/modified/removed/unchanged
        default_factory=lambda: {"added": [], "modified": [], "removed": [], "unchanged": []}
    )
    errors: list[tuple[str, str]] = field(default_factory=list)  # (name, error)
    etags: dict[str, str | None] = field(default_factory=dict)  # filename -> etag
    last_modified: dict[str, str | None] = field(default_factory=dict)  # filename -> last-modified


def _target_parts(
    target: tuple[str, str] | tuple[str, str, dict[str, str]],
) -> tuple[str, str, dict[str, str]]:
    if len(target) == 2:
        name, url = target
        return name, url, {}
    name, url, metadata = target
    return name, url, metadata or {}


def _previous_filename(name: str, number: str, previous: RefreshManifest | None) -> str | None:
    if previous is None:
        return None
    candidates = [f"{name}.json"]
    if number:
        candidates.insert(0, f"{number}-{name}.json")
    for candidate in candidates:
        if candidate in previous.entries:
            return candidate
    return None


def _output_filename(detail: dict, fallback_name: str) -> str:
    name = str(detail.get("name") or fallback_name).strip()
    number = str(detail.get("profile", {}).get("编号", "")).strip()
    if number:
        return f"{number}-{name}.json"
    return f"{name}.json"


def _quarantine_write(quarantine_dir: Path, filename: str, content: str, error: str) -> None:
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    (quarantine_dir / filename).write_text(content, encoding="utf-8")
    error_path = quarantine_dir / f"{Path(filename).stem}.error.json"
    error_path.write_text(
        json.dumps({"file": filename, "error": error}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def incremental_fetch(
    targets: list[tuple[str, str, dict[str, str]]],
    *,
    previous: RefreshManifest | None,
    fetcher: HTTPFetcher,
    output_dir: Path,
    quarantine_dir: Path,
    parser_version: int,
    force: bool = False,
    use_raw_pages: bool = False,
    delay_seconds: float = 0.0,
    workers: int = 1,
    progress: Callable[[int, int], None] | None = None,
) -> FetchOutcome:
    """Fetch changed targets into output_dir (publish moves them after gates pass).

    - 304 / content-hash match -> unchanged, no parse, no write.
    - changed body -> parse via parse_pet_detail + apply_index_metadata,
      validate via PetDetail.model_validate; invalid -> quarantine.
    - files in previous but absent from output dir and absent from targets -> removed.
    - output_dir is the staging directory in refresh orchestration (Task 7);
      unchanged files are never copied into it.
    - workers > 1 fetches concurrently (each worker paces itself with
      delay_seconds per request); results are merged in input order.
    - progress(done, total) is invoked once per target when given.

    The output dir is emptied first: a previous failed run leaves stale files
    behind that would otherwise be merged as if freshly fetched.
    """
    if output_dir.exists():
        for stale in output_dir.glob("*"):
            if stale.is_file():
                stale.unlink()

    total = len(targets)

    def fetch_one(target: tuple[str, str, dict[str, str]]) -> tuple[Any, ...]:
        if delay_seconds:
            time.sleep(delay_seconds)
        name, url, index_metadata = _target_parts(target)
        number = index_metadata.get("精灵编号", "").strip()
        old_name = _previous_filename(name, number, previous)
        old_entry = previous.entries[old_name] if old_name and previous else None
        previous_sha256 = old_entry.sha256 if old_entry else None

        fetch_url = raw_page_url(url) if use_raw_pages else url
        headers = (
            {}
            if force
            else conditional_headers(
                old_entry.etag if old_entry else None,
                old_entry.last_modified if old_entry else None,
            )
        )
        try:
            response = fetcher.fetch(fetch_url, headers or None)
        except Exception as exc:  # noqa: BLE001 - network errors of any kind
            return ("error", name, f"{type(exc).__name__}: {exc}")

        kind = "changed" if force else classify(previous_sha256, response)
        if kind == "unchanged":
            return ("unchanged", name)
        if kind == "error":
            return ("error", name, f"HTTP {response.status}")

        detail: dict[str, Any] | None = None
        try:
            detail = parse_pet_detail(fetch_url, response.body)
            apply_index_metadata(detail, index_metadata)
            # The parser emits evolution_edges; the data contract uses evolution
            # blocks (same conversion the legacy script applies to the directory).
            detail = normalize_pet_details([detail])[0]
            detail.pop("evolution_edges", None)
            detail.setdefault("metadata", {}).setdefault(
                "generated_at", datetime.now(timezone.utc).isoformat()
            )
            PetDetail.model_validate(detail)
        except Exception as exc:  # noqa: BLE001 - parse/validation failures quarantine
            payload = json.dumps(detail, ensure_ascii=False, indent=2) if detail else response.body
            _quarantine_write(
                quarantine_dir, f"{name}.json", payload, f"{type(exc).__name__}: {exc}"
            )
            return ("quarantined", name, f"quarantined: {type(exc).__name__}: {exc}")

        filename = _output_filename(detail, name)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / filename).write_text(
            json.dumps(detail, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return (
            "written",
            name,
            filename,
            bool(old_entry),
            response.headers.get("etag") if response.headers else None,
            response.headers.get("last-modified") if response.headers else None,
            str(detail.get("source_url") or ""),
        )

    results: list[tuple[Any, ...]] = []
    if workers > 1:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for result in pool.map(fetch_one, targets):
                results.append(result)
                if progress:
                    progress(len(results), total)
    else:
        for target in targets:
            results.append(fetch_one(target))
            if progress:
                progress(len(results), total)

    outcome = FetchOutcome(change_set={"added": [], "modified": [], "removed": [], "unchanged": []})
    written: dict[str, str] = {}  # filename -> source url for this run
    for result in results:
        kind = result[0]
        if kind == "unchanged":
            outcome.change_set["unchanged"].append(result[1])
        elif kind == "error":
            outcome.errors.append((result[1], result[2]))
        elif kind == "quarantined":
            outcome.errors.append((result[1], result[2]))
        elif kind == "written":
            _, name, filename, was_old, etag, last_modified, source_url = result
            written[filename] = source_url
            outcome.change_set["modified" if was_old else "added"].append(name)
            outcome.etags[filename] = etag
            outcome.last_modified[filename] = last_modified

    # removed: tracked in previous, absent from this run's targets, never rewritten.
    target_names = {_target_parts(t)[0] for t in targets}
    output_files = {p.name for p in output_dir.glob("*.json")} if output_dir.exists() else set()
    for old_filename in sorted(previous.entries) if previous else []:
        stem = Path(old_filename).stem
        base = stem.split("-", 1)[1] if "-" in stem else stem
        in_targets = any(t_name in (stem, base) for t_name in target_names)
        if old_filename not in output_files and old_filename not in written and not in_targets:
            outcome.change_set["removed"].append(old_filename)

    return outcome
