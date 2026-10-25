"""昵称目录（S11-AUTO-04）：清洗规则、TTL 缓存与失败退避。"""

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
async def test_directory_caches_within_ttl_and_prefers_card() -> None:
    calls: list[int] = []

    async def fetch(group_id: int) -> list[dict]:
        calls.append(group_id)
        return [
            {"user_id": 1, "card": "风哥", "nickname": "n1"},
            {"user_id": 2, "card": "", "nickname": "  "},  # 两者皆空 → 丢弃
            {"user_id": 3, "card": "424242"},  # 纯数字 → 丢弃
        ]

    clock = _FakeClock()
    directory = NicknameDirectory(fetch, ttl_seconds=600.0, clock=clock)

    mapping = await directory.get(7)
    assert mapping == {1: "风哥"}
    await directory.get(7)
    assert calls.count(7) == 1  # TTL 内不重复拉取

    clock.now += 601.0
    await directory.get(7)
    assert calls.count(7) == 2  # 过期后刷新


@pytest.mark.asyncio
async def test_directory_failure_serves_empty_and_backs_off() -> None:
    calls: list[int] = []

    async def fetch(group_id: int) -> list[dict]:
        calls.append(group_id)
        raise RuntimeError("onebot down")

    clock = _FakeClock()
    directory = NicknameDirectory(fetch, ttl_seconds=600.0, clock=clock)

    assert await directory.get(7) == {}
    assert await directory.get(7) == {}
    assert calls.count(7) == 1  # 失败后 TTL 内不重试
    # 昵称是装饰性增强：get 绝不抛异常，由字母代称兜底


@pytest.mark.asyncio
async def test_directory_failure_keeps_previous_mapping() -> None:
    payloads: list[list[dict] | Exception] = [
        [{"user_id": 5, "card": "老卡"}],
        RuntimeError("refresh down"),
    ]

    async def fetch(group_id: int) -> list[dict]:
        item = payloads.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    clock = _FakeClock()
    directory = NicknameDirectory(fetch, ttl_seconds=600.0, clock=clock)

    assert await directory.get(7) == {5: "老卡"}
    clock.now += 601.0
    assert await directory.get(7) == {5: "老卡"}  # 刷新失败沿用旧值
