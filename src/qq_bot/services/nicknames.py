"""群成员昵称目录（S11-AUTO-04）：id→群昵称按需缓存，落盘复用。

部署者裁决（2026-10-25）：「用户A/B」字母代称复盘分不清人、模型无法自然
称呼群友；拉取策略按部署者要求**只在遇到未知 id 时拉取并保存**——不做
周期刷新（群名片改名会滞后，部署者接受；删缓存文件即全量刷新）。

- 存储：data/group_nicknames.json（gitignored，重启复用，冷启动零拉取）
- 防抖：拉取后 min_refetch_seconds 内不再拉（默认 300s）——失败退避与
  已离群者（名单里永远查不到的 id）都靠它避免每条消息触发请求
- 群名片（card）优先退 QQ 昵称；清洗：压空白、限长 20、空串与纯数字
  （形似 QQ 号）丢弃——渲染层绝不出现原始 QQ 号
- 任何失败都不抛异常：昵称是装饰性增强，缺谁谁回落字母代称"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any

_NICK_MAX_CHARS = 20
DEFAULT_STORE_PATH = Path("data/group_nicknames.json")


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
        store_path: Path | None = None,
        min_refetch_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch_members
        self._min_refetch = min_refetch_seconds
        self._clock = clock
        self._store_path = Path(store_path) if store_path is not None else DEFAULT_STORE_PATH
        self._cache: dict[int, dict[int, str]] = self._load()
        self._fetched_at: dict[int, float] = {}

    def _load(self) -> dict[int, dict[int, str]]:
        """启动时读取落盘缓存（坏文件当空缓存，下次拉取重建）。"""
        try:
            payload = json.loads(self._store_path.read_text(encoding="utf-8"))
            out: dict[int, dict[int, str]] = {}
            for gid, members in payload.items():
                mapping: dict[int, str] = {}
                for uid, nick in members.items():
                    cleaned = sanitize_nickname(nick)
                    if cleaned is not None:
                        mapping[int(uid)] = cleaned
                out[int(gid)] = mapping
            return out
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def _save(self) -> None:
        """落盘；失败只影响重启后的首次拉取，不影响主流程。"""
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                str(gid): {str(uid): nick for uid, nick in mapping.items()}
                for gid, mapping in self._cache.items()
            }
            self._store_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except Exception:
            pass

    async def get(self, group_id: int, user_ids: Iterable[int]) -> dict[int, str]:
        """返回该群 id→昵称映射。仅当所需 id 里有缓存未知的、且距上次拉取
        超过 min_refetch_seconds 时才真正拉取一次并落盘；其余情况纯内存。"""
        needed = {int(u) for u in user_ids}
        cached = self._cache.setdefault(group_id, {})
        now = self._clock()
        last = self._fetched_at.get(group_id)
        if needed - cached.keys() and (last is None or now - last >= self._min_refetch):
            # 先记时间再等网络：asyncio 单线程内check-and-set 无 await，并发
            # 调用不会重复触发拉取
            self._fetched_at[group_id] = now
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
                self._save()
            except Exception:
                pass  # 沿用旧缓存；防抖窗口内不会重试
        return dict(self._cache.get(group_id, {}))
