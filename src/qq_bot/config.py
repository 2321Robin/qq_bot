from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def parse_id_list(value: str | None) -> list[int]:
    if value is None:
        return []

    text = value.strip()
    if not text:
        return []

    ids: list[int] = []
    for part in text.split(","):
        item = part.strip()
        if not item:
            continue
        try:
            ids.append(int(item))
        except ValueError as exc:
            raise ValueError("ID lists must be comma-separated integers") from exc
    return ids


def parse_schedule_time_list(value: str | None) -> list[tuple[int, int]]:
    if value is None:
        return []

    text = value.strip()
    if not text:
        return []

    times: list[tuple[int, int]] = []
    for part in text.split(","):
        item = part.strip()
        if not item:
            continue
        pieces = item.split(":")
        if len(pieces) != 2:
            raise ValueError("scheduled_cron_times must use HH:MM comma-separated values")
        try:
            hour = int(pieces[0])
            minute = int(pieces[1])
        except ValueError as exc:
            raise ValueError("scheduled_cron_times must use HH:MM comma-separated values") from exc
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            raise ValueError("scheduled_cron_times must use valid HH:MM values")
        times.append((hour, minute))
    return times


def parse_named_mention_replacements(value: str | None) -> dict[str, str]:
    """Parse ``name=qq,name=qq`` pairs into a replacement mapping.

    Only integer QQ numbers are accepted so deployers never accidentally
    commit their real account into source.
    """
    if value is None:
        return {}

    text = value.strip()
    if not text:
        return {}

    replacements: dict[str, str] = {}
    for part in text.split(","):
        item = part.strip()
        if not item:
            continue
        pieces = item.split("=", 1)
        if len(pieces) != 2:
            raise ValueError("named_mention_replacements must use name=qq comma-separated pairs")
        name = pieces[0].strip()
        account = pieces[1].strip()
        if not name or not account.isdigit():
            raise ValueError("named_mention_replacements must use name=qq comma-separated pairs")
        replacements[name] = account
    return replacements


