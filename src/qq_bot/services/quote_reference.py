"""引用回复解析（S10-HUMAN-P0 H4）：优先 OneBot reply 段自带扩展字段
（NapCat/go-cqhttp 惯例的 qq/text），退回进程内 LRU；S11-QUOTE-01 起再加
第三级——按消息 id 调 OneBot get_msg 回查（fetch 闭包由调用方注入，本模块
保持 bot 无关可测）。都不可得返回 None（调用方降级为普通文本）。标记格式
`u{id}` 只进本地 chat 存储，渲染时由 memory_prompt 替换为匿名别名，
绝不出现在日志/指标/发送文本。"""

from __future__ import annotations

import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, Sequence

_REF_TEXT_MAX = 30
_LRU_CAPACITY = 200
_LRU_TTL_SECONDS = 1800.0


class _LruLike(Protocol):
    def lookup(self, message_id: int) -> QuoteReference | None: ...


@dataclass(frozen=True)
class QuoteReference:
    ref_user_id: int
    ref_text: str


def _clean_text(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip())[:_REF_TEXT_MAX]


def parse_reply_reference(
    segments: Sequence[Any], *, self_id: int, lru: _LruLike | None
) -> QuoteReference | None:
    for segment in segments:
        if getattr(segment, "type", None) != "reply":
            continue
        data = getattr(segment, "data", {}) or {}
        qq = data.get("qq")
        try:
            ref_user_id = int(qq)
        except (TypeError, ValueError):
            ref_user_id = 0
        if ref_user_id:
            return QuoteReference(ref_user_id=ref_user_id, ref_text=_clean_text(data.get("text")))
        try:
            message_id = int(data.get("id"))
        except (TypeError, ValueError):
            continue
        if lru is None:
            return None
        return lru.lookup(message_id)
    return None


def render_quoted_raw_text(raw_text: str, ref: QuoteReference) -> str:
    return f"回复 [引用 u{ref.ref_user_id}:{ref.ref_text}]：{raw_text}"


async def resolve_reply_reference(
    segments: Sequence[Any],
    *,
    self_id: int,
    lru: _LruLike | None,
    fetch: Callable[[int], Awaitable[tuple[int, str] | None]] | None = None,
) -> QuoteReference | None:
    """三级解析（S11-QUOTE-01）：扩展字段 → LRU → fetch 回查，任一级命中
    即返回。前两级复用 parse_reply_reference；都失败且有 fetch 闭包时，取
    第一个 reply 段的消息 id 回查原文（NapCat 实际不填 qq/text 扩展字段，
    LRU 又有重启即空/30 分钟/200 条的天花板——get_msg 是唯一可靠来源）。
    fetch 失败/无结果一律 None，调用方降级为无引用，绝不拖累主流程。"""
    ref = parse_reply_reference(segments, self_id=self_id, lru=lru)
    if ref is not None or fetch is None:
        return ref
    for segment in segments:
        if getattr(segment, "type", None) != "reply":
            continue
        data = getattr(segment, "data", {}) or {}
        try:
            message_id = int(data.get("id"))
        except (TypeError, ValueError):
            continue
        fetched: tuple[int, str] | None
        try:
            fetched = await fetch(message_id)
        except Exception:
            return None
        if fetched is None:
            return None
        user_id, text = fetched
        try:
            return QuoteReference(ref_user_id=int(user_id), ref_text=_clean_text(text))
        except (TypeError, ValueError):
            return None
    return None


class QuoteReferenceLRU:
    def __init__(
        self,
        *,
        capacity: int = _LRU_CAPACITY,
        ttl_seconds: float = _LRU_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._capacity = capacity
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: OrderedDict[int, tuple[int, str, float]] = OrderedDict()

    def remember(self, message_id: int, user_id: int, text: str) -> None:
        self._entries[message_id] = (user_id, _clean_text(text), self._clock())
        self._entries.move_to_end(message_id)
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)

    def lookup(self, message_id: int) -> QuoteReference | None:
        entry = self._entries.get(message_id)
        if entry is None:
            return None
        user_id, text, seen_at = entry
        if self._clock() - seen_at >= self._ttl:
            del self._entries[message_id]
            return None
        return QuoteReference(ref_user_id=user_id, ref_text=text)


_SHARED_LRU: QuoteReferenceLRU | None = None


def shared_quote_lru() -> QuoteReferenceLRU:
    global _SHARED_LRU
    if _SHARED_LRU is None:
        _SHARED_LRU = QuoteReferenceLRU()
    return _SHARED_LRU
