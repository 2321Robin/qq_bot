"""群聊图片持久化（S12-IMGSTORE-01）：把群友发来的图下载/落盘到本地，
供后续轮次回看与回捞——只会说话不会看图显然不是活人（部署者北极星）。

隐私边界（部署者裁决 2026-10-27，覆盖 V2「原始图片不入库」旧决策）：
- 图片文件存 data/chat_images/<群>/<消息行id>_<序号>（compose 命名卷内）；
- 双限清理：每群滚动保留最近 CHAT_IMAGE_KEEP_COUNT 张，且超过
  chat_memory_retention_days 的文件与行一起删（与文本记忆同寿命）；
- 不进日志、不进 prompt 以外的任何出口；DB 行只存相对路径不存内容。

设计纪律：存储是记忆增强，任何失败（下载超时/磁盘满/DB 异常）一律静默
计数，绝不拖累消息主流程。"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qq_bot.config import BotSettings
from qq_bot.observability import metrics
from qq_bot.services.vision_extract import read_image_as_data_url

_DATA_URL_RE = re.compile(r"^data:image/([a-zA-Z0-9.+-]+);base64,(.*)$", re.S)
_MIME_EXT = {"png": ".png", "jpeg": ".jpg", "jpg": ".jpg", "gif": ".gif", "webp": ".webp"}
_DOWNLOAD_CHUNK = 64 * 1024


@dataclass(frozen=True)
class StoredImage:
    path: str
    message_id: int


class ChatImageStore:
    """文件侧的图片仓库；DB 行走 ChatMemoryRepository 的 chat_images 方法。"""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir

    def _group_dir(self, group_id: int) -> Path:
        return self._base_dir / str(group_id)

    async def ingest(
        self,
        *,
        repository: Any,
        settings: BotSettings,
        group_id: int,
        message_row_id: int | None,
        sources: tuple[str, ...],
        http_client: Any = None,
        created_at: Any = None,
    ) -> int:
        """把一条消息携带的图片源落盘并登记 DB 行，返回成功张数。"""
        if message_row_id is None or not sources:
            return 0
        stored = 0
        group_dir = self._group_dir(group_id)
        for index, source in enumerate(sources):
            try:
                data, ext = await self._load(source, settings=settings, http_client=http_client)
                if not data:
                    metrics.AUTO_CHAT.labels("chat_image", "empty").inc()
                    continue
                group_dir.mkdir(parents=True, exist_ok=True)
                path = group_dir / f"{message_row_id}_{index}{ext}"
                path.write_bytes(data)
                await repository.add_chat_image(
                    group_id, message_row_id, path.as_posix(), created_at=created_at
                )
                stored += 1
            except Exception:
                metrics.AUTO_CHAT.labels("chat_image", "failed").inc()
                continue
        if stored:
            metrics.AUTO_CHAT.labels("chat_image", "stored").inc(stored)
            await self.cleanup(repository=repository, settings=settings, group_id=group_id)
        return stored

    async def _load(
        self, source: str, *, settings: BotSettings, http_client: Any
    ) -> tuple[bytes, str]:
        """单张图片源 → (字节, 扩展名)。http 下载带超时与 8MB 上限；data URL
        直接解码（与视觉提取同一红线：单图超限丢弃）。"""
        limit = 8 * 1024 * 1024
        if source.startswith("http://") or source.startswith("https://"):
            if http_client is None:
                return b"", ""
            import httpx

            async with httpx.AsyncClient(
                timeout=settings.chat_image_download_timeout_seconds, follow_redirects=True
            ) as client:
                response = await client.get(source)
                response.raise_for_status()
                content_type = response.headers.get("content-type", "image/jpeg").split(";")[0]
                data = response.content
            if len(data) > limit:
                return b"", ""
            ext = _MIME_EXT.get(content_type.removeprefix("image/").lower(), ".jpg")
            return data, ext
        match = _DATA_URL_RE.match(source)
        if match:
            mime, payload = match.group(1).lower(), match.group(2)
            data = base64.b64decode(payload, validate=False)
            if len(data) > limit:
                return b"", ""
            return data, _MIME_EXT.get(mime, ".jpg")
        # 本地路径源（NapCat 落地图等）：复制进仓库，原文件可能随时被清
        local = Path(source.removeprefix("file:///").removeprefix("file://"))
        if local.is_file():
            data = local.read_bytes()
            if len(data) > limit:
                return b"", ""
            return data, local.suffix.lower() or ".jpg"
        return b"", ""

    async def cleanup(self, *, repository: Any, settings: BotSettings, group_id: int) -> None:
        """双限清理（best effort）：先删行拿路径，再 unlink 文件。"""
        from datetime import datetime, timedelta, timezone

        try:
            cutoff = datetime.now(timezone.utc) - timedelta(
                days=settings.chat_memory_retention_days
            )
            paths = await repository.prune_chat_images(
                group_id, keep_count=settings.chat_image_keep_count, cutoff=cutoff
            )
        except Exception:
            metrics.AUTO_CHAT.labels("chat_image", "cleanup_failed").inc()
            return
        for raw in paths:
            try:
                Path(raw).unlink(missing_ok=True)
            except Exception:
                metrics.AUTO_CHAT.labels("chat_image", "cleanup_failed").inc()

    async def recall_latest(
        self, *, repository: Any, group_id: int, ttl_seconds: float
    ) -> tuple[str, int] | None:
        """TTL 内该群最新落库图 → (data URL, 发送者)。落库版回捞：重启不丢。"""
        from datetime import datetime, timedelta, timezone

        if ttl_seconds <= 0:
            return None
        try:
            hit = await repository.latest_chat_image(
                group_id, newer_than=datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)
            )
        except Exception:
            metrics.AUTO_CHAT.labels("chat_image", "recall_failed").inc()
            return None
        if hit is None:
            return None
        path, sender = hit
        data_url = read_image_as_data_url(path)
        if data_url is None:
            return None
        return data_url, sender


def is_store_enabled(settings: BotSettings) -> bool:
    return settings.chat_image_store_enabled
