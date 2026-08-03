"""Async chat memory repository (S1-DB-01..05).

The repository owns a single ``aiosqlite`` connection per application
lifecycle: ``open()`` runs migrations before any CRUD is exposed, and
``close()`` releases the connection. All operations are awaitable and use
parameterized SQL; user input never becomes SQL structure. Retention cleanup
keeps the existing "cleanup before each read/write" observable semantics.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

import aiosqlite

from qq_bot.services.migrations import apply_migrations

logger = logging.getLogger("qq_bot.chat_memory")

_BUSY_TIMEOUT_MS = 5000


@dataclass(frozen=True)
class ChatMemoryRow:
    id: int
    group_id: int
    user_id: int
    message_text: str
    created_at: str
    is_ai_prompt: bool
    ai_reply: str


@dataclass(frozen=True)
class ChatSummaryRow:
    id: int
    group_id: int
    summary: str
    created_at: str
    expires_at: str


@dataclass(frozen=True)
class UserPreferenceRow:
    id: int
    group_id: int
    user_id: int
    preference: str
    created_at: str
    expires_at: str | None


class RepositoryClosedError(RuntimeError):
    """Raised when operating on a closed repository."""


class ChatMemoryRepository:
    def __init__(self, path: str | Path, *, retention_days: int) -> None:
        self.path = Path(path)
        self.retention_days = retention_days
        self._connection: aiosqlite.Connection | None = None
        self.journal_mode: str = ""

    @property
    def is_open(self) -> bool:
        return self._connection is not None

    async def open(self) -> None:
        """Create the parent directory, connect, apply pragmas and migrations.

        Raises when the database cannot be opened or migrations fail; the
        connection is closed again on failure.
        """
        if self._connection is not None:
            raise RuntimeError("chat memory repository is already open")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self.path)
        connection.row_factory = aiosqlite.Row
        try:
            await connection.execute("PRAGMA foreign_keys = ON")
            await connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            self.journal_mode = await self._enable_wal(connection)
            await apply_migrations(connection)
        except Exception:
            await connection.close()
            self._connection = None
            raise
        self._connection = connection

    @staticmethod
    async def _enable_wal(connection: aiosqlite.Connection) -> str:
        """Request WAL journaling; fall back with a diagnostic when the
        filesystem rejects it (S1-DB-05) — never silently."""
        try:
            cursor = await connection.execute("PRAGMA journal_mode = WAL")
            row = await cursor.fetchone()
            return str(row[0]) if row is not None else ""
        except aiosqlite.Error as exc:
            logger.warning(
                "WAL journal mode unavailable (%s); continuing with the default journal", exc
            )
            return ""

    async def close(self) -> None:
        """Close the connection; repeated close is a no-op."""
        connection, self._connection = self._connection, None
        if connection is not None:
            await connection.close()

    def _require_open(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RepositoryClosedError("chat memory repository is not open")
        return self._connection

    async def check_ready(self) -> int | None:
        """Read-only liveness probe: return the max applied schema version,
        or None when the probe fails (S4-HEALTH-02). Bounded to 2 seconds so
        a wedged database can never hang the health endpoint (S4-HEALTH-05)."""
        try:

            async def _probe() -> int | None:
                cursor = await self._connection.execute(  # type: ignore[union-attr]
                    "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
                )
                row = await cursor.fetchone()
                return int(row[0]) if row is not None else None

            return await asyncio.wait_for(_probe(), timeout=2.0)
        except Exception:
            return None

    async def add_message(
        self,
        group_id: int,
        user_id: int,
        message_text: str,
        is_ai_prompt: bool = False,
        created_at: datetime | None = None,
        now: datetime | None = None,
    ) -> int:
        connection = self._require_open()
        await self._cleanup(connection, now)
        timestamp = self._to_utc_iso(created_at or now or datetime.now(UTC))
        cursor = await connection.execute(
            """
            INSERT INTO chat_messages (
                group_id, user_id, message_text, created_at, is_ai_prompt
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (group_id, user_id, message_text, timestamp, int(is_ai_prompt)),
        )
        await connection.commit()
        return int(cursor.lastrowid)

    async def update_ai_reply(self, message_id: int, ai_reply: str) -> None:
        connection = self._require_open()
        await connection.execute(
            "UPDATE chat_messages SET ai_reply = ? WHERE id = ?",
            (ai_reply, message_id),
        )
        await connection.commit()

    async def recent_user_turns(
        self,
        *,
        group_id: int,
        user_id: int,
        limit: int,
        now: datetime | None = None,
    ) -> list[ChatMemoryRow]:
        connection = self._require_open()
        await self._cleanup(connection, now)
        return await self._latest_rows(
            connection,
            "group_id = ? AND user_id = ? AND is_ai_prompt = 1",
            (group_id, user_id),
            limit,
        )

    async def recent_group_messages(
        self,
        *,
        group_id: int,
        limit: int,
        now: datetime | None = None,
    ) -> list[ChatMemoryRow]:
        connection = self._require_open()
        await self._cleanup(connection, now)
        return await self._latest_rows(connection, "group_id = ?", (group_id,), limit)

    async def search_messages(
        self,
        *,
        group_id: int,
        keyword: str | None = None,
        user_id: int | None = None,
        limit: int,
        now: datetime | None = None,
    ) -> list[ChatMemoryRow]:
        connection = self._require_open()
        await self._cleanup(connection, now)
        clauses = ["group_id = ?"]
        params: list[Any] = [group_id]
        if keyword:
            clauses.append("message_text LIKE ? ESCAPE '\\'")
            params.append(f"%{self._escape_like(keyword)}%")
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        return await self._latest_rows(connection, " AND ".join(clauses), tuple(params), limit)

    # -- layered memory (S2-MEM-01..04) -------------------------------------

    async def add_summary(
        self,
        group_id: int,
        summary: str,
        source_message_ids: Sequence[int],
        *,
        now: datetime | None = None,
    ) -> int:
        """Store a short-term summary whose expiry is the earliest source
        deadline (S2-MEM-02, S2-MEM-03): it never outlives any source."""
        connection = self._require_open()
        if not source_message_ids:
            raise ValueError("summary requires at least one source message")
        placeholders = ",".join("?" for _ in source_message_ids)
        cursor = await connection.execute(
            f"SELECT MIN(created_at) FROM chat_messages WHERE id IN ({placeholders})",
            tuple(source_message_ids),
        )
        row = await cursor.fetchone()
        if row is None or row[0] is None:
            raise ValueError("summary sources must reference existing chat messages")
        oldest_source = datetime.fromisoformat(str(row[0]))
        expires_at = self._to_utc_iso(oldest_source + timedelta(days=self.retention_days))
        timestamp = self._to_utc_iso(now or datetime.now(UTC))
        cursor = await connection.execute(
            "INSERT INTO chat_summaries (group_id, summary, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (group_id, summary, timestamp, expires_at),
        )
        summary_id = int(cursor.lastrowid)
        await connection.executemany(
            "INSERT INTO summary_sources (summary_id, message_id) VALUES (?, ?)",
            [(summary_id, int(message_id)) for message_id in source_message_ids],
        )
        await connection.commit()
        return summary_id

    async def get_summaries(
        self, *, group_id: int, now: datetime | None = None
    ) -> list[ChatSummaryRow]:
        """Expired summaries are removed on read and never reach the prompt."""
        connection = self._require_open()
        timestamp = self._to_utc_iso(now or datetime.now(UTC))
        await connection.execute("DELETE FROM chat_summaries WHERE expires_at < ?", (timestamp,))
        await connection.commit()
        cursor = await connection.execute(
            """
            SELECT id, group_id, summary, created_at, expires_at
            FROM chat_summaries
            WHERE group_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (group_id,),
        )
        rows = await cursor.fetchall()
        return [
            ChatSummaryRow(
                id=int(row[0]),
                group_id=int(row[1]),
                summary=str(row[2]),
                created_at=str(row[3]),
                expires_at=str(row[4]),
            )
            for row in rows
        ]

    # -- long-term preferences (S2-MEM-05..06, S2-MEM-09) --------------------

    async def get_preferences(self, *, group_id: int, user_id: int) -> list[UserPreferenceRow]:
        connection = self._require_open()
        cursor = await connection.execute(
            """
            SELECT id, group_id, user_id, preference, created_at, expires_at
            FROM user_preferences
            WHERE group_id = ? AND user_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (group_id, user_id),
        )
        rows = await cursor.fetchall()
        return [
            UserPreferenceRow(
                id=int(row[0]),
                group_id=int(row[1]),
                user_id=int(row[2]),
                preference=str(row[3]),
                created_at=str(row[4]),
                expires_at=str(row[5]) if row[5] is not None else None,
            )
            for row in rows
        ]

    async def save_preference(
        self,
        *,
        group_id: int,
        user_id: int,
        preference: str,
        now: datetime | None = None,
    ) -> int:
        """An explicit save is a fresh opt-in and lifts any closed state
        (S2-MEM-05)."""
        connection = self._require_open()
        timestamp = self._to_utc_iso(now or datetime.now(UTC))
        await connection.execute(
            "DELETE FROM user_memory_state WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        )
        cursor = await connection.execute(
            "INSERT INTO user_preferences (group_id, user_id, preference, created_at) "
            "VALUES (?, ?, ?, ?)",
            (group_id, user_id, preference, timestamp),
        )
        await connection.commit()
        return int(cursor.lastrowid)

    async def delete_preference(self, *, group_id: int, user_id: int, preference_id: int) -> bool:
        """Delete one preference; returns False when it does not belong to
        this group/user (S2-MEM-06: only the owner's own preferences)."""
        connection = self._require_open()
        cursor = await connection.execute(
            "DELETE FROM user_preferences WHERE id = ? AND group_id = ? AND user_id = ?",
            (preference_id, group_id, user_id),
        )
        await connection.commit()
        return cursor.rowcount > 0

    async def delete_all_preferences(self, *, group_id: int, user_id: int) -> None:
        connection = self._require_open()
        await connection.execute(
            "DELETE FROM user_preferences WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        )
        await connection.commit()

    async def close_preferences(
        self, *, group_id: int, user_id: int, now: datetime | None = None
    ) -> None:
        """Clear every preference and mark the opt-out so nothing of the
        user's long-term memory enters a prompt (S2-MEM-06)."""
        connection = self._require_open()
        timestamp = self._to_utc_iso(now or datetime.now(UTC))
        await connection.execute(
            "DELETE FROM user_preferences WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        )
        await connection.execute(
            """
            INSERT INTO user_memory_state (group_id, user_id, preferences_closed_at)
            VALUES (?, ?, ?)
            ON CONFLICT (group_id, user_id)
            DO UPDATE SET preferences_closed_at = excluded.preferences_closed_at
            """,
            (group_id, user_id, timestamp),
        )
        await connection.commit()

    async def preferences_enabled(self, *, group_id: int, user_id: int) -> bool:
        """False after the user closed long-term preferences (S2-MEM-06)."""
        connection = self._require_open()
        cursor = await connection.execute(
            "SELECT 1 FROM user_memory_state "
            "WHERE group_id = ? AND user_id = ? AND preferences_closed_at IS NOT NULL",
            (group_id, user_id),
        )
        row = await cursor.fetchone()
        return row is None

    # -- full user deletion (S2-MEM-07) -------------------------------------

    async def delete_user_data(self, *, group_id: int, user_id: int) -> list[int]:
        """Delete one user's messages (with their AI replies), preferences and
        closed state; delete summaries that reference any of those messages.
        Other users' rows are untouched. Returns the affected message ids for
        in-process cache invalidation (S2-MEM-07)."""
        connection = self._require_open()
        cursor = await connection.execute(
            "SELECT id FROM chat_messages WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        )
        affected = [int(row[0]) for row in await cursor.fetchall()]
        if affected:
            placeholders = ",".join("?" for _ in affected)
            params = tuple(affected)
            # a summary dies with any of its sources (S2-MEM-03)
            await connection.execute(
                f"""
                DELETE FROM chat_summaries
                WHERE id IN (
                    SELECT DISTINCT summary_id FROM summary_sources
                    WHERE message_id IN ({placeholders})
                )
                """,
                params,
            )
            await connection.execute(
                f"DELETE FROM summary_sources WHERE message_id IN ({placeholders})",
                params,
            )
            await connection.execute(
                f"DELETE FROM chat_messages WHERE id IN ({placeholders})",
                params,
            )
        await connection.execute(
            "DELETE FROM user_preferences WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        )
        await connection.execute(
            "DELETE FROM user_memory_state WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        )
        await connection.commit()
        return affected

    async def _cleanup(self, connection: aiosqlite.Connection, now: datetime | None) -> None:
        cutoff = self._to_utc_iso((now or datetime.now(UTC)) - timedelta(days=self.retention_days))
        await connection.execute("DELETE FROM chat_messages WHERE created_at < ?", (cutoff,))
        await connection.commit()

    @staticmethod
    async def _latest_rows(
        connection: aiosqlite.Connection,
        where_clause: str,
        params: tuple[Any, ...],
        limit: int,
    ) -> list[ChatMemoryRow]:
        cursor = await connection.execute(
            f"""
            SELECT id, group_id, user_id, message_text, created_at, is_ai_prompt, ai_reply
            FROM (
                SELECT id, group_id, user_id, message_text, created_at, is_ai_prompt, ai_reply
                FROM chat_messages
                WHERE {where_clause}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
            )
            ORDER BY created_at ASC, id ASC
            """,
            (*params, limit),
        )
        rows = await cursor.fetchall()
        return [ChatMemoryRepository._row_from_sqlite(row) for row in rows]

    @staticmethod
    def _to_utc_iso(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @staticmethod
    def _row_from_sqlite(row: aiosqlite.Row | tuple[Any, ...]) -> ChatMemoryRow:
        return ChatMemoryRow(
            id=int(row[0]),
            group_id=int(row[1]),
            user_id=int(row[2]),
            message_text=str(row[3]),
            created_at=str(row[4]),
            is_ai_prompt=bool(row[5]),
            ai_reply=str(row[6]),
        )
