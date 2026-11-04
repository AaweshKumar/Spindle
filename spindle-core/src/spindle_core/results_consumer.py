# Component: Spindle Architecture
# File: results_consumer.py
# Description: Standalone process entrypoint for the results consumer.
#              Reads spindle:results, decodes events, and dispatches to the Runner.
#
# This module is NOT imported by any __init__.py.
# Launch via: uv run spindle-results-consumer  (after package install)
#          or: uv run --no-sync python -m spindle_core.results_consumer

from __future__ import annotations

import logging
import signal
import socket
import threading

import redis

from spindle_core.config import get_settings
from spindle_core.persistence.database import create_db_engine, make_session_factory
from spindle_core.persistence.repositories import PgSagaRepository
from spindle_core.orchestrator.runner import Runner
from spindle_core.streams.consumer import consume_loop
from spindle_core.streams.keys import RESULTS_GROUP_NAME, RESULTS_STREAM_KEY
from spindle_core.streams.results import make_dead_letter_writer, make_result_handler

logger = logging.getLogger(__name__)


def run(
    stop: threading.Event,
    *,
    redis_client: redis.Redis,
    runner: Runner,
    consumer_name: str,
) -> None:
    """Consume spindle:results until ``stop`` is set.

    Builds a result handler with dead-letter support and delegates to
    consume_loop.  Returns normally when ``stop`` is set (within one
    block_ms interval).  Redis or infrastructure errors propagate to the
    caller.

    Never logs URLs, passwords, or secrets.
    """
    handler = make_result_handler(
        runner,
        dead_letter=make_dead_letter_writer(redis_client),
    )
    logger.info(
        "results-consumer starting: stream=%s group=%s consumer=%s",
        RESULTS_STREAM_KEY,
        RESULTS_GROUP_NAME,
        consumer_name,
    )
    consume_loop(
        redis_client,
        consumer_name,
        handler,
        stream=RESULTS_STREAM_KEY,
        group=RESULTS_GROUP_NAME,
        stop=stop,
    )


def main() -> int:
    """Process entrypoint.

    Wires together settings, DB, Redis, and the runner; installs SIGTERM/SIGINT
    handlers; runs the consume loop; performs cleanup on exit.

    Returns 0 on clean shutdown, 1 on unexpected exception.
    """
    logging.basicConfig(level=logging.INFO)

    try:
        settings = get_settings()
        engine = create_db_engine(settings.DATABASE_URL.get_secret_value())
        session_factory = make_session_factory(engine)
        repo = PgSagaRepository(session_factory)
        runner = Runner(repo)
        redis_client = redis.Redis.from_url(
            settings.REDIS_URL.get_secret_value(),
            decode_responses=True,
        )
        consumer_name = f"results:{socket.gethostname()}"

        stop = threading.Event()

        def _handle_signal(signum, frame) -> None:  # noqa: ANN001
            logger.info("results-consumer received signal %s, stopping", signum)
            stop.set()

        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

        try:
            run(stop, redis_client=redis_client, runner=runner, consumer_name=consumer_name)
            return 0
        except Exception:
            logger.exception("results-consumer encountered an unexpected error")
            return 1
        finally:
            try:
                redis_client.close()
            except Exception:
                logger.exception("error closing Redis client")
            try:
                engine.dispose()
            except Exception:
                logger.exception("error disposing DB engine")

    except Exception:
        logger.exception("results-consumer failed during startup")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

