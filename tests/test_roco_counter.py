from pathlib import Path

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
