from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
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


class BotSettings(BaseSettings):
    allowed_group_ids: str = ""
    admin_user_ids: str = ""
    scheduled_group_ids: str = ""
    scheduled_message: str = "现在是定时提醒时间。"
    scheduled_cron_times: str = ""
    scheduled_cron_hour: int = 9
    scheduled_cron_minute: int = 0

    ai_api_key: str = Field(default="", repr=False)
    ai_base_url: str = "https://api.openai.com/v1"
    ai_model: str = "gpt-4o-mini"
    ai_prefix: str = "ai"
    ai_timeout_seconds: float = 30.0

    search_enabled: bool = False
    tavily_api_key: str = Field(default="", repr=False)
    search_max_results: int = 5
    search_timeout_seconds: float = 10.0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("allowed_group_ids", "admin_user_ids", "scheduled_group_ids")
    @classmethod
    def validate_id_list(cls, value: str) -> str:
        parse_id_list(value)
        return value.strip()

    @field_validator("scheduled_cron_times")
    @classmethod
    def validate_schedule_times(cls, value: str) -> str:
        parse_schedule_time_list(value)
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

    @field_validator("search_timeout_seconds")
    @classmethod
    def validate_search_timeout_seconds(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("search_timeout_seconds must be greater than 0")
        return value

    @property
    def allowed_group_id_list(self) -> list[int]:
        return parse_id_list(self.allowed_group_ids)

    @property
    def admin_user_id_list(self) -> list[int]:
        return parse_id_list(self.admin_user_ids)

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
    def normalized_ai_base_url(self) -> str:
        return self.ai_base_url.strip().rstrip("/")

    def group_allowed(self, group_id: int) -> bool:
        allowed_groups = self.allowed_group_id_list
        return not allowed_groups or group_id in allowed_groups

    def has_ai_config(self) -> bool:
        return bool(self.ai_api_key.strip())

    def has_search_config(self) -> bool:
        return self.search_enabled and bool(self.tavily_api_key.strip())

    def scheduled_enabled(self) -> bool:
        return bool(self.scheduled_group_id_list) and bool(self.scheduled_message.strip())


@lru_cache(maxsize=1)
def get_settings() -> BotSettings:
    return BotSettings()
