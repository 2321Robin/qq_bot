"""Task 8: packaging and private distribution (S3-DIST-02..05), fully offline."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

from scripts.download_roco_data import (
    _swap_directory,
    download_roco_data,
    parse_sums,
    verify_archive,
)
from scripts.package_roco_data import build_sums, package_roco_data

DETAILS_DIR = Path("tests/fixtures/data_pipeline/details")
MANIFESTS_DIR = Path("tests/fixtures/data_pipeline/manifests")
PREVIOUS_MANIFEST = MANIFESTS_DIR / "previous.json"


def _packed(tmp_path: Path) -> tuple[Path, str, dict[str, str]]:
    """Package the fixtures; returns (archive path, dataset_hash, sums)."""
    archive, dataset_hash = package_roco_data(DETAILS_DIR, PREVIOUS_MANIFEST, tmp_path / "dist")
    sums = build_sums(DETAILS_DIR, PREVIOUS_MANIFEST)
    return archive, dataset_hash, sums


def test_package_layout_and_dataset_hash(tmp_path: Path) -> None:
    archive, dataset_hash, sums = _packed(tmp_path)
    assert archive.name == f"roco-data-{dataset_hash[:8]}.tar.gz"
    with tarfile.open(archive, "r:gz") as tar:
        names = sorted(m.name for m in tar.getmembers())
    assert "latest.json" in names
    assert "sha256SUMS.txt" in names
    # Six valid fixture details, no quarantine/cache/report content.
    details = [n for n in names if n not in ("latest.json", "sha256SUMS.txt")]
    assert len(details) == 6
    assert all("error" not in n for n in details)
    # sha256SUMS covers details + latest.json with the expected format.
    sums_text = (tmp_path / "dist" / "sha256SUMS.txt").read_text(encoding="utf-8")
    assert set(parse_sums(sums_text)) == set(sums)
    line = sums_text.splitlines()[0]
    assert line.count("  ") == 1 and len(line.split()[0]) == 64
    # The packaged manifest hash equals the manifest's own dataset_hash.
    with tarfile.open(archive, "r:gz") as tar:
        latest = json.loads(tar.extractfile("latest.json").read())  # type: ignore[union-attr]
    assert latest["dataset_hash"] == dataset_hash


class CountingTransport:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.calls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        try:
            return self.files[url]
        except KeyError:
            raise RuntimeError(f"unexpected URL: {url}") from None


def _serve(tmp_path: Path, archive: Path, sums: dict[str, str]) -> CountingTransport:
    server = tmp_path / "server"
    server.mkdir()
    sums_text = "".join(f"{digest}  {rel}\n" for rel, digest in sorted(sums.items()))
    (server / "sha256SUMS.txt").write_text(sums_text, encoding="utf-8")
    return CountingTransport(
        {
            f"https://dist.example.invalid/{archive.name}": archive.read_bytes(),
            "https://dist.example.invalid/sha256SUMS.txt": sums_text.encode("utf-8"),
        }
    )


def test_download_success_and_install(tmp_path: Path) -> None:
    archive, dataset_hash, sums = _packed(tmp_path)
    transport = _serve(tmp_path, archive, sums)
    dest = tmp_path / "data"
    code = download_roco_data(
        "https://dist.example.invalid",
        dataset_hash,
        dest,
        tmp_path / "cache",
        fetch=transport,
    )
    assert code == 0
    assert (dest / "roco_pet_details" / "101-测试宠物A.json").exists()
    assert (dest / "manifests" / "latest.json").exists()
    installed = json.loads((dest / "manifests" / "latest.json").read_text(encoding="utf-8"))
    assert installed["dataset_hash"] == dataset_hash
    # Content identical to the source fixtures.
    assert (dest / "roco_pet_details" / "102-测试宠物B.json").read_bytes() == (
        DETAILS_DIR / "102-测试宠物B.json"
    ).read_bytes()
    # Verified archive becomes the cache.
    assert (tmp_path / "cache" / f"{dataset_hash}.tar.gz").exists()


def test_cache_hit_skips_downloads(tmp_path: Path) -> None:
    archive, dataset_hash, sums = _packed(tmp_path)
    transport = _serve(tmp_path, archive, sums)
    dest = tmp_path / "data"
    first = download_roco_data(
        "https://dist.example.invalid",
        dataset_hash,
        dest,
        tmp_path / "cache",
        fetch=transport,
    )
    assert first == 0
    downloads_first = len(transport.calls)
    assert downloads_first == 2  # sha256SUMS.txt + archive

    dest2 = tmp_path / "data2"
    second = download_roco_data(
        "https://dist.example.invalid",
        dataset_hash,
        dest2,
        tmp_path / "cache",
        fetch=transport,
    )
    assert second == 0
    assert len(transport.calls) == downloads_first  # cache hit: zero new fetches
    assert (dest2 / "roco_pet_details" / "101-测试宠物A.json").exists()


def test_tampered_archive_rejected_no_install_no_cache(tmp_path: Path) -> None:
    archive, dataset_hash, sums = _packed(tmp_path)
    transport = _serve(tmp_path, archive, sums)
    # Flip one byte inside the served payload.
    payload = bytearray(archive.read_bytes())
    payload[len(payload) // 2] ^= 0xFF
    transport.files[f"https://dist.example.invalid/{archive.name}"] = bytes(payload)

    dest = tmp_path / "data"
    code = download_roco_data(
        "https://dist.example.invalid",
        dataset_hash,
        dest,
        tmp_path / "cache",
        fetch=transport,
    )
    assert code == 1
    assert not (dest / "roco_pet_details").exists()
    assert not (dest / "manifests").exists()
    assert list((tmp_path / "cache").glob("*")) == []


def test_dataset_hash_mismatch_rejected(tmp_path: Path) -> None:
    archive, dataset_hash, sums = _packed(tmp_path)
    transport = _serve(tmp_path, archive, sums)
    dest = tmp_path / "data"
    code = download_roco_data(
        "https://dist.example.invalid",
        "0" * 64,
        dest,
        tmp_path / "cache",
        fetch=transport,
    )
    assert code == 1
    assert not (dest / "roco_pet_details").exists()
    assert not (dest / "manifests").exists()


def test_corrupt_cache_ignored_and_redownloaded(tmp_path: Path) -> None:
    archive, dataset_hash, sums = _packed(tmp_path)
    transport = _serve(tmp_path, archive, sums)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / f"{dataset_hash}.tar.gz").write_bytes(b"not a tarball")
    dest = tmp_path / "data"
    code = download_roco_data(
        "https://dist.example.invalid",
        dataset_hash,
        dest,
        cache,
        fetch=transport,
    )
    assert code == 0
    assert (dest / "roco_pet_details" / "101-测试宠物A.json").exists()
    assert len(transport.calls) == 2


def test_verify_archive_detects_each_kind_of_problem(tmp_path: Path) -> None:
    archive, dataset_hash, sums = _packed(tmp_path)
    # A sums entry with no matching archive member.
    extra = dict(sums)
    extra["ghost.json"] = "0" * 64
    problems = verify_archive(archive, extra)
    assert any("missing" in p for p in problems)
    # An archive member that no sums entry covers.
    missing = dict(sums)
    missing.pop(next(iter(missing)))
    problems = verify_archive(archive, missing)
    assert any("unexpected" in p for p in problems)
    # Wrong digest.
    wrong = dict(sums)
    wrong["101-测试宠物A.json"] = "0" * 64
    problems = verify_archive(archive, wrong)
    assert any("hash mismatch" in p for p in problems)


def test_swap_directory_replaces_and_restores(tmp_path: Path) -> None:
    dest = tmp_path / "target"
    dest.mkdir()
    (dest / "old.txt").write_text("old", encoding="utf-8")
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    (incoming / "new.txt").write_text("new", encoding="utf-8")
    _swap_directory(incoming, dest)
    assert (dest / "new.txt").read_text(encoding="utf-8") == "new"
    assert not (dest / "old.txt").exists()
    # Fresh install onto a missing destination.
    fresh = tmp_path / "fresh"
    incoming2 = tmp_path / "incoming2"
    incoming2.mkdir()
    (incoming2 / "a.txt").write_text("a", encoding="utf-8")
    _swap_directory(incoming2, fresh)
    assert (fresh / "a.txt").read_text(encoding="utf-8") == "a"


def test_parse_sums_rejects_malformed(tmp_path: Path) -> None:
    assert parse_sums("abc  def\n") == {"def": "abc"}
    assert parse_sums("") == {}
    assert parse_sums("# comment\n\n") == {}
    try:
        parse_sums("broken line without two fields")
    except ValueError:
        pass
    else:
        raise AssertionError("malformed line must raise")
