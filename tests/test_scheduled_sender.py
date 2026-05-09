import pytest
from nonebot.adapters.onebot.v11 import Message

from qq_bot.config import BotSettings
from qq_bot.services.scheduled_sender import (
    build_scheduler_jobs_kwargs,
    build_scheduler_job_kwargs,
    filter_allowed_group_ids,
    send_group_messages,
)


class FakeBot:
    def __init__(self, failing_group_ids: set[int] | None = None):
        self.failing_group_ids = failing_group_ids or set()
        self.sent: list[tuple[int, object]] = []

    async def send_group_msg(self, *, group_id: int, message: object) -> None:
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


def test_build_scheduler_jobs_kwargs_uses_configured_times() -> None:
    settings = BotSettings(scheduled_cron_times="11:00,12:10,16:10,20:10")

    kwargs = build_scheduler_jobs_kwargs(settings)

    assert kwargs == [
        {
            "trigger": "cron",
            "hour": 11,
            "minute": 0,
            "id": "daily_group_message_1100",
            "replace_existing": True,
        },
        {
            "trigger": "cron",
            "hour": 12,
            "minute": 10,
            "id": "daily_group_message_1210",
            "replace_existing": True,
        },
        {
            "trigger": "cron",
            "hour": 16,
            "minute": 10,
            "id": "daily_group_message_1610",
            "replace_existing": True,
        },
        {
            "trigger": "cron",
            "hour": 20,
            "minute": 10,
            "id": "daily_group_message_2010",
            "replace_existing": True,
        },
    ]


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
    assert bot.sent[0][0] == 1001
    assert isinstance(bot.sent[0][1], Message)
    assert bot.sent[0][1].extract_plain_text() == "早上好"
    assert bot.sent[1][0] == 1002
    assert isinstance(bot.sent[1][1], Message)
    assert bot.sent[1][1].extract_plain_text() == "早上好"


@pytest.mark.asyncio
async def test_send_group_messages_replaces_named_mentions() -> None:
    bot = FakeBot()

    failures = await send_group_messages(bot, [1001], "@小呱呱 /远行商人")

    assert failures == []
    sent_message = bot.sent[0][1]
    assert isinstance(sent_message, Message)
    assert sent_message[0].type == "at"
    assert sent_message[0].data["qq"] == "2854203313"
    assert sent_message[1].type == "text"
    assert sent_message[1].data["text"] == " /远行商人"


@pytest.mark.asyncio
async def test_send_group_messages_continues_after_failure() -> None:
    bot = FakeBot(failing_group_ids={1002})

    failures = await send_group_messages(bot, [1001, 1002, 1003], "早上好")
    assert failures == [1002]
    assert bot.sent[0][0] == 1001
    assert isinstance(bot.sent[0][1], Message)
    assert bot.sent[0][1].extract_plain_text() == "早上好"
    assert bot.sent[1][0] == 1003
    assert isinstance(bot.sent[1][1], Message)
    assert bot.sent[1][1].extract_plain_text() == "早上好"
