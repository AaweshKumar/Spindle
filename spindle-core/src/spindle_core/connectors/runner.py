# Component: Spindle Architecture
# File: runner.py
# Description: Process entry point for a Spindle connector.
#
#   Starts ConnectorConsumer in the calling thread (blocking) and a background
#   daemon thread that sends periodic heartbeats to the Layer-1 liveness
#   registry.  The heartbeat thread exits automatically when the process ends.

from __future__ import annotations

import logging
import threading

import redis

from spindle_core.tapestry.loader import TapestryStore
from spindle_core.workers.registry import LivenessRegistry

from .caller import HttpClient
from .consumer import ConnectorConsumer
from .outcomes import OutcomeTracker

logger = logging.getLogger(__name__)


def run_connector(
    redis_client: redis.Redis,
    tapestry: TapestryStore,
    step_type: str,
    consumer_name: str,
    worker_id: str,
    *,
    heartbeat_interval_seconds: float = 15.0,
    http_client: HttpClient | None = None,
) -> None:
    """
    Start the connector for *step_type*.  Blocks until the process is killed.

    Spawns a daemon heartbeat thread that calls LivenessRegistry.heartbeat()
    every *heartbeat_interval_seconds*.  The default interval (15 s) is half
    the default TTL (30 s) so a single missed heartbeat does not immediately
    mark the process dead.

    Args:
        redis_client:               Shared Redis connection (injected; do not
                                    open a second one just for the connector).
        tapestry:                   Loaded TapestryStore for this org.
        step_type:                  The step type this process consumes.
        consumer_name:              Unique name within the consumer group
                                    (e.g. hostname + pid).
        worker_id:                  Identifier written to the liveness key
                                    (e.g. f"{step_type}:{consumer_name}").
        heartbeat_interval_seconds: How often to call LivenessRegistry.heartbeat().
        http_client:                Override for testing; defaults to
                                    UrllibHttpClient.
    """
    registry = LivenessRegistry(redis_client)
    tracker = OutcomeTracker()
    stop = threading.Event()

    def _heartbeat_loop() -> None:
        """Send a heartbeat, sleep, repeat until *stop* is set."""
        while True:
            try:
                registry.heartbeat(worker_id)
            except Exception:
                logger.exception(
                    "heartbeat failed for worker_id=%r; will retry", worker_id
                )
            if stop.wait(heartbeat_interval_seconds):
                break

    heartbeat_thread = threading.Thread(
        target=_heartbeat_loop,
        name=f"hb-{worker_id}",
        daemon=True,   # exits automatically when the main thread exits
    )
    heartbeat_thread.start()
    logger.info(
        "connector process starting: step_type=%r worker_id=%r "
        "heartbeat_interval=%.1fs",
        step_type,
        worker_id,
        heartbeat_interval_seconds,
    )

    consumer = ConnectorConsumer(
        redis_client=redis_client,
        tapestry=tapestry,
        step_type=step_type,
        consumer_name=consumer_name,
        tracker=tracker,
        http_client=http_client,
    )

    try:
        consumer.run()
    finally:
        stop.set()
        heartbeat_thread.join(timeout=5.0)
