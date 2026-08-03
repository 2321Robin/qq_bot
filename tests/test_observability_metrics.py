"""Metrics registry and /metrics endpoint tests (S4-METRIC)."""

from __future__ import annotations

import re

import bot  # noqa: F401  (initializes NoneBot before plugin imports)
import pytest
from nonebot.adapters.onebot.v11 import Message
from prometheus_client.parser import text_string_to_metric_families

from qq_bot.config import BotSettings, get_settings
from qq_bot.observability import metrics
from qq_bot.plugins.health import install_health_routes, metrics_endpoint


class FakeEvent:
    group_id = 1001
    user_id = 2001
    self_id = 2880000001

    def __init__(self, text: str, *, to_me: bool = False):
        self.text = text
        self.to_me = to_me

    def get_message(self) -> "FakeEvent":
        return self

    def extract_plain_text(self) -> str:
        return self.text

    def __iter__(self):
        return iter(())

    def is_tome(self) -> bool:
        return self.to_me


class FinishCalled(Exception):
    def __init__(self, message: object):
        self.message = message


class EmptyMemoryStore:
    async def add_message(self, *args, **kwargs) -> int:
        return 123

    async def update_ai_reply(self, message_id: int, ai_reply: str) -> None:
        return None

    async def recent_user_turns(self, *, group_id: int, user_id: int, limit: int):
        return []

    async def recent_group_messages(self, *, group_id: int, limit: int):
        return []


async def _sample_value(name: str, labels: dict[str, str]) -> float:
    response = await metrics_endpoint()
    text = response.body.decode("utf-8")
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            if sample.name != name:
                continue
            if all(sample.labels.get(key) == value for key, value in labels.items()):
                return float(sample.value)
    return 0.0


@pytest.mark.asyncio
async def test_metrics_endpoint_returns_parseable_text() -> None:
    response = await metrics_endpoint()
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    text = response.body.decode("utf-8")
    families = {family.name: family for family in text_string_to_metric_families(text)}
    for expected in (
        "qq_bot_messages",
        "qq_bot_commands",
        "qq_bot_ai_requests",
        "qq_bot_ai_request_duration_seconds",
        "qq_bot_search_requests",
        "qq_bot_retry",
        "qq_bot_provider_fallback",
        "qq_bot_errors",
        "qq_bot_circuit_breaker_info",
        "qq_bot_circuit_breaker_transitions",
        "qq_bot_send",
        "qq_bot_tokens",
        "qq_bot_cost_usd",
        "qq_bot_quota_denied",
        "qq_bot_agent_outcome",
        "qq_bot_route",
        "qq_bot_span_duration_seconds",
        "qq_bot_scheduler_send",
    ):
        assert expected in families, f"missing metric family {expected}"


@pytest.mark.asyncio
async def test_metric_labels_never_carry_raw_ids() -> None:
    response = await metrics_endpoint()
    text = response.body.decode("utf-8")
    raw_id_pattern = re.compile(r"^\d{6,}$")
    for family in text_string_to_metric_families(text):
        if not family.name.startswith("qq_bot_"):
            continue
        for sample in family.samples:
            for label_key, label_value in sample.labels.items():
                assert not raw_id_pattern.match(label_value), (
                    f"{family.name} label {label_key}={label_value!r} looks like a raw id"
                )


@pytest.mark.asyncio
async def test_ai_chat_message_increments_messages_counter(monkeypatch) -> None:
    from qq_bot.plugins import ai_chat as ai_chat_plugin

    async def fake_request_ai_reply(
        prompt: str,
        *,
        settings: BotSettings,
        client: object | None = None,
        search_context: str = "",
        chat_context: str = "",
        roco_context: str = "",
    ) -> str:
        return "ok"

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", ai_api_key="secret"),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_http_client", lambda: object())
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: EmptyMemoryStore())
    monkeypatch.setattr(ai_chat_plugin, "request_ai_reply", fake_request_ai_reply)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    before = await _sample_value("qq_bot_messages_total", {"kind": "ai_prompt"})
    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 提醒我"))  # type: ignore[arg-type]
    after = await _sample_value("qq_bot_messages_total", {"kind": "ai_prompt"})
    assert after == before + 1


