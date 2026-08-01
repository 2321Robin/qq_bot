"""Async chat memory repository (S1-DB-01..05).

The repository owns a single ``aiosqlite`` connection per application
lifecycle: ``open()`` runs migrations before any CRUD is exposed, and
``close()`` releases the connection. All operations are awaitable and use
parameterized SQL; user input never becomes SQL structure. Retention cleanup
keeps the existing "cleanup before each read/write" observable semantics.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

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
