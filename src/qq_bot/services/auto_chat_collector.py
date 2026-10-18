"""收集窗口（S10-HUMAN-P0 H1）：非旁路消息先合并"同人连发/短时突发"，
静默到期或总窗封顶后整批交付，防抢答。进程内存态，重启清零。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from qq_bot.services.auto_chat import BatchItem


@dataclass(frozen=True)
class CollectedBatch:
    items: tuple[BatchItem, ...]
    reason: str  # "merged" | "timeout"


class AutoChatCollector:
    """每群最多一个挂起批次；新消息重置静默计时，总窗自首条起封顶。
    交付前逐条 `prefilter`（`static_prefilter` 同签名），剔除失败项并回调
    `metric("prefilter", reason)`；全空回调 `metric("collect", "empty")`
    且不交付。交付异常吞掉并回调 `metric("collect", "error")`（护栏优先，
    不影响消息主路径）。"""

    def __init__(
        self,
        *,
        quiet_seconds: float,
        max_seconds: float,
        prefilter: Callable[[str], str],
        metric: Callable[[str, str], None],
        deliver: Callable[[int, CollectedBatch], Awaitable[None]],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._quiet = quiet_seconds
        self._max = max_seconds
        self._prefilter = prefilter
        self._metric = metric
        self._deliver = deliver
        self._sleep = sleep
        self._clock = clock
        self._pending: dict[int, list[BatchItem]] = {}
        self._tasks: dict[int, asyncio.Task] = {}

    def pending_count(self) -> int:
        """挂起批次数（= 有挂起批次的群数，与 gauge 语义一致）。"""
        return len(self._pending)

    async def submit(self, group_id: int, item: BatchItem) -> None:
        self._pending.setdefault(group_id, []).append(item)
        if group_id not in self._tasks:
            self._tasks[group_id] = asyncio.ensure_future(self._run(group_id))

    async def _run(self, group_id: int) -> None:
        start = self._clock()
        reason = "merged"
        while True:
            before = len(self._pending.get(group_id, []))
            await self._sleep(self._quiet)
            if self._clock() - start >= self._max:
                reason = "timeout"
                break
            if len(self._pending.get(group_id, [])) == before:
                break  # 静默期内没有新消息 → 话尾已到
        items = self._pending.pop(group_id, [])
        self._tasks.pop(group_id, None)
        kept: list[BatchItem] = []
        for item in items:
            verdict = self._prefilter(item.raw_text)
            if verdict == "pass":
                kept.append(item)
            else:
                self._metric("prefilter", verdict)
        if not kept:
            self._metric("collect", "empty")
            return
        self._metric("collect", reason)
        try:
            await self._deliver(group_id, CollectedBatch(items=tuple(kept), reason=reason))
        except Exception:
            self._metric("collect", "error")
