"""Refresh orchestration: fetch -> validate -> gates -> diff -> publish (S3-DIST-01)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from qq_bot.config import BotSettings
from qq_bot.datapipeline.diff import (
    compute_diff,
    edge_signature,
    render_markdown,
    row_signature,
)
from qq_bot.datapipeline.fetch import (
    FetchOutcome,
    HTTPFetcher,
    UrllibFetcher,
    incremental_fetch,
)
from qq_bot.datapipeline.manifest import (
    RefreshManifest,
    build_manifest,
    load_manifest,
    verify_manifest,
    write_manifest_atomic,
)
from qq_bot.datapipeline.quality import run_quality_gates, skill_field_missing_rates
from qq_bot.datapipeline.schemas import PetDetail
from qq_bot.datapipeline.validation import validate_directory
from qq_bot.services.roco_evolution import normalize_pet_detail_directory

BWIKI_INDEX_URL = "https://wiki.biligame.com/rocom/%E7%B2%BE%E7%81%B5%E7%AD%9B%E9%80%89"


@dataclass
class RefreshArgs:
    details_dir: Path
    manifest_dir: Path
    reports_dir: Path
    quarantine_dir: Path
    staging_dir: Path
    index_path: Path
    offline: bool = False
    dry_run: bool = False
    verify_only: bool = False
    force: bool = False
    use_raw_pages: bool = False
    retries: int = 3
    delay_seconds: float = 0.0
    fetch_workers: int = 1
    no_normalize: bool = False
    no_cards: bool = False
    no_index: bool = False
    prune_removed: bool = False
    allow_quarantine: bool = False
    min_parser_version: int = 0
    index_url: str = BWIKI_INDEX_URL
    details: list[str] = field(default_factory=list)  # 仅离线：限定目标文件（可选）
    # 上次刷新报告的路径：只重抓其中记录失败的条目（WAF 等偶发错误续跑）
    retry_errors_from: Path | None = None


def _quarantine_summary(quarantine_dir: Path) -> dict[str, str]:
    """filename -> error text for every .error.json currently in quarantine."""
    summary: dict[str, str] = {}
    for error_path in sorted(quarantine_dir.glob("*.error.json")):
        try:
            payload = json.loads(error_path.read_text(encoding="utf-8"))
            filename = str(payload.get("file") or error_path.stem)
            summary[filename] = str(payload.get("error") or "unknown error")
        except (OSError, json.JSONDecodeError):
            summary[error_path.name] = "unreadable error file"
    return summary


def _targets_from_index(
    fetcher: HTTPFetcher, index_url: str
) -> list[tuple[str, str, dict[str, str]]]:
    from scripts.fetch_roco_pet_detail import load_bwiki_index_target_records

    response = fetcher.fetch(index_url)
    if response.status != 200:
        raise RuntimeError(f"index page returned HTTP {response.status}")
    return load_bwiki_index_target_records(response.body)


def _snapshot(details_dir: Path, snapshot_dir: Path) -> None:
    if snapshot_dir.exists():
        shutil.rmtree(snapshot_dir)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    if details_dir.exists():
        for path in sorted(details_dir.glob("*.json")):
            shutil.copy2(path, snapshot_dir / path.name)


def _merge(details_dir: Path, fetch_dir: Path, merged_dir: Path) -> None:
    if merged_dir.exists():
        shutil.rmtree(merged_dir)
    merged_dir.mkdir(parents=True)
    if details_dir.exists():
        for path in sorted(details_dir.glob("*.json")):
            shutil.copy2(path, merged_dir / path.name)
    if fetch_dir.exists():
        for path in sorted(fetch_dir.glob("*.json")):
            shutil.copy2(path, merged_dir / path.name)


def _quarantine_blocks(
    previous: RefreshManifest | None,
    quarantine_dir: Path,
    this_run_quarantined: dict[str, str],
) -> bool:
    """Pre-existing quarantine counts only when the file was trusted before;
    this-run moves count when the file was tracked in the previous manifest.
    Brand-new files that never passed gates do not block (107-BadPet.json in
    the CI fixture is the canonical case)."""
    previous_names = set(previous.entries) if previous is not None else set()
    pre_existing_tracked = any(
        path.name in previous_names for path in quarantine_dir.glob("*.json")
    )
    this_run_tracked = any(name in previous_names for name in this_run_quarantined)
    return pre_existing_tracked or this_run_tracked


def _report_paths(reports_dir: Path) -> tuple[Path, Path]:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return (
        reports_dir / f"refresh-{stamp}.json",
        reports_dir / f"refresh-{stamp}.md",
    )


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _change_set_payload(
    outcome: FetchOutcome,
    validated: dict[str, PetDetail],
    previous: RefreshManifest | None,
    snapshot_dir: Path,
    merged_dir: Path,
) -> dict[str, Any]:
    name_to_file = {detail.name: filename for filename, detail in validated.items()}
    if previous is not None:
        for filename in previous.entries:
            stem = filename[: filename.rfind(".")]
            base = stem.split("-", 1)[1] if "-" in stem else stem
            name_to_file.setdefault(base, filename)

    def files_for(names: list[str]) -> list[str]:
        return sorted({name_to_file.get(name, name) for name in names})

    payload: dict[str, Any] = {
        "added": files_for(outcome.change_set["added"]),
        "modified": files_for(outcome.change_set["modified"]),
        "removed": files_for(outcome.change_set["removed"]),
        "unchanged": files_for(outcome.change_set["unchanged"]),
        "details": {},
    }
    for filename in [*payload["added"], *payload["modified"]]:
        if filename not in validated:
            continue
        new_detail = validated[filename]
        old_detail: dict[str, Any] = {}
        snapshot_path = snapshot_dir / filename
        if snapshot_path.exists():
            try:
                old_detail = json.loads(snapshot_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                old_detail = {}
        old_rows = [
            row
            for group in old_detail.get("skills", [])
            if isinstance(group, dict)
            for row in group.get("rows", [])
            if isinstance(row, dict)
        ]
        new_rows = [
            row.model_dump(by_alias=True) for group in new_detail.skills for row in group.rows
        ]
        old_by_key = {row_signature(row): row for row in old_rows}
        new_by_key = {row_signature(row): row for row in new_rows}
        old_edges = {edge_signature(edge): edge for edge in _edges_of(old_detail)}
        new_edges = {
            edge_signature(edge): edge
            for edge in [
                edge.model_dump()
                for edge in [*new_detail.evolution.from_, *new_detail.evolution.to]
            ]
        }
        payload["details"][filename] = {
            "skills": {
                "added": sum(1 for key in new_by_key if key not in old_by_key),
                "modified": sum(
                    1
                    for key in new_by_key
                    if key in old_by_key
                    and row_signature(old_by_key[key]) != row_signature(new_by_key[key])
                ),
                "removed": sum(1 for key in old_by_key if key not in new_by_key),
            },
            "evolution": {
                "added": sorted(new_edges.keys() - old_edges.keys()),
                "modified": [
                    key
                    for key in sorted(old_edges.keys() & new_edges.keys())
                    if old_edges[key] != new_edges[key]
                ],
                "removed": sorted(old_edges.keys() - new_edges.keys()),
            },
        }
    if not payload["details"]:
        payload.pop("details")
    return payload


def _edges_of(detail: dict[str, Any]) -> list[dict[str, Any]]:
    evolution = detail.get("evolution", {})
    if not isinstance(evolution, dict):
        return []
    return [
        edge for key in ("from", "to") for edge in evolution.get(key, []) if isinstance(edge, dict)
    ]


def _write_reports(diff, reports_dir: Path) -> Path:
    json_path, md_path = _report_paths(reports_dir)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(json_path, diff.model_dump_json(indent=2) + "\n")
    _write_atomic(md_path, render_markdown(diff))
    return json_path


def _fetch_progress(done: int, total: int) -> None:
    """Live fetch progress: one compact line per 5% (unbuffered, hub-log visible)."""
    if total <= 0:
        return
    if done == total or done % max(1, total // 20) == 0:
        print(f"[fetch] {done}/{total} ({done * 100 // total}%)", flush=True)


def _filter_targets_by_report(
    targets: list[tuple[str, str, dict[str, str]]], report_path: Path
) -> list[tuple[str, str, dict[str, str]]]:
    """Keep only targets whose name appears in the report's fetch errors.

    Used for续跑: after a WAF-heavy run, re-fetch just the failed pages
    instead of walking the whole index again.
    """
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read report {report_path}: {exc}") from exc
    failed_names = {str(error.get("name")) for error in payload.get("errors", [])}
    if not failed_names:
        print("No fetch errors recorded in report; nothing to retry", flush=True)
    kept = [t for t in targets if (t[0] if len(t) > 1 else "") in failed_names]
    missing = sorted(failed_names - {t[0] for t in targets})
    if missing:
        print(
            f"{len(missing)} failed names not in current index: {missing[:8]}",
            file=sys.stderr,
        )
    return kept


def _run_refresh(
    args: RefreshArgs,
    previous: RefreshManifest | None,
    fetcher: HTTPFetcher | None,
    settings: BotSettings,
    publish_hook: Callable[[str], None] | None,
) -> int:
    details_dir = args.details_dir
    manifest_dir = args.manifest_dir
    quarantine_dir = args.quarantine_dir
    staging_dir = args.staging_dir

    snapshot_dir = staging_dir / "previous"
    fetch_dir = staging_dir / "fetch"
    merged_dir = staging_dir / "merged"
    _snapshot(details_dir, snapshot_dir)

    # 3. fetch (offline bypasses the fetcher entirely; disk files stay unchanged)
    if args.offline:
        # offline mode leaves no fetch artifacts; clear stale files from a
        # previous failed run so the merge below sees only disk state.
        if fetch_dir.exists():
            for stale in fetch_dir.glob("*"):
                if stale.is_file():
                    stale.unlink()
        outcome = FetchOutcome()
        disk_files = (
            {path.name for path in details_dir.glob("*.json")} if details_dir.exists() else set()
        )
        for filename in sorted(disk_files):
            stem = Path(filename).stem
            base = stem.split("-", 1)[1] if "-" in stem else stem
            outcome.change_set["unchanged"].append(base)
        if previous is not None:
            outcome.change_set["removed"] = sorted(
                name for name in previous.entries if name not in disk_files
            )
    else:
        fetcher = fetcher or UrllibFetcher()
        try:
            targets = _targets_from_index(fetcher, args.index_url)
        except Exception as exc:  # noqa: BLE001 - index fetch failure is a refresh error
            print(f"Failed to fetch index {args.index_url}: {exc}", file=sys.stderr)
            return 1
        if args.retry_errors_from is not None:
            targets = _filter_targets_by_report(targets, args.retry_errors_from)
        outcome = incremental_fetch(
            targets,
            previous=previous,
            fetcher=fetcher,
            output_dir=fetch_dir,
            quarantine_dir=quarantine_dir,
            parser_version=args.min_parser_version,
            force=args.force,
            use_raw_pages=args.use_raw_pages,
            delay_seconds=args.delay_seconds,
            workers=args.fetch_workers,
            progress=_fetch_progress,
        )

    # 4. merge validation: staged changes + untouched disk files, on the staging
    # copy; failures move into quarantine (S3-SCHEMA-03/06, official dir untouched)
    _merge(details_dir, fetch_dir, merged_dir)
    # normalize on the staged set before gates: evolution relations are built
    # against the full merged name set, and display-name annotations resolve
    # (e.g. 黑化加尔（黑化的样子） -> 黑化加尔) so dangling-edge gating sees
    # the published shape (S3-DIFF-05).
    if not args.no_normalize:
        normalize_pet_detail_directory(merged_dir)
    validation = validate_directory(merged_dir, quarantine_dir)
    validated = validation.ok

    quarantine_nonempty = _quarantine_blocks(previous, quarantine_dir, validation.quarantined)
    this_run_quarantine: dict[str, str] = {}
    for filename, error in validation.quarantined.items():
        this_run_quarantine[filename] = error
    for name, error in outcome.errors:
        if error.startswith("quarantined:"):
            this_run_quarantine.setdefault(f"{name}.json", error)

    # 5-6. quality gates; hard failure -> gate_failed report, exit 1, no publish
    gates = run_quality_gates(
        validated, previous=previous, settings=settings, quarantine_nonempty=quarantine_nonempty
    )
    gate_failed = any(not gate.passed for gate in gates)

    merged_manifest = build_manifest(
        merged_dir,
        index_url=(previous.index_url if previous else args.index_url),
        refreshed_at=datetime.now().astimezone(),
        parser_version=args.min_parser_version,
        previous=previous,
        etags=outcome.etags,
    )
    diff = compute_diff(
        previous,
        merged_manifest,
        validated,
        previous_files_dir=snapshot_dir,
        gates=gates,
        quarantine=this_run_quarantine,
        allow_quarantine=args.allow_quarantine,
        skill_field_missing=skill_field_missing_rates(validated),
        errors=outcome.errors,
    )

    # Fetch errors: report with failure reasons, exit 1, no publish (S3-DIFF-06).
    if outcome.errors:
        for name, error in outcome.errors:
            print(f"Fetch error {name}: {error}", file=sys.stderr)
        report_path = _write_reports(diff, args.reports_dir)
        print(f"Fetch errors; no publish. Report: {report_path}", file=sys.stderr)
        return 1

    # Hard failure blocks unless it is ONLY the quarantine gate and the
    # operator explicitly waived it (S3-SCHEMA-04).
    quarantine_gate = next((g for g in gates if g.name == "quarantine_empty"), None)
    only_quarantine_blocked = (
        quarantine_gate is not None
        and not quarantine_gate.passed
        and all(g.passed or g.name == "quarantine_empty" for g in gates)
    )
    if gate_failed and not (only_quarantine_blocked and args.allow_quarantine):
        report_path = _write_reports(diff, args.reports_dir)
        print(f"Quality gates failed; no publish. Report: {report_path}", file=sys.stderr)
        for gate in gates:
            if not gate.passed:
                print(
                    f"  gate {gate.name}: current={gate.current} threshold={gate.threshold}",
                    file=sys.stderr,
                )
        return 1

    # 7. publish (skipped in dry-run; official dir untouched on any failure)
    if not args.dry_run:
        try:
            if publish_hook is not None:
                publish_hook("before_replace")
            _publish_files(details_dir, merged_dir, validation.quarantined, args.prune_removed)
            if publish_hook is not None:
                publish_hook("after_replace")
            if not args.no_normalize:
                normalize_pet_detail_directory(details_dir)
            manifest = build_manifest(
                details_dir,
                index_url=(previous.index_url if previous else args.index_url),
                refreshed_at=datetime.now().astimezone(),
                parser_version=args.min_parser_version,
                previous=previous,
                etags=outcome.etags,
            )
            manifest.checks = {f"{gate.name}": gate.current for gate in gates} | {
                f"{gate.name}_threshold": gate.threshold for gate in gates
            }
            write_manifest_atomic(manifest, manifest_dir / "latest.json")
            change_set = _change_set_payload(outcome, validated, previous, snapshot_dir, merged_dir)
            _write_atomic(
                manifest_dir / "change_set.json",
                json.dumps(change_set, ensure_ascii=False, indent=2) + "\n",
            )
            if not args.no_index:
                try:
                    from qq_bot.datapipeline.index import build_search_index

                    build_search_index(
                        details_dir, args.index_path, args.manifest_dir / "latest.json"
                    )
                except Exception as exc:  # noqa: BLE001 - index is a fallback service
                    print(f"Search index build failed (refresh continues): {exc}", file=sys.stderr)
            if not args.no_cards:
                _run_card_generation(args, manifest_dir, details_dir)
        except Exception as exc:  # noqa: BLE001 - exception during publish -> exit 2
            print(f"Publish failed; official dir untouched: {exc}", file=sys.stderr)
            return 2

    report_path = _write_reports(diff, args.reports_dir)
    summary = outcome.change_set
    print(
        f"Refresh complete: added {len(summary['added'])}, modified {len(summary['modified'])}, "
        f"removed {len(summary['removed'])}, unchanged {len(summary['unchanged'])}; "
        f"gates {'passed' if not gate_failed else 'FAILED'}; report: {report_path}"
    )
    return 0


def _publish_files(
    details_dir: Path,
    merged_dir: Path,
    quarantined: dict[str, str],
    prune_removed: bool,
) -> None:
    details_dir.mkdir(parents=True, exist_ok=True)
    merged_names = {path.name for path in merged_dir.glob("*.json")}
    for path in sorted(details_dir.glob("*.json")):
        if path.name in merged_names:
            continue
        if path.name in quarantined or prune_removed:
            path.unlink()
    for path in sorted(merged_dir.glob("*.json")):
        path.replace(details_dir / path.name)


def _run_card_generation(args: RefreshArgs, manifest_dir: Path, details_dir: Path) -> None:
    script = Path(__file__).resolve().parents[3] / "scripts" / "generate_roco_pet_cards.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--change-set",
            str(manifest_dir / "change_set.json"),
            "--details-dir",
            str(details_dir),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(
            f"Card generation failed (refresh continues): {result.stderr.strip()}",
            file=sys.stderr,
        )


def run_refresh(
    args: RefreshArgs,
    *,
    fetcher: HTTPFetcher | None = None,
    settings: BotSettings | None = None,
    publish_hook: Callable[[str], None] | None = None,
) -> int:
    """Run one refresh cycle; returns 0 success, 1 gate/fetch/verify failure, 2 exception."""
    settings = settings or BotSettings()
    args.manifest_dir.mkdir(parents=True, exist_ok=True)
    args.reports_dir.mkdir(parents=True, exist_ok=True)
    args.quarantine_dir.mkdir(parents=True, exist_ok=True)

    latest_path = args.manifest_dir / "latest.json"
    previous_path = args.manifest_dir / "previous.json"
    # S3-MANIFEST-04: previous.json := current latest.json before computing anything.
    # dry-run never writes manifests, so the rotation is applied in memory only.
    previous: RefreshManifest | None = None
    if latest_path.exists():
        latest = load_manifest(latest_path)
        if args.dry_run or not args.verify_only:
            previous = latest
        if not args.dry_run:
            write_manifest_atomic(latest, previous_path)
    if previous is None and previous_path.exists():
        previous = load_manifest(previous_path)

    if args.verify_only:
        problems = verify_manifest(previous, args.details_dir) if previous is not None else []
        for problem in problems:
            print(problem)
        return 1 if problems else 0

    try:
        return _run_refresh(args, previous, fetcher, settings, publish_hook)
    except Exception as exc:  # noqa: BLE001 - any exception -> exit code 2
        print(f"Refresh failed: {exc}", file=sys.stderr)
        return 2
