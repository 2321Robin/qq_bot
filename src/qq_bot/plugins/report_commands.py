"""Manual life-report commands (S6-REPORT-07): /早报 and /晚报 trigger the
same builders the scheduled jobs use, into the invoking group. Sections that
fail simply stay absent; an empty source config yields a date-only report.
Explicit commands bypass the AI quota by design (same as /help, /精灵)."""

from nonebot import on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent

from qq_bot.config import get_settings
from qq_bot.observability import metrics
from qq_bot.services.daily_report import (
    build_life_evening_message,
    build_life_morning_message,
)
from qq_bot.services.onebot_send import finish_with_send_errors_logged

life_morning_command = on_command("早报", priority=5, block=True)
life_evening_command = on_command("晚报", priority=5, block=True)


def _resolve_report_client():
    """Shared runtime HTTP client; None lets every section degrade gracefully."""
    try:
        from qq_bot.runtime import get_http_client

        return get_http_client()
    except Exception:
        return None


async def _build_report(settings, builder):
    return await builder(settings, client=_resolve_report_client())


@life_morning_command.handle()
async def handle_life_morning(event: GroupMessageEvent) -> None:
    settings = get_settings()
    if not settings.group_allowed(event.group_id):
        return
    metrics.COMMANDS.labels("早报").inc()
    await finish_with_send_errors_logged(
        life_morning_command, await _build_report(settings, build_life_morning_message)
    )


@life_evening_command.handle()
async def handle_life_evening(event: GroupMessageEvent) -> None:
    settings = get_settings()
    if not settings.group_allowed(event.group_id):
        return
    metrics.COMMANDS.labels("晚报").inc()
    await finish_with_send_errors_logged(
        life_evening_command, await _build_report(settings, build_life_evening_message)
    )
