"""Versioned, transactional SQLite schema migrations (S1-MIG-01..05).

Migrations are ordered Python definitions with unique, strictly increasing,
gap-free versions. Each unapplied migration runs in an explicit transaction:
DDL/DML and the version record commit or roll back together. A database whose
schema version is newer than the code supports is rejected instead of being
modified by an older program. There is no destructive downgrade.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Sequence

import aiosqlite

logger = logging.getLogger("qq_bot.migrations")

_SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
)
"""


class MigrationError(RuntimeError):
    """Raised when migrations cannot be applied or verified safely."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


# Version 1 establishes the current chat_messages baseline with idempotent
# DDL, so it works on both empty databases and databases that already carry
# the current schema (S1-MIG-02). Never edit an applied migration; add a new
# version instead.
MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="baseline_chat_messages",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                message_text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                is_ai_prompt INTEGER NOT NULL DEFAULT 0,
                ai_reply TEXT NOT NULL DEFAULT ''
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_chat_messages_group_user_created
            ON chat_messages (group_id, user_id, created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_chat_messages_group_created
            ON chat_messages (group_id, created_at)
            """,
        ),
    ),
    Migration(
        version=2,
        name="layered_memory",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS chat_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL,
                summary TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS summary_sources (
                summary_id INTEGER NOT NULL
                    REFERENCES chat_summaries(id) ON DELETE CASCADE,
                message_id INTEGER NOT NULL,
                PRIMARY KEY (summary_id, message_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS user_preferences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                preference TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT
            )
            """,
            # per-user memory state (S2-MEM-10): preferences_closed_at marks
            # the opt-out for long-term preferences (S2-MEM-06)
            """
            CREATE TABLE IF NOT EXISTS user_memory_state (
                group_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                preferences_closed_at TEXT,
                PRIMARY KEY (group_id, user_id)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_chat_summaries_group
            ON chat_summaries (group_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_user_preferences_group_user
            ON user_preferences (group_id, user_id)
            """,
        ),
    ),
    Migration(
        version=3,
        name="quota_usage",
        statements=(
            # stage-4 quota: raw integer scope ids (existing privacy
            # convention); only observability outputs use hashes (S4-QUOTA-07)
            """
            CREATE TABLE IF NOT EXISTS quota_usage (
                scope_type TEXT NOT NULL,            -- 'group' | 'user'
                scope_id INTEGER NOT NULL,           -- raw integer id
                day TEXT NOT NULL,                   -- 'YYYY-MM-DD'
                requests INTEGER NOT NULL DEFAULT 0,
                tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0.0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (scope_type, scope_id, day)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS quota_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT NOT NULL,
                scope_type TEXT NOT NULL,
                scope_id INTEGER NOT NULL,
                kind TEXT NOT NULL,   -- 'rate_denied' | 'cost_denied' | 'failure' | 'cost_estimated'
                reason TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT ''
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_quota_usage_day
            ON quota_usage (day)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_quota_events_scope
            ON quota_events (scope_type, scope_id, at)
            """,
        ),
    ),
)

SUPPORTED_SCHEMA_VERSION = max(migration.version for migration in MIGRATIONS)


def _validate_migration_list(migrations: Sequence[Migration]) -> None:
    expected = 1
    seen: set[int] = set()
    for migration in migrations:
        if migration.version in seen:
            raise MigrationError(f"duplicate migration version {migration.version}")
        seen.add(migration.version)
        if migration.version != expected:
            raise MigrationError(
                f"migration versions must be unique, strictly increasing and gap-free; "
                f"expected version {expected}, found {migration.version}"
            )
        expected += 1
    if not migrations:
        raise MigrationError("migration list must not be empty")


async def apply_migrations(
    connection: aiosqlite.Connection,
    *,
    migrations: Sequence[Migration] = MIGRATIONS,
    supported_version: int | None = None,
    now: datetime | None = None,
) -> None:
    """Apply unapplied migrations in order; raise ``MigrationError`` on any
    unsafe state or failed migration. Logs never include SQL parameters or row
    content (S1-MIG-05)."""
    _validate_migration_list(migrations)
    supported = supported_version if supported_version is not None else SUPPORTED_SCHEMA_VERSION
    applied_at = (now or datetime.now(UTC)).astimezone(UTC).isoformat()

    await connection.execute(_SCHEMA_MIGRATIONS_DDL)
    await connection.commit()

    cursor = await connection.execute("SELECT version FROM schema_migrations ORDER BY version")
    rows = await cursor.fetchall()
    applied_versions = [int(row[0]) for row in rows]

    if applied_versions and applied_versions[-1] > supported:
        raise MigrationError(
            f"database schema version {applied_versions[-1]} is newer than the "
            f"supported version {supported}; refusing to modify it"
        )
    # Applied versions must be a prefix of the migration list; a gap means a
    # later migration was recorded without its predecessors (S1-MIG-04).
    for index, version in enumerate(applied_versions):
        if version != migrations[index].version:
            raise MigrationError(
                f"database has migration {version} applied without its "
                f"predecessor {migrations[index].version}; refusing to continue"
            )

    pending = migrations[len(applied_versions) :]
    if not pending:
        logger.info("schema is up to date at version %d", supported)
        return

    for migration in pending:
        logger.info("applying migration %d (%s)", migration.version, migration.name)
        await connection.execute("BEGIN")
        try:
            for statement in migration.statements:
                await connection.execute(statement)
            await connection.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (migration.version, applied_at),
            )
            await connection.commit()
        except Exception as exc:
            await connection.execute("ROLLBACK")
            raise MigrationError(
                f"migration {migration.version} ({migration.name}) failed; transaction rolled back"
            ) from exc
        logger.info("applied migration %d (%s)", migration.version, migration.name)
