"""Async chat memory repository tests (S1-DB-01..05, S1-GATE-03)."""

from __future__ import annotations

import asyncio

import pytest
from datetime import datetime, timedelta, timezone

from qq_bot.services.chat_memory import ChatMemoryRepository, RepositoryClosedError

_UTC = timezone.utc


@pytest.fixture
async def repository(tmp_path) -> ChatMemoryRepository:
    repo = ChatMemoryRepository(tmp_path / "memory.sqlite3", retention_days=3)
    await repo.open()
    yield repo
    await repo.close()


@pytest.mark.asyncio
async def test_open_runs_migrations_and_enables_foreign_keys(tmp_path) -> None:
    repo = ChatMemoryRepository(tmp_path / "memory.sqlite3", retention_days=3)
    assert not repo.is_open
    await repo.open()
    try:
        assert repo.is_open
        assert repo.journal_mode == "wal"
        cursor = await repo._connection.execute(  # noqa: SLF001 - test-only peek
            "SELECT version FROM schema_migrations ORDER BY version"
        )
        versions = [int(row[0]) for row in await cursor.fetchall()]
        assert versions == [1, 2]
    finally:
        await repo.close()
    assert not repo.is_open


@pytest.mark.asyncio
async def test_open_failure_closes_connection(tmp_path) -> None:
    # a directory in place of the db file makes open fail
    blocker = tmp_path / "blocker.sqlite3"
    blocker.mkdir()
    broken = ChatMemoryRepository(blocker, retention_days=3)
    with pytest.raises(Exception):
        await broken.open()
    assert not broken.is_open
    await broken.close()  # must be safe


@pytest.mark.asyncio
async def test_add_message_initializes_database_and_reads_user_history(
    repository,
) -> None:
    now = datetime(2026, 5, 11, 12, 2, tzinfo=_UTC)
    message_id = await repository.add_message(
        group_id=1001,
        user_id=2001,
        message_text="ai 你好",
        is_ai_prompt=True,
        created_at=datetime(2026, 5, 11, 12, 0, tzinfo=_UTC),
        now=now,
    )
    await repository.update_ai_reply(message_id, "你好呀")
    await repository.add_message(
        group_id=1001,
        user_id=2002,
        message_text="别人的消息",
        created_at=datetime(2026, 5, 11, 12, 1, tzinfo=_UTC),
        now=now,
    )

    rows = await repository.recent_user_turns(group_id=1001, user_id=2001, limit=10, now=now)

    assert len(rows) == 1
    assert rows[0].message_text == "ai 你好"
    assert rows[0].ai_reply == "你好呀"
    assert rows[0].is_ai_prompt is True


@pytest.mark.asyncio
async def test_recent_user_turns_excludes_non_ai_group_messages(repository) -> None:
    base = datetime(2026, 5, 11, 12, 0, tzinfo=_UTC)
    now = base + timedelta(minutes=3)
    await repository.add_message(
        group_id=1001,
        user_id=2001,
        message_text="普通聊天",
        created_at=base,
        now=now,
    )
    await repository.add_message(
        group_id=1001,
        user_id=2001,
        message_text="ai 之前的问题",
        is_ai_prompt=True,
        created_at=base + timedelta(minutes=1),
        now=now,
    )

    rows = await repository.recent_user_turns(group_id=1001, user_id=2001, limit=10, now=now)

    assert [row.message_text for row in rows] == ["ai 之前的问题"]


@pytest.mark.asyncio
async def test_recent_group_messages_returns_newest_limited_rows_in_chronological_order(
    repository,
) -> None:
    base = datetime(2026, 5, 11, 12, 0, tzinfo=_UTC)
    now = base + timedelta(minutes=5)
    for index in range(5):
        await repository.add_message(
            group_id=1001,
            user_id=2001 + index,
            message_text=f"消息{index}",
            created_at=base + timedelta(minutes=index),
            now=now,
        )

    rows = await repository.recent_group_messages(group_id=1001, limit=3, now=now)

    assert [row.message_text for row in rows] == ["消息2", "消息3", "消息4"]


