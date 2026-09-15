"""Game calendar schema parsing and validation (S6-GAME-01).

The calendar file (``data/game_calendar.json``) is hand-maintained (route 1:
humans or coding agents edit it directly; a template ships in
``tests/fixtures/game_reports/game_calendar.template.json``). A missing,
unreadable or schema-invalid file must fail loudly at startup rather than
silently skip reminders, so every IO/JSON error is normalized to
``GameCalendarError``.
"""

from __future__ import annotations

import json

from dataclasses import dataclass
from datetime import date
from pathlib import Path

SUPPORTED_SCHEMA_VERSION = 1
GAMES: tuple[str, ...] = ("原神", "星穹铁道", "明日方舟", "终末地")
KINDS: tuple[str, ...] = ("version", "event")
_WEEKDAY_NAMES = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


class GameCalendarError(ValueError):
    """Game calendar file is missing, unreadable, or schema-invalid."""


@dataclass(frozen=True)
class GameEvent:
    game: str
    kind: str
    title: str
    start: date
    end: date | None


@dataclass(frozen=True)
class WeeklyRule:
    weekday: int
    items: tuple[str, ...]


@dataclass(frozen=True)
class MonthlyRule:
    items: tuple[str, ...]  # trigger 固定 last_day（S6-GAME-01）


@dataclass(frozen=True)
class GameCalendar:
    events: tuple[GameEvent, ...]
    weekly: WeeklyRule | None
    monthly: MonthlyRule | None


def _parse_date(value: object, field: str) -> date:
    if not isinstance(value, str):
        raise GameCalendarError(f"{field} must be an ISO date string")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise GameCalendarError(f"{field} must use YYYY-MM-DD: {value!r}") from exc


def parse_game_calendar(raw: object) -> GameCalendar:
    if not isinstance(raw, dict):
        raise GameCalendarError("calendar root must be a JSON object")
    if raw.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
        raise GameCalendarError(f"unsupported schema_version: {raw.get('schema_version')!r}")
    events: list[GameEvent] = []
    seen: set[tuple[str, str, str]] = set()
    for entry in raw.get("events", []):
        if not isinstance(entry, dict):
            raise GameCalendarError("each event must be an object")
        game, kind, title = entry.get("game"), entry.get("kind"), entry.get("title")
        if game not in GAMES:
            raise GameCalendarError(f"game must be one of {GAMES}: {game!r}")
        if kind not in KINDS:
            raise GameCalendarError(f"kind must be one of {KINDS}: {kind!r}")
        if not isinstance(title, str) or not title.strip():
            raise GameCalendarError("event title must be a non-empty string")
        start = _parse_date(entry.get("start"), "start")
        end_raw = entry.get("end")
        end = None if end_raw is None else _parse_date(end_raw, "end")
        if kind == "version" and end is not None:
            raise GameCalendarError("version events must not carry an end date")
        if kind == "event":
            if end is None:
                raise GameCalendarError("event entries require an end date")
            if end < start:
                raise GameCalendarError(f"event end must not precede start: {title!r}")
        key = (game, title.strip(), start.isoformat())
        if key in seen:
            raise GameCalendarError(f"duplicate event entry: {key}")
        seen.add(key)
        events.append(
            GameEvent(game=game, kind=kind, title=title.strip(), start=start, end=end)
        )
    weekly_raw = raw.get("weekly")
    weekly = None
    if weekly_raw is not None:
        day = weekly_raw.get("day")
        if day not in _WEEKDAY_NAMES:
            raise GameCalendarError(f"weekly.day must be one of {tuple(_WEEKDAY_NAMES)}")
        weekly = WeeklyRule(
            weekday=_WEEKDAY_NAMES[day],
            items=_parse_items(weekly_raw.get("items"), "weekly.items"),
        )
    monthly_raw = raw.get("monthly")
    monthly = None
    if monthly_raw is not None:
        if monthly_raw.get("trigger") != "last_day":
            raise GameCalendarError("monthly.trigger must be last_day")
        monthly = MonthlyRule(items=_parse_items(monthly_raw.get("items"), "monthly.items"))
    return GameCalendar(events=tuple(events), weekly=weekly, monthly=monthly)


def _parse_items(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise GameCalendarError(f"{field} must be a non-empty list of non-empty strings")
    return tuple(item.strip() for item in value)


def load_game_calendar(path: Path) -> GameCalendar:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GameCalendarError(f"cannot read game calendar {path}: {exc}") from exc
    return parse_game_calendar(raw)
