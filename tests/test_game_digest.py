"""Game digest message assembly tests (S6-GAME-03)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from qq_bot.config import BotSettings
from qq_bot.services.game_calendar import load_game_calendar, parse_game_calendar
from qq_bot.services.game_digest import (
    build_game_evening_message,
    build_game_morning_message,
    set_calendar,
)

TEMPLATE = Path("tests/fixtures/game_reports/game_calendar.template.json")


def test_morning_message_format():
    set_calendar(
        parse_game_calendar(
            {
                "schema_version": 1,
                "events": [
                    {
                        "game": "原神",
                        "kind": "version",
                        "title": "6.1版本更新",
                        "start": "2026-12-20",
                    },
                ],
            }
        )
    )
    text = build_game_morning_message(BotSettings(), today=date(2026, 12, 20))
    assert text == "【游戏早报】12月20日 周日\n🔴 今日版本更新\n· 原神 6.1版本更新 今日开服"


def test_evening_message_cleanup_and_ending():
    set_calendar(load_game_calendar(TEMPLATE))
    # 模板日历：跨年欢庆 end=2027-01-08，周常周日
    # 2027-01-06 周三：end-today==2 → 即将结束；无周常
    text = build_game_evening_message(BotSettings(), today=date(2027, 1, 6))
    assert "【游戏晚报】1月6日 周三" in text
    assert "「跨年欢庆」还有 2 天结束" in text
    assert "周常清理" not in text
    # 2027-01-03 周日：周常清理触发，活动不在窗口
    sunday_text = build_game_evening_message(BotSettings(), today=date(2027, 1, 3))
    assert "🧹 周常清理" in sunday_text
    assert "跨年欢庆" not in sunday_text


def test_empty_day_returns_none():
    set_calendar(parse_game_calendar({"schema_version": 1, "events": []}))
    assert build_game_morning_message(BotSettings(), today=date(2026, 9, 16)) is None
    assert build_game_evening_message(BotSettings(), today=date(2026, 9, 16)) is None
