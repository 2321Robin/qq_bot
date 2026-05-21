from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class RocoCounterRow:
    group_id: int
    user_id: int
    season: str
    pet_name: str
    normal_count: int
    shiny_count: int
    updated_at: str

    @property
    def total_count(self) -> int:
        return self.normal_count + self.shiny_count


COUNTER_USAGE = "用法：/计数 迪莫 或 /计数 异色 迪莫；发送 /计数 查看汇总。"


@dataclass(frozen=True)
class CounterAction:
    pet_name: str
    shiny: bool = False
    show_summary: bool = False
    error: str = ""


def parse_counter_args(text: str) -> CounterAction:
    parts = text.split()
    if not parts:
        return CounterAction(pet_name="", show_summary=True)

    if parts[0] == "异色":
        pet_name = " ".join(parts[1:]).strip()
        if not pet_name:
            return CounterAction(pet_name="", shiny=True, error=COUNTER_USAGE)
        return CounterAction(pet_name=pet_name, shiny=True)

    return CounterAction(pet_name=" ".join(parts).strip())


class RocoCounterStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_database()

    def add_capture(
        self,
        *,
        group_id: int,
        user_id: int,
        season: str,
        pet_name: str,
        shiny: bool,
        now: datetime | None = None,
    ) -> RocoCounterRow:
        timestamp = self._to_utc_iso(now or datetime.now(UTC))
        normal_delta = 0 if shiny else 1
        shiny_delta = 1 if shiny else 0
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO roco_counter (
                        group_id, user_id, season, pet_name, normal_count, shiny_count, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(group_id, user_id, season, pet_name) DO UPDATE SET
                        normal_count = normal_count + excluded.normal_count,
                        shiny_count = shiny_count + excluded.shiny_count,
                        updated_at = excluded.updated_at
                    """,
                    (group_id, user_id, season, pet_name, normal_delta, shiny_delta, timestamp),
                )
                if shiny:
                    row = connection.execute(
                        """
                        SELECT normal_count + shiny_count
                        FROM roco_counter
                        WHERE group_id = ? AND user_id = ? AND season = ? AND pet_name = ?
                        """,
                        (group_id, user_id, season, pet_name),
                    ).fetchone()
                    if row is None:
                        raise RuntimeError("failed to read roco counter total after shiny update")
                    connection.execute(
                        """
                        INSERT INTO roco_counter_shiny_marks (
                            group_id, user_id, season, pet_name, total_index, created_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (group_id, user_id, season, pet_name, int(row[0]), timestamp),
                    )
        row = self.get_pet_count(
            group_id=group_id,
            user_id=user_id,
            season=season,
            pet_name=pet_name,
        )
        if row is None:
            raise RuntimeError("failed to read roco counter row after update")
        return row

    def get_pet_count(
        self,
        *,
        group_id: int,
        user_id: int,
        season: str,
        pet_name: str,
    ) -> RocoCounterRow | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT group_id, user_id, season, pet_name, normal_count, shiny_count, updated_at
                FROM roco_counter
                WHERE group_id = ? AND user_id = ? AND season = ? AND pet_name = ?
                """,
                (group_id, user_id, season, pet_name),
            ).fetchone()
        if row is None:
            return None
        return self._row_from_sqlite(row)

    def get_summary(
        self,
        *,
        group_id: int,
        user_id: int,
        season: str,
    ) -> list[RocoCounterRow]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT group_id, user_id, season, pet_name, normal_count, shiny_count, updated_at
                FROM roco_counter
                WHERE group_id = ? AND user_id = ? AND season = ?
                ORDER BY (normal_count + shiny_count) DESC, pet_name ASC
                """,
                (group_id, user_id, season),
            ).fetchall()
        return [self._row_from_sqlite(row) for row in rows]

    def get_shiny_indexes(
        self,
        *,
        group_id: int,
        user_id: int,
        season: str,
        pet_name: str,
    ) -> list[int]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT total_index
                FROM roco_counter_shiny_marks
                WHERE group_id = ? AND user_id = ? AND season = ? AND pet_name = ?
                ORDER BY total_index ASC, id ASC
                """,
                (group_id, user_id, season, pet_name),
            ).fetchall()
        return [int(row[0]) for row in rows]

    def get_summary_shiny_indexes(
        self,
        *,
        group_id: int,
        user_id: int,
        season: str,
    ) -> dict[str, list[int]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT pet_name, total_index
                FROM roco_counter_shiny_marks
                WHERE group_id = ? AND user_id = ? AND season = ?
                ORDER BY pet_name ASC, total_index ASC, id ASC
                """,
                (group_id, user_id, season),
            ).fetchall()
        indexes: dict[str, list[int]] = {}
        for pet_name, total_index in rows:
            indexes.setdefault(str(pet_name), []).append(int(total_index))
        return indexes

    def _initialize_database(self) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS roco_counter (
                        group_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        season TEXT NOT NULL,
                        pet_name TEXT NOT NULL,
                        normal_count INTEGER NOT NULL DEFAULT 0,
                        shiny_count INTEGER NOT NULL DEFAULT 0,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (group_id, user_id, season, pet_name)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_roco_counter_scope_total
                    ON roco_counter (group_id, user_id, season, normal_count, shiny_count)
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS roco_counter_shiny_marks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        group_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        season TEXT NOT NULL,
                        pet_name TEXT NOT NULL,
                        total_index INTEGER NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_roco_counter_shiny_marks_scope
                    ON roco_counter_shiny_marks (group_id, user_id, season, pet_name, total_index)
                    """
                )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    @staticmethod
    def _to_utc_iso(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()

    @staticmethod
    def _row_from_sqlite(row: sqlite3.Row | tuple[object, ...]) -> RocoCounterRow:
        return RocoCounterRow(
            group_id=int(row[0]),
            user_id=int(row[1]),
            season=str(row[2]),
            pet_name=str(row[3]),
            normal_count=int(row[4]),
            shiny_count=int(row[5]),
            updated_at=str(row[6]),
        )


def format_counter_summary(
    *,
    season: str,
    rows: list[RocoCounterRow],
    shiny_indexes: dict[str, list[int]] | None = None,
) -> str:
    if not rows:
        return f"{season} 捕捉计数器\n暂无记录。发送 /计数 迪莫 开始记录。"

    total_count = sum(row.total_count for row in rows)
    shiny_count = sum(row.shiny_count for row in rows)
    lines = [f"{season} 捕捉计数器", f"总捕捉：{total_count} | 异色：{shiny_count}"]
    lines.extend(_format_counter_summary_row(row, shiny_indexes or {}) for row in rows)
    return "\n".join(lines)


def format_capture_result(
    *,
    season: str,
    row: RocoCounterRow,
    rows: list[RocoCounterRow],
    shiny: bool,
    shiny_index: int | None = None,
) -> str:
    total_count = sum(summary_row.total_count for summary_row in rows)
    shiny_count = sum(summary_row.shiny_count for summary_row in rows)
    if shiny and shiny_index is not None:
        title = f"{season} 异色 {row.pet_name} +1（第 {shiny_index} 只是异色）"
    else:
        title = f"{season} 异色 {row.pet_name} +1" if shiny else f"{season} {row.pet_name} +1"
    return "\n".join(
        [
            title,
            f"当前：{row.total_count} | 异色：{row.shiny_count}",
            f"总捕捉：{total_count} | 总异色：{shiny_count}",
        ]
    )


def _format_counter_summary_row(row: RocoCounterRow, shiny_indexes: dict[str, list[int]]) -> str:
    indexes = shiny_indexes.get(row.pet_name, [])
    if row.shiny_count > 0 and indexes:
        joined_indexes = "、".join(str(index) for index in indexes)
        return f"{row.pet_name}：{row.total_count}（异色 {row.shiny_count}：第 {joined_indexes} 只）"
    return f"{row.pet_name}：{row.total_count}（异色 {row.shiny_count}）"