@pytest.mark.asyncio
async def test_plain_message_increments_messages_counter(monkeypatch) -> None:
    from qq_bot.plugins import ai_chat as ai_chat_plugin

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", ai_api_key="secret"),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_http_client", lambda: object())
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: EmptyMemoryStore())

    before = await _sample_value("qq_bot_messages_total", {"kind": "plain"})
    await ai_chat_plugin.handle_ai_chat(FakeEvent("随便聊聊"))  # type: ignore[arg-type]
    after = await _sample_value("qq_bot_messages_total", {"kind": "plain"})
    assert after == before + 1


@pytest.mark.asyncio
async def test_help_command_increments_commands_counter(monkeypatch) -> None:
    from qq_bot.plugins import commands as commands_plugin

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(
        commands_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001"),
    )
    monkeypatch.setattr(commands_plugin.help_command, "finish", fake_finish)

    before = await _sample_value("qq_bot_commands_total", {"command": "help"})
    with pytest.raises(FinishCalled):
        await commands_plugin.handle_help(FakeEvent("/help"))  # type: ignore[arg-type]
    after = await _sample_value("qq_bot_commands_total", {"command": "help"})
    assert after == before + 1


@pytest.mark.asyncio
async def test_roco_pet_command_increments_commands_counter(monkeypatch) -> None:
    from qq_bot.plugins import roco as roco_plugin

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(
        roco_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001"),
    )
    monkeypatch.setattr(roco_plugin, "get_pet_records", lambda: ())
    monkeypatch.setattr(roco_plugin.roco_pet_command, "finish", fake_finish)

    before = await _sample_value("qq_bot_commands_total", {"command": "精灵"})
    with pytest.raises(FinishCalled):
        await roco_plugin.handle_roco_pet(FakeEvent("精灵 x"), Message("x"))  # type: ignore[arg-type]
    after = await _sample_value("qq_bot_commands_total", {"command": "精灵"})
    assert after == before + 1


@pytest.mark.asyncio
async def test_disabled_metrics_are_zero_side_effect() -> None:
    metrics.set_metrics_enabled(False)
    try:
        before = await _sample_value("qq_bot_messages_total", {"kind": "ai_prompt"})
        for _ in range(50):
            metrics.MESSAGES.labels("ai_prompt").inc()
            metrics.COMMANDS.labels("help").inc()
            metrics.AI_DURATION.labels("fake").observe(1.5)
        after = await _sample_value("qq_bot_messages_total", {"kind": "ai_prompt"})
        assert after == before
    finally:
        metrics.set_metrics_enabled(True)


def test_metrics_route_not_registered_when_disabled(monkeypatch) -> None:
    monkeypatch.setenv("METRICS_ENABLED", "false")
    get_settings.cache_clear()
    registered: list[str] = []

    class FakeAsgi:
        def add_api_route(self, path: str, endpoint: object, **kwargs: object) -> None:
            registered.append(path)

    monkeypatch.setattr("nonebot.get_asgi", lambda: FakeAsgi())
    try:
        install_health_routes()
    finally:
        metrics.set_metrics_enabled(True)
        get_settings.cache_clear()
    assert "/healthz" in registered
    assert "/readyz" in registered
    assert "/metrics" not in registered


def test_metrics_route_registered_when_enabled(monkeypatch) -> None:
    registered: list[str] = []

    class FakeAsgi:
        def add_api_route(self, path: str, endpoint: object, **kwargs: object) -> None:
            registered.append(path)

    monkeypatch.setattr("nonebot.get_asgi", lambda: FakeAsgi())
    install_health_routes()
    assert "/metrics" in registered


