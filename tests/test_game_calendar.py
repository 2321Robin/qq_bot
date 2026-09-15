"""Game calendar schema parsing and rule engine tests (S6-GAME-01/02)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from qq_bot.services.game_calendar import (
    GameCalendar,
    GameCalendarError,
    _merge_cleanup,
    evening_sections,
    load_game_calendar,
    morning_sections,
    parse_game_calendar,
)

TEMPLATE = Path("tests/fixtures/game_reports/game_calendar.template.json")


def test_template_loads():
    calendar = load_game_calendar(TEMPLATE)
    assert len(calendar.events) == 2
    assert calendar.weekly is not None and calendar.weekly.weekday == 6
    assert calendar.monthly is not None


@pytest.mark.parametrize(
    "mutation, match",
    [
        ({"schema_version": 2}, "schema_version"),
        (lambda d: d["events"][0].__setitem__("game", "王者荣耀"), "game"),
        (lambda d: d["events"][1].__setitem__("end", "2026-12-01"), "end"),  # end < start
        (
            lambda d: d["events"][0].__setitem__("end", "2026-10-22"),
            "version",
        ),  # version 不应有 end
        (lambda d: d["events"][0].__setitem__("start", "2026/10/21"), "start"),
        (lambda d: d["events"].append(dict(d["events"][0])), "duplicate"),
        (lambda d: d.__setitem__("weekly", {"day": "funday", "items": ["x"]}), "day"),
        (lambda d: d.__setitem__("monthly", {"trigger": "first_day", "items": ["x"]}), "trigger"),
    ],
)
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


TODAY = date(2026, 12, 20)  # 周日；2026-12-31 月末另测


def test_morning_version_today_top():
    cal = parse_game_calendar(
        {
            "schema_version": 1,
            "events": [
                {"game": "原神", "kind": "version", "title": "6.1版本更新", "start": "2026-12-20"},
            ],
        }
    )
    sections = morning_sections(cal, today=TODAY)
    assert sections.versions == ("原神 6.1版本更新 今日开服",)
    assert not sections.is_empty()


def test_version_past_hidden_and_not_in_evening():
    cal = parse_game_calendar(
        {
            "schema_version": 1,
            "events": [
                {"game": "原神", "kind": "version", "title": "6.0版本更新", "start": "2026-12-19"},
            ],
        }
    )
    assert morning_sections(cal, today=TODAY).is_empty()
    assert evening_sections(cal, today=TODAY).is_empty()


def test_event_window_boundaries():
    def cal_with_end(end: str) -> GameCalendar:
        return parse_game_calendar(
            {
                "schema_version": 1,
                "events": [
                    {
                        "game": "明日方舟",
                        "kind": "event",
                        "title": "活动X",
                        "start": "2026-12-01",
                        "end": end,
                    },
                ],
            }
        )

    # end-today == 3 → 不可见；== 2 与 == 1 → 即将结束；== 0 → 今日结束
    assert evening_sections(cal_with_end("2026-12-23"), today=TODAY).is_empty()
    assert evening_sections(cal_with_end("2026-12-22"), today=TODAY).ending_soon == (
        "明日方舟 「活动X」还有 2 天结束（12月22日截止）",
    )
    assert evening_sections(cal_with_end("2026-12-21"), today=TODAY).ending_soon != ()
    assert evening_sections(cal_with_end("2026-12-20"), today=TODAY).ending_today == (
        "明日方舟 「活动X」今日结束",
    )


def test_event_start_today_morning_only():
    cal = parse_game_calendar(
        {
            "schema_version": 1,
            "events": [
                {
                    "game": "原神",
                    "kind": "event",
                    "title": "新活动",
                    "start": "2026-12-20",
                    "end": "2027-01-10",
                },
            ],
        }
    )
    assert morning_sections(cal, today=TODAY).starting == ("原神 「新活动」今日开启",)
    assert evening_sections(cal, today=TODAY).is_empty()


def test_weekly_sunday_cleanup():
    cal = parse_game_calendar(
        {
            "schema_version": 1,
            "events": [],
            "weekly": {"day": "sunday", "items": ["原神周本", "星穹铁道差分宇宙"]},
        }
    )
    evening = evening_sections(cal, today=TODAY)
    assert evening.cleanup == ("🧹 周常清理：原神周本 / 星穹铁道差分宇宙",)


def test_monthly_last_day_only():
    cal = parse_game_calendar(
        {
            "schema_version": 1,
            "events": [],
            "monthly": {"trigger": "last_day", "items": ["原神幻想真境剧诗"]},
        }
    )
    assert evening_sections(cal, today=date(2026, 12, 31)).cleanup == (
        "🧹 月常清理：原神幻想真境剧诗",
    )
    assert evening_sections(cal, today=date(2026, 12, 30)).is_empty()
    assert evening_sections(cal, today=date(2027, 2, 28)).cleanup != ()  # 平年二月末
    assert evening_sections(cal, today=date(2028, 2, 29)).cleanup != ()  # 闰年二月末


def test_weekly_monthly_merge_dedup():
    # 2027-01-31：经 calendar.monthrange 验证，既是周日又是当月最后一天
    cal = parse_game_calendar(
        {
            "schema_version": 1,
            "events": [],
            "weekly": {"day": "sunday", "items": ["原神周本", "剿灭"]},
            "monthly": {"trigger": "last_day", "items": ["剿灭", "黄票商店"]},
        }
    )
    evening = evening_sections(cal, today=date(2027, 1, 31))
    assert evening.cleanup == ("🧹 周常清理：原神周本 / 剿灭；月常清理：剿灭 / 黄票商店",)

    # 纯合并逻辑单测：去重保序（周常在前）
    assert _merge_cleanup(("a", "b"), ("b", "c")) == ("a", "b", "c")
    # 非重合日互不触发：2027-01-30 是周六
    assert evening_sections(cal, today=date(2027, 1, 30)).is_empty()
