"""Evaluation runner entry point (S2-EVAL-06..15).

Modes:
- validate: structural validation of the dataset (schema, ids, split/category
  coverage, dataset hash). No network, no API keys — CI-safe.
- offline:  fixture-driven run over the frozen test split (Task 2).
- live:     real-provider benchmark (Task 15); requires AGENT_EVAL_LIVE=1
  and a configured provider (AI_API_KEY/AI_MODEL). Never touches the
  production chat database.

Usage:
    python scripts/run_agent_eval.py --mode validate --dataset evals/cases/roco_agent_v1.jsonl
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qq_bot.evaluation.models import DatasetValidationError, load_dataset  # noqa: E402


def _mode_validate(dataset: Path) -> int:
    try:
        cases, manifest = load_dataset(dataset)
    except (DatasetValidationError, FileNotFoundError, ValueError) as exc:
        print(f"dataset validation FAILED: {exc}", file=sys.stderr)
        return 1

    from scripts.generate_eval_cases import CATEGORY_TARGETS

    if not (100 <= manifest.case_count <= 300):
        print(
            f"dataset validation FAILED: case_count {manifest.case_count} outside 100..300",
            file=sys.stderr,
        )
        return 1

    counts: dict[str, int] = {}
    reviewed = 0
    for case in cases:
        counts[case.tags[0]] = counts.get(case.tags[0], 0) + 1
        if "human_reviewed" in case.tags:
            reviewed += 1

    failures: list[str] = []
    for category, target in CATEGORY_TARGETS.items():
        actual = counts.get(category, 0)
        if not (target * 0.9 <= actual <= target * 1.1):
            failures.append(f"{category}: {actual} (target {target})")
    if reviewed < 40:
        failures.append(f"human_reviewed={reviewed} < 40")

    print(f"dataset={dataset}")
    print(f"case_count={manifest.case_count}")
    print(f"split_counts={manifest.split_counts}")
    print(f"dataset_hash={manifest.dataset_hash}")
    print(f"human_reviewed={reviewed}")
    for category, target in CATEGORY_TARGETS.items():
        print(f"  {category}: {counts.get(category, 0)} (target {target})")

    if failures:
        print(f"dataset validation FAILED: {failures}", file=sys.stderr)
        return 1
    print("dataset validation OK")
    return 0


def _mode_offline(dataset: Path, split: str, report: Path | None) -> int:
    from qq_bot.evaluation.runner import OfflineRunner, RunConfig

    config = RunConfig(dataset_path=dataset, split=split, report_path=report)
    return OfflineRunner(config).run()


def _mode_live(dataset: Path, split: str, report: Path | None) -> int:
    from qq_bot.evaluation.live import LiveRunner
    from qq_bot.evaluation.runner import RunConfig

    config = RunConfig(dataset_path=dataset, split=split, report_path=report)
    return LiveRunner(config).run()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage-2 evaluation runner")
    parser.add_argument("--mode", choices=("validate", "offline", "live"), required=True)
    parser.add_argument("--dataset", default=str(ROOT / "evals" / "cases" / "roco_agent_v1.jsonl"))
    parser.add_argument("--split", default="test")
    parser.add_argument("--report", default=None, help="report output path")
    args = parser.parse_args(argv)

    dataset = Path(args.dataset)
    if args.mode == "validate":
        return _mode_validate(dataset)
    if args.mode == "offline":
        return _mode_offline(dataset, args.split, Path(args.report) if args.report else None)
    return _mode_live(dataset, args.split, Path(args.report) if args.report else None)


if __name__ == "__main__":
    raise SystemExit(main())
