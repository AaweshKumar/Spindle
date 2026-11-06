# Component: Spindle Architecture
# File: worker_process.py
# Description: Standalone process entrypoint for a Spindle connector worker.
#              Reads step work from spindle:work:{step_type} and calls the
#              configured microservice endpoint via ConnectorConsumer.
#
# This module is NOT imported by any __init__.py.
# Launch via: spindle-worker --step-type ReserveInventory  (after package install)
#          or: python -m spindle_core.worker_process --step-type ReserveInventory

from __future__ import annotations

import argparse
import logging
import signal
import socket
import threading
from pathlib import Path

import redis
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from spindle_core.api.dependencies import TapestryLoadError, load_tapestry_store
from spindle_core.connectors.runner import run_connector

logger = logging.getLogger(__name__)


class WorkerSettings(BaseSettings):
    """Settings for a standalone connector worker process.

    Only reads the fields it needs; completely independent of the main
    ``Settings`` class (which requires DATABASE_URL, JWT_SECRET, etc.).
    """

    model_config = SettingsConfigDict(
        env_prefix="",
        extra="ignore",
        hide_input_in_errors=True,
        frozen=True,
    )

    REDIS_URL: SecretStr
    TAPESTRY_PATH: Path = Path("tapestry.yaml")
    STEP_TYPE: str | None = None
    HEARTBEAT_INTERVAL_SECONDS: float = 15.0


def main(argv: list[str] | None = None) -> int:
    """Process entrypoint for a connector worker.

    Wires together settings, Tapestry config, and Redis; installs
    SIGTERM/SIGINT stop-event handlers; runs the connector loop; performs
    cleanup on exit.

    Returns 0 on clean shutdown, 1 on unexpected exception, 2 on
    configuration error (no step type, unknown step type, bad settings).
    """
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description="Spindle connector worker")
    parser.add_argument(
        "--step-type",
        default=None,
        help="Step type this worker handles (overrides STEP_TYPE env var)",
    )
    args = parser.parse_args(argv)

    # --- settings ---
    try:
        settings = WorkerSettings()
    except Exception:
        logger.exception("worker_process: failed to load settings")
        return 2

    # --- resolve step_type ---
    step_type: str | None = args.step_type if args.step_type is not None else settings.STEP_TYPE
    if not step_type:
        logger.error(
            "worker_process: no step type provided; "
            "set --step-type or STEP_TYPE env var"
        )
        return 2

    # --- tapestry ---
    try:
        tapestry = load_tapestry_store(settings.TAPESTRY_PATH)
    except TapestryLoadError:
        logger.exception(
            "worker_process: failed to load tapestry from %s", settings.TAPESTRY_PATH
        )
        return 2

    if tapestry.get(step_type) is None:
        logger.error(
            "worker_process: step_type %r not found in tapestry; "
            "check tapestry config at %s",
            step_type,
            settings.TAPESTRY_PATH,
        )
        return 2

    # --- redis ---
    redis_client = redis.Redis.from_url(
        settings.REDIS_URL.get_secret_value(),
        decode_responses=True,
    )

    consumer_name = f"worker:{socket.gethostname()}"
    worker_id = f"{step_type}:{consumer_name}"

    stop = threading.Event()

    def _handle_signal(signum, frame) -> None:  # noqa: ANN001
        stop.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        run_connector(
            redis_client,
            tapestry,
            step_type,
            consumer_name,
            worker_id,
            heartbeat_interval_seconds=settings.HEARTBEAT_INTERVAL_SECONDS,
            stop=stop,
        )
        return 0
    except Exception:
        logger.exception("worker_process: unexpected error in connector loop")
        return 1
    finally:
        try:
            redis_client.close()
        except Exception:
            logger.exception("worker_process: error closing Redis client")


if __name__ == "__main__":
    raise SystemExit(main())

