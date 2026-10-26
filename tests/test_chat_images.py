"""群聊图片落库（S12-IMGSTORE-01）：下载/data URL/本地路径三源落盘、双限
清理、落库回捞。真 SQLite（tmp_path）+ 假 HTTP 下载，不依赖 nonebot。"""

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from qq_bot.config import BotSettings
from qq_bot.services.chat_images import ChatImageStore
from qq_bot.services.chat_memory import ChatMemoryRepository

PNG_BYTES = b"\x89PNG\r\n\x1a\nfakepng"


def _settings(**overrides) -> BotSettings:
    base = {
        "ai_api_key": "k",
        "chat_image_dir": "data/chat_images",
        "chat_image_keep_count": 100,
        "chat_memory_retention_days": 3,
    }
    base.update(overrides)
    return BotSettings(**base)


class _FakeResponse:
    def __init__(self, content: bytes, content_type: str = "image/png", status: int = 200):
        self.content = content
        self.headers = {"content-type": content_type}
        self.status_code = status

    def raise_for_status(self) -> None:
        assert self.status_code == 200


class _FakeHttpClient:
    """记录式假下载器（chat_images 自管 AsyncClient，注入的是模块级假件）。"""


@pytest.fixture
async def repository(tmp_path: Path):
    repo = ChatMemoryRepository(tmp_path / "memory.sqlite3", retention_days=3)
    await repo.open()
    yield repo
    await repo.close()


@pytest.fixture
def image_dir(tmp_path: Path) -> Path:
    d = tmp_path / "images"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def store(image_dir: Path) -> ChatImageStore:
    return ChatImageStore(image_dir)


async def _add_message(
    repo: ChatMemoryRepository, group_id: int = 1001, user_id: int = 2001
) -> int:
    return await repo.add_message(
        group_id=group_id, user_id=user_id, message_text="看这个", is_ai_prompt=False
    )


@pytest.mark.asyncio
async def test_ingest_http_source_downloads_and_registers(
    monkeypatch: pytest.MonkeyPatch, repository: ChatMemoryRepository, store: ChatImageStore
) -> None:
    row_id = await _add_message(repository)

    import httpx

    def fake_async_client(*args, **kwargs):
        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url):
                return _FakeResponse(PNG_BYTES)

        return _Client()

    monkeypatch.setattr(httpx, "AsyncClient", fake_async_client)
    stored = await store.ingest(
        repository=repository,
        settings=_settings(),
        group_id=1001,
        message_row_id=row_id,
        sources=("https://multimedia.example/a.png",),
        http_client=object(),
    )
    assert stored == 1
    paths = await repository.chat_image_paths_for_message(row_id)
    assert len(paths) == 1
    assert Path(paths[0]).read_bytes() == PNG_BYTES
    assert paths[0].endswith(f"{row_id}_0.png")


@pytest.mark.asyncio
async def test_ingest_data_url_and_local_source(
    repository: ChatMemoryRepository, store: ChatImageStore, tmp_path: Path
) -> None:
    row_id = await _add_message(repository)
    local = tmp_path / "raw.jpg"
    local.write_bytes(b"localjpg")

    data_url = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode()
    stored = await store.ingest(
        repository=repository,
        settings=_settings(),
        group_id=1001,
        message_row_id=row_id,
        sources=(data_url, str(local)),
        http_client=None,
    )
    assert stored == 2
    paths = await repository.chat_image_paths_for_message(row_id)
    assert Path(paths[0]).read_bytes() == PNG_BYTES
    assert Path(paths[1]).read_bytes() == b"localjpg"


@pytest.mark.asyncio
async def test_ingest_failure_is_silent_and_counted(
    monkeypatch: pytest.MonkeyPatch, repository: ChatMemoryRepository, store: ChatImageStore
) -> None:
    row_id = await _add_message(repository)

    import httpx

    def broken_client(*args, **kwargs):
        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url):
                raise RuntimeError("network down")

        return _Client()

    monkeypatch.setattr(httpx, "AsyncClient", broken_client)
    stored = await store.ingest(
        repository=repository,
        settings=_settings(),
        group_id=1001,
        message_row_id=row_id,
        sources=("https://multimedia.example/a.png",),
        http_client=object(),
    )
    assert stored == 0
    assert await repository.chat_image_paths_for_message(row_id) == []


@pytest.mark.asyncio
async def test_cleanup_enforces_keep_count_and_age(
    repository: ChatMemoryRepository, store: ChatImageStore, image_dir: Path
) -> None:
    old_time = datetime.now(UTC) - timedelta(days=4)
    # 3 张：1 张超龄（4 天前）、2 张新鲜——keep_count=2 也要把超龄那张清掉
    await repository.add_chat_image(1001, 901, str(image_dir / "old.png"), created_at=old_time)
    (image_dir / "old.png").write_bytes(b"old")
    for mid in (902, 903):
        row = await _add_message(repository)
        await repository.add_chat_image(1001, row, str(image_dir / f"{mid}.png"))
        (image_dir / f"{mid}.png").write_bytes(b"new")

    await store.cleanup(
        repository=repository, settings=_settings(chat_image_keep_count=2), group_id=1001
    )
    rows = await repository.latest_chat_image(
        1001, newer_than=datetime.now(UTC) - timedelta(days=5)
    )
    assert rows is not None  # 还有存活的图
    assert not (image_dir / "old.png").exists()  # 超龄文件被删
    assert (image_dir / "902.png").exists() and (image_dir / "903.png").exists()


@pytest.mark.asyncio
async def test_cleanup_keeps_newest_within_count(
    repository: ChatMemoryRepository, store: ChatImageStore, image_dir: Path
) -> None:
    for n in range(4):
        row = await _add_message(repository)
        await repository.add_chat_image(1001, row, str(image_dir / f"{n}.png"))
        (image_dir / f"{n}.png").write_bytes(b"x")
    await store.cleanup(
        repository=repository, settings=_settings(chat_image_keep_count=2), group_id=1001
    )
    assert (image_dir / "2.png").exists() and (image_dir / "3.png").exists()
    assert not (image_dir / "0.png").exists() and not (image_dir / "1.png").exists()


@pytest.mark.asyncio
async def test_recall_latest_returns_data_url_and_sender(
    repository: ChatMemoryRepository, store: ChatImageStore, image_dir: Path
) -> None:
    row_id = await _add_message(repository, user_id=2002)
    path = image_dir / "recall.png"
    path.write_bytes(PNG_BYTES)
    await repository.add_chat_image(1001, row_id, path.as_posix())

    hit = await store.recall_latest(repository=repository, group_id=1001, ttl_seconds=300.0)
    assert hit is not None
    data_url, sender = hit
    assert data_url.startswith("data:image/png;base64,")
    assert sender == 2002

    # TTL 外取不到
    assert await store.recall_latest(repository=repository, group_id=1001, ttl_seconds=0.0) is None
