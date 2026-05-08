import pytest
from pydantic import ValidationError

from qq_bot.config import BotSettings, parse_id_list


def test_parse_id_list_accepts_comma_separated_values() -> None:
    assert parse_id_list("1001, 1002,1003") == [1001, 1002, 1003]


def test_parse_id_list_accepts_empty_value() -> None:
    assert parse_id_list("") == []
    assert parse_id_list(None) == []


def test_settings_expose_group_id_lists() -> None:
    settings = BotSettings(
        allowed_group_ids="1001,1002",
        admin_user_ids="2001",
        scheduled_group_ids="3001, 3002",
    )

    assert settings.allowed_group_id_list == [1001, 1002]
    assert settings.admin_user_id_list == [2001]
    assert settings.scheduled_group_id_list == [3001, 3002]


def test_empty_allowed_group_list_allows_any_group() -> None:
    settings = BotSettings(allowed_group_ids="")

    assert settings.group_allowed(123456)


def test_allowed_group_list_blocks_unknown_group() -> None:
    settings = BotSettings(allowed_group_ids="123456")

    assert settings.group_allowed(123456)
    assert not settings.group_allowed(999999)


def test_scheduled_enabled_requires_group_and_message() -> None:
    disabled = BotSettings(scheduled_group_ids="", scheduled_message="hello")
    empty_message = BotSettings(scheduled_group_ids="123456", scheduled_message="")
    whitespace_message = BotSettings(scheduled_group_ids="123456", scheduled_message="   ")
    enabled = BotSettings(scheduled_group_ids="123456", scheduled_message="hello")

    assert not disabled.scheduled_enabled()
    assert not empty_message.scheduled_enabled()
    assert not whitespace_message.scheduled_enabled()
    assert enabled.scheduled_enabled()


def test_invalid_id_list_raises_validation_error() -> None:
    with pytest.raises(ValidationError, match="comma-separated integers"):
        BotSettings(allowed_group_ids="123,abc")


def test_invalid_schedule_time_raises_validation_error() -> None:
    with pytest.raises(ValidationError, match="scheduled_cron_hour"):
        BotSettings(scheduled_cron_hour=24)

    with pytest.raises(ValidationError, match="scheduled_cron_minute"):
        BotSettings(scheduled_cron_minute=60)
