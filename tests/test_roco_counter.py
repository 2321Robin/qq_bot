import sqlite3
from pathlib import Path
from typing import Any

import pytest

from qq_bot.services.roco_counter import RocoCounterStore


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
