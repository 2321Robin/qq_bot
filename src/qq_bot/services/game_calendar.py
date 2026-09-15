"""Game calendar schema parsing and validation (S6-GAME-01).

The calendar file (``data/game_calendar.json``) is hand-maintained (route 1:
humans or coding agents edit it directly; a template ships in
``tests/fixtures/game_reports/game_calendar.template.json``). A missing,
unreadable or schema-invalid file must fail loudly at startup rather than
silently skip reminders, so every IO/JSON error is normalized to
``GameCalendarError``.
"""

from __future__ import annotations

import calendar as _calendar_module
import json

from collections.abc import Iterable
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
        events.append(GameEvent(game=game, kind=kind, title=title.strip(), start=start, end=end))
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
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item.strip() for item in value)
    ):
        raise GameCalendarError(f"{field} must be a non-empty list of non-empty strings")
    return tuple(item.strip() for item in value)


def load_game_calendar(path: Path) -> GameCalendar:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GameCalendarError(f"cannot read game calendar {path}: {exc}") from exc
    return parse_game_calendar(raw)


# ---- 规则引擎（S6-GAME-02）：纯函数、离线、today 可注入 ----

ENDING_SOON_WINDOW_DAYS = 2

_VERSION_FMT = "{game} {title} 今日开服"
_START_FMT = "{game} 「{title}」今日开启"
_ENDING_TODAY_FMT = "{game} 「{title}」今日结束"
_ENDING_SOON_FMT = "{game} 「{title}」还有 {days} 天结束（{month}月{day}日截止）"


def _is_live(event: GameEvent, today: date) -> bool:
    # 过期隐藏：version 看 start，event 看 end（S6-GAME-02）
    boundary = event.start if event.kind == "version" else event.end
    assert boundary is not None
    return boundary >= today


def is_last_day_of_month(today: date) -> bool:
    last_day = _calendar_module.monthrange(today.year, today.month)[1]
    return today.day == last_day


def _merge_cleanup(weekly: Iterable[str], monthly: Iterable[str]) -> tuple[str, ...]:
    merged: list[str] = []
    for item in (*weekly, *monthly):
        if item not in merged:
            merged.append(item)
    return tuple(merged)


@dataclass(frozen=True)
class MorningSections:
    versions: tuple[str, ...]
    starting: tuple[str, ...]

    def is_empty(self) -> bool:
        return not self.versions and not self.starting


@dataclass(frozen=True)
class EveningSections:
    cleanup: tuple[str, ...]
    ending_today: tuple[str, ...]
    ending_soon: tuple[str, ...]

    def is_empty(self) -> bool:
        return not self.cleanup and not self.ending_today and not self.ending_soon


def morning_sections(calendar: GameCalendar, today: date) -> MorningSections:
    live = [event for event in calendar.events if _is_live(event, today)]
    versions = tuple(
        _VERSION_FMT.format(game=e.game, title=e.title)
        for e in live
        if e.kind == "version" and e.start == today
    )
    starting = tuple(
        _START_FMT.format(game=e.game, title=e.title)
        for e in live
        if e.kind == "event" and e.start == today
    )
    return MorningSections(versions=versions, starting=starting)


def evening_sections(calendar: GameCalendar, today: date) -> EveningSections:
    live = [
        event
        for event in calendar.events
        if event.kind == "event" and event.end is not None and _is_live(event, today)
    ]
    ending_today = tuple(
        _ENDING_TODAY_FMT.format(game=e.game, title=e.title) for e in live if e.end == today
    )
    ending_soon = tuple(
        _ENDING_SOON_FMT.format(
            game=e.game, title=e.title, days=(e.end - today).days, month=e.end.month, day=e.end.day
        )
        for e in live
        if 1 <= (e.end - today).days <= ENDING_SOON_WINDOW_DAYS
    )
    cleanup: tuple[str, ...] = ()
    weekly_items: tuple[str, ...] = ()
    monthly_items: tuple[str, ...] = ()
    if calendar.weekly is not None and today.weekday() == calendar.weekly.weekday:
        weekly_items = calendar.weekly.items
    if calendar.monthly is not None and is_last_day_of_month(today):
        monthly_items = calendar.monthly.items
    if weekly_items or monthly_items:
        parts = []
        if weekly_items:
            parts.append("周常清理：" + " / ".join(calendar.weekly.items))
        if monthly_items:
            parts.append("月常清理：" + " / ".join(calendar.monthly.items))
        cleanup = ("🧹 " + "；".join(parts),)
    return EveningSections(cleanup=cleanup, ending_today=ending_today, ending_soon=ending_soon)
