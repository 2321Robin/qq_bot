"""Health endpoint tests (S1-HEALTH-01..03)."""

from __future__ import annotations

import json

import pytest

from qq_bot import runtime as runtime_module
from qq_bot.plugins.health import _response_body, healthz, readyz


@pytest.mark.asyncio
async def test_healthz_returns_stable_ok_payload_without_runtime() -> None:
    response = await healthz()
    assert response.status_code == 200
    # exact field whitelist: no env, paths, accounts or provider details
    assert _response_body(response) == {"status": "ok"}


@pytest.mark.asyncio
async def test_readyz_returns_503_when_runtime_not_initialized(monkeypatch) -> None:
    def raise_missing() -> None:
        raise runtime_module.RuntimeStateError("runtime has not been initialized")

    monkeypatch.setattr(runtime_module, "get_runtime", raise_missing)
    response = await readyz()
    assert response.status_code == 503
    assert _response_body(response) == {"status": "not_ready"}


@pytest.mark.asyncio
async def test_readyz_returns_503_when_runtime_not_ready(monkeypatch) -> None:
    class NotReadyRuntime:
        def is_ready(self) -> bool:
            return False

    monkeypatch.setattr(runtime_module, "get_runtime", lambda: NotReadyRuntime())
    response = await readyz()
    assert response.status_code == 503
    assert _response_body(response) == {"status": "not_ready"}


@pytest.mark.asyncio
async def test_readyz_returns_200_when_runtime_ready(monkeypatch) -> None:
    class ReadyRuntime:
        def is_ready(self) -> bool:
            return True

    monkeypatch.setattr(runtime_module, "get_runtime", lambda: ReadyRuntime())
    response = await readyz()
    assert response.status_code == 200
    assert _response_body(response) == {"status": "ready"}


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
