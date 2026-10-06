# Component: Spindle Architecture
# File: registry.py
# Description: Layer-1 heartbeat/liveness registry for Spindle worker processes.
#   Uses Redis TTL keys so that liveness state is visible across OS-process
#   boundaries.  "Alive" = key exists; "dead" = key expired or never set.


import logging
import time

import redis

logger = logging.getLogger(__name__)

# Key template: spindle:worker:live:{worker_id}
_KEY_PREFIX = "spindle:worker:live:"

# Default TTL in seconds.  Callers should heartbeat at least twice this often
# to avoid false "dead" readings under momentary latency.
DEFAULT_TTL_SECONDS: int = 30


class LivenessRegistry:

    def __init__(
        self,
        redis_client: redis.Redis,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._r = redis_client
        self._ttl = ttl_seconds

    # -- write side (called by the process being tracked) --

    def heartbeat(self, worker_id: str) -> None:
        """Record that *worker_id* is alive right now."""
        key = f"{_KEY_PREFIX}{worker_id}"
        # Value is the POSIX timestamp of the last heartbeat, stored as a
        # plain string.  last_seen() reads it back if staleness detail is
        # needed; is_alive() only checks key existence.
        self._r.set(key, str(time.time()), ex=self._ttl)
        logger.debug(
            "heartbeat recorded for worker %r (ttl=%ds)", worker_id, self._ttl
        )

    # -- read side (called by monitoring / orchestration code) --

    def is_alive(self, worker_id: str) -> bool:
        """Return True iff the worker has sent a heartbeat within the TTL window."""
        key = f"{_KEY_PREFIX}{worker_id}"
        return bool(self._r.exists(key))

    def last_seen(self, worker_id: str) -> float | None:
        """Return the POSIX timestamp of the last heartbeat, or None if expired/never."""
        key = f"{_KEY_PREFIX}{worker_id}"
        raw = self._r.get(key)
        if raw is None:
            return None
        return float(raw.decode() if isinstance(raw, bytes) else raw)
