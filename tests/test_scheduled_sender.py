import pytest

from qq_bot.config import BotSettings
from qq_bot.services.scheduled_sender import (
    build_scheduler_job_kwargs,
    filter_allowed_group_ids,
    send_group_messages,
)


class FakeBot:
    def __init__(self, failing_group_ids: set[int] | None = None):
        self.failing_group_ids = failing_group_ids or set()
        self.sent: list[tuple[int, str]] = []

    async def send_group_msg(self, *, group_id: int, message: str) -> None:
        if group_id in self.failing_group_ids:
            raise RuntimeError("send failed")
        self.sent.append((group_id, message))


def test_build_scheduler_job_kwargs_uses_configured_time() -> None:
    settings = BotSettings(scheduled_cron_hour=8, scheduled_cron_minute=30)

    kwargs = build_scheduler_job_kwargs(settings)

    assert kwargs == {
        "trigger": "cron",
        "hour": 8,
        "minute": 30,
        "id": "daily_group_message",
        "replace_existing": True,
    }


def test_filter_allowed_group_ids_allows_all_when_allowlist_is_empty() -> None:
    settings = BotSettings(allowed_group_ids="")

    group_ids = filter_allowed_group_ids([1001, 1002], settings)

    assert group_ids == [1001, 1002]


def test_filter_allowed_group_ids_applies_configured_allowlist() -> None:
    settings = BotSettings(allowed_group_ids="1002,1004")

    group_ids = filter_allowed_group_ids([1001, 1002, 1003, 1004], settings)

    assert group_ids == [1002, 1004]


@pytest.mark.asyncio
async def test_send_group_messages_sends_to_each_group() -> None:
    bot = FakeBot()

    failures = await send_group_messages(bot, [1001, 1002], "早上好")

    assert failures == []
    assert bot.sent == [(1001, "早上好"), (1002, "早上好")]


@pytest.mark.asyncio
async def test_send_group_messages_continues_after_failure() -> None:
    bot = FakeBot(failing_group_ids={1002})

    failures = await send_group_messages(bot, [1001, 1002, 1003], "早上好")
    assert failures == [1002]
    assert bot.sent == [(1001, "早上好"), (1003, "早上好")]
