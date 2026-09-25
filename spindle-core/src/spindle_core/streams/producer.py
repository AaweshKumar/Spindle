import json
import logging
import threading
from dataclasses import asdict

import redis

from spindle_core.orchestrator.ports import CommandBus, OutboxRecord

logger = logging.getLogger(__name__)

STREAM_KEY = "spindle:commands"


def _serialize_command(cmd) -> dict[str, str]:
    """Flatten a Command dataclass into a Redis-friendly string dict."""
    data = {"type": type(cmd).__name__, **asdict(cmd)}
    return {k: str(v) for k, v in data.items()}


class RedisCommandBus:
    """CommandBus implementation: XADD to a Redis Stream."""

    def __init__(self, redis_client: redis.Redis, stream: str = STREAM_KEY):
        self._r = redis_client
        self._stream = stream

    def publish(self, command) -> None:
        fields = _serialize_command(command)
        self._r.xadd(self._stream, fields)