@pytest.mark.asyncio
async def test_search_group_messages_filters_keyword_and_user(repository) -> None:
    created_at = datetime(2026, 5, 11, 12, 0, tzinfo=_UTC)
    await repository.add_message(1001, 2001, "洛克王国 迪莫", created_at=created_at, now=created_at)
    await repository.add_message(1001, 2002, "洛克王国 火花", created_at=created_at, now=created_at)
    await repository.add_message(1001, 2001, "别的话题", created_at=created_at, now=created_at)

    rows = await repository.search_messages(
        group_id=1001,
        keyword="洛克",
        user_id=2001,
        limit=10,
        now=created_at,
    )

    assert [row.message_text for row in rows] == ["洛克王国 迪莫"]


@pytest.mark.asyncio
async def test_search_treats_percent_and_underscore_as_literals(repository) -> None:
    created_at = datetime(2026, 5, 11, 12, 0, tzinfo=_UTC)
    await repository.add_message(1001, 2001, "百分比 50%", created_at=created_at, now=created_at)
    await repository.add_message(1001, 2001, "下划线 a_b", created_at=created_at, now=created_at)
    await repository.add_message(1001, 2001, "没有特殊字符", created_at=created_at, now=created_at)

    percent_rows = await repository.search_messages(
        group_id=1001, keyword="%", limit=10, now=created_at
    )
    underscore_rows = await repository.search_messages(
        group_id=1001, keyword="_", limit=10, now=created_at
    )

    assert [row.message_text for row in percent_rows] == ["百分比 50%"]
    assert [row.message_text for row in underscore_rows] == ["下划线 a_b"]


@pytest.mark.asyncio
async def test_cleanup_removes_records_older_than_retention(repository) -> None:
    now = datetime(2026, 5, 11, 12, 0, tzinfo=_UTC)
    await repository.add_message(1001, 2001, "旧消息", created_at=now - timedelta(days=4), now=now)
    await repository.add_message(1001, 2001, "新消息", created_at=now - timedelta(days=1), now=now)

    rows = await repository.recent_group_messages(group_id=1001, limit=10, now=now)

    assert [row.message_text for row in rows] == ["新消息"]


@pytest.mark.asyncio
async def test_closed_repository_rejects_operations(repository) -> None:
    await repository.close()
    with pytest.raises(RepositoryClosedError):
        await repository.add_message(1001, 2001, "x", now=datetime(2026, 5, 11, tzinfo=_UTC))
    with pytest.raises(RepositoryClosedError):
        await repository.recent_group_messages(group_id=1001, limit=5)
    with pytest.raises(RepositoryClosedError):
        await repository.search_messages(group_id=1001, limit=5)
    with pytest.raises(RepositoryClosedError):
        await repository.update_ai_reply(1, "reply")
    await repository.close()  # double close is a no-op


# ---------------------------------------------------------------------------
# Layered memory (S2-MEM-01..07, S2-MEM-09..10)
# ---------------------------------------------------------------------------


async def _seed_messages(repository: ChatMemoryRepository, *, now: datetime) -> tuple[int, int]:
    """Two user-2001 messages + one other-user message; returns their ids."""
    first = await repository.add_message(
        1001, 2001, "讨论主题：宠物养成", created_at=now - timedelta(hours=2), now=now
    )
    second = await repository.add_message(
        1001, 2001, "决定：优先练火系", created_at=now - timedelta(hours=1), now=now
    )
    await repository.add_message(
        1001, 2002, "其他人的消息", created_at=now - timedelta(hours=3), now=now
    )
    return first, second


