"""Health endpoint tests (S1-HEALTH-01..03, S4-HEALTH-01..06)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qq_bot import runtime as runtime_module
from qq_bot.config import get_settings
from qq_bot.plugins import health as health_module
from qq_bot.plugins.health import _response_body, healthz, readyz
from qq_bot.services.chat_memory import ChatMemoryRepository


def _open_repository(tmp_path: Path) -> ChatMemoryRepository:
    repository = ChatMemoryRepository(tmp_path / "mem.sqlite3", retention_days=30)
    return repository


@pytest.mark.asyncio
async def test_healthz_returns_stable_ok_payload_without_runtime() -> None:
    response = await healthz()
    assert response.status_code == 200
    # exact field whitelist: no env, paths, accounts or provider details
    assert _response_body(response) == {"status": "ok"}


class _ReadyRuntime:
    """Minimal ready runtime backed by a real open repository."""

    def __init__(self, repository: ChatMemoryRepository) -> None:
        self._repository = repository

    def is_ready(self) -> bool:
        return True

    def get_chat_repository(self) -> ChatMemoryRepository:
        return self._repository


class _NotReadyRuntime:
    def is_ready(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_readyz_returns_503_when_runtime_not_initialized(monkeypatch) -> None:
    def raise_missing() -> None:
        raise runtime_module.RuntimeStateError("runtime has not been initialized")

    monkeypatch.setattr(runtime_module, "get_runtime", raise_missing)
    monkeypatch.setattr(health_module, "_details_dir", lambda: Path("nonexistent/details"))
    response = await readyz()
    assert response.status_code == 503
    body = _response_body(response)
    assert body["status"] == "not_ready"
    assert set(body["checks"]) == {"database", "data_version", "onebot"}
    assert body["checks"]["database"] == "fail"
    assert body["checks"]["data_version"] == "ok"
    assert body["checks"]["onebot"] == "disconnected"


@pytest.mark.asyncio
async def test_readyz_returns_503_when_runtime_not_ready(monkeypatch) -> None:
    monkeypatch.setattr(runtime_module, "get_runtime", lambda: _NotReadyRuntime())
    monkeypatch.setattr(health_module, "_details_dir", lambda: Path("nonexistent/details"))
    response = await readyz()
    assert response.status_code == 503
    assert _response_body(response)["status"] == "not_ready"


@pytest.mark.asyncio
async def test_readyz_returns_200_when_runtime_ready_and_checks_pass(monkeypatch, tmp_path) -> None:
    repository = _open_repository(tmp_path)
    await repository.open()
    try:
        monkeypatch.setattr(runtime_module, "get_runtime", lambda: _ReadyRuntime(repository))
        monkeypatch.setattr(health_module, "_details_dir", lambda: Path("nonexistent/details"))
        response = await readyz()
        assert response.status_code == 200
        body = _response_body(response)
        assert body["status"] == "ready"
        assert body["checks"]["database"] == "ok"
        assert body["checks"]["data_version"] == "ok"
        assert body["checks"]["onebot"] == "disconnected"
    finally:
        await repository.close()


@pytest.mark.asyncio
async def test_readyz_database_failure_is_503(monkeypatch, tmp_path) -> None:
    repository = _open_repository(tmp_path)
    await repository.open()
    await repository.close()  # closed repository probe fails
    monkeypatch.setattr(runtime_module, "get_runtime", lambda: _ReadyRuntime(repository))
    monkeypatch.setattr(health_module, "_details_dir", lambda: Path("nonexistent/details"))
    response = await readyz()
    assert response.status_code == 503
    body = _response_body(response)
    assert body["status"] == "not_ready"
    assert body["checks"]["database"] == "fail"


@pytest.mark.asyncio
async def test_readyz_data_version_fails_on_invalid_manifest(monkeypatch, tmp_path) -> None:
    details_dir = tmp_path / "details"
    details_dir.mkdir()
    (details_dir / "001.json").write_text("{}", encoding="utf-8")
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    (manifests / "latest.json").write_text("{not json", encoding="utf-8")
    repository = _open_repository(tmp_path)
    await repository.open()
    try:
        monkeypatch.setattr(runtime_module, "get_runtime", lambda: _ReadyRuntime(repository))
        monkeypatch.setattr(health_module, "_details_dir", lambda: details_dir)
        response = await readyz()
        assert response.status_code == 503
        body = _response_body(response)
        assert body["checks"]["data_version"] == "fail"
    finally:
        await repository.close()


@pytest.mark.asyncio
async def test_readyz_no_data_dir_is_ok_and_disconnected_onebot_does_not_block(
    monkeypatch, tmp_path
) -> None:
    repository = _open_repository(tmp_path)
    await repository.open()
    try:
        monkeypatch.setattr(runtime_module, "get_runtime", lambda: _ReadyRuntime(repository))
        monkeypatch.setattr(health_module, "_details_dir", lambda: tmp_path / "missing_details")
        response = await readyz()
        assert response.status_code == 200
        body = _response_body(response)
        assert body["status"] == "ready"
        assert body["checks"]["data_version"] == "ok"
        assert body["checks"]["onebot"] == "disconnected"
    finally:
        await repository.close()


@pytest.mark.asyncio
async def test_readyz_require_onebot_blocks_when_disconnected(monkeypatch, tmp_path) -> None:
    repository = _open_repository(tmp_path)
    await repository.open()
    try:
        monkeypatch.setenv("READYZ_REQUIRE_ONEBOT", "true")
        get_settings.cache_clear()
        try:
            monkeypatch.setattr(runtime_module, "get_runtime", lambda: _ReadyRuntime(repository))
            monkeypatch.setattr(health_module, "_details_dir", lambda: tmp_path / "missing_details")
            response = await readyz()
            assert response.status_code == 503
            body = _response_body(response)
            assert body["status"] == "not_ready"
            assert body["checks"]["onebot"] == "disconnected"
        finally:
            monkeypatch.setenv("READYZ_REQUIRE_ONEBOT", "false")
            get_settings.cache_clear()
    finally:
        await repository.close()


@pytest.mark.asyncio
async def test_readyz_response_has_no_privacy_fields(monkeypatch, tmp_path) -> None:
    repository = _open_repository(tmp_path)
    await repository.open()
    try:
        monkeypatch.setattr(runtime_module, "get_runtime", lambda: _ReadyRuntime(repository))
        monkeypatch.setattr(health_module, "_details_dir", lambda: Path("nonexistent/details"))
        response = await readyz()
        text = response.body.decode("utf-8")
        assert "/" not in text  # no paths
        assert "=" not in text  # no env-style pairs or API keys
        body = _response_body(response)
        assert set(body) == {"status", "checks"}
        assert not any(key in body for key in ("env", "path", "key", "secret"))
    finally:
        await repository.close()


def test_install_health_routes_is_safe_without_asgi(monkeypatch) -> None:
    """Installing routes outside a running driver must be a no-op, not crash."""

    def raise_import_error() -> None:
        raise ImportError("no nonebot driver")

    monkeypatch.setattr("nonebot.get_asgi", raise_import_error)
    from qq_bot.plugins.health import install_health_routes

    install_health_routes()  # must not raise


def test_response_body_parses_json_payload() -> None:
    from fastapi.responses import JSONResponse

    response = JSONResponse({"status": "ok"})
    assert json.loads(response.body) == {"status": "ok"}
