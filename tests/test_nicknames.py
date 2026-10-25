"""昵称目录（S11-AUTO-04）：清洗规则、未知才拉取、落盘复用与防抖。

部署者裁决（2026-10-25）：不做周期刷新——只在遇到未知 id 时拉取一次并
保存（data/group_nicknames.json，gitignored）；改名滞后可接受。"""

import json

import pytest

from qq_bot.services.nicknames import NicknameDirectory, sanitize_nickname


def test_sanitize_nickname_strips_and_caps() -> None:
    assert sanitize_nickname("  风\n哥  ") == "风 哥"
    assert sanitize_nickname("x" * 30) == "x" * 20


def test_sanitize_nickname_rejects_dirty_values() -> None:
    # 纯数字形似 QQ 号：渲染层绝不出现原始 QQ 号，同一红线挡住数字形昵称
    assert sanitize_nickname("12345678") is None
    assert sanitize_nickname("") is None
    assert sanitize_nickname("   ") is None
    assert sanitize_nickname(None) is None
    assert sanitize_nickname(123) is None


class _FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_fetches_once_for_unknown_ids_then_serves_memory(tmp_path) -> None:
    calls: list[int] = []

    async def fetch(group_id: int) -> list[dict]:
        calls.append(group_id)
        return [
            {"user_id": 1, "card": "风哥", "nickname": "n1"},
            {"user_id": 2, "card": "", "nickname": "  "},  # 两者皆空 → 丢弃
            {"user_id": 3, "card": "424242"},  # 纯数字 → 丢弃
        ]

    clock = _FakeClock()
    directory = NicknameDirectory(
        fetch, store_path=tmp_path / "nicks.json", min_refetch_seconds=300.0, clock=clock
    )

    mapping = await directory.get(7, {1, 2, 3})
    assert mapping == {1: "风哥"}  # 2、3 清洗后丢弃 → 回落字母
    # 已知 id 再问：不拉取（纯内存）
    await directory.get(7, {1})
    assert calls.count(7) == 1
    # 未知的 id 在防抖窗口内：也不拉取
    await directory.get(7, {1, 99})
    assert calls.count(7) == 1
    # 过了防抖窗口且有未知 id：再拉一次
    clock.now += 301.0
    await directory.get(7, {1, 99})
    assert calls.count(7) == 2


@pytest.mark.asyncio
async def test_persists_and_reloads_without_fetch(tmp_path) -> None:
    calls: list[int] = []

    async def fetch(group_id: int) -> list[dict]:
        calls.append(group_id)
        return [{"user_id": 5, "card": "老卡"}]

    store = tmp_path / "nicks.json"
    clock = _FakeClock()
    directory = NicknameDirectory(fetch, store_path=store, min_refetch_seconds=300.0, clock=clock)
    await directory.get(7, {5})
    assert calls.count(7) == 1
    assert json.loads(store.read_text(encoding="utf-8"))["7"]["5"] == "老卡"

    # 新实例（模拟重启）：落盘缓存直接命中，零拉取
    reborn = NicknameDirectory(fetch, store_path=store, min_refetch_seconds=300.0, clock=clock)
    assert await reborn.get(7, {5}) == {5: "老卡"}
    assert calls.count(7) == 1


@pytest.mark.asyncio
async def test_failure_backs_off_and_serves_stale(tmp_path) -> None:
    payloads: list[list[dict] | Exception] = [
        [{"user_id": 5, "card": "老卡"}],
        RuntimeError("refresh down"),
    ]
    calls: list[int] = []

    async def fetch(group_id: int) -> list[dict]:
        calls.append(group_id)
        item = payloads.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    clock = _FakeClock()
    directory = NicknameDirectory(
        fetch, store_path=tmp_path / "nicks.json", min_refetch_seconds=300.0, clock=clock
    )

    assert await directory.get(7, {5}) == {5: "老卡"}
    clock.now += 301.0
    # 刷新失败：沿用旧值，防抖窗口内不再重试
    assert await directory.get(7, {5, 9}) == {5: "老卡"}
    assert calls.count(7) == 2
    await directory.get(7, {9})
    assert calls.count(7) == 2


@pytest.mark.asyncio
async def test_corrupt_store_file_starts_empty(tmp_path) -> None:
    store = tmp_path / "nicks.json"
    store.write_text("not-json{", encoding="utf-8")
    calls: list[int] = []

    async def fetch(group_id: int) -> list[dict]:
        calls.append(group_id)
        return [{"user_id": 1, "card": "风哥"}]

    directory = NicknameDirectory(
        fetch, store_path=store, min_refetch_seconds=300.0, clock=_FakeClock()
    )
    assert await directory.get(7, {1}) == {1: "风哥"}  # 坏文件当空缓存，拉取重建
    assert calls.count(7) == 1
