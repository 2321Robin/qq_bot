"""Health endpoints (S1-HEALTH-01..03).

``/healthz`` proves only that the event loop responds; it never touches the
database, NapCat or external providers. ``/readyz`` reports 200 only when the
runtime finished startup (database opened and migrations at a supported
version). Responses are a stable minimal JSON — no environment variables,
paths, accounts, provider URLs, database content or stack traces.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi.responses import JSONResponse

from qq_bot import runtime as runtime_module

logger = logging.getLogger("qq_bot.health")


async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})


async def readyz() -> JSONResponse:
    try:
        ready = runtime_module.get_runtime().is_ready()
    except Exception:
        logger.exception("readyz failed to inspect runtime state")
        ready = False
    if ready:
        return JSONResponse({"status": "ready"})
    return JSONResponse({"status": "not_ready"}, status_code=503)


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
    asgi.add_api_route("/healthz", healthz, methods=["GET"], include_in_schema=False)
    asgi.add_api_route("/readyz", readyz, methods=["GET"], include_in_schema=False)
    logger.info("health routes installed: /healthz, /readyz")


def _response_body(response: JSONResponse) -> dict[str, Any]:
    import json

    return json.loads(response.body)