@pytest.mark.asyncio
async def test_fallback_counter_counts_only_primary_failure(monkeypatch) -> None:
    from qq_bot.services import ai_client as ai_client_module

    async def fake_post_chat_completion(
        payload: object,
        *,
        settings: object,
        client: object,
        base_url: str,
        api_key: str,
        model: str,
        breaker_name: str,
    ) -> dict[str, object]:
        calls.append(breaker_name)
        if breaker_name == "ai_primary":
            from qq_bot.services.ai_client import AIReplyError

            raise AIReplyError("primary down")
        return {"choices": [{"message": {"content": "fallback ok"}}]}

    calls: list[str] = []
    monkeypatch.setattr(ai_client_module, "_post_chat_completion", fake_post_chat_completion)
    settings = BotSettings(ai_api_key="secret", ai_fallback_api_key="fallback-secret")

    before = await _sample_value("qq_bot_provider_fallback_total", {"provider": "ai"})
    text = await ai_client_module.request_ai_reply("hi", settings=settings, client=object())
    after = await _sample_value("qq_bot_provider_fallback_total", {"provider": "ai"})
    assert text == "fallback ok"
    assert calls == ["ai_primary", "ai_fallback"]
    assert after == before + 1

    # Primary success must not count as a fallback.
    calls.clear()

    async def fake_post_ok(
        payload: object,
        *,
        settings: object,
        client: object,
        base_url: str,
        api_key: str,
        model: str,
        breaker_name: str,
    ) -> dict[str, object]:
        calls.append(breaker_name)
        return {"choices": [{"message": {"content": "primary ok"}}]}

    monkeypatch.setattr(ai_client_module, "_post_chat_completion", fake_post_ok)
    text = await ai_client_module.request_ai_reply("hi", settings=settings, client=object())
    after_ok = await _sample_value("qq_bot_provider_fallback_total", {"provider": "ai"})
    assert text == "primary ok"
    assert calls == ["ai_primary"]
    assert after_ok == after


@pytest.mark.asyncio
async def test_ambiguous_timeout_send_counts_no_retry() -> None:
    from nonebot.adapters.onebot.v11.exception import NetworkError

    from qq_bot.services.scheduled_sender import send_group_messages

    class TimeoutBot:
        async def send_group_msg(self, *, group_id: int, message: object) -> None:
            raise NetworkError("WebSocket call api send_group_msg timeout")

    retries_before = await _sample_value("qq_bot_retry_total", {"dependency": "send"})
    before = await _sample_value("qq_bot_scheduler_send_total", {"result": "ambiguous_timeout"})
    failures = await send_group_messages(TimeoutBot(), [1001], "早上好", max_attempts=3)
    retries_after = await _sample_value("qq_bot_retry_total", {"dependency": "send"})
    after = await _sample_value("qq_bot_scheduler_send_total", {"result": "ambiguous_timeout"})
    assert failures == [1001]
    assert retries_after == retries_before, "ambiguous timeout must never count a retry"
    assert after == before + 1


@pytest.mark.asyncio
async def test_runtime_breaker_gauge_tracks_state(tmp_path) -> None:
    from qq_bot.runtime import AppRuntime

    settings = BotSettings(
        allowed_group_ids="1001",
        chat_memory_path=str(tmp_path / "mem.db"),
        breaker_failure_threshold=1,
        breaker_recovery_seconds=30,
    )
    runtime = AppRuntime(settings)
    await runtime.startup()
    try:
        breaker = runtime.get_breaker("onebot")
        closed_gauge = await _sample_value(
            "qq_bot_circuit_breaker_info", {"name": "onebot", "state": "closed"}
        )
        assert closed_gauge == 1.0

        from qq_bot.services.reliability import TRANSIENT

        await breaker.on_failure(TRANSIENT)
        open_gauge = await _sample_value(
            "qq_bot_circuit_breaker_info", {"name": "onebot", "state": "open"}
        )
        closed_gauge = await _sample_value(
            "qq_bot_circuit_breaker_info", {"name": "onebot", "state": "closed"}
        )
        transitions = await _sample_value(
            "qq_bot_circuit_breaker_transitions_total", {"name": "onebot", "to_state": "open"}
        )
        assert open_gauge == 1.0
        assert closed_gauge == 0.0
        assert transitions == 1.0
    finally:
        await runtime.shutdown()
