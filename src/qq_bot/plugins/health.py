"""Health endpoints (S1-HEALTH-01..03).

``/healthz`` proves only that the event loop responds; it never touches the
database, NapCat or external providers. ``/readyz`` reports 200 only when the
runtime finished startup (database opened and migrations at a supported
version). Responses are a stable minimal JSON — no environment variables,
paths, accounts, provider URLs, database content or stack traces.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from fastapi.responses import JSONResponse, Response

from qq_bot import runtime as runtime_module
from qq_bot.config import get_settings
from qq_bot.observability import metrics
from qq_bot.observability.logging import get_logger, record_event

logger = get_logger("qq_bot.health")

_PROBE_TIMEOUT = 2.0  # seconds; every dependency probe is bounded (S4-HEALTH-05)
_SUPPORTED_MANIFEST_SCHEMA_VERSION = 1


async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})


async def metrics_endpoint() -> Response:
    """Prometheus text exposition (S4-METRIC-10).

    Registered only while ``metrics_enabled`` is true; the route is absent
    (404) otherwise (S4-METRIC-12).
    """
    from prometheus_client import generate_latest

    return Response(
        generate_latest(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


async def readyz() -> JSONResponse:
    """Readiness: runtime READY + database probe at the supported schema
    version; data_version must pass when a data directory exists
    (``readyz_require_data``); onebot reports without blocking unless
    ``readyz_require_onebot`` (S4-HEALTH-02/03). Every probe is bounded and
    failure degrades to its own check (S4-HEALTH-05); the response carries
    only stable enum values, never paths or contents (S4-HEALTH-04)."""
    settings = get_settings()
    checks: dict[str, str] = {}
    runtime_ready = False
    repository = None
    try:
        runtime = runtime_module.get_runtime()
        runtime_ready = runtime.is_ready()
        repository = runtime.get_chat_repository()
    except Exception:
        logger.exception("readyz failed to inspect runtime state")

    checks["onebot"] = _check_onebot()
    if repository is not None:
        checks["database"] = await _check_database(repository)
    else:
        checks["database"] = "fail"
    checks["data_version"] = await _check_data_version()

    ready = runtime_ready and checks["database"] == "ok"
    if settings.readyz_require_data and checks["data_version"] == "fail":
        ready = False
    if settings.readyz_require_onebot and checks["onebot"] != "connected":
        ready = False
    body = {"status": "ready" if ready else "not_ready", "checks": checks}
    return JSONResponse(body, status_code=200 if ready else 503)


async def _check_database(repository: Any) -> str:
    """ok when the read-only probe reports the supported schema version
    (S4-HEALTH-02); any failure or timeout degrades to fail."""
    from qq_bot.services.migrations import SUPPORTED_SCHEMA_VERSION

    try:
        version = await asyncio.wait_for(repository.check_ready(), timeout=_PROBE_TIMEOUT)
    except Exception:
        return "fail"
    return "ok" if version == SUPPORTED_SCHEMA_VERSION else "fail"


def _details_dir() -> Path:
    """The local data details directory (loader-compatible; S4-HEALTH-02)."""
    from qq_bot.services.roco_pets import DEFAULT_PET_DETAIL_DIR

    return DEFAULT_PET_DETAIL_DIR


async def _check_data_version() -> str:
    """ok/fail: no detail files -> ok; otherwise ``data/manifests/latest.json``
    must exist, parse and carry a supported schema version (S4-HEALTH-02).
    Lightweight read-only probe; never a full ``--verify``."""
    try:
        return await asyncio.wait_for(_probe_data_version(), timeout=_PROBE_TIMEOUT)
    except Exception:
        return "fail"


async def _probe_data_version() -> str:
    from qq_bot.datapipeline.manifest import RefreshManifest

    details_dir = _details_dir()
    if not (details_dir.is_dir() and any(details_dir.glob("*.json"))):
        return "ok"
    manifest_path = details_dir.parent / "manifests" / "latest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return "fail"
    manifest = RefreshManifest.model_validate(payload)
    if manifest.schema_version != _SUPPORTED_MANIFEST_SCHEMA_VERSION:
        return "fail"
    return "ok"


def _check_onebot() -> str:
    """connected when the driver reports at least one bot; any driver
    absence degrades to disconnected (S4-HEALTH-02, test-safe)."""
    try:
        from nonebot import get_bots

        return "connected" if get_bots() else "disconnected"
    except Exception:
        return "disconnected"


def install_health_routes() -> None:
    """Register the health routes on the FastAPI ASGI app. No-op when the
    ASGI driver is unavailable (e.g. pure test imports)."""
    try:
        from nonebot import get_asgi
    except Exception:
        return
    try:
        asgi = get_asgi()
    except Exception:
        logger.warning("health routes not installed: ASGI driver unavailable")
        return
    if not hasattr(asgi, "add_api_route"):
        return
    settings = get_settings()
    metrics.set_metrics_enabled(settings.metrics_enabled)
    asgi.add_api_route("/healthz", healthz, methods=["GET"], include_in_schema=False)
    asgi.add_api_route("/readyz", readyz, methods=["GET"], include_in_schema=False)
    installed = "/healthz, /readyz"
    if settings.metrics_enabled:
        asgi.add_api_route("/metrics", metrics_endpoint, methods=["GET"], include_in_schema=False)
        installed += ", /metrics"
    record_event(
        logger,
        logging.INFO,
        "health_routes_installed",
        message=f"health routes installed: {installed}",
    )


def _response_body(response: JSONResponse) -> dict[str, Any]:
    import json

    return json.loads(response.body)
