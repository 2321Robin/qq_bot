"""本地表情包库（S10-HUMAN-P2 H10）。

部署者手工维护 `data/stickers/manifest.json` + 同目录图片；机器人按清单里的
**文字描述**挑图（不做识图）。素材本身是第三方版权内容，不入公开仓库，
见 `DATA_LICENSE.md`。

加载语义：

- 清单格式非法（坏 JSON / version 不符 / 顶层结构不对）→ 整库拒绝加载，
  抛 `StickerLibraryError`。启动期响亮失败，不猜格式。
- 单条非法（重复 id、路径逃逸、非图片扩展名、描述空/超长、文件缺失）→
  跳过该条并记 warning：一张坏图不该拖垮整库。
- 热重载按清单 mtime 变化触发；**重载失败保留旧库**并记 warning——表情包
  是装饰性能力，部署者手滑改坏清单不该让插话链路失败。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("qq_bot.stickers")

MANIFEST_NAME = "manifest.json"
SUPPORTED_VERSION = 1
MAX_STICKERS = 200
MAX_DESCRIPTION_CHARS = 40
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class StickerLibraryError(ValueError):
    """清单整体不可用（格式/版本/结构错误）。"""


@dataclass(frozen=True)
class Sticker:
    id: str
    path: Path
    description: str


@dataclass(frozen=True)
class StickerLibrary:
    stickers: tuple[Sticker, ...]

    def get(self, sticker_id: str) -> Sticker | None:
        for sticker in self.stickers:
            if sticker.id == sticker_id:
                return sticker
        return None

    def candidates(self, limit: int) -> tuple[Sticker, ...]:
        """按 id 稳定排序取前 ``limit`` 条：同一份库每次 prompt 候选一致，
        便于复盘模型的选择。"""
        ordered = sorted(self.stickers, key=lambda sticker: sticker.id)
        return tuple(ordered[: max(limit, 0)])


EMPTY_LIBRARY = StickerLibrary(stickers=())


def _safe_relative_file(raw: object) -> str | None:
    """文件名只允许清单目录下的单层相对名：拒绝绝对路径、`..`、分隔符。"""
    if not isinstance(raw, str):
        return None
    name = raw.strip()
    if not name or name in (".", ".."):
        return None
    if "/" in name or "\\" in name:
        return None
    if Path(name).is_absolute() or Path(name).name != name:
        return None
    if Path(name).suffix.lower() not in _IMAGE_SUFFIXES:
        return None
    return name


def parse_manifest(raw: object, *, base_dir: Path) -> StickerLibrary:
    """解析清单。整体结构错误抛 ``StickerLibraryError``；单条错误跳过。"""
    if not isinstance(raw, dict):
        raise StickerLibraryError("sticker manifest must be a JSON object")
    if raw.get("version") != SUPPORTED_VERSION:
        raise StickerLibraryError(f"unsupported sticker manifest version: {raw.get('version')!r}")
    items = raw.get("stickers")
    if not isinstance(items, list):
        raise StickerLibraryError("sticker manifest must contain a stickers list")

    if len(items) > MAX_STICKERS:
        logger.warning(
            "sticker manifest has %d entries; truncating to %d", len(items), MAX_STICKERS
        )
        items = items[:MAX_STICKERS]

    seen: set[str] = set()
    stickers: list[Sticker] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            logger.warning("sticker manifest entry %d is not an object; skipped", index)
            continue
        sticker_id = item.get("id")
        if not isinstance(sticker_id, str) or not _ID_RE.match(sticker_id.strip()):
            logger.warning("sticker manifest entry %d has an invalid id; skipped", index)
            continue
        sticker_id = sticker_id.strip()
        if sticker_id in seen:
            logger.warning("duplicate sticker id %r; later entry skipped", sticker_id)
            continue
        if item.get("enabled", True) is False:
            continue
        description = item.get("description")
        if not isinstance(description, str) or not description.strip():
            logger.warning("sticker %r has no description; skipped", sticker_id)
            continue
        description = description.strip()
        if len(description) > MAX_DESCRIPTION_CHARS:
            logger.warning("sticker %r description too long; skipped", sticker_id)
            continue
        filename = _safe_relative_file(item.get("file"))
        if filename is None:
            logger.warning("sticker %r has an unsafe or unsupported file; skipped", sticker_id)
            continue
        path = base_dir / filename
        if not path.is_file():
            logger.warning("sticker %r file is missing; skipped", sticker_id)
            continue
        seen.add(sticker_id)
        stickers.append(Sticker(id=sticker_id, path=path, description=description))
    return StickerLibrary(stickers=tuple(stickers))


def load_manifest_file(manifest_path: Path) -> StickerLibrary:
    """读取并解析清单文件；IO/JSON 错误一律包成 ``StickerLibraryError``。"""
    try:
        raw: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise StickerLibraryError(f"cannot read sticker manifest {manifest_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise StickerLibraryError(f"invalid sticker manifest JSON: {exc}") from exc
    return parse_manifest(raw, base_dir=manifest_path.parent)


class StickerStore:
    """进程内库快照 + 按 mtime 的热重载（同 game_digest 的日历模式）。"""

    def __init__(self, directory: Path | None) -> None:
        self._directory = directory
        self._library = EMPTY_LIBRARY
        self._mtime: float | None = None

    @property
    def library(self) -> StickerLibrary:
        self.refresh_if_changed()
        return self._library

    def _manifest_path(self) -> Path | None:
        if self._directory is None:
            return None
        return self._directory / MANIFEST_NAME

    @staticmethod
    def _current_mtime(path: Path | None) -> float | None:
        if path is None:
            return None
        try:
            return path.stat().st_mtime
        except OSError:
            return None

    def refresh_if_changed(self) -> None:
        """清单 mtime 变化时重载；失败保留旧库（装饰性能力不响亮失败）。"""
        manifest = self._manifest_path()
        mtime = self._current_mtime(manifest)
        if manifest is None or mtime is None or mtime == self._mtime:
            return
        try:
            library = load_manifest_file(manifest)
        except StickerLibraryError:
            logger.warning("sticker manifest reload failed; keeping the previous library")
            self._mtime = mtime  # 同一份坏文件不重复刷告警
            return
        self._library = library
        self._mtime = mtime
