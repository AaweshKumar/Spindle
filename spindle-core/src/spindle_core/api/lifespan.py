# Component: Spindle Architecture
# File: lifespan.py
# Description: FastAPI lifespan for the Spindle orchestration platform.

"""Application lifespan — composition root.

``make_lifespan(settings)`` returns an async context manager that:

1. Builds shared resources in order onto ``app.state``.
2. Starts the outbox relay in a daemon thread.
3. Starts the command router in a daemon thread.
4. On shutdown, stops both threads gracefully and tears down resources in
   reverse.

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
from spindle_core.streams.router import CommandRouter
from spindle_core.templates.loader import TemplateStore

logger = logging.getLogger(__name__)

# How long (seconds) to wait for the relay thread to finish after stop is set.
_RELAY_JOIN_TIMEOUT = 5.0
# How long (seconds) to wait for the router thread to finish after stop is set.
# router.run(stop) returns within one block_ms interval (≤ 2 s) once stop is
# set, so the join should complete well within this limit.
_ROUTER_JOIN_TIMEOUT = 5.0


def _run_relay_supervised(
    relay: OutboxRelay,
    stop: threading.Event,
) -> None:
    """Run the relay with exponential-backoff restart on failure.

    Loops until ``stop`` is set.  After each crash the function waits
    ``backoff`` seconds (starting at 1, doubling to 30) before
    restarting so a persistent external fault does not busy-loop.
    After a clean return from ``run_forever`` (i.e. ``stop`` was set)
    the loop exits normally.

    Never logs URLs, passwords, or secrets — only the exception type
    and message are surfaced.
    """
    _MIN_BACKOFF = 1.0
    _MAX_BACKOFF = 30.0
    backoff = _MIN_BACKOFF
    while not stop.is_set():
        try:
            relay.run_forever(stop)
            # Clean return means stop was set; exit loop.
            backoff = _MIN_BACKOFF
        except Exception:
            logger.exception(
                "outbox-relay crashed; restarting in %.0fs", backoff
            )
            stop.wait(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF)


def _run_router_supervised(
    router: CommandRouter,
    stop: threading.Event,
) -> None:
    """Run the command router with exponential-backoff restart on failure.

    Passes ``stop`` to ``router.run()``, which forwards it to ``consume_loop``.
    The loop exits at the next block_ms boundary (≤ 2 s) after ``stop`` is set,
    ``router.run()`` returns normally, and this function exits cleanly without
    logging a warning.  The join in ``_shutdown`` therefore completes well within
    ``_ROUTER_JOIN_TIMEOUT``.

    On an unexpected exception the supervisor waits ``backoff`` seconds
    (doubling from 1 s to 30 s) and restarts, unless ``stop`` is already set.

    Never logs URLs, passwords, or secrets.
    """
    _MIN_BACKOFF = 1.0
    _MAX_BACKOFF = 30.0
    backoff = _MIN_BACKOFF
    while not stop.is_set():
        try:
            router.run(stop)
            # Clean return from run() means stop was set; exit supervisor loop.
            break
        except Exception:
            logger.exception(
                "command-router crashed; restarting in %.0fs", backoff
            )
            stop.wait(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF)


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
        router_thread: threading.Thread | None = None

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

            # Shared stop event — both daemon threads observe the same event so
            # a single stop_event.set() shuts them both down.
            stop_event = threading.Event()

            # 6. Outbox relay in a daemon thread.
            # Each replica runs one relay; the engine's idempotency_key guard
            # makes concurrent relays safe (at-least-once delivery).
            relay_repo = PgSagaRepository(session_factory)
            relay_bus = RedisCommandBus(redis_client)
            relay = OutboxRelay(relay_repo, relay_bus)

            relay_thread = threading.Thread(
                target=_run_relay_supervised,
                args=(relay, stop_event),
                daemon=True,
                name="outbox-relay",
            )
            relay_thread.start()

            # 7. Command router in a daemon thread.
            # Reads spindle:commands and fans out to spindle:work:{step_type}.
            import socket
            router_consumer_name = f"router:{socket.gethostname()}"
            router = CommandRouter(redis_client, tapestry, router_consumer_name)
            router_thread = threading.Thread(
                target=_run_router_supervised,
                args=(router, stop_event),
                daemon=True,
                name="command-router",
            )
            router_thread.start()

            logger.info("Spindle startup complete")
            yield  # application is running

        except Exception:
            logger.exception("Spindle startup failed — unwinding")
            _shutdown(stop_event, relay_thread, router_thread, redis_client, engine)
            raise

        else:
            # Normal shutdown path.
            logger.info("Spindle shutdown starting")
            _shutdown(stop_event, relay_thread, router_thread, redis_client, engine)
            logger.info("Spindle shutdown complete")

    return _lifespan


def _shutdown(
    stop_event: threading.Event | None,
    relay_thread: threading.Thread | None,
    router_thread: threading.Thread | None,
    redis_client: "redis_lib.Redis | None",
    engine: object | None,
) -> None:
    """Stop the relay and router threads, close Redis, and dispose the DB engine.

    Mirrors the reverse of startup order.  Each step is attempted even if a
    previous one fails so all resources are released.
    """
    if stop_event is not None:
        stop_event.set()
    if relay_thread is not None and relay_thread.is_alive():
        relay_thread.join(timeout=_RELAY_JOIN_TIMEOUT)
        if relay_thread.is_alive():
            logger.warning("outbox-relay thread did not stop within %ss", _RELAY_JOIN_TIMEOUT)
    if router_thread is not None and router_thread.is_alive():
        router_thread.join(timeout=_ROUTER_JOIN_TIMEOUT)
        if router_thread.is_alive():
            logger.warning("command-router thread did not stop within %ss", _ROUTER_JOIN_TIMEOUT)

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
