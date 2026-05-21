from pathlib import Path

import pytest
from pydantic import ValidationError

from qq_bot.config import BotSettings, get_settings, parse_id_list, parse_schedule_time_list, resolve_project_path


def test_parse_id_list_accepts_comma_separated_values() -> None:
    assert parse_id_list("1001, 1002,1003") == [1001, 1002, 1003]


def test_parse_id_list_accepts_empty_value() -> None:
    assert parse_id_list("") == []
    assert parse_id_list(None) == []


def test_parse_schedule_time_list_accepts_comma_separated_times() -> None:
    assert parse_schedule_time_list("11:00, 12:10,16:10,20:10") == [
        (11, 0),
        (12, 10),
        (16, 10),
        (20, 10),
    ]


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


def test_scheduled_cron_time_list_uses_multi_time_config() -> None:
    settings = BotSettings(
        scheduled_cron_hour=9,
        scheduled_cron_minute=0,
        scheduled_cron_times="11:00,12:10,16:10,20:10",
    )

    assert settings.scheduled_cron_time_list == [
        (11, 0),
        (12, 10),
        (16, 10),
        (20, 10),
    ]


def test_scheduled_cron_time_list_falls_back_to_single_time_config() -> None:
    settings = BotSettings(
        scheduled_cron_times="",
        scheduled_cron_hour=8,
        scheduled_cron_minute=30,
    )

    assert settings.scheduled_cron_time_list == [(8, 30)]


def test_ai_api_key_is_hidden_from_settings_repr() -> None:
    assert "secret-token" not in repr(BotSettings(ai_api_key="secret-token"))


def test_normalized_ai_base_url_strips_whitespace_and_trailing_slash() -> None:
    settings = BotSettings(ai_base_url=" https://api.example.com/v1/ ")

    assert settings.normalized_ai_base_url == "https://api.example.com/v1"


def test_has_ai_config_requires_non_empty_key() -> None:
    assert BotSettings(ai_api_key="secret-token").has_ai_config()
    assert not BotSettings(ai_api_key="   ").has_ai_config()


def test_ai_fallback_settings_are_exposed_and_secret_is_hidden() -> None:
    settings = BotSettings(
        ai_fallback_api_key="fallback-secret",
        ai_fallback_base_url="https://open.bigmodel.cn/api/paas/v4",
        ai_fallback_model="glm-4-flash",
    )

    assert settings.ai_fallback_api_key == "fallback-secret"
    assert settings.ai_fallback_base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert settings.ai_fallback_model == "glm-4-flash"
    assert "fallback-secret" not in repr(settings)


def test_normalized_ai_fallback_base_url_strips_whitespace_and_trailing_slash() -> None:
    settings = BotSettings(ai_fallback_base_url=" https://fallback.example.com/v1/ ")

    assert settings.normalized_ai_fallback_base_url == "https://fallback.example.com/v1"


def test_has_ai_fallback_config_requires_non_empty_key() -> None:
    assert BotSettings(ai_fallback_api_key="fallback-secret").has_ai_fallback_config()
    assert not BotSettings(ai_fallback_api_key="   ").has_ai_fallback_config()


def test_get_settings_loads_environment_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    try:
        monkeypatch.setenv("AI_API_KEY", "env-token")

        first = get_settings()
        monkeypatch.setenv("AI_API_KEY", "changed-token")
        second = get_settings()

        assert first.ai_api_key == "env-token"
        assert second is first
    finally:
        get_settings.cache_clear()


def test_invalid_id_list_raises_validation_error() -> None:
    with pytest.raises(ValidationError, match="comma-separated integers"):
        BotSettings(allowed_group_ids="123,abc")


def test_invalid_schedule_time_raises_validation_error() -> None:
    with pytest.raises(ValidationError, match="scheduled_cron_hour"):
        BotSettings(scheduled_cron_hour=24)

    with pytest.raises(ValidationError, match="scheduled_cron_minute"):
        BotSettings(scheduled_cron_minute=60)

    with pytest.raises(ValidationError, match="scheduled_cron_times"):
        BotSettings(scheduled_cron_times="11:00,25:10")


def test_search_settings_are_exposed_and_secret_is_hidden() -> None:
    settings = BotSettings(
        search_enabled=True,
        tavily_api_key="tvly-secret",
        search_max_results=3,
        search_timeout_seconds=7,
    )

    assert settings.search_enabled is True
    assert settings.tavily_api_key == "tvly-secret"
    assert settings.search_max_results == 3
    assert settings.search_timeout_seconds == 7
    assert "tvly-secret" not in repr(settings)


def test_has_search_config_requires_enabled_and_key() -> None:
    assert BotSettings(search_enabled=True, tavily_api_key="tvly-secret").has_search_config()
    assert not BotSettings(search_enabled=False, tavily_api_key="tvly-secret").has_search_config()
    assert not BotSettings(search_enabled=True, tavily_api_key="   ").has_search_config()


def test_invalid_search_limits_raise_validation_error() -> None:
    with pytest.raises(ValidationError, match="search_max_results"):
        BotSettings(search_max_results=0)

    with pytest.raises(ValidationError, match="search_timeout_seconds"):
        BotSettings(search_timeout_seconds=0)


def test_chat_memory_settings_are_exposed() -> None:
    settings = BotSettings(
        chat_memory_path="data/test-memory.sqlite3",
        chat_memory_retention_days=3,
        chat_memory_default_turns=10,
        chat_memory_max_results=20,
    )

    assert settings.chat_memory_path == "data/test-memory.sqlite3"
    assert settings.chat_memory_retention_days == 3
    assert settings.chat_memory_default_turns == 10
    assert settings.chat_memory_max_results == 20


def test_roco_counter_settings_have_defaults() -> None:
    settings = BotSettings()

    assert settings.roco_counter_path == "data/roco_counter.sqlite3"
    assert settings.roco_counter_season == "S2"


def test_roco_counter_path_resolves_from_project_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    settings = BotSettings()

    assert settings.resolved_roco_counter_path == resolve_project_path("data/roco_counter.sqlite3")
    assert settings.resolved_roco_counter_path != tmp_path / "data" / "roco_counter.sqlite3"


def test_roco_counter_season_is_stripped() -> None:
    settings = BotSettings(roco_counter_season=" S3 ")

    assert settings.roco_counter_season == "S3"


def test_chat_memory_settings_validate_positive_limits() -> None:
    with pytest.raises(ValidationError, match="chat_memory_retention_days"):
        BotSettings(chat_memory_retention_days=0)

    with pytest.raises(ValidationError, match="chat_memory_default_turns"):
        BotSettings(chat_memory_default_turns=0)

    with pytest.raises(ValidationError, match="chat_memory_max_results"):
        BotSettings(chat_memory_max_results=0)
