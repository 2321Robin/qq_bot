"""Versioned migration tests (S1-MIG-01..05, S1-GATE-03)."""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite
import pytest

from qq_bot.services.migrations import (
    MIGRATIONS,
    SUPPORTED_SCHEMA_VERSION,
    Migration,
    MigrationError,
    apply_migrations,
)

_CHAT_MESSAGES_DDL = """
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    message_text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    is_ai_prompt INTEGER NOT NULL DEFAULT 0,
    ai_reply TEXT NOT NULL DEFAULT ''
)
"""


async def _tables(connection: aiosqlite.Connection) -> set[str]:
    cursor = await connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {str(row[0]) for row in await cursor.fetchall()}


async def _indexes(connection: aiosqlite.Connection) -> set[str]:
    cursor = await connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    return {str(row[0]) for row in await cursor.fetchall()}


async def _applied_versions(connection: aiosqlite.Connection) -> list[int]:
    cursor = await connection.execute("SELECT version FROM schema_migrations ORDER BY version")
    return [int(row[0]) for row in await cursor.fetchall()]


async def _open_connection(path) -> aiosqlite.Connection:
    connection = await aiosqlite.connect(path)
    connection.row_factory = aiosqlite.Row
    return connection


@pytest.mark.asyncio
async def test_empty_database_gets_full_schema_and_version_record(tmp_path) -> None:
    connection = await _open_connection(tmp_path / "empty.sqlite3")
    try:
        await apply_migrations(connection)
        tables = await _tables(connection)
        assert "schema_migrations" in tables
        assert "chat_messages" in tables
        assert "chat_summaries" in tables
        assert "summary_sources" in tables
        assert "user_preferences" in tables
        assert "user_memory_state" in tables
        indexes = await _indexes(connection)
        assert "idx_chat_messages_group_user_created" in indexes
        assert "idx_chat_messages_group_created" in indexes
        assert "idx_chat_summaries_group" in indexes
        assert "idx_user_preferences_group_user" in indexes
        assert await _applied_versions(connection) == [1, 2]
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_legacy_current_schema_database_is_adopted_without_data_loss(
    tmp_path,
) -> None:
    """S1-GATE-03: a database carrying the current schema without the migration
    table must be taken over, keeping its rows."""
    path = tmp_path / "legacy.sqlite3"
    connection = await _open_connection(path)
    await connection.execute(_CHAT_MESSAGES_DDL)
    await connection.execute(
        "INSERT INTO chat_messages (group_id, user_id, message_text, created_at) "
        "VALUES (1001, 2001, 'legacy message', '2026-01-01T00:00:00+00:00')"
    )
    await connection.commit()
    await connection.close()

    connection = await _open_connection(path)
    try:
        await apply_migrations(connection)
        cursor = await connection.execute("SELECT COUNT(*) FROM chat_messages")
        row = await cursor.fetchone()
        assert int(row[0]) == 1
        assert await _applied_versions(connection) == [1, 2]
        tables = await _tables(connection)
        assert "chat_summaries" in tables
        assert "user_preferences" in tables
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_version_one_database_upgrades_to_two_without_data_loss(tmp_path) -> None:
    """A stage-1 database (version 1 recorded, messages present) upgrades to
    version 2 keeping every existing row (S2-MEM-10)."""
    path = tmp_path / "v1.sqlite3"
    connection = await _open_connection(path)
    await connection.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    await connection.execute(
        "INSERT INTO schema_migrations VALUES (1, '2026-08-01T00:00:00+00:00')"
    )
    await connection.execute(_CHAT_MESSAGES_DDL)
    await connection.execute(
        "INSERT INTO chat_messages (group_id, user_id, message_text, created_at, ai_reply) "
        "VALUES (1001, 2001, 'stage-1 message', '2026-08-01T00:00:00+00:00', 'stage-1 reply')"
    )
    await connection.commit()
    await connection.close()

    connection = await _open_connection(path)
    try:
        await apply_migrations(connection)
        assert await _applied_versions(connection) == [1, 2]
        cursor = await connection.execute(
            "SELECT message_text, ai_reply FROM chat_messages WHERE id = 1"
        )
        row = await cursor.fetchone()
        assert str(row[0]) == "stage-1 message"
        assert str(row[1]) == "stage-1 reply"
        tables = await _tables(connection)
        assert "chat_summaries" in tables
        assert "user_preferences" in tables
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_rerunning_migrations_is_idempotent(tmp_path) -> None:
    connection = await _open_connection(tmp_path / "idem.sqlite3")
    try:
        await apply_migrations(connection)
        await apply_migrations(connection)
        assert await _applied_versions(connection) == [1, 2]
        cursor = await connection.execute("SELECT COUNT(*) FROM schema_migrations")
        row = await cursor.fetchone()
        assert int(row[0]) == 2
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_migrations_apply_without_crud_before_they_run(tmp_path) -> None:
    """The repository API must not be usable before migrations; this is covered
    by the repository tests. Here we assert the migration list itself is valid."""
    assert MIGRATIONS
    versions = [migration.version for migration in MIGRATIONS]
    assert versions == list(range(1, len(versions) + 1))
    assert SUPPORTED_SCHEMA_VERSION == versions[-1]
    for migration in MIGRATIONS:
        assert migration.name
        assert migration.statements


