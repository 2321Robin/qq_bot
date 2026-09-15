"""Countdown section formatting with expiry hiding (S6-COUNT-02)."""

from __future__ import annotations

from datetime import date

from qq_bot.config import BotSettings
from qq_bot.services.countdown import (
    CountdownEntry,
    entries_from_settings,
    format_countdown_section,
)


def _entries(*dates: date) -> list[CountdownEntry]:
    return [CountdownEntry(f"考试{i}", d) for i, d in enumerate(dates, 1)]


def test_future_event_counts_days() -> None:
    text = format_countdown_section(_entries(date(2026, 12, 12)), today=date(2026, 9, 15))
    assert "距离考试1还有 88 天" in text


def test_one_day_out_event_counts_days() -> None:
    text = format_countdown_section(_entries(date(2026, 9, 16)), today=date(2026, 9, 15))
    assert "距离考试1还有 1 天" in text


def test_same_day_event() -> None:
    text = format_countdown_section(_entries(date(2026, 12, 12)), today=date(2026, 12, 12))
    assert "考试1就是今天！" in text


def test_past_event_hidden() -> None:
    text = format_countdown_section(_entries(date(2026, 1, 1)), today=date(2026, 9, 15))
    assert text == ""


def test_mixed_events_only_future_shown() -> None:
    text = format_countdown_section(
        _entries(date(2026, 1, 1), date(2027, 1, 5)), today=date(2026, 9, 15)
    )
    assert "考试1" not in text
    assert "距离考试2还有 112 天" in text


def test_empty_entries_return_empty_string() -> None:
    assert format_countdown_section([], today=date(2026, 9, 15)) == ""


def test_entries_from_settings_parses_configured_events() -> None:
    settings = BotSettings(countdown_events="六级考试:2026-12-12, 期末周:2027-01-05")
    assert entries_from_settings(settings) == (
        CountdownEntry(name="六级考试", event_date=date(2026, 12, 12)),
        CountdownEntry(name="期末周", event_date=date(2027, 1, 5)),
    )
