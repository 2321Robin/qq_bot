"""群成员昵称目录（S11-AUTO-04）：id→群昵称内存缓存。

部署者裁决（2026-10-25）：「用户A/B」字母代称在复盘记录里分不清人、模型
也无法自然称呼群友——拉 OneBot `get_group_member_list` 用群名片（card，
退 QQ 昵称）替换 prompt 与 tune log 里的字母代称。昵称是群内公开信息；
原始 QQ 号仍不进模型 prompt（纯数字形似 QQ 号的昵称按脏数据丢弃回落字母）。

不落盘：重启后首个事件即拉一次全量名单，避免过期名片长期驻留；拉取失败
按同一 TTL 退避，期间用旧值或空映射（字母代称兜底），绝不阻塞主流程。"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

_NICK_MAX_CHARS = 20


def sanitize_nickname(raw: Any) -> str | None:
    """昵称清洗：压缩空白、限长；空串与纯数字（形似 QQ 号）丢弃——
    渲染层绝不出现原始 QQ 号，同一红线也挡住数字形昵称。"""
    if not isinstance(raw, str):
        return None
    cleaned = " ".join(raw.split())
    if not cleaned or cleaned.isdigit():
        return None
    return cleaned[:_NICK_MAX_CHARS]


class NicknameDirectory:
    """按群缓存成员昵称。``fetch_members(group_id)`` 由插件注入（OneBot
    调用），返回 ``[{user_id, card, nickname}, ...]`` 形态的成员列表。"""

    def __init__(
        self,
        fetch_members: Callable[[int], Awaitable[Any]],
        *,
        ttl_seconds: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch_members
        self._ttl = ttl_seconds
        self._clock = clock
        self._cache: dict[int, dict[int, str]] = {}
        self._stale_at: dict[int, float] = {}

    async def get(self, group_id: int) -> dict[int, str]:
        """返回该群 id→昵称映射；过期/缺失时刷新，失败沿用旧值（可能为空）。
        本方法不抛异常——昵称是装饰性增强，任何失败都回落字母代称。"""
        now = self._clock()
        if group_id in self._cache and now < self._stale_at.get(group_id, 0.0):
            return self._cache[group_id]
        try:
            members = await self._fetch(group_id)
            mapping: dict[int, str] = {}
            for member in members or ():
                if not isinstance(member, dict):
                    continue
                nick = sanitize_nickname(member.get("card") or member.get("nickname"))
                if nick is not None:
                    mapping[int(member["user_id"])] = nick
            self._cache[group_id] = mapping
        except Exception:
            self._cache.setdefault(group_id, {})
        self._stale_at[group_id] = now + self._ttl
        return self._cache[group_id]
