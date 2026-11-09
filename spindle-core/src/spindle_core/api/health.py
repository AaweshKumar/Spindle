# Component: Spindle Architecture
# File: health.py
# Description: GET /health readiness probe for the Spindle orchestration platform.

"""Unauthenticated readiness check endpoint.

Returns 200 when all four subsystems are reachable, 503 otherwise.
All four checks are always evaluated; one failure does not skip the rest.
Never logs URLs, hostnames, passwords, or secrets — only the exception class name.
"""

from __future__ import annotations

import logging

import redis as redis_lib
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, text

from spindle_core.config import Settings, get_settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

_PROBE_TIMEOUT_SECONDS = 2


def _check_postgres(settings: Settings) -> bool:
    """Return True iff a SELECT 1 succeeds against the configured Postgres URL."""
    engine = None
    try:
        engine = create_engine(
            settings.DATABASE_URL.get_secret_value(),
            connect_args={"connect_timeout": _PROBE_TIMEOUT_SECONDS},
        )
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.warning("health check postgres failed: %s", type(exc).__name__)
        return False
    finally:
        if engine is not None:
            engine.dispose()


def _check_redis(settings: Settings) -> bool:
    """Return True iff a PING succeeds against the configured Redis URL."""
    client = None
    try:
        client = redis_lib.Redis.from_url(
            settings.REDIS_URL.get_secret_value(),
            socket_connect_timeout=_PROBE_TIMEOUT_SECONDS,
            socket_timeout=_PROBE_TIMEOUT_SECONDS,
        )
        client.ping()
        return True
    except Exception as exc:
        logger.warning("health check redis failed: %s", type(exc).__name__)
        return False
    finally:
        if client is not None:
            client.close()


@router.get("/health")
def get_health(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> Response:
    """Readiness probe: checks postgres, redis, outbox_relay, and command_router."""
    pg_ok = _check_postgres(settings)
    rd_ok = _check_redis(settings)

    relay_thread = getattr(request.app.state, "relay_thread", None)
    relay_ok = relay_thread is not None and relay_thread.is_alive()

    router_thread = getattr(request.app.state, "router_thread", None)
    router_ok = router_thread is not None and router_thread.is_alive()

    checks = {
        "postgres": "ok" if pg_ok else "fail",
        "redis": "ok" if rd_ok else "fail",
        "outbox_relay": "ok" if relay_ok else "fail",
        "command_router": "ok" if router_ok else "fail",
    }
    all_ok = all(v == "ok" for v in checks.values())
    status_code = 200 if all_ok else 503
    body = {"status": "ok" if all_ok else "unavailable", "checks": checks}

    return JSONResponse(
        content=body,
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )

