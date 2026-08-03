import pytest
from pydantic import ValidationError

from qq_bot.config import (
    parse_named_mention_replacements,
    BotSettings,
    get_settings,
    parse_id_list,
    parse_schedule_time_list,
)


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


def test_chat_memory_settings_validate_positive_limits() -> None:
    with pytest.raises(ValidationError, match="chat_memory_retention_days"):
        BotSettings(chat_memory_retention_days=0)

    with pytest.raises(ValidationError, match="chat_memory_default_turns"):
        BotSettings(chat_memory_default_turns=0)

    with pytest.raises(ValidationError, match="chat_memory_max_results"):
        BotSettings(chat_memory_max_results=0)


def test_reliability_settings_defaults() -> None:
    settings = BotSettings()
    assert settings.ai_max_attempts == 2
    assert settings.ai_retry_base_delay_seconds == 0.5
    assert settings.ai_retry_max_delay_seconds == 4.0
    assert settings.search_max_attempts == 3
    assert settings.search_retry_base_delay_seconds == 0.5
    assert settings.search_retry_max_delay_seconds == 4.0
    assert settings.send_max_attempts == 2
    assert settings.send_retry_base_delay_seconds == 0.5
    assert settings.send_retry_max_delay_seconds == 3.0
    assert settings.retry_jitter_ratio == 0.1
    assert settings.breaker_failure_threshold == 3
    assert settings.breaker_recovery_seconds == 30.0


def test_reliability_settings_reject_invalid_values() -> None:
    with pytest.raises(ValidationError, match="max attempts"):
        BotSettings(ai_max_attempts=0)
    with pytest.raises(ValidationError, match="max attempts"):
        BotSettings(send_max_attempts=-1)
    with pytest.raises(ValidationError, match="greater than 0"):
        BotSettings(ai_retry_base_delay_seconds=0)
    with pytest.raises(ValidationError, match="greater than 0"):
        BotSettings(breaker_recovery_seconds=-5)
    with pytest.raises(ValidationError, match="between 0 and 1"):
        BotSettings(retry_jitter_ratio=-0.1)
    with pytest.raises(ValidationError, match="between 0 and 1"):
        BotSettings(retry_jitter_ratio=1.5)
    with pytest.raises(ValidationError, match="breaker_failure_threshold"):
        BotSettings(breaker_failure_threshold=0)


def test_reliability_base_delay_must_not_exceed_max() -> None:
    with pytest.raises(ValidationError, match="must not exceed"):
        BotSettings(ai_retry_base_delay_seconds=5.0, ai_retry_max_delay_seconds=2.0)
    with pytest.raises(ValidationError, match="must not exceed"):
        BotSettings(send_retry_base_delay_seconds=9.0, send_retry_max_delay_seconds=3.0)
    # equal values are allowed (no backoff growth)
    BotSettings(ai_retry_base_delay_seconds=2.0, ai_retry_max_delay_seconds=2.0)


def test_parse_named_mention_replacements_accepts_pairs() -> None:
    assert parse_named_mention_replacements("@小呱呱=2880000001, @提醒=2880000002") == {
        "@小呱呱": "2880000001",
        "@提醒": "2880000002",
    }


def test_parse_named_mention_replacements_accepts_empty_value() -> None:
    assert parse_named_mention_replacements("") == {}
    assert parse_named_mention_replacements(None) == {}


def test_named_mention_replacements_reject_non_integer_accounts() -> None:
    with pytest.raises(ValidationError, match="name=qq"):
        BotSettings(named_mention_replacements="@小呱呱=abc")


def test_named_mention_replacements_reject_missing_equals() -> None:
    with pytest.raises(ValidationError, match="name=qq"):
        BotSettings(named_mention_replacements="@小呱呱")


def test_named_mention_replacement_map_property() -> None:
    settings = BotSettings(named_mention_replacements="@小呱呱=2880000001")
    assert settings.named_mention_replacement_map == {"@小呱呱": "2880000001"}
    assert BotSettings().named_mention_replacement_map == {}


def test_agent_settings_defaults() -> None:
    settings = BotSettings()
    assert settings.agent_enabled is False
    assert settings.ai_router_model == ""
    assert settings.ai_router_confidence_threshold == 0.75
    assert settings.agent_max_rounds == 3
    assert settings.agent_max_tool_calls == 4
    assert settings.agent_tools_per_round == 2
    assert settings.agent_deadline_seconds == 60.0
    assert settings.ai_provider_tools_enabled is True
    assert settings.ai_provider_structured_output_enabled is True
    assert settings.ai_semantic_verifier_enabled is False
    assert settings.ai_verifier_model == ""
    assert settings.ai_context_window_tokens == 128000
    assert settings.ai_output_reserve_tokens == 2048
    assert settings.ai_token_safety_margin == 1024
    assert settings.agent_budget_local_ratio == 0.30
    assert settings.agent_budget_web_ratio == 0.25
    assert settings.agent_budget_recent_ratio == 0.15
    assert settings.agent_budget_summary_ratio == 0.10
    assert settings.agent_budget_preference_max_tokens == 256


