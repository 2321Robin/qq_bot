"""Download and verify the packaged dataset for PRIVATE distribution (S3-DIST-03/04/05).

``--base-url`` and ``--dataset-hash`` are REQUIRED; there is no built-in
public URL (the data is not publicly redistributable — see DATA_LICENSE.md
section 6). Every downloaded byte is verified against sha256SUMS.txt and
the manifest dataset_hash before anything touches the official dirs.
Failed verification: nothing is installed, nothing is cached, exit 1.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tarfile
import urllib.request
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qq_bot.datapipeline.manifest import (  # noqa: E402
    load_manifest,
    write_manifest_atomic,
)

SUMS_NAME = "sha256SUMS.txt"
DEFAULT_DEST = ROOT / "data"
DEFAULT_CACHE_DIR = ROOT / "data" / ".cache"


def parse_sums(text: str) -> dict[str, str]:
    """sha256SUMS.txt -> relpath -> sha256; blank lines and comments ignored."""
    sums: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2:
            raise ValueError(f"malformed sha256SUMS line: {line!r}")
        digest, rel = parts
        sums[rel] = digest
    return sums


def verify_archive(archive: Path, sums: dict[str, str]) -> list[str]:
    """Check every file inside the archive against sha256SUMS.

    The archive's own sha256SUMS.txt member is expected but not hashed.
    Returns a list of problems; empty means fully consistent.
    """
    problems: list[str] = []
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = {m.name: m for m in tar.getmembers() if m.isfile()}
            for rel, expected in sorted(sums.items()):
                member = members.get(rel)
                if member is None:
                    problems.append(f"missing file: {rel}")
                    continue
                handle = tar.extractfile(member)
                assert handle is not None
                digest = hashlib.sha256()
                for chunk in iter(lambda: handle.read(65536), b""):
                    digest.update(chunk)
                if digest.hexdigest() != expected:
                    problems.append(f"hash mismatch: {rel}")
            for rel in members:
                if rel != SUMS_NAME and rel not in sums:
                    problems.append(f"unexpected file: {rel}")
    except (tarfile.TarError, OSError, zlib.error) as exc:
        problems.append(f"cannot read archive: {exc}")
    return problems


def _extract_to(archive: Path, target_dir: Path) -> None:
    """Safe extraction: strip prefixes, refuse absolute/parent paths."""
    target_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            name = member.name.replace("\\", "/")
            if name.startswith("/") or ".." in name.split("/"):
                raise ValueError(f"unsafe archive member: {member.name}")
        # filter="data" only exists on Python >=3.11.4/3.12; 3.11.0-3.11.3
        # reject the keyword entirely, so omit it there (pre-check loop above
        # already rejects absolute/".." members).
        if hasattr(tarfile, "data_filter"):
            tar.extractall(target_dir, filter="data")
        else:
            tar.extractall(target_dir)


def _swap_directory(tmp: Path, dest: Path) -> None:
    """Near-atomic swap: move current aside, promote tmp, drop backup."""
    backup: Path | None = None
    if dest.exists():
        backup = dest.with_name(f"{dest.name}.bak-{tmp.parent.name}")
        if backup.exists():
            shutil.rmtree(backup)
        dest.replace(backup)
    try:
        tmp.replace(dest)
    except OSError:
        if backup is not None and backup.exists() and not dest.exists():
            backup.replace(dest)
        raise
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)


def download_roco_data(
    base_url: str,
    dataset_hash: str,
    dest: Path,
    cache_dir: Path,
    *,
    fetch=None,
) -> int:
    """Fetch + verify + install; returns process exit code (0 ok / 1 failed)."""
    if fetch is None:

        def fetch(url: str) -> bytes:
            with urllib.request.urlopen(url, timeout=60) as response:
                return response.read()

    base = base_url.rstrip("/")
    hash8 = dataset_hash[:8]
    archive_name = f"roco-data-{hash8}.tar.gz"
    cache_archive = cache_dir / f"{dataset_hash}.tar.gz"

    # Cache hit: reuse only when the archive is self-consistent (S3-DIST-04).
    if cache_archive.exists():
        try:
            with tarfile.open(cache_archive, "r:gz") as tar:
                member = tar.getmember(SUMS_NAME)
                handle = tar.extractfile(member)
                assert handle is not None
                sums = parse_sums(handle.read().decode("utf-8"))
            problems = verify_archive(cache_archive, sums)
            if not problems:
                return _install(cache_archive, dataset_hash, dest)
        except (tarfile.TarError, ValueError, KeyError, OSError):
            pass  # corrupt cache: ignore and re-download

    try:
        sums_text = fetch(f"{base}/{SUMS_NAME}").decode("utf-8")
        sums = parse_sums(sums_text)
        payload = fetch(f"{base}/{archive_name}")
    except Exception as exc:
        print(f"download failed: {exc}", file=sys.stderr)
        return 1

    cache_dir.mkdir(parents=True, exist_ok=True)
    download_path = cache_dir / f"{dataset_hash}.part"
    download_path.write_bytes(payload)
    problems = verify_archive(download_path, sums)
    if problems:
        download_path.unlink(missing_ok=True)
        for problem in problems:
            print(f"verification failed: {problem}", file=sys.stderr)
        return 1
    code = _install(download_path, dataset_hash, dest)
    if code == 0:
        download_path.replace(cache_archive)  # verified archive becomes the cache
    else:
        download_path.unlink(missing_ok=True)
    return code


def _install(archive: Path, dataset_hash: str, dest: Path) -> int:
    """Extract + dataset_hash check + atomic swap; exit 0/1, no partial install."""
    details_dest = dest / "roco_pet_details"
    manifests_dest = dest / "manifests"
    staging = dest / ".install-tmp"
    if staging.exists():
        shutil.rmtree(staging)
    try:
        dest.mkdir(parents=True, exist_ok=True)
        _extract_to(archive, staging)
        latest = staging / "latest.json"
        data = json.loads(latest.read_text(encoding="utf-8"))
        if data.get("dataset_hash") != dataset_hash:
            print(
                "verification failed: package dataset_hash mismatch "
                f"({data.get('dataset_hash')!r} != {dataset_hash!r})",
                file=sys.stderr,
            )
            return 1
        # Collect the flat detail files into a clean tree for the swap.
        details_tmp = staging / "details"
        details_tmp.mkdir()
        for path in sorted(staging.glob("*.json")):
            if path.name != "latest.json":
                path.rename(details_tmp / path.name)
        _swap_directory(details_tmp, details_dest)
        manifests_dest.mkdir(parents=True, exist_ok=True)
        write_manifest_atomic(load_manifest(latest), manifests_dest / "latest.json")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"install failed: {exc}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="许可边界：数据不可公开再分发；base-url 必须是私有载体。",
    )
    parser.add_argument(
        "--base-url",
        required=True,
        help="私有分发基址（必填；数据不可公开再分发，见 DATA_LICENSE.md）",
    )
    parser.add_argument("--dataset-hash", required=True, help="期望的 dataset_hash")
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    args = parser.parse_args(argv)
    return download_roco_data(args.base_url, args.dataset_hash, args.dest, args.cache_dir)


if __name__ == "__main__":
    raise SystemExit(main())
