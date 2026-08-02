"""Package validated pet data for PRIVATE distribution (S3-DIST-02).

Produces ``data/dist/roco-data-<dataset_hash8>.tar.gz`` containing the
detail JSON files, ``latest.json`` and ``sha256SUMS.txt``.

The archive is meant for private object-storage buckets only: the game
data is NOT redistributable publicly (see DATA_LICENSE.md section 6).
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qq_bot.datapipeline.manifest import load_manifest  # noqa: E402

DEFAULT_DETAILS_DIR = ROOT / "data" / "roco_pet_details"
DEFAULT_MANIFEST = ROOT / "data" / "manifests" / "latest.json"
DEFAULT_OUTPUT_DIR = ROOT / "data" / "dist"

SUMS_NAME = "sha256SUMS.txt"


def build_sums(details_dir: Path, manifest_path: Path) -> dict[str, str]:
    """relpath -> sha256 over the files that will enter the archive."""
    manifest = load_manifest(manifest_path)
    sums: dict[str, str] = {}
    for filename in sorted(manifest.entries):
        path = details_dir / filename
        if not path.exists():
            raise FileNotFoundError(f"manifest entry missing on disk: {filename}")
        sums[filename] = hashlib.sha256(path.read_bytes()).hexdigest()
    sums["latest.json"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    return sums


def package_roco_data(details_dir: Path, manifest_path: Path, output_dir: Path) -> tuple[Path, str]:
    """Build the archive; returns (archive path, dataset_hash)."""
    manifest = load_manifest(manifest_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"roco-data-{manifest.dataset_hash[:8]}.tar.gz"

    sums = build_sums(details_dir, manifest_path)
    sums_text = "".join(f"{digest}  {rel}\n" for rel, digest in sorted(sums.items()))
    sums_path = output_dir / SUMS_NAME
    sums_path.write_text(sums_text, encoding="utf-8")

    with tarfile.open(archive, "w:gz") as tar:
        for filename in sorted(manifest.entries):
            tar.add(details_dir / filename, arcname=filename)
        tar.add(manifest_path, arcname="latest.json")
        tar.add(sums_path, arcname=SUMS_NAME)
    return archive, manifest.dataset_hash


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--details-dir", type=Path, default=DEFAULT_DETAILS_DIR)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)
    archive, dataset_hash = package_roco_data(args.details_dir, args.manifest, args.output_dir)
    print(f"packaged {archive} (dataset_hash={dataset_hash})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
