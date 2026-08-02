"""Rebuild the pet/skill n-gram search index manually (S3-INDEX-07)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qq_bot.datapipeline.index import build_index  # noqa: E402
from qq_bot.datapipeline.manifest import load_manifest  # noqa: E402
from qq_bot.datapipeline.validation import validate_detail_file  # noqa: E402

DEFAULT_DETAILS_DIR = ROOT / "data" / "roco_pet_details"
DEFAULT_MANIFEST = ROOT / "data" / "manifests" / "latest.json"
DEFAULT_INDEX_PATH = ROOT / "data" / "roco_search.sqlite3"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--details-dir", type=Path, default=DEFAULT_DETAILS_DIR)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--index-path", type=Path, default=DEFAULT_INDEX_PATH)
    args = parser.parse_args(argv)

    manifest = load_manifest(args.manifest)
    validated: dict[str, object] = {}
    skipped: list[str] = []
    for path in sorted(args.details_dir.glob("*.json")):
        detail = validate_detail_file(path)
        if detail is None:
            skipped.append(path.name)
        else:
            validated[path.name] = detail
    skill_count = build_index(args.details_dir, validated, args.index_path, manifest)
    print(
        f"indexed {len(validated)} pet record(s), {skill_count} skill row(s), "
        f"dataset_hash={manifest.dataset_hash}"
    )
    for name in skipped:
        print(f"skipped invalid file: {name}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
