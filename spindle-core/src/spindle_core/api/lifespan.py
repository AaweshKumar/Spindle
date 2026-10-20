# Component: Spindle Architecture
# File: lifespan.py
# Description: FastAPI lifespan for the Spindle orchestration platform.

"""Application lifespan — composition root.

``make_lifespan(settings)`` returns an async context manager that:

1. Builds shared resources in order onto ``app.state``.
2. Starts the outbox relay in a daemon thread.
3. On shutdown, stops the relay gracefully and tears down resources in reverse.

If any startup step fails, all resources built so far are unwound in reverse
order before the exception propagates so the process does not leak file
descriptors or connections.

Never log URLs, passwords or secrets.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager

import redis as redis_lib
from fastapi import FastAPI

from spindle_core.api.dependencies import load_tapestry_store
from spindle_core.config import Settings
from spindle_core.orchestrator.publisher import OutboxRelay
from spindle_core.persistence.database import create_db_engine, make_session_factory
from spindle_core.persistence.repositories import PgSagaRepository
from spindle_core.streams.producer import RedisCommandBus
from spindle_core.templates.loader import TemplateStore

logger = logging.getLogger(__name__)

# How long (seconds) to wait for the relay thread to finish after stop is set.
_RELAY_JOIN_TIMEOUT = 5.0


def make_lifespan(settings: Settings) -> Callable[[FastAPI], AsyncGenerator[None, None]]:
    """Return an async context manager factory suitable for FastAPI(lifespan=…).

    Resources are built in dependency order; if any step fails the already-built
    resources are disposed in reverse order before the exception re-raises.
    This prevents connection leaks in misconfigured or partially-started processes.
    """

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        # Track what has been built so we can unwind on failure.
        engine = None
        redis_client = None
        stop_event: threading.Event | None = None
        relay_thread: threading.Thread | None = None

        try:
            # 1. Database engine
            engine = create_db_engine(settings.DATABASE_URL.get_secret_value())
            app.state.engine = engine

            # 2. Session factory
            session_factory = make_session_factory(engine)
            app.state.session_factory = session_factory

            # 3. Redis client.
            # decode_responses=True so that Redis string values are returned as
            # Python str rather than bytes.  RedisCommandBus serialises commands
            # to str via _serialize_command, so the client must speak str.
            # If a downstream consumer needs bytes it should create its own client;
            # sharing a decode_responses=True client with bytes callers would break.
            redis_client = redis_lib.Redis.from_url(
                settings.REDIS_URL.get_secret_value(),
                decode_responses=True,
            )
            app.state.redis = redis_client

            # 4. Tapestry store (validated from YAML)
            tapestry = load_tapestry_store(settings.TAPESTRY_PATH)
            app.state.tapestry = tapestry

            # 5. Template store (validated against tapestry)
            templates = TemplateStore.from_dir(settings.SAGA_TEMPLATES_DIR, tapestry)
            app.state.templates = templates

            # 6. Outbox relay in a daemon thread.
            # Each replica runs one relay; the engine's idempotency_key guard
            # makes concurrent relays safe (at-least-once delivery).
            relay_repo = PgSagaRepository(session_factory)
            relay_bus = RedisCommandBus(redis_client)
            relay = OutboxRelay(relay_repo, relay_bus)

            stop_event = threading.Event()
            relay_thread = threading.Thread(
                target=relay.run_forever,
                kwargs={"stop": stop_event},
                daemon=True,
                name="outbox-relay",
            )
            relay_thread.start()

            logger.info("Spindle startup complete")
            yield  # application is running

        except Exception:
            logger.exception("Spindle startup failed — unwinding")
            _shutdown(stop_event, relay_thread, redis_client, engine)
            raise

        else:
            # Normal shutdown path.
            logger.info("Spindle shutdown starting")
            _shutdown(stop_event, relay_thread, redis_client, engine)
            logger.info("Spindle shutdown complete")

    return _lifespan


def _shutdown(
    stop_event: threading.Event | None,
    relay_thread: threading.Thread | None,
    redis_client: "redis_lib.Redis | None",
    engine: object | None,
) -> None:
    """Stop the relay thread, close Redis, and dispose the DB engine.

    Mirrors the reverse of startup order.  Each step is attempted even if a
    previous one fails so all resources are released.
    """
    if stop_event is not None:
        stop_event.set()
    if relay_thread is not None and relay_thread.is_alive():
        relay_thread.join(timeout=_RELAY_JOIN_TIMEOUT)
        if relay_thread.is_alive():
            logger.warning("outbox-relay thread did not stop within %ss", _RELAY_JOIN_TIMEOUT)

    if redis_client is not None:
        try:
            redis_client.close()
        except Exception:
            logger.exception("error closing Redis client")

    if engine is not None:
        try:
            engine.dispose()
        except Exception:
            logger.exception("error disposing DB engine")