@pytest.mark.asyncio
async def test_failed_migration_rolls_back_ddl_and_version_together(tmp_path) -> None:
    migrations = (
        Migration(1, "ok", ("CREATE TABLE t1 (id INTEGER PRIMARY KEY)",)),
        Migration(
            2,
            "broken",
            ("CREATE TABLE t2 (id INTEGER PRIMARY KEY)", "THIS IS NOT SQL"),
        ),
    )
    connection = await _open_connection(tmp_path / "rollback.sqlite3")
    try:
        with pytest.raises(MigrationError, match="rolled back"):
            await apply_migrations(connection, migrations=migrations)
        # migration 1 committed; migration 2 left nothing behind
        assert await _applied_versions(connection) == [1]
        assert "t1" in await _tables(connection)
        assert "t2" not in await _tables(connection)

        # re-running after the fix heals the database without touching v1
        fixed = (
            migrations[0],
            Migration(2, "fixed", ("CREATE TABLE t2 (id INTEGER PRIMARY KEY)",)),
        )
        await apply_migrations(connection, migrations=fixed)
        assert await _applied_versions(connection) == [1, 2]
        assert "t2" in await _tables(connection)
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_duplicate_migration_versions_rejected(tmp_path) -> None:
    connection = await _open_connection(tmp_path / "dup.sqlite3")
    try:
        with pytest.raises(MigrationError, match="duplicate"):
            await apply_migrations(
                connection,
                migrations=(
                    Migration(1, "a", ("CREATE TABLE a1 (id INTEGER)",)),
                    Migration(1, "b", ("CREATE TABLE b1 (id INTEGER)",)),
                ),
            )
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_gap_in_migration_versions_rejected(tmp_path) -> None:
    connection = await _open_connection(tmp_path / "gap.sqlite3")
    try:
        with pytest.raises(MigrationError, match="gap-free"):
            await apply_migrations(
                connection,
                migrations=(
                    Migration(1, "a", ("CREATE TABLE a1 (id INTEGER)",)),
                    Migration(3, "c", ("CREATE TABLE c1 (id INTEGER)",)),
                ),
            )
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_empty_migration_list_rejected(tmp_path) -> None:
    connection = await _open_connection(tmp_path / "empty-list.sqlite3")
    try:
        with pytest.raises(MigrationError, match="empty"):
            await apply_migrations(connection, migrations=())
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_database_newer_than_supported_version_rejected(tmp_path) -> None:
    connection = await _open_connection(tmp_path / "newer.sqlite3")
    try:
        await connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        await connection.execute(
            "INSERT INTO schema_migrations VALUES (99, '2026-08-01T00:00:00+00:00')"
        )
        await connection.commit()
        with pytest.raises(MigrationError, match="newer than the supported"):
            await apply_migrations(connection)
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_applied_versions_must_be_a_prefix_of_migration_list(tmp_path) -> None:
    migrations = (
        Migration(1, "a", ("CREATE TABLE a1 (id INTEGER)",)),
        Migration(2, "b", ("CREATE TABLE b1 (id INTEGER)",)),
        Migration(3, "c", ("CREATE TABLE c1 (id INTEGER)",)),
    )
    connection = await _open_connection(tmp_path / "prefix.sqlite3")
    try:
        await connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        # version 2 recorded without version 1: a corrupted gap within the
        # supported range (too-new check cannot catch this)
        await connection.execute(
            "INSERT INTO schema_migrations VALUES (2, '2026-08-01T00:00:00+00:00')"
        )
        await connection.commit()
        with pytest.raises(MigrationError, match="without its predecessor"):
            await apply_migrations(connection, migrations=migrations, supported_version=3)
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_applied_at_is_utc_iso_timestamp(tmp_path) -> None:
    connection = await _open_connection(tmp_path / "stamp.sqlite3")
    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    try:
        await apply_migrations(connection, now=now)
        cursor = await connection.execute(
            "SELECT applied_at FROM schema_migrations WHERE version = 1"
        )
        row = await cursor.fetchone()
        assert str(row[0]) == "2026-08-01T12:00:00+00:00"
    finally:
        await connection.close()
