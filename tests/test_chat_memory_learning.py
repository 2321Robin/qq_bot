"""Expression learning + jargon vocabulary repository tests (S10-HUMAN-P1-01)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from qq_bot.services.chat_memory import (
    ChatMemoryRepository,
    ExpressionRow,
    JargonRow,
    RepositoryClosedError,
)
from qq_bot.services.migrations import MIGRATIONS, apply_migrations


def _repo(tmp_path) -> ChatMemoryRepository:
    return ChatMemoryRepository(tmp_path / "learning.sqlite3", retention_days=30)


async def _add_message(repo, group_id, text, *, minutes_ago=0, user_id=2001):
    await repo.add_message(
        group_id=group_id,
        user_id=user_id,
        message_text=text,
        created_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
    )


async def test_messages_since_filters_by_window_and_orders(tmp_path):
    repo = _repo(tmp_path)
    await repo.open()
    try:
        await _add_message(repo, 1001, "旧消息", minutes_ago=60 * 24 * 8)  # 8 天前
        await _add_message(repo, 1001, "新消息一", minutes_ago=10)
        await _add_message(repo, 1001, "新消息二", minutes_ago=5)
        rows = await repo.messages_since(group_id=1001, days=7)
        assert [r.message_text for r in rows] == ["新消息一", "新消息二"]
    finally:
        await repo.close()


async def test_messages_since_is_group_scoped_and_reads_empty_ai_reply(tmp_path):
    repo = _repo(tmp_path)
    await repo.open()
    try:
        await _add_message(repo, 1001, "本群消息", minutes_ago=5)
        await _add_message(repo, 9999, "别群消息", minutes_ago=1)
        # a legacy row seeded by direct SQL (no ai_reply column) must read
        # back with ai_reply == "" through messages_since
        connection: aiosqlite.Connection = repo._connection  # noqa: SLF001 - test-only seed
        await connection.execute(
            "INSERT INTO chat_messages (group_id, user_id, message_text, created_at, "
            "is_ai_prompt) VALUES (1001, 2002, '空回复消息', ?, 0)",
            ((datetime.now(UTC) - timedelta(minutes=3)).isoformat(),),
        )
        await connection.commit()

        rows = await repo.messages_since(group_id=1001, days=7)

        assert [r.message_text for r in rows] == ["本群消息", "空回复消息"]
        assert all(r.ai_reply == "" for r in rows)
    finally:
        await repo.close()


async def test_messages_since_never_returns_bot_reply_as_message_text(tmp_path):
    """自我污染的真正不变式（S10-HUMAN-P1-FINAL 第二轮）。

    机器人说过的话只写在 ``ai_reply`` 列：它挂在人类那一行的附带上，
    从不作为 ``message_text`` 行出现。若 ``messages_since`` 把机器人回复
    当消息正文返回，学习轮的证据解析就会在人类消息里「找到」这句机器人
    话并把它学成句式——自我污染闭环就此打开。此处在真实 sqlite 上锁死
    该不变式：机器人文本只出现在 ``ai_reply``，``message_text`` 仍是人类自己的话。

    轮级回归（候选模板被拒）见 ``tests/test_expression_learner.py``
    ``test_bot_reply_text_is_never_learnable_evidence``。"""
    repo = _repo(tmp_path)
    await repo.open()
    try:
        message_id = await repo.add_message(
            group_id=1001,
            user_id=2001,
            message_text="人类自己的话",
            created_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        await repo.update_ai_reply(message_id, "机器人自己的回复")
        await repo.add_message(
            group_id=1001,
            user_id=2002,
            message_text="另一个人说的话",
            created_at=datetime.now(UTC) - timedelta(minutes=4),
        )

        rows = await repo.messages_since(group_id=1001, days=7)

        assert [r.message_text for r in rows] == ["人类自己的话", "另一个人说的话"]
        assert all("机器人自己的回复" not in r.message_text for r in rows)
        # 机器人文本确实落库了，但只在 ai_reply 列——上面的断言因此有意义
        assert [r.ai_reply for r in rows] == ["机器人自己的回复", ""]
    finally:
        await repo.close()


async def test_expression_upsert_create_then_bump(tmp_path):
    repo = _repo(tmp_path)
    await repo.open()
    try:
        first = await repo.upsert_expression(
            group_id=1001,
            template="{对象}吗",
            slot_count=1,
            evidence_row_id=1,
            evidence_digest="ab" * 8,
        )
        second = await repo.upsert_expression(
            group_id=1001,
            template="{对象}吗",
            slot_count=1,
            evidence_row_id=2,
            evidence_digest="cd" * 8,
        )
        assert (first, second) == ("created", "bumped")
        rows = await repo.list_active_expressions(group_id=1001)
        assert len(rows) == 1 and rows[0].count == 2
    finally:
        await repo.close()


async def test_sample_expressions_threshold_and_weighted(tmp_path):
    repo = _repo(tmp_path)
    await repo.open()
    try:
        for i in range(9):
            await repo.upsert_expression(
                group_id=1001,
                template=f"模板{i}",
                slot_count=0,
                evidence_row_id=None,
                evidence_digest="",
            )
        assert await repo.sample_expressions(group_id=1001, limit=5, threshold=10) == []
        await repo.upsert_expression(
            group_id=1001, template="模板9", slot_count=0, evidence_row_id=None, evidence_digest=""
        )
        sampled = await repo.sample_expressions(
            group_id=1001, limit=5, threshold=10, rng=lambda: 0.0
        )
        assert 1 <= len(sampled) <= 5
        assert len({r.template for r in sampled}) == len(sampled)  # 不放回
    finally:
        await repo.close()


async def test_sample_expressions_keeps_stable_order_with_zero_rng(tmp_path):
    """rng=0.0 gives every row key 0.0, so the fallback keeps insertion order."""
    repo = _repo(tmp_path)
    await repo.open()
    try:
        for i in range(4):
            await repo.upsert_expression(
                group_id=1001,
                template=f"模板{i}",
                slot_count=0,
                evidence_row_id=None,
                evidence_digest="",
            )
        sampled: list[ExpressionRow] = await repo.sample_expressions(
            group_id=1001, limit=2, threshold=4, rng=lambda: 0.0
        )
        assert [r.template for r in sampled] == ["模板0", "模板1"]
    finally:
        await repo.close()


async def test_touch_and_disable(tmp_path):
    repo = _repo(tmp_path)
    await repo.open()
    try:
        await repo.upsert_expression(
            group_id=1001, template="确实", slot_count=0, evidence_row_id=None, evidence_digest=""
        )
        rows = await repo.list_active_expressions(group_id=1001)
        assert await repo.touch_expressions([rows[0].id]) == 1
        assert (await repo.list_active_expressions(group_id=1001))[0].count == 2
        assert await repo.disable_expression(expression_id=rows[0].id) is True
        assert await repo.list_active_expressions(group_id=1001) == []
    finally:
        await repo.close()


async def test_touch_empty_selection_and_double_disable(tmp_path):
    repo = _repo(tmp_path)
    await repo.open()
    try:
        assert await repo.touch_expressions([]) == 0
        await repo.upsert_expression(
            group_id=1001, template="双禁", slot_count=0, evidence_row_id=None, evidence_digest=""
        )
        row = (await repo.list_active_expressions(group_id=1001))[0]
        assert await repo.disable_expression(expression_id=row.id) is True
        # already disabled: the second disable is a no-op returning False
        assert await repo.disable_expression(expression_id=row.id) is False
    finally:
        await repo.close()


async def test_jargon_new_hit_confirm_manual_priority(tmp_path):
    repo = _repo(tmp_path)
    await repo.open()
    try:
        assert await repo.upsert_jargon(group_id=1001, term="咸鱼翻车") == "new"
        assert await repo.upsert_jargon(group_id=1001, term="咸鱼翻车") == "hit"
        row = await repo.get_jargon(group_id=1001, term="咸鱼翻车")
        assert row is not None and row.hits == 2 and row.status == "unconfirmed"
        assert (
            await repo.confirm_jargon(
                group_id=1001, term="咸鱼翻车", definition="翻车了", manual=False
            )
            is True
        )
        # 人工修正覆盖并置 manual
        assert (
            await repo.override_jargon(group_id=1001, term="咸鱼翻车", definition="彻底翻车")
            is True
        )
        final = await repo.get_jargon(group_id=1001, term="咸鱼翻车")
        assert final.definition == "彻底翻车" and final.manual == 1
        assert await repo.disable_jargon(group_id=1001, term="咸鱼翻车") is True
        assert await repo.list_jargon(group_id=1001, status="confirmed") == []
    finally:
        await repo.close()


async def test_upsert_jargon_disabled_stays_disabled(tmp_path):
    """A disabled term keeps absorbing hits without incrementing or reviving."""
    repo = _repo(tmp_path)
    await repo.open()
    try:
        assert await repo.upsert_jargon(group_id=1001, term="过气梗") == "new"
        assert await repo.disable_jargon(group_id=1001, term="过气梗") is True
        assert await repo.upsert_jargon(group_id=1001, term="过气梗") == "hit"
        row = await repo.get_jargon(group_id=1001, term="过气梗")
        assert row is not None
        assert row.status == "disabled" and row.hits == 1
    finally:
        await repo.close()


async def test_confirm_jargon_never_overrides_manual_entries(tmp_path):
    repo = _repo(tmp_path)
    await repo.open()
    try:
        await repo.upsert_jargon(group_id=1001, term="人工词")
        assert (
            await repo.override_jargon(group_id=1001, term="人工词", definition="人工定义") is True
        )
        # the automated confirm must not touch a manual entry
        assert (
            await repo.confirm_jargon(
                group_id=1001, term="人工词", definition="自动定义", manual=False
            )
            is False
        )
        row = await repo.get_jargon(group_id=1001, term="人工词")
        assert row is not None
        assert row.definition == "人工定义" and row.manual == 1 and row.status == "confirmed"
        # a non-manual row confirms normally and returns True
        await repo.upsert_jargon(group_id=1001, term="普通词")
        assert (
            await repo.confirm_jargon(
                group_id=1001, term="普通词", definition="自动定义", manual=False
            )
            is True
        )
    finally:
        await repo.close()


async def test_list_jargon_orders_by_hits_desc(tmp_path):
    repo = _repo(tmp_path)
    await repo.open()
    try:
        for term in ["冷词", "热词", "温词"]:
            await repo.upsert_jargon(group_id=1001, term=term)
        await repo.upsert_jargon(group_id=1001, term="热词")
        await repo.upsert_jargon(group_id=1001, term="热词")
        await repo.upsert_jargon(group_id=1001, term="温词")

        rows: list[JargonRow] = await repo.list_jargon(group_id=1001, status="unconfirmed")
        assert [r.term for r in rows] == ["热词", "温词", "冷词"]
        assert [r.hits for r in rows] == [3, 2, 1]
        # other groups are invisible
        assert await repo.list_jargon(group_id=9999, status="unconfirmed") == []
        assert await repo.get_jargon(group_id=9999, term="热词") is None
    finally:
        await repo.close()


async def test_distinct_group_ids_filters_window_and_orders(tmp_path):
    repo = _repo(tmp_path)
    await repo.open()
    try:
        await _add_message(repo, 2002, "另一群消息", minutes_ago=30, user_id=3001)
        await _add_message(repo, 1001, "本群消息一", minutes_ago=90, user_id=3001)
        await _add_message(repo, 1001, "本群消息二", minutes_ago=10, user_id=3002)
        await _add_message(repo, 3003, "太旧的消息", minutes_ago=60 * 24 * 20, user_id=3001)

        ids = await repo.distinct_group_ids(days=7)
        assert ids == [1001, 2002]
    finally:
        await repo.close()


async def test_person_observation_upsert_overwrites_per_group_user(tmp_path):
    repo = _repo(tmp_path)
    await repo.open()
    try:
        await repo.upsert_person_observation(
            group_id=1001, user_id=2001, observation="老观察", source_message_id=1
        )
        assert await repo.get_person_observation(group_id=1001, user_id=2001) is not None
        # 第二次提取滚动覆盖：字段整体替换，旧文本不残留
        await repo.upsert_person_observation(
            group_id=1001,
            user_id=2001,
            observation="新观察",
            source_message_id=2,
            insufficient=True,
        )
        row = await repo.get_person_observation(group_id=1001, user_id=2001)
        assert row is not None
        assert row.observation == "新观察"
        assert row.insufficient is True
        assert row.source_message_id == 2
        # 每群每人一张：不同用户/群互不可见
        assert await repo.get_person_observation(group_id=1001, user_id=2002) is None
        assert await repo.get_person_observation(group_id=9999, user_id=2001) is None
    finally:
        await repo.close()


async def test_person_observation_delete_and_observations_enabled(tmp_path):
    repo = _repo(tmp_path)
    await repo.open()
    try:
        await repo.upsert_person_observation(
            group_id=1001, user_id=2001, observation="观察", insufficient=False
        )
        assert await repo.observations_enabled(group_id=1001, user_id=2001) is True
        assert await repo.delete_person_observation(group_id=1001, user_id=2001) is True
        assert await repo.delete_person_observation(group_id=1001, user_id=2001) is False
        # /记忆关闭 同样封死观察卡读写
        await repo.close_preferences(group_id=1001, user_id=2001)
        assert await repo.observations_enabled(group_id=1001, user_id=2001) is False
    finally:
        await repo.close()


async def test_delete_user_data_clears_person_observation(tmp_path):
    repo = _repo(tmp_path)
    await repo.open()
    try:
        await _add_message(repo, 1001, "我的消息", minutes_ago=5, user_id=2001)
        await _add_message(repo, 1001, "别人的消息", minutes_ago=5, user_id=2002)
        await repo.upsert_person_observation(group_id=1001, user_id=2001, observation="将被删除")
        await repo.upsert_person_observation(group_id=1001, user_id=2002, observation="别人的观察")

        affected = await repo.delete_user_data(group_id=1001, user_id=2001)

        assert affected == [1]
        assert await repo.get_person_observation(group_id=1001, user_id=2001) is None
        # 别人的观察卡必须保留
        other = await repo.get_person_observation(group_id=1001, user_id=2002)
        assert other is not None and other.observation == "别人的观察"
    finally:
        await repo.close()


async def test_v3_database_upgrades_to_v4_keeping_messages(tmp_path):
    """A schema-v3 database auto-migrates to v4 on open, keeping its rows."""
    path = tmp_path / "upgrade.sqlite3"
    connection = await aiosqlite.connect(path)
    await apply_migrations(connection, migrations=MIGRATIONS[:3])
    await connection.execute(
        "INSERT INTO chat_messages (group_id, user_id, message_text, created_at) "
        "VALUES (1001, 2001, 'v3 时代的消息', '2026-01-01T00:00:00+00:00')"
    )
    await connection.commit()
    await connection.close()

    repo = ChatMemoryRepository(path, retention_days=3650)
    await repo.open()
    try:
        assert await repo.upsert_jargon(group_id=1001, term="新库词") == "new"
        rows = await repo.messages_since(
            group_id=1001, days=10000, now=datetime(2026, 2, 1, tzinfo=UTC)
        )
        assert [r.message_text for r in rows] == ["v3 时代的消息"]
    finally:
        await repo.close()


async def test_learning_methods_reject_closed_repository(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(RepositoryClosedError):
        await repo.messages_since(group_id=1001, days=7)
    with pytest.raises(RepositoryClosedError):
        await repo.upsert_expression(
            group_id=1001, template="x", slot_count=0, evidence_row_id=None, evidence_digest=""
        )
    with pytest.raises(RepositoryClosedError):
        await repo.sample_expressions(group_id=1001, limit=5, threshold=1)
    with pytest.raises(RepositoryClosedError):
        await repo.touch_expressions([1])
    with pytest.raises(RepositoryClosedError):
        await repo.disable_expression(expression_id=1)
    with pytest.raises(RepositoryClosedError):
        await repo.list_active_expressions(group_id=1001)
    with pytest.raises(RepositoryClosedError):
        await repo.upsert_jargon(group_id=1001, term="x")
    with pytest.raises(RepositoryClosedError):
        await repo.list_jargon(group_id=1001, status="unconfirmed")
    with pytest.raises(RepositoryClosedError):
        await repo.get_jargon(group_id=1001, term="x")
    with pytest.raises(RepositoryClosedError):
        await repo.confirm_jargon(group_id=1001, term="x", definition="d")
    with pytest.raises(RepositoryClosedError):
        await repo.override_jargon(group_id=1001, term="x", definition="d")
    with pytest.raises(RepositoryClosedError):
        await repo.disable_jargon(group_id=1001, term="x")
    with pytest.raises(RepositoryClosedError):
        await repo.distinct_group_ids(days=7)
    with pytest.raises(RepositoryClosedError):
        await repo.upsert_person_observation(group_id=1001, user_id=2001, observation="x")
    with pytest.raises(RepositoryClosedError):
        await repo.get_person_observation(group_id=1001, user_id=2001)
    with pytest.raises(RepositoryClosedError):
        await repo.delete_person_observation(group_id=1001, user_id=2001)
    with pytest.raises(RepositoryClosedError):
        await repo.observations_enabled(group_id=1001, user_id=2001)
