import sqlite3
from pathlib import Path
from typing import Any

import pytest

from qq_bot.services.roco_counter import (
    RocoCounterRow,
    RocoCounterStore,
    format_capture_result,
    format_counter_summary,
)


def test_add_normal_capture_creates_and_increments_pet(tmp_path: Path) -> None:
    store = RocoCounterStore(tmp_path / "counter.sqlite3")

    row = store.add_capture(group_id=1001, user_id=2002, season="S2", pet_name="迪莫", shiny=False)
    row = store.add_capture(group_id=1001, user_id=2002, season="S2", pet_name="迪莫", shiny=False)

    assert row.group_id == 1001
    assert row.user_id == 2002
    assert row.season == "S2"
    assert row.pet_name == "迪莫"
    assert row.normal_count == 2
    assert row.shiny_count == 0
    assert row.total_count == 2


def test_add_shiny_capture_increments_shiny_and_total(tmp_path: Path) -> None:
    store = RocoCounterStore(tmp_path / "counter.sqlite3")

    store.add_capture(group_id=1001, user_id=2002, season="S2", pet_name="迪莫", shiny=False)
    row = store.add_capture(group_id=1001, user_id=2002, season="S2", pet_name="迪莫", shiny=True)

    assert row.normal_count == 1
    assert row.shiny_count == 1
    assert row.total_count == 2


def test_store_closes_sqlite_connections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    connections: list[ClosingConnection] = []
    original_connect = sqlite3.connect

    def connect(*args: Any, **kwargs: Any) -> "ClosingConnection":
        connection = ClosingConnection(original_connect(*args, **kwargs))
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)

    store = RocoCounterStore(tmp_path / "counter.sqlite3")
    store.add_capture(group_id=1001, user_id=2002, season="S2", pet_name="迪莫", shiny=False)
    store.get_pet_count(group_id=1001, user_id=2002, season="S2", pet_name="迪莫")

    assert connections
    assert all(connection.closed for connection in connections)


def test_counter_summary_is_isolated_by_group_user_and_season(tmp_path: Path) -> None:
    store = RocoCounterStore(tmp_path / "counter.sqlite3")

    store.add_capture(group_id=1001, user_id=2002, season="S2", pet_name="迪莫", shiny=False)
    store.add_capture(group_id=1001, user_id=2002, season="S2", pet_name="迪莫", shiny=True)
    store.add_capture(group_id=1001, user_id=9999, season="S2", pet_name="喵喵", shiny=False)
    store.add_capture(group_id=9999, user_id=2002, season="S2", pet_name="火花", shiny=False)
    store.add_capture(group_id=1001, user_id=2002, season="S3", pet_name="水蓝蓝", shiny=False)

    rows = store.get_summary(group_id=1001, user_id=2002, season="S2")

    assert [row.pet_name for row in rows] == ["迪莫"]
    assert rows[0].normal_count == 1
    assert rows[0].shiny_count == 1


def test_counter_summary_sorts_by_total_desc_then_name(tmp_path: Path) -> None:
    store = RocoCounterStore(tmp_path / "counter.sqlite3")

    store.add_capture(group_id=1001, user_id=2002, season="S2", pet_name="喵喵", shiny=False)
    store.add_capture(group_id=1001, user_id=2002, season="S2", pet_name="迪莫", shiny=False)
    store.add_capture(group_id=1001, user_id=2002, season="S2", pet_name="迪莫", shiny=False)
    store.add_capture(group_id=1001, user_id=2002, season="S2", pet_name="阿布", shiny=True)

    rows = store.get_summary(group_id=1001, user_id=2002, season="S2")

    assert [row.pet_name for row in rows] == ["迪莫", "喵喵", "阿布"]


def test_format_empty_summary() -> None:
    text = format_counter_summary(season="S2", rows=[])

    assert text == "S2 捕捉计数器\n暂无记录。发送 /计数 迪莫 开始记录。"


def test_format_counter_summary() -> None:
    rows = [
        RocoCounterRow(1001, 2002, "S2", "迪莫", 2, 1, "2026-05-21T00:00:00+00:00"),
        RocoCounterRow(1001, 2002, "S2", "喵喵", 1, 0, "2026-05-21T00:00:00+00:00"),
    ]

    text = format_counter_summary(season="S2", rows=rows)

    assert text == "S2 捕捉计数器\n总捕捉：4 | 异色：1\n迪莫：3（异色 1）\n喵喵：1（异色 0）"


def test_format_capture_result() -> None:
    row = RocoCounterRow(1001, 2002, "S2", "迪莫", 2, 1, "2026-05-21T00:00:00+00:00")
    rows = [
        row,
        RocoCounterRow(1001, 2002, "S2", "喵喵", 1, 0, "2026-05-21T00:00:00+00:00"),
    ]

    text = format_capture_result(season="S2", row=row, rows=rows, shiny=True)

    assert text == "S2 异色 迪莫 +1\n当前：3 | 异色：1\n总捕捉：4 | 总异色：1"


class ClosingConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.closed = False

    def __enter__(self) -> sqlite3.Connection:
        return self.connection.__enter__()

    def __exit__(self, *args: Any) -> bool | None:
        return self.connection.__exit__(*args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.connection, name)

    def close(self) -> None:
        self.closed = True
        self.connection.close()
