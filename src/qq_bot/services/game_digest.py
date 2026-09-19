"""Game digest message assembly (S6-GAME-03).

Holds the module-level calendar snapshot loaded by the scheduler plugin at
startup and renders the morning/evening digests from rule-engine sections.
Empty days return ``None`` so the dispatch layer skips sending entirely
(no "nothing today" noise).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from qq_bot.config import BotSettings
from qq_bot.services.game_calendar import (
    GameCalendar,
    GameCalendarError,
    evening_sections,
    morning_sections,
)

_WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

_CALENDAR: GameCalendar | None = None


def set_calendar(calendar: GameCalendar) -> None:
    global _CALENDAR
    _CALENDAR = calendar


def _require_calendar() -> GameCalendar:
    if _CALENDAR is None:
        raise GameCalendarError("game calendar not loaded; register game jobs first")
    return _CALENDAR


def _calendar_ready() -> bool:
    return _CALENDAR is not None


def _header(kind: str, today: date) -> str:
    return f"【游戏{kind}】{today.month}月{today.day}日 {_WEEKDAY_CN[today.weekday()]}"


def _numbered(items: Sequence[str]) -> list[str]:
    # QQ 纯文本不渲染 -/· 列表符，统一数字编号（与早晚报一致）
    return [f"{index}. {item}" for index, item in enumerate(items, start=1)]


def build_game_morning_message(settings: BotSettings, *, today: date | None = None) -> str | None:
    effective_today = today or date.today()
    sections = morning_sections(_require_calendar(), effective_today)
    if sections.is_empty():
        return None
    lines = [_header("早报", effective_today)]
    if sections.versions:
        lines += ["🔴 今日版本更新", *_numbered(sections.versions)]
    if sections.starting:
        lines += ["📅 今日开启", *_numbered(sections.starting)]
    return "\n".join(lines)


def build_game_evening_message(settings: BotSettings, *, today: date | None = None) -> str | None:
    effective_today = today or date.today()
    sections = evening_sections(_require_calendar(), effective_today)
    if sections.is_empty():
        return None
    lines = [_header("晚报", effective_today)]
    if sections.cleanup:
        lines += [sections.cleanup[0]]
    if sections.ending_today:
        lines += ["⛔ 今日结束", *_numbered(sections.ending_today)]
    if sections.ending_soon:
        lines += ["⏳ 即将结束", *_numbered(sections.ending_soon)]
    return "\n".join(lines)