@pytest.mark.asyncio
async def test_add_summary_expiry_is_earliest_source_deadline(repository) -> None:
    now = datetime(2026, 5, 11, 12, 0, tzinfo=_UTC)
    first, second = await _seed_messages(repository, now=now)

    summary_id = await repository.add_summary(
        1001, "主题：宠物养成；决定：优先练火系", [first, second], now=now
    )

    summaries = await repository.get_summaries(group_id=1001, now=now)
    assert [s.id for s in summaries] == [summary_id]
    # expires_at = oldest source (2h ago) + 3 days retention, never later
    assert (
        summaries[0].expires_at
        == (now - timedelta(hours=2) + timedelta(days=3)).astimezone(_UTC).isoformat()
    )


@pytest.mark.asyncio
async def test_summary_without_sources_is_rejected(repository) -> None:
    now = datetime(2026, 5, 11, 12, 0, tzinfo=_UTC)
    with pytest.raises(ValueError, match="at least one source"):
        await repository.add_summary(1001, "无源摘要", [], now=now)
    with pytest.raises(ValueError, match="existing chat messages"):
        await repository.add_summary(1001, "幽灵源摘要", [999999], now=now)


@pytest.mark.asyncio
async def test_expired_summaries_never_reach_prompt(repository) -> None:
    now = datetime(2026, 5, 11, 12, 0, tzinfo=_UTC)
    first, second = await _seed_messages(repository, now=now)
    await repository.add_summary(1001, "旧摘要", [first, second], now=now)

    late = now + timedelta(days=4)
    summaries = await repository.get_summaries(group_id=1001, now=late)
    assert summaries == []


@pytest.mark.asyncio
async def test_summaries_are_group_scoped(repository) -> None:
    now = datetime(2026, 5, 11, 12, 0, tzinfo=_UTC)
    first, second = await _seed_messages(repository, now=now)
    await repository.add_summary(1001, "群 1001 摘要", [first, second], now=now)

    assert await repository.get_summaries(group_id=9999, now=now) == []


@pytest.mark.asyncio
async def test_delete_user_data_invalidates_summaries_and_other_user_safe(
    repository,
) -> None:
    now = datetime(2026, 5, 11, 12, 0, tzinfo=_UTC)
    first, second = await _seed_messages(repository, now=now)
    other_id = (await repository.recent_group_messages(group_id=1001, limit=10, now=now))[
        0
    ].id  # oldest row: the other user's message (now - 3h)
    shared = await repository.add_summary(1001, "混合源摘要", [first, other_id], now=now)
    own = await repository.add_summary(1001, "仅本人源摘要", [second], now=now)

    affected = await repository.delete_user_data(group_id=1001, user_id=2001)

    assert set(affected) == {first, second}
    # summaries touching any deleted source are gone (S2-MEM-03)
    assert [s.id for s in await repository.get_summaries(group_id=1001, now=now)] == []
    # the other user's message survives
    rows = await repository.recent_group_messages(group_id=1001, limit=10, now=now)
    assert [r.id for r in rows] == [other_id]
    assert shared != own  # both summaries really existed before deletion


@pytest.mark.asyncio
async def test_preference_crud_and_ownership(repository) -> None:
    now = datetime(2026, 5, 11, 12, 0, tzinfo=_UTC)
    assert await repository.preferences_enabled(group_id=1001, user_id=2001) is True

    first = await repository.save_preference(
        group_id=1001, user_id=2001, preference="喜欢火系宠物", now=now
    )
    second = await repository.save_preference(
        group_id=1001, user_id=2001, preference="不喜欢自动回复", now=now + timedelta(minutes=1)
    )
    await repository.save_preference(
        group_id=1001, user_id=2002, preference="别人家的偏好", now=now
    )

    own = await repository.get_preferences(group_id=1001, user_id=2001)
    assert [p.preference for p in own] == ["不喜欢自动回复", "喜欢火系宠物"]
    other = await repository.get_preferences(group_id=1001, user_id=2002)
    assert [p.preference for p in other] == ["别人家的偏好"]

    # deleting a preference that is not the owner's is a no-op
    assert (
        await repository.delete_preference(group_id=1001, user_id=2001, preference_id=other[0].id)
        is False
    )
    assert (
        await repository.delete_preference(group_id=1001, user_id=2001, preference_id=first) is True
    )
    assert [p.id for p in await repository.get_preferences(group_id=1001, user_id=2001)] == [second]


