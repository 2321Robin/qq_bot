from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path


@dataclass(frozen=True)
class ChatMemoryRow:
    id: int
    group_id: int
    user_id: int
    message_text: str
    created_at: str
    is_ai_prompt: bool
    ai_reply: str


class ChatMemoryStore:
    def __init__(self, path: str | Path, *, retention_days: int) -> None:
        self.path = Path(path)
        self.retention_days = retention_days
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_database()

    def add_message(
        self,
        group_id: int,
        user_id: int,
        message_text: str,
        is_ai_prompt: bool = False,
        created_at: datetime | None = None,
        now: datetime | None = None,
    ) -> int:
        self._cleanup(now)
        timestamp = self._to_utc_iso(created_at or now or datetime.now(UTC))
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO chat_messages (
                    group_id, user_id, message_text, created_at, is_ai_prompt
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (group_id, user_id, message_text, timestamp, int(is_ai_prompt)),
            )
            return int(cursor.lastrowid)

    def update_ai_reply(self, message_id: int, ai_reply: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE chat_messages SET ai_reply = ? WHERE id = ?",
                (ai_reply, message_id),
            )

    def recent_user_turns(
        self,
        *,
        group_id: int,
        user_id: int,
        limit: int,
        now: datetime | None = None,
    ) -> list[ChatMemoryRow]:
        self._cleanup(now)
        return self._latest_rows(
            "group_id = ? AND user_id = ? AND is_ai_prompt = 1",
            (group_id, user_id),
            limit,
        )

    def recent_group_messages(
        self,
        *,
        group_id: int,
        limit: int,
        now: datetime | None = None,
    ) -> list[ChatMemoryRow]:
        self._cleanup(now)
        return self._latest_rows("group_id = ?", (group_id,), limit)

    def search_messages(
        self,
        *,
        group_id: int,
        keyword: str | None = None,
        user_id: int | None = None,
        limit: int,
        now: datetime | None = None,
    ) -> list[ChatMemoryRow]:
        self._cleanup(now)
        clauses = ["group_id = ?"]
        params: list[object] = [group_id]
        if keyword:
            clauses.append("message_text LIKE ? ESCAPE '\\'")
            params.append(f"%{self._escape_like(keyword)}%")
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        return self._latest_rows(" AND ".join(clauses), tuple(params), limit)

    def _initialize_database(self) -> None:
        with self._connect() as connection:
            connection.execute(
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
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_chat_messages_group_user_created
                ON chat_messages (group_id, user_id, created_at)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_chat_messages_group_created
                ON chat_messages (group_id, created_at)
                """
            )

    def _cleanup(self, now: datetime | None) -> None:
        cutoff = self._to_utc_iso((now or datetime.now(UTC)) - timedelta(days=self.retention_days))
        with self._connect() as connection:
            connection.execute("DELETE FROM chat_messages WHERE created_at < ?", (cutoff,))

    def _latest_rows(
        self,
        where_clause: str,
        params: tuple[object, ...],
        limit: int,
    ) -> list[ChatMemoryRow]:
        with self._connect() as connection:
            rows = connection.execute(
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
            ).fetchall()
        return [self._row_from_sqlite(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    @staticmethod
    def _to_utc_iso(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @staticmethod
    def _row_from_sqlite(row: sqlite3.Row | tuple[object, ...]) -> ChatMemoryRow:
        return ChatMemoryRow(
            id=int(row[0]),
            group_id=int(row[1]),
            user_id=int(row[2]),
            message_text=str(row[3]),
            created_at=str(row[4]),
            is_ai_prompt=bool(row[5]),
            ai_reply=str(row[6]),
        )
