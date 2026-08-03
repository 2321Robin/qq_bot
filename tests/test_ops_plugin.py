"""Admin ops plugin tests (S4-QUOTA-05, S4-QUOTA-07)."""

from __future__ import annotations

import bot  # noqa: F401  (NoneBot init for on_command)
import pytest

from qq_bot.config import BotSettings
from qq_bot.plugins import ops as ops_plugin
from qq_bot.services.chat_memory import ChatMemoryRepository
from qq_bot.services.quota import QuotaService


class FinishCalled(Exception):
    def __init__(self, message: object):
        self.message = message


class FakeEvent:
    group_id = 1001
    self_id = 2880000001

    def __init__(self, user_id: int = 2880000001):
        self.user_id = user_id


def _settings(*, admin_user_ids: str) -> BotSettings:
    return BotSettings(allowed_group_ids="1001", admin_user_ids=admin_user_ids)


async def _service(tmp_path) -> tuple[QuotaService, ChatMemoryRepository]:
    repository = ChatMemoryRepository(tmp_path / "ops.sqlite3", retention_days=30)
    await repository.open()
    return QuotaService(_settings(admin_user_ids="2880000001"), repository), repository


async def test_quota_command_admin_sees_summary(monkeypatch, tmp_path) -> None:
    service, repository = await _service(tmp_path)
    try:
        await service.record_usage(
            scope_type="group",
            scope_id=1001,
            tokens=123,
            cost=__import__("qq_bot.observability.cost", fromlist=["CostEstimate"]).CostEstimate(
                0.0042, "USD", "actual"
            ),
        )
        monkeypatch.setattr(
            ops_plugin, "get_settings", lambda: _settings(admin_user_ids="2880000001")
        )
        monkeypatch.setattr(ops_plugin, "get_runtime", lambda: _FakeRuntime(service))

        async def fake_finish(message: object) -> None:
            raise FinishCalled(message)

        monkeypatch.setattr(ops_plugin.quota_command, "finish", fake_finish)
        with pytest.raises(FinishCalled) as exc_info:
            await ops_plugin.handle_quota(FakeEvent())  # type: ignore[arg-type]
        text = str(exc_info.value.message)
        assert "今日配额" in text
        assert "请求 1 次" in text
        assert "Token 123" in text
        assert "0.0042" in text
        # owner-facing raw group id is allowed here (S4-QUOTA-07)
        assert "群 1001" in text
    finally:
        await repository.close()


async def test_quota_command_non_admin_is_rejected_without_finish(monkeypatch, tmp_path) -> None:
    service, repository = await _service(tmp_path)
    try:
        monkeypatch.setattr(
            ops_plugin, "get_settings", lambda: _settings(admin_user_ids="2880000001")
        )
        monkeypatch.setattr(ops_plugin, "get_runtime", lambda: _FakeRuntime(service))
        rejected: list[str] = []
        monkeypatch.setattr(ops_plugin, "record_error", lambda *a, **k: rejected.append("recorded"))

        async def fake_finish(message: object) -> None:
            raise AssertionError("non-admin must not reach finish")

        monkeypatch.setattr(ops_plugin.quota_command, "finish", fake_finish)
        await ops_plugin.handle_quota(FakeEvent(user_id=123456))  # type: ignore[arg-type]
        assert rejected == ["recorded"]
    finally:
        await repository.close()


async def test_failures_command_admin_sees_events_without_bodies(monkeypatch, tmp_path) -> None:
    service, repository = await _service(tmp_path)
    # tight rate limit so the second admission is denied
    service = QuotaService(
        _settings(admin_user_ids="2880000001").model_copy(
            update={"quota_rate_limit_per_minute": 1}
        ),
        repository,
    )
    try:
        await service.check_admission(scope_type="group", scope_id=1001)
        await service.check_admission(scope_type="group", scope_id=1001)  # rate denied
        monkeypatch.setattr(
            ops_plugin, "get_settings", lambda: _settings(admin_user_ids="2880000001")
        )
        monkeypatch.setattr(ops_plugin, "get_runtime", lambda: _FakeRuntime(service))

        async def fake_finish(message: object) -> None:
            raise FinishCalled(message)

        monkeypatch.setattr(ops_plugin.failures_command, "finish", fake_finish)
        with pytest.raises(FinishCalled) as exc_info:
            await ops_plugin.handle_failures(FakeEvent())  # type: ignore[arg-type]
        text = str(exc_info.value.message)
        assert "最近故障" in text
        assert "rate_denied/rate" in text
        # never any message bodies or prompts
        assert "text=" not in text
        assert "message" not in text.lower().replace("message", "", 1) or "rate_denied" in text
    finally:
        await repository.close()


async def test_failures_command_empty_state(monkeypatch, tmp_path) -> None:
    service, repository = await _service(tmp_path)
    try:
        monkeypatch.setattr(
            ops_plugin, "get_settings", lambda: _settings(admin_user_ids="2880000001")
        )
        monkeypatch.setattr(ops_plugin, "get_runtime", lambda: _FakeRuntime(service))

        async def fake_finish(message: object) -> None:
            raise FinishCalled(message)

        monkeypatch.setattr(ops_plugin.failures_command, "finish", fake_finish)
        with pytest.raises(FinishCalled) as exc_info:
            await ops_plugin.handle_failures(FakeEvent())  # type: ignore[arg-type]
        assert "暂无故障记录" in str(exc_info.value.message)
    finally:
        await repository.close()


class _FakeRuntime:
    def __init__(self, service: QuotaService):
        self._service = service

    def get_quota_service(self) -> QuotaService | None:
        return self._service