@pytest.mark.asyncio
async def test_close_preferences_blocks_and_save_reopens(repository) -> None:
    now = datetime(2026, 5, 11, 12, 0, tzinfo=_UTC)
    await repository.save_preference(
        group_id=1001, user_id=2001, preference="待清除的偏好", now=now
    )

    await repository.close_preferences(group_id=1001, user_id=2001, now=now)
    assert await repository.get_preferences(group_id=1001, user_id=2001) == []
    assert await repository.preferences_enabled(group_id=1001, user_id=2001) is False

    # an explicit save is a fresh opt-in (S2-MEM-05)
    await repository.save_preference(
        group_id=1001, user_id=2001, preference="新的显式偏好", now=now + timedelta(hours=1)
    )
    assert await repository.preferences_enabled(group_id=1001, user_id=2001) is True
    assert [
        p.preference for p in await repository.get_preferences(group_id=1001, user_id=2001)
    ] == ["新的显式偏好"]


@pytest.mark.asyncio
async def test_delete_all_preferences(repository) -> None:
    now = datetime(2026, 5, 11, 12, 0, tzinfo=_UTC)
    await repository.save_preference(group_id=1001, user_id=2001, preference="a", now=now)
    await repository.save_preference(group_id=1001, user_id=2001, preference="b", now=now)
    await repository.delete_all_preferences(group_id=1001, user_id=2001)

    assert await repository.get_preferences(group_id=1001, user_id=2001) == []
    # delete-all does not close: a later save still works as normal opt-in
    assert await repository.preferences_enabled(group_id=1001, user_id=2001) is True


@pytest.mark.asyncio
async def test_delete_user_data_removes_preferences_and_other_group_untouched(
    repository,
) -> None:
    now = datetime(2026, 5, 11, 12, 0, tzinfo=_UTC)
    await repository.add_message(1001, 2001, "本人消息", created_at=now, now=now)
    await repository.save_preference(group_id=1001, user_id=2001, preference="本人的偏好", now=now)
    await repository.save_preference(group_id=9999, user_id=2001, preference="另一群偏好", now=now)
    await repository.close_preferences(group_id=1001, user_id=2001, now=now)

    affected = await repository.delete_user_data(group_id=1001, user_id=2001)

    assert len(affected) == 1
    assert await repository.get_preferences(group_id=1001, user_id=2001) == []
    # state row is removed too: fresh start after full deletion
    assert await repository.preferences_enabled(group_id=1001, user_id=2001) is True
    # other group's rows for the same user survive (group scope, S2-MEM-01)
    assert await repository.get_preferences(group_id=9999, user_id=2001) != []


@pytest.mark.asyncio
async def test_concurrent_writes_do_not_corrupt(repository) -> None:
    now = datetime(2026, 5, 11, 12, 0, tzinfo=_UTC)

    async def write(index: int) -> None:
        await repository.add_message(1001, 2001, f"并发{index}", created_at=now, now=now)

    await asyncio.gather(*(write(index) for index in range(20)))

    rows = await repository.recent_group_messages(group_id=1001, limit=20, now=now)
    assert len(rows) == 20
    assert {row.message_text for row in rows} == {f"并发{index}" for index in range(20)}


@pytest.mark.asyncio
async def test_event_loop_remains_schedulable_during_operations(repository) -> None:
    """A blocking SQLite implementation would starve concurrent tasks."""
    progress = {"ticks": 0}

    async def ticker() -> None:
        for _ in range(30):
            progress["ticks"] += 1
            await asyncio.sleep(0)

    task = asyncio.create_task(ticker())
    now = datetime(2026, 5, 11, 12, 0, tzinfo=_UTC)
    for index in range(30):
        await repository.add_message(1001, 2001, f"m{index}", created_at=now, now=now)
    await task
    assert progress["ticks"] > 0
