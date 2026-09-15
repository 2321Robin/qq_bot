"""Game calendar schema parsing and rule engine tests (S6-GAME-01/02)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from qq_bot.services.game_calendar import (
    GameCalendar,
    GameCalendarError,
    load_game_calendar,
    parse_game_calendar,
)

TEMPLATE = Path("tests/fixtures/game_reports/game_calendar.template.json")


def test_template_loads():
    calendar = load_game_calendar(TEMPLATE)
    assert len(calendar.events) == 2
    assert calendar.weekly is not None and calendar.weekly.weekday == 6
    assert calendar.monthly is not None


@pytest.mark.parametrize("mutation, match", [
    ({"schema_version": 2}, "schema_version"),
    (lambda d: d["events"][0].__setitem__("game", "王者荣耀"), "game"),
    (lambda d: d["events"][1].__setitem__("end", "2026-12-01"), "end"),   # end < start
    (lambda d: d["events"][0].__setitem__("end", "2026-10-22"), "version"),  # version 不应有 end
    (lambda d: d["events"][0].__setitem__("start", "2026/10/21"), "start"),
    (lambda d: d["events"].append(dict(d["events"][0])), "duplicate"),
    (lambda d: d.__setitem__("weekly", {"day": "funday", "items": ["x"]}), "day"),
    (lambda d: d.__setitem__("monthly", {"trigger": "first_day", "items": ["x"]}), "trigger"),
])
def test_invalid_calendars_rejected(mutation, match, tmp_path):
    data = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    if callable(mutation):
        mutation(data)
    else:
        data.update(mutation)
    path = tmp_path / "calendar.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(GameCalendarError, match=match):
        load_game_calendar(path)


def test_missing_file_raises(tmp_path):
    with pytest.raises(GameCalendarError):
        load_game_calendar(tmp_path / "absent.json")
