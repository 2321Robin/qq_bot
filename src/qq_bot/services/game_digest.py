"""Game digest message assembly (S6-GAME-03).

Holds the module-level calendar snapshot loaded by the scheduler plugin at
startup and renders the morning/evening digests from rule-engine sections.
Empty days return ``None`` so the dispatch layer skips sending entirely
(no "nothing today" noise).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from pathlib import Path

from qq_bot.config import BotSettings
from qq_bot.services.game_calendar import (
    GameCalendar,
    GameCalendarError,
    evening_sections,
    load_game_calendar,
    morning_sections,
)

_WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

_CALENDAR: GameCalendar | None = None
_CALENDAR_PATH: Path | None = None
_CALENDAR_MTIME: float | None = None


def set_calendar(calendar: GameCalendar, *, path: Path | None = None) -> None:
    global _CALENDAR, _CALENDAR_PATH, _CALENDAR_MTIME
    _CALENDAR = calendar
    _CALENDAR_PATH = path
    _CALENDAR_MTIME = _current_mtime(path)


def _current_mtime(path: Path | None) -> float | None:
    if path is None:
        return None
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _refresh_if_changed() -> None:
    """本地日历同步工具改写文件后，下一条消息自动使用新数据（S9-GAMECAL-SYNC）。

    与启动语义一致：换到的文件非法就抛 GameCalendarError 响亮失败，
    绝不带着旧快照静默发送过期提醒。
    """
    if _CALENDAR_PATH is None:
        return
    mtime = _current_mtime(_CALENDAR_PATH)
    if mtime is None or mtime == _CALENDAR_MTIME:
        return
    set_calendar(load_game_calendar(_CALENDAR_PATH), path=_CALENDAR_PATH)


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
    _refresh_if_changed()
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
    _refresh_if_changed()
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
