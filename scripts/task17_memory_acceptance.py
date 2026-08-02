"""Task 17 acceptance: memory deletion lifecycle on a TEMPORARY database.

Sequence: 保存 → 查看 → 单条删除 → 全部删除 → 关闭 → 摘要失效 → 重启后仍删除.
Uses a temp SQLite file; never touches the production database. The summary
gateway is a scripted fake (no real AI calls).
"""

import asyncio
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from qq_bot.config import BotSettings
from qq_bot.services.chat_memory import ChatMemoryRepository
from qq_bot.services.layered_memory import LayeredMemoryService

_UTC = timezone.utc
_NOW = datetime(2026, 5, 11, 12, 0, tzinfo=_UTC)

_SUMMARY_JSON = json.dumps(
    {
        "speaker_range": "2001",
        "time_range": "2026-05-11T10:00Z~11:00Z",
        "topics": ["宠物养成"],
        "decisions": ["优先练火系"],
        "explicit_preferences": [],
        "open_questions": [],
    },
    ensure_ascii=False,
)


class _FakeGateway:
    def __init__(self) -> None:
        self.calls = 0

    async def request_model_turn(self, **kwargs):
        self.calls += 1
        return type("Response", (), {"text": _SUMMARY_JSON})()


class _Alloc:
    source = "recent_messages"
    tokens = 99
    estimated = True
    reason = "quota_exceeded"
    dropped_units = 1


class _DroppingBudget:
    """Plan whose recent_messages allocation always overflows, so the
    service generates a structured summary (S2-MEM-04)."""

    class _Plan:
        allocations = (_Alloc(),)

    def allocate(self, **kwargs):
        return self._Plan()


async def main() -> None:
    db = Path(tempfile.mkdtemp()) / "acceptance.sqlite3"
    repo = ChatMemoryRepository(db, retention_days=30)
    await repo.open()
    settings = BotSettings(memory_summary_enabled=True)
    service = LayeredMemoryService(repo, settings, gateway=_FakeGateway(), budget=_DroppingBudget())

    # 保存: explicit command writes the preference
    pid = await service.save_preference(
        group_id=1001, user_id=2001, content="用户喜欢暗影格斗和水系精灵"
    )
    assert pid is not None
    # 查看
    prefs, enabled = await service.list_preferences(group_id=1001, user_id=2001)
    assert enabled and any(p.preference == "用户喜欢暗影格斗和水系精灵" for p in prefs)
    # messages + summary generation
    await repo.add_message(1001, 2001, "ai 我最近在练暗影格斗", created_at=_NOW, now=_NOW)
    await repo.add_message(
        1001,
        2001,
        "ai 我喜欢水系精灵",
        created_at=_NOW + timedelta(minutes=1),
        now=_NOW + timedelta(minutes=1),
    )
    recent = await service.recent_layer(group_id=1001, limit=50, now=_NOW + timedelta(minutes=2))
    assert len(recent) == 2
    summaries = await service.summary_layer(group_id=1001, now=_NOW + timedelta(minutes=2))
    assert len(summaries) == 1, "summary generated from overflowing history"
    # 单条删除
    assert await service.delete_preference(group_id=1001, user_id=2001, preference_id=pid) is True
    prefs, _ = await service.list_preferences(group_id=1001, user_id=2001)
    assert prefs == []
    # 全部删除 + 关闭: own messages, AI replies, preferences and linked
    # summaries are all removed (S2-MEM-07)
    await service.save_preference(group_id=1001, user_id=2001, content="再次保存的偏好")
    await service.delete_all(group_id=1001, user_id=2001)
    await service.close_preferences(group_id=1001, user_id=2001)
    prefs, enabled = await service.list_preferences(group_id=1001, user_id=2001)
    assert prefs == [] and not enabled
    recent = await service.recent_layer(group_id=1001, limit=50, now=_NOW + timedelta(minutes=2))
    assert recent == [], "deleted content must not reach the prompt (incl. summaries)"
    # 重启后仍删除
    await repo.close()
    repo2 = ChatMemoryRepository(db, retention_days=30)
    await repo2.open()
    service2 = LayeredMemoryService(
        repo2, settings, gateway=_FakeGateway(), budget=_DroppingBudget()
    )
    prefs, enabled = await service2.list_preferences(group_id=1001, user_id=2001)
    assert prefs == [] and not enabled
    recent2 = await service2.recent_layer(group_id=1001, limit=50, now=_NOW + timedelta(minutes=2))
    assert recent2 == []
    # other users untouched
    await repo2.add_message(1001, 3001, "ai 其他用户的消息", created_at=_NOW, now=_NOW)
    recent3 = await service2.recent_layer(group_id=1001, limit=50, now=_NOW + timedelta(minutes=3))
    assert len(recent3) == 1
    print(
        "memory deletion acceptance: PASS "
        "(save/view/single/delete-all/close/summary-invalidate/reopen)"
    )
    await repo2.close()


asyncio.run(main())
