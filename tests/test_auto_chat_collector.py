import asyncio
from datetime import UTC, datetime

import pytest

from qq_bot.services.auto_chat import BatchItem
from qq_bot.services.auto_chat_collector import AutoChatCollector, CollectedBatch


def _item(text: str, sender: int = 3001) -> BatchItem:
    return BatchItem(raw_text=text, sender_user_id=sender, created_at=datetime.now(UTC))


def _pass(text: str) -> str:
    return "pass"


@pytest.mark.asyncio
async def test_quiet_window_merges_burst_and_delivers_once():
    delivered: list[CollectedBatch] = []

    async def deliver(group_id: int, batch: CollectedBatch) -> None:
        delivered.append(batch)

    c = AutoChatCollector(
        quiet_seconds=0.05,
        max_seconds=1.0,
        prefilter=_pass,
        metric=lambda stage, result: None,
        deliver=deliver,
    )
    await c.submit(1, _item("第一句"))
    await c.submit(1, _item("第二句"))
    await asyncio.sleep(0.3)
    assert len(delivered) == 1
    assert [it.raw_text for it in delivered[0].items] == ["第一句", "第二句"]
    assert delivered[0].reason == "merged"
    assert c.pending_count() == 0


@pytest.mark.asyncio
async def test_max_window_forces_delivery():
    delivered: list[CollectedBatch] = []

    async def deliver(group_id: int, batch: CollectedBatch) -> None:
        delivered.append(batch)

    c = AutoChatCollector(
        # max=0.3（而非 0.2）：Windows select 精度 ~15.6ms，sleep 每次过冲一个
        # tick——max=0.2 时总窗在第 3 次唤醒（~0.285s）触发，早于第 4 次
        # submit（~0.302s）落地，偶发拆出第二个批次。0.3 让封顶落在第 4 次
        # 唤醒（~0.38s），晚于最后一次 submit；第 3 次唤醒（~0.285s）仍低于
        # 上限不提前交付。Linux 精细计时下同样成立（0.24 < 0.3 < 0.32）。
        quiet_seconds=0.08,
        max_seconds=0.3,
        prefilter=_pass,
        metric=lambda stage, result: None,
        deliver=deliver,
    )
    await c.submit(1, _item("第一句"))
    # 持续追加新消息：静默一直被重置，总窗封顶交付
    for _ in range(4):
        await asyncio.sleep(0.06)
        await c.submit(1, _item("再补一句"))
    await asyncio.sleep(0.3)
    assert len(delivered) == 1
    assert delivered[0].reason == "timeout"


@pytest.mark.asyncio
async def test_prefilter_drops_items_and_empty_batch_skips_delivery():
    delivered: list[CollectedBatch] = []
    drops: list[str] = []

    async def deliver(group_id: int, batch: CollectedBatch) -> None:
        delivered.append(batch)

    def prefilter(text: str) -> str:
        return "too_short"

    c = AutoChatCollector(
        quiet_seconds=0.03,
        max_seconds=1.0,
        prefilter=prefilter,
        metric=lambda stage, result: (
            drops.append(f"{stage}/{result}") if stage == "prefilter" else None
        ),
        deliver=deliver,
    )
    await c.submit(1, _item("嗯"))
    await asyncio.sleep(0.15)
    assert delivered == []
    assert any("prefilter" in d for d in drops)


@pytest.mark.asyncio
async def test_per_group_isolation_and_pending_count():
    batches: list[CollectedBatch] = []

    async def deliver(group_id: int, batch: CollectedBatch) -> None:
        batches.append(batch)

    c = AutoChatCollector(
        quiet_seconds=0.05,
        max_seconds=1.0,
        prefilter=_pass,
        metric=lambda stage, result: None,
        deliver=deliver,
    )
    await c.submit(1, _item("A群"))
    await c.submit(2, _item("B群"))
    assert c.pending_count() == 2
    await asyncio.sleep(0.3)
    assert {b.items[0].raw_text for b in batches} == {"A群", "B群"}
