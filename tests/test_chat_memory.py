from datetime import datetime, timedelta, timezone

from qq_bot.services.chat_memory import ChatMemoryStore


def test_add_message_initializes_database_and_reads_user_history(tmp_path) -> None:
    store = ChatMemoryStore(tmp_path / "memory.sqlite3", retention_days=3)
    message_id = store.add_message(
        group_id=1001,
        user_id=2001,
        message_text="ai 你好",
        is_ai_prompt=True,
        created_at=datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc),
    )
    store.update_ai_reply(message_id, "你好呀")
    store.add_message(
        group_id=1001,
        user_id=2002,
        message_text="别人的消息",
        created_at=datetime(2026, 5, 11, 12, 1, tzinfo=timezone.utc),
    )

    rows = store.recent_user_turns(group_id=1001, user_id=2001, limit=10)

    assert len(rows) == 1
    assert rows[0].message_text == "ai 你好"
    assert rows[0].ai_reply == "你好呀"
    assert rows[0].is_ai_prompt is True


def test_recent_group_messages_returns_newest_limited_rows_in_chronological_order(
    tmp_path,
) -> None:
    store = ChatMemoryStore(tmp_path / "memory.sqlite3", retention_days=3)
    base = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
    for index in range(5):
        store.add_message(
            group_id=1001,
            user_id=2001 + index,
            message_text=f"消息{index}",
            created_at=base + timedelta(minutes=index),
        )

    rows = store.recent_group_messages(group_id=1001, limit=3)

    assert [row.message_text for row in rows] == ["消息2", "消息3", "消息4"]


def test_search_group_messages_filters_keyword_and_user(tmp_path) -> None:
    store = ChatMemoryStore(tmp_path / "memory.sqlite3", retention_days=3)
    created_at = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
    store.add_message(1001, 2001, "洛克王国 迪莫", created_at=created_at)
    store.add_message(1001, 2002, "洛克王国 火花", created_at=created_at)
    store.add_message(1001, 2001, "别的话题", created_at=created_at)

    rows = store.search_messages(group_id=1001, keyword="洛克", user_id=2001, limit=10)

    assert [row.message_text for row in rows] == ["洛克王国 迪莫"]


def test_cleanup_removes_records_older_than_retention(tmp_path) -> None:
    store = ChatMemoryStore(tmp_path / "memory.sqlite3", retention_days=3)
    now = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
    store.add_message(1001, 2001, "旧消息", created_at=now - timedelta(days=4), now=now)
    store.add_message(1001, 2001, "新消息", created_at=now - timedelta(days=1), now=now)

    rows = store.recent_group_messages(group_id=1001, limit=10, now=now)

    assert [row.message_text for row in rows] == ["新消息"]