def test_agent_settings_parse_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_ENABLED", "true")
    monkeypatch.setenv("AI_ROUTER_MODEL", "router-mini")
    monkeypatch.setenv("AI_ROUTER_CONFIDENCE_THRESHOLD", "0.6")
    monkeypatch.setenv("AGENT_MAX_ROUNDS", "5")
    monkeypatch.setenv("AGENT_MAX_TOOL_CALLS", "6")
    monkeypatch.setenv("AGENT_TOOLS_PER_ROUND", "3")
    monkeypatch.setenv("AGENT_DEADLINE_SECONDS", "45")
    monkeypatch.setenv("AI_SEMANTIC_VERIFIER_ENABLED", "true")
    monkeypatch.setenv("AI_VERIFIER_MODEL", "verifier-mini")
    monkeypatch.setenv("AI_CONTEXT_WINDOW_TOKENS", "64000")
    monkeypatch.setenv("AI_OUTPUT_RESERVE_TOKENS", "2048")
    monkeypatch.setenv("AI_TOKEN_SAFETY_MARGIN", "1024")
    monkeypatch.setenv("AGENT_BUDGET_LOCAL_RATIO", "0.4")
    monkeypatch.setenv("AGENT_BUDGET_WEB_RATIO", "0.2")
    monkeypatch.setenv("AGENT_BUDGET_RECENT_RATIO", "0.1")
    monkeypatch.setenv("AGENT_BUDGET_SUMMARY_RATIO", "0.05")
    monkeypatch.setenv("AGENT_BUDGET_PREFERENCE_MAX_TOKENS", "128")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.agent_enabled is True
        assert settings.ai_router_model == "router-mini"
        assert settings.ai_router_confidence_threshold == 0.6
        assert settings.agent_max_rounds == 5
        assert settings.agent_max_tool_calls == 6
        assert settings.agent_tools_per_round == 3
        assert settings.agent_deadline_seconds == 45.0
        assert settings.ai_semantic_verifier_enabled is True
        assert settings.ai_verifier_model == "verifier-mini"
        assert settings.ai_context_window_tokens == 64000
        assert settings.ai_output_reserve_tokens == 2048
        assert settings.ai_token_safety_margin == 1024
        assert settings.agent_budget_local_ratio == 0.4
        assert settings.agent_budget_web_ratio == 0.2
        assert settings.agent_budget_recent_ratio == 0.1
        assert settings.agent_budget_summary_ratio == 0.05
        assert settings.agent_budget_preference_max_tokens == 128
    finally:
        get_settings.cache_clear()


def test_agent_settings_reject_out_of_range_threshold() -> None:
    with pytest.raises(ValidationError, match="between 0 and 1"):
        BotSettings(ai_router_confidence_threshold=1.5)
    with pytest.raises(ValidationError, match="between 0 and 1"):
        BotSettings(ai_router_confidence_threshold=-0.1)


def test_agent_settings_reject_non_positive_limits() -> None:
    with pytest.raises(ValidationError, match="positive integer"):
        BotSettings(agent_max_rounds=0)
    with pytest.raises(ValidationError, match="positive integer"):
        BotSettings(agent_max_tool_calls=0)
    with pytest.raises(ValidationError, match="positive integer"):
        BotSettings(agent_tools_per_round=0)
    with pytest.raises(ValidationError, match="greater than 0"):
        BotSettings(agent_deadline_seconds=0)


def test_token_budget_relations_validated() -> None:
    with pytest.raises(ValidationError, match="must exceed output reserve"):
        BotSettings(ai_context_window_tokens=3000, ai_output_reserve_tokens=2048)
    with pytest.raises(ValidationError, match="non-negative and sum to at most 1"):
        BotSettings(agent_budget_local_ratio=0.6, agent_budget_web_ratio=0.5)
    with pytest.raises(ValidationError, match="non-negative and sum to at most 1"):
        BotSettings(agent_budget_local_ratio=-0.1)
    with pytest.raises(ValidationError, match="positive"):
        BotSettings(agent_budget_preference_max_tokens=0)


def test_agent_settings_repr_does_not_leak_secrets() -> None:
    settings = BotSettings(ai_api_key="agent-secret-key", ai_fallback_api_key="agent-fallback")
    text = repr(settings)
    assert "agent-secret-key" not in text
    assert "agent-fallback" not in text


def test_data_pipeline_settings_defaults() -> None:
    settings = BotSettings()
    assert settings.data_min_records == 500
    assert settings.data_max_record_drop == 30
    assert settings.data_max_new_number_gaps == 0
    assert settings.data_min_stats_complete_rate == 0.80
    assert settings.data_min_total_race_rate == 0.95
    assert settings.data_max_dangling_edges == 0
    assert settings.data_max_skill_key_missing_rate == 0.005
    assert settings.data_search_index_path == "data/roco_search.sqlite3"
    assert settings.data_use_search_index is True


