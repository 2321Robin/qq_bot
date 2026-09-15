"""Countdown section for life reports (S6-COUNT-02).

Pure date arithmetic over ``COUNTDOWN_EVENTS``; expired events are hidden
instead of being announced as "already passed".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from qq_bot.config import BotSettings


@dataclass(frozen=True)
class CountdownEntry:
    name: str
    event_date: date


def entries_from_settings(settings: BotSettings) -> tuple[CountdownEntry, ...]:
    return tuple(
        CountdownEntry(name=name, event_date=date.fromisoformat(date_text))
        for name, date_text in settings.countdown_event_list
    )


def _format_entry(entry: CountdownEntry, today: date) -> str | None:
    days = (entry.event_date - today).days
    if days < 0:
        return None  # 过期自动隐藏（S6-COUNT-02）
    if days == 0:
        return f"{entry.name}就是今天！"
    return f"距离{entry.name}还有 {days} 天"


def format_countdown_section(
    entries: Sequence[CountdownEntry], today: date | None = None
) -> str:
    effective_today = today if today is not None else date.today()
    lines = [
        line
        for line in (_format_entry(entry, effective_today) for entry in entries)
        if line is not None
    ]
    if not lines:
        return ""
    return "\n".join(["⏰ 倒计时", *lines])
