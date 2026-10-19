"""Sticker library tests (S10-HUMAN-P2 H10)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from qq_bot.services.sticker_library import (
    MAX_STICKERS,
    StickerLibraryError,
    StickerStore,
    parse_manifest,
)


def _write(path: Path, data: bytes = b"fake-image-bytes") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


@pytest.fixture
def library_dir(tmp_path) -> Path:
    _write(tmp_path / "cat_laugh.png")
    _write(tmp_path / "dog_question.gif")
    _write(tmp_path / "angry.jpg")
    return tmp_path


def test_parse_manifest_keeps_valid_entries(library_dir: Path) -> None:
    library = parse_manifest(
        {
            "version": 1,
            "stickers": [
                {"id": "cat_laugh", "file": "cat_laugh.png", "description": "猫大笑"},
                {"id": "dog_question", "file": "dog_question.gif", "description": "问号狗"},
                {"id": "angry", "file": "angry.jpg", "description": "生气", "enabled": False},
            ],
        },
        base_dir=library_dir,
    )
    assert [s.id for s in library.stickers] == ["cat_laugh", "dog_question"]
    assert library.get("dog_question") is not None
    assert library.get("nope") is None


def test_candidates_are_stable_ordered_and_capped(library_dir: Path) -> None:
    entries = [
        {"id": f"s{index:02d}", "file": "cat_laugh.png", "description": f"图{index}"}
        for index in range(5)
    ]
    library = parse_manifest({"version": 1, "stickers": entries}, base_dir=library_dir)
    first = [s.id for s in library.candidates(3)]
    second = [s.id for s in library.candidates(3)]
    assert first == ["s00", "s01", "s02"]
    assert first == second  # 同一份库每次候选一致


def test_disabled_entries_are_absent(library_dir: Path) -> None:
    library = parse_manifest(
        {
            "version": 1,
            "stickers": [
                {"id": "cat_laugh", "file": "cat_laugh.png", "description": "猫", "enabled": False}
            ],
        },
        base_dir=library_dir,
    )
    assert library.stickers == ()


def test_duplicate_id_keeps_first_entry(library_dir: Path) -> None:
    library = parse_manifest(
        {
            "version": 1,
            "stickers": [
                {"id": "cat_laugh", "file": "cat_laugh.png", "description": "第一次"},
                {"id": "cat_laugh", "file": "cat_laugh.png", "description": "第二次"},
            ],
        },
        base_dir=library_dir,
    )
    assert len(library.stickers) == 1
    assert library.stickers[0].description == "第一次"


def test_unsafe_file_names_are_rejected(library_dir: Path) -> None:
    library = parse_manifest(
        {
            "version": 1,
            "stickers": [
                {"id": "escape", "file": "../outside.png", "description": "逃逸"},
                {"id": "abs", "file": "C:/windows/system32/x.png", "description": "绝对路径"},
                {"id": "nested", "file": "sub/cat.png", "description": "子目录"},
                {"id": "notimage", "file": "cat_laugh.txt", "description": "非图片"},
            ],
        },
        base_dir=library_dir,
    )
    assert library.stickers == ()


def test_missing_file_entry_is_skipped(library_dir: Path) -> None:
    library = parse_manifest(
        {"version": 1, "stickers": [{"id": "gone", "file": "gone.png", "description": "不在"}]},
        base_dir=library_dir,
    )
    assert library.stickers == ()


def test_bad_ids_and_descriptions_are_skipped(library_dir: Path) -> None:
    library = parse_manifest(
        {
            "version": 1,
            "stickers": [
                {"id": "有空格 id", "file": "cat_laugh.png", "description": "非法 id"},
                {"id": "ok", "file": "cat_laugh.png", "description": "   "},
                {"id": "long", "file": "cat_laugh.png", "description": "描" * 41},
                {"id": "nofile", "description": "缺 file 字段"},
            ],
        },
        base_dir=library_dir,
    )
    assert library.stickers == ()


def test_structural_errors_raise(library_dir: Path) -> None:
    with pytest.raises(StickerLibraryError):
        parse_manifest(["not", "an", "object"], base_dir=library_dir)
    with pytest.raises(StickerLibraryError):
        parse_manifest({"version": 2, "stickers": []}, base_dir=library_dir)
    with pytest.raises(StickerLibraryError):
        parse_manifest({"version": 1}, base_dir=library_dir)


def test_manifest_entry_limit_truncates(library_dir: Path) -> None:
    entries = [
        {"id": f"s{index:03d}", "file": "cat_laugh.png", "description": "图"}
        for index in range(MAX_STICKERS + 10)
    ]
    library = parse_manifest({"version": 1, "stickers": entries}, base_dir=library_dir)
    assert len(library.stickers) == MAX_STICKERS


def test_store_loads_on_first_read(library_dir: Path) -> None:
    (library_dir / "manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "stickers": [{"id": "cat_laugh", "file": "cat_laugh.png", "description": "猫大笑"}],
            }
        ),
        encoding="utf-8",
    )
    store = StickerStore(library_dir)
    assert [s.id for s in store.library.stickers] == ["cat_laugh"]


def test_store_hot_reloads_on_mtime_change(library_dir: Path) -> None:
    manifest = library_dir / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "stickers": [{"id": "cat_laugh", "file": "cat_laugh.png", "description": "猫"}],
            }
        ),
        encoding="utf-8",
    )
    store = StickerStore(library_dir)
    assert [s.id for s in store.library.stickers] == ["cat_laugh"]

    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "stickers": [
                    {"id": "dog_question", "file": "dog_question.gif", "description": "狗"}
                ],
            }
        ),
        encoding="utf-8",
    )
    os.utime(manifest, (manifest.stat().st_atime + 2, manifest.stat().st_mtime + 2))
    assert [s.id for s in store.library.stickers] == ["dog_question"]


def test_store_keeps_previous_library_when_reload_fails(library_dir: Path) -> None:
    manifest = library_dir / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "stickers": [{"id": "cat_laugh", "file": "cat_laugh.png", "description": "猫"}],
            }
        ),
        encoding="utf-8",
    )
    store = StickerStore(library_dir)
    assert [s.id for s in store.library.stickers] == ["cat_laugh"]

    manifest.write_text("{ broken json", encoding="utf-8")
    os.utime(manifest, (manifest.stat().st_atime + 2, manifest.stat().st_mtime + 2))
    assert [s.id for s in store.library.stickers] == ["cat_laugh"]  # 旧库保留


def test_store_without_directory_is_empty() -> None:
    assert StickerStore(None).library.stickers == ()
