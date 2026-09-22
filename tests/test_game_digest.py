"""Game digest message assembly tests (S6-GAME-03)."""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import pytest

from qq_bot.config import BotSettings
from qq_bot.services.game_calendar import (
    GameCalendarError,
    load_game_calendar,
    parse_game_calendar,
)
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
    assert text == "【游戏早报】12月20日 周日\n🔴 今日版本更新\n1. 原神 6.1版本更新 今日开服"


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


def _write_calendar(path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_hot_reload_picks_up_file_change(tmp_path):
    """S9-GAMECAL-SYNC:本地同步工具改写日历文件后,构建消息时自动用新数据。"""
    calendar_path = tmp_path / "game_calendar.json"
    _write_calendar(
        calendar_path,
        {
            "schema_version": 1,
            "events": [
                {"game": "原神", "kind": "version", "title": "7.0版本", "start": "2026-08-01"}
            ],
        },
    )
    set_calendar(load_game_calendar(calendar_path), path=calendar_path)
    assert build_game_morning_message(BotSettings(), today=date(2026, 8, 1)) is not None

    _write_calendar(
        calendar_path,
        {
            "schema_version": 1,
            "events": [
                {"game": "原神", "kind": "version", "title": "7.1版本", "start": "2026-09-23"}
            ],
        },
    )
    os.utime(calendar_path, (2000000000, 2000000000))  # 确保跨过 mtime 判定
    text = build_game_morning_message(BotSettings(), today=date(2026, 9, 23))
    assert "7.1版本" in text
    assert "7.0版本" not in text


def test_hot_reload_fails_loudly_on_broken_file(tmp_path):
    calendar_path = tmp_path / "game_calendar.json"
    _write_calendar(
        calendar_path,
        {
            "schema_version": 1,
            "events": [
                {"game": "原神", "kind": "version", "title": "7.1版本", "start": "2026-09-23"}
            ],
        },
    )
    set_calendar(load_game_calendar(calendar_path), path=calendar_path)
    calendar_path.write_text("{not json", encoding="utf-8")
    os.utime(calendar_path, (2000000000, 2000000000))
    with pytest.raises(GameCalendarError):
        build_game_morning_message(BotSettings(), today=date(2026, 9, 23))


def test_no_path_skips_reload_check():
    set_calendar(parse_game_calendar({"schema_version": 1, "events": []}))
    # 无 path 时热重载是 no-op,不抛错
    assert build_game_morning_message(BotSettings(), today=date(2026, 9, 16)) is None