def test_data_pipeline_settings_parse_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_USE_SEARCH_INDEX", "false")
    monkeypatch.setenv("DATA_SEARCH_INDEX_PATH", "data/custom_index.sqlite3")
    monkeypatch.setenv("DATA_MIN_RECORDS", "600")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.data_use_search_index is False
        assert settings.data_search_index_path == "data/custom_index.sqlite3"
        assert settings.data_min_records == 600
    finally:
        monkeypatch.delenv("DATA_USE_SEARCH_INDEX")
        monkeypatch.delenv("DATA_SEARCH_INDEX_PATH")
        monkeypatch.delenv("DATA_MIN_RECORDS")
        get_settings.cache_clear()


def test_data_pipeline_settings_reject_out_of_range_rates() -> None:
    with pytest.raises(ValidationError, match="between 0 and 1"):
        BotSettings(data_min_stats_complete_rate=1.5)
    with pytest.raises(ValidationError, match="between 0 and 1"):
        BotSettings(data_min_total_race_rate=-0.1)
    with pytest.raises(ValidationError, match="between 0 and 1"):
        BotSettings(data_max_skill_key_missing_rate=2.0)


def test_data_pipeline_settings_reject_negative_int_thresholds() -> None:
    with pytest.raises(ValidationError, match="non-negative"):
        BotSettings(data_max_record_drop=-1)
    with pytest.raises(ValidationError, match="non-negative"):
        BotSettings(data_min_records=-1)
    with pytest.raises(ValidationError, match="non-negative"):
        BotSettings(data_max_new_number_gaps=-1)
    with pytest.raises(ValidationError, match="non-negative"):
        BotSettings(data_max_dangling_edges=-1)


def test_data_pipeline_settings_reject_empty_index_path() -> None:
    with pytest.raises(ValidationError, match="non-empty"):
        BotSettings(data_search_index_path="   ")


def test_observability_settings_defaults() -> None:
    settings = BotSettings()
    assert settings.log_format == "text"
    assert settings.log_level == "INFO"
    assert settings.metrics_enabled is True
    assert settings.trace_enabled is True
    assert settings.readyz_require_onebot is False
    assert settings.readyz_require_data is True
    assert settings.quota_enabled is True
    assert settings.quota_rate_limit_per_minute == 30
    assert settings.quota_daily_cost_limit_usd == 2.0
    assert settings.quota_group_daily_cost_limit_usd == 0.5


def test_observability_settings_parse_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.setenv("METRICS_ENABLED", "false")
    monkeypatch.setenv("TRACE_ENABLED", "false")
    monkeypatch.setenv("READYZ_REQUIRE_ONEBOT", "true")
    monkeypatch.setenv("READYZ_REQUIRE_DATA", "false")
    monkeypatch.setenv("QUOTA_ENABLED", "false")
    monkeypatch.setenv("QUOTA_RATE_LIMIT_PER_MINUTE", "10")
    monkeypatch.setenv("QUOTA_DAILY_COST_LIMIT_USD", "3.5")
    monkeypatch.setenv("QUOTA_GROUP_DAILY_COST_LIMIT_USD", "0.25")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.log_format == "json"
        assert settings.log_level == "WARNING"
        assert settings.metrics_enabled is False
        assert settings.trace_enabled is False
        assert settings.readyz_require_onebot is True
        assert settings.readyz_require_data is False
        assert settings.quota_enabled is False
        assert settings.quota_rate_limit_per_minute == 10
        assert settings.quota_daily_cost_limit_usd == 3.5
        assert settings.quota_group_daily_cost_limit_usd == 0.25
    finally:
        for key in (
            "LOG_FORMAT",
            "LOG_LEVEL",
            "METRICS_ENABLED",
            "TRACE_ENABLED",
            "READYZ_REQUIRE_ONEBOT",
            "READYZ_REQUIRE_DATA",
            "QUOTA_ENABLED",
            "QUOTA_RATE_LIMIT_PER_MINUTE",
            "QUOTA_DAILY_COST_LIMIT_USD",
            "QUOTA_GROUP_DAILY_COST_LIMIT_USD",
        ):
            monkeypatch.delenv(key)
        get_settings.cache_clear()


def test_observability_settings_reject_invalid_format_and_level() -> None:
    with pytest.raises(ValidationError, match="log_format"):
        BotSettings(log_format="yaml")
    with pytest.raises(ValidationError, match="log_level"):
        BotSettings(log_level="TRACE")


def test_quota_settings_reject_negative_values() -> None:
    with pytest.raises(ValidationError, match="non-negative"):
        BotSettings(quota_rate_limit_per_minute=-1)
    with pytest.raises(ValidationError, match="non-negative"):
        BotSettings(quota_daily_cost_limit_usd=-0.1)
    with pytest.raises(ValidationError, match="non-negative"):
        BotSettings(quota_group_daily_cost_limit_usd=-0.1)


def test_quota_zero_values_disable_enforcement() -> None:
    settings = BotSettings(
        quota_rate_limit_per_minute=0,
        quota_daily_cost_limit_usd=0,
        quota_group_daily_cost_limit_usd=0,
    )
    assert settings.quota_rate_limit_per_minute == 0
    assert settings.quota_daily_cost_limit_usd == 0.0
    assert settings.quota_group_daily_cost_limit_usd == 0.0