class BotSettings(BaseSettings):
    allowed_group_ids: str = ""
    admin_user_ids: str = ""
    scheduled_group_ids: str = ""
    scheduled_message: str = "现在是定时提醒时间。"
    scheduled_cron_times: str = ""
    scheduled_cron_hour: int = 9
    scheduled_cron_minute: int = 0
    # "@昵称=QQ号,@昵称2=QQ号2" pairs; only integer QQ numbers are accepted
    # so deployers never commit their real account into source.
    named_mention_replacements: str = ""

    ai_api_key: str = Field(default="", repr=False)
    ai_base_url: str = "https://api.openai.com/v1"
    ai_model: str = "gpt-4o-mini"
    ai_prefix: str = "ai"
    ai_ignored_user_ids: str = ""
    ai_timeout_seconds: float = 30.0
    ai_fallback_api_key: str = Field(default="", repr=False)
    ai_fallback_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    ai_fallback_model: str = "glm-4-flash"

    chat_memory_path: str = "data/chat_memory.sqlite3"
    chat_memory_retention_days: int = 3
    chat_memory_default_turns: int = 10
    chat_memory_max_results: int = 20

    search_enabled: bool = False
    tavily_api_key: str = Field(default="", repr=False)
    search_max_results: int = 5
    search_timeout_seconds: float = 10.0

    # Reliability configuration (S1-RET-01). Attempts include the first call;
    # delays follow capped exponential backoff with jitter; breakers count only
    # transient dependency failures and recover after the recovery window.
    ai_max_attempts: int = 2
    ai_retry_base_delay_seconds: float = 0.5
    ai_retry_max_delay_seconds: float = 4.0
    search_max_attempts: int = 3
    search_retry_base_delay_seconds: float = 0.5
    search_retry_max_delay_seconds: float = 4.0
    send_max_attempts: int = 2
    send_retry_base_delay_seconds: float = 0.5
    send_retry_max_delay_seconds: float = 3.0
    retry_jitter_ratio: float = 0.1
    breaker_failure_threshold: int = 3
    breaker_recovery_seconds: float = 30.0

    # Agent capability (S2-CONFIG-01). AGENT_ENABLED gates the structured
    # tool-calling path; when disabled the stage-1 prompt pipeline stays
    # active as the rollback path.
    agent_enabled: bool = False
    ai_router_model: str = ""
    ai_router_confidence_threshold: float = 0.75
    agent_max_rounds: int = 3
    agent_max_tool_calls: int = 4
    agent_tools_per_round: int = 2
    agent_deadline_seconds: float = 60.0
    ai_provider_tools_enabled: bool = True
    ai_provider_structured_output_enabled: bool = True
    # Semantic verification of claims against evidence; deterministic grounding
    # checks always run and cannot be disabled (S2-EVID-04..06).
    ai_semantic_verifier_enabled: bool = False
    ai_verifier_model: str = ""

    # Layered memory (S2-MEM-05..09, S2-CONFIG-03). Summaries are opt-in;
    # long-term preferences are only ever saved by an explicit user command.
    memory_summary_enabled: bool = False
    memory_preference_max_chars: int = 200

    # Token budget (S2-TOKEN-01..08, S2-CONFIG-02). context window must
    # exceed output reserve + safety margin; per-source ratios must be
    # non-negative and sum to at most 1.
    ai_context_window_tokens: int = 128000
    ai_output_reserve_tokens: int = 2048
    ai_token_safety_margin: int = 1024
    agent_budget_local_ratio: float = 0.30
    agent_budget_web_ratio: float = 0.25
    agent_budget_recent_ratio: float = 0.15
    agent_budget_summary_ratio: float = 0.10
    agent_budget_preference_max_tokens: int = 256

    # ---- 数据管道（阶段 3）----
    # 门禁默认阈值来自 2026-08-02 实测基线并留出裕量（S3-QUALITY-02）；
    # 全部可经环境变量覆盖，当前值与阈值写入 manifest.checks 与差异报告。
    data_min_records: int = 500
    data_max_record_drop: int = 30
    data_max_new_number_gaps: int = 0
    data_min_stats_complete_rate: float = 0.80
    data_min_total_race_rate: float = 0.95
    data_max_dangling_edges: int = 0
    data_max_skill_key_missing_rate: float = 0.005
    data_search_index_path: str = "data/roco_search.sqlite3"
    data_use_search_index: bool = True

    # ---- 可观测性与配额（阶段 4）----
    log_format: str = "text"  # text | json
    log_level: str = "INFO"  # DEBUG | INFO | WARNING | ERROR
    metrics_enabled: bool = True  # false 时 /metrics 404 且埋点零开销
    trace_enabled: bool = True  # false 时 span 为空操作
    readyz_require_onebot: bool = False  # OneBot 断开默认只报告不阻断
    readyz_require_data: bool = True  # 数据目录存在时 manifest 必须有效
    quota_enabled: bool = True  # 限流与费用预算总开关
    quota_rate_limit_per_minute: int = 30  # 0 = 关闭；AI 消息/群/分钟
    quota_daily_cost_limit_usd: float = 2.0  # 0 = 关闭；全局每日 actual 成本上限
    quota_group_daily_cost_limit_usd: float = 0.5  # 0 = 关闭；每群每日上限

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator(
        "allowed_group_ids", "admin_user_ids", "scheduled_group_ids", "ai_ignored_user_ids"
    )
    @classmethod
    def validate_id_list(cls, value: str) -> str:
        parse_id_list(value)
        return value.strip()

    @field_validator("scheduled_cron_times")
    @classmethod
    def validate_schedule_times(cls, value: str) -> str:
        parse_schedule_time_list(value)
        return value.strip()

    @field_validator("named_mention_replacements")
    @classmethod
    def validate_named_mention_replacements(cls, value: str) -> str:
        parse_named_mention_replacements(value)
        return value.strip()

    @field_validator("scheduled_cron_hour")
    @classmethod
    def validate_schedule_hour(cls, value: int) -> int:
        if value < 0 or value > 23:
            raise ValueError("scheduled_cron_hour must be between 0 and 23")
        return value

    @field_validator("scheduled_cron_minute")
    @classmethod
    def validate_schedule_minute(cls, value: int) -> int:
        if value < 0 or value > 59:
            raise ValueError("scheduled_cron_minute must be between 0 and 59")
        return value

    @field_validator("search_max_results")
    @classmethod
    def validate_search_max_results(cls, value: int) -> int:
        if value < 1 or value > 20:
            raise ValueError("search_max_results must be between 1 and 20")
        return value

    @field_validator("ai_max_attempts", "search_max_attempts", "send_max_attempts")
    @classmethod
    def validate_positive_attempts(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max attempts must be a positive integer")
        return value

    @field_validator(
        "ai_retry_base_delay_seconds",
        "ai_retry_max_delay_seconds",
        "search_retry_base_delay_seconds",
        "search_retry_max_delay_seconds",
        "send_retry_base_delay_seconds",
        "send_retry_max_delay_seconds",
        "breaker_recovery_seconds",
    )
    @classmethod
    def validate_positive_delays(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("retry delays and recovery must be greater than 0")
        return value

    @field_validator("retry_jitter_ratio")
    @classmethod
    def validate_jitter_ratio(cls, value: float) -> float:
        if value < 0 or value > 1:
            raise ValueError("retry_jitter_ratio must be between 0 and 1")
        return value

    @field_validator("breaker_failure_threshold")
    @classmethod
    def validate_breaker_threshold(cls, value: int) -> int:
        if value < 1:
            raise ValueError("breaker_failure_threshold must be a positive integer")
        return value

    @field_validator("ai_router_confidence_threshold")
    @classmethod
    def validate_router_confidence_threshold(cls, value: float) -> float:
        if value < 0 or value > 1:
            raise ValueError("ai_router_confidence_threshold must be between 0 and 1")
        return value

    @field_validator("agent_max_rounds", "agent_max_tool_calls", "agent_tools_per_round")
    @classmethod
    def validate_positive_agent_limits(cls, value: int) -> int:
        if value < 1:
            raise ValueError("agent limits must be positive integers")
        return value

    @field_validator("agent_deadline_seconds")
    @classmethod
    def validate_agent_deadline_seconds(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("agent_deadline_seconds must be greater than 0")
        return value

    @model_validator(mode="after")
    def validate_delay_ranges(self) -> "BotSettings":
        pairs = (
            ("ai", self.ai_retry_base_delay_seconds, self.ai_retry_max_delay_seconds),
            ("search", self.search_retry_base_delay_seconds, self.search_retry_max_delay_seconds),
            ("send", self.send_retry_base_delay_seconds, self.send_retry_max_delay_seconds),
        )
        for name, base, maximum in pairs:
            if base > maximum:
                raise ValueError(
                    f"{name}_retry_base_delay_seconds must not exceed "
                    f"{name}_retry_max_delay_seconds"
                )
        return self

    @field_validator("search_timeout_seconds")
    @classmethod
    def validate_search_timeout_seconds(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("search_timeout_seconds must be greater than 0")
        return value

    @field_validator("chat_memory_retention_days")
    @classmethod
    def validate_chat_memory_retention_days(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("chat_memory_retention_days must be greater than 0")
        return value

    @field_validator("chat_memory_default_turns")
    @classmethod
    def validate_chat_memory_default_turns(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("chat_memory_default_turns must be greater than 0")
        return value

    @field_validator("chat_memory_max_results")
    @classmethod
    def validate_chat_memory_max_results(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("chat_memory_max_results must be greater than 0")
        return value

    @field_validator(
        "data_min_records",
        "data_max_record_drop",
        "data_max_new_number_gaps",
        "data_max_dangling_edges",
    )
    @classmethod
    def validate_data_gate_int_fields(cls, value: int) -> int:
        if value < 0:
            raise ValueError("data gate thresholds must be non-negative")
        return value

    @field_validator(
        "data_min_stats_complete_rate",
        "data_min_total_race_rate",
        "data_max_skill_key_missing_rate",
    )
    @classmethod
    def validate_data_gate_rate_fields(cls, value: float) -> float:
        if value < 0 or value > 1:
            raise ValueError("data gate rate thresholds must be between 0 and 1")
        return value

    @field_validator("data_search_index_path")
    @classmethod
    def validate_data_search_index_path(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("data_search_index_path must be a non-empty string")
        return value

    @field_validator("log_format")
    @classmethod
    def validate_log_format(cls, value: str) -> str:
        if value not in {"text", "json"}:
            raise ValueError("log_format must be one of: text, json")
        return value

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        if value not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError("log_level must be one of: DEBUG, INFO, WARNING, ERROR")
        return value

    @field_validator("quota_rate_limit_per_minute")
    @classmethod
    def validate_quota_rate_limit(cls, value: int) -> int:
        if value < 0:
            raise ValueError("quota_rate_limit_per_minute must be non-negative")
        return value

    @field_validator("quota_daily_cost_limit_usd", "quota_group_daily_cost_limit_usd")
    @classmethod
    def validate_quota_cost_limits(cls, value: float) -> float:
        if value < 0:
            raise ValueError("quota cost limits must be non-negative")
        return value

    @field_validator(
        "ai_context_window_tokens", "ai_output_reserve_tokens", "ai_token_safety_margin"
    )
    @classmethod
    def validate_positive_token_budget(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("token budget fields must be positive integers")
        return value

    @field_validator("agent_budget_preference_max_tokens")
    @classmethod
    def validate_preference_cap(cls, value: int) -> int:
        if value < 1:
            raise ValueError("agent_budget_preference_max_tokens must be positive")
        return value

    @field_validator("memory_preference_max_chars")
    @classmethod
    def validate_preference_chars(cls, value: int) -> int:
        if value < 1:
            raise ValueError("memory_preference_max_chars must be positive")
        return value

    @model_validator(mode="after")
    def validate_token_budget_relations(self) -> "BotSettings":
        if (
            self.ai_context_window_tokens
            <= self.ai_output_reserve_tokens + self.ai_token_safety_margin
        ):
            raise ValueError("ai_context_window_tokens must exceed output reserve + safety margin")
        ratios = (
            self.agent_budget_local_ratio,
            self.agent_budget_web_ratio,
            self.agent_budget_recent_ratio,
            self.agent_budget_summary_ratio,
        )
        if any(ratio < 0 for ratio in ratios) or sum(ratios) > 1.0:
            raise ValueError("agent budget ratios must be non-negative and sum to at most 1")
        return self

    @property
    def allowed_group_id_list(self) -> list[int]:
        return parse_id_list(self.allowed_group_ids)

    @property
    def admin_user_id_list(self) -> list[int]:
        return parse_id_list(self.admin_user_ids)

    @property
    def ai_ignored_user_id_list(self) -> list[int]:
        return parse_id_list(self.ai_ignored_user_ids)

    @property
    def scheduled_group_id_list(self) -> list[int]:
        return parse_id_list(self.scheduled_group_ids)

    @property
    def scheduled_cron_time_list(self) -> list[tuple[int, int]]:
        configured_times = parse_schedule_time_list(self.scheduled_cron_times)
        if configured_times:
            return configured_times
        return [(self.scheduled_cron_hour, self.scheduled_cron_minute)]

    @property
    def named_mention_replacement_map(self) -> dict[str, str]:
        return parse_named_mention_replacements(self.named_mention_replacements)

    @property
    def normalized_ai_base_url(self) -> str:
        return self.ai_base_url.strip().rstrip("/")

    @property
    def normalized_ai_fallback_base_url(self) -> str:
        return self.ai_fallback_base_url.strip().rstrip("/")

    def group_allowed(self, group_id: int) -> bool:
        allowed_groups = self.allowed_group_id_list
        return not allowed_groups or group_id in allowed_groups

    def has_ai_config(self) -> bool:
        return bool(self.ai_api_key.strip())

    def has_ai_fallback_config(self) -> bool:
        return bool(self.ai_fallback_api_key.strip())

    def has_search_config(self) -> bool:
        return self.search_enabled and bool(self.tavily_api_key.strip())

    def scheduled_enabled(self) -> bool:
        return bool(self.scheduled_group_id_list) and bool(self.scheduled_message.strip())

    @property
    def router_model(self) -> str:
        """Router model; empty means the router reuses the main AI model."""
        return self.ai_router_model.strip() or self.ai_model


@lru_cache(maxsize=1)
def get_settings() -> BotSettings:
    return BotSettings()
