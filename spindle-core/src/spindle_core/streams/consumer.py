import logging
import time
from collections.abc import Callable

import redis

logger = logging.getLogger(__name__)

STREAM_KEY = "spindle:commands"
GROUP_NAME = "spindle-workers"


def ensure_group(r: redis.Redis, stream: str = STREAM_KEY, group: str = GROUP_NAME):
    """Create the consumer group if it doesn't exist yet."""
    try:
        r.xgroup_create(stream, group, id="0", mkstream=True)
    except redis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


def consume_loop(
    r: redis.Redis,
    consumer_name: str,
    handler: Callable[[str, dict[str, str]], None],
    stream: str = STREAM_KEY,
    group: str = GROUP_NAME,
    block_ms: int = 2000,
    batch_size: int = 10,
    claim_idle_ms: int = 30_000,
):
    """
    Read from stream via XREADGROUP, call handler(message_id, fields) for each,
    then XACK. On startup and periodically, reclaim stuck messages via XPENDING+XCLAIM.

    handler signature: (message_id: str, fields: dict[str,str]) -> None
    handler must raise on failure so the message is NOT acked.
    """
    ensure_group(r, stream, group)

    claim_counter = 0

    while True:
        # every 10 iterations, try to reclaim old pending messages
        claim_counter += 1
        if claim_counter % 10 == 1:
            _reclaim_pending(r, stream, group, consumer_name, handler, claim_idle_ms)

        # read new messages
        entries = r.xreadgroup(
            group, consumer_name, {stream: ">"}, count=batch_size, block=block_ms
        )
        if not entries:
            continue

        for _stream_name, messages in entries:
            for msg_id, fields in messages:
                # fields come as bytes from redis-py; decode
                decoded = {
                    (k.decode() if isinstance(k, bytes) else k):
                    (v.decode() if isinstance(v, bytes) else v)
                    for k, v in fields.items()
                }
                try:
                    handler(msg_id, decoded)
                    r.xack(stream, group, msg_id)
                except Exception:
                    logger.exception("handler failed for %s, will retry on reclaim", msg_id)


def _reclaim_pending(r, stream, group, consumer_name, handler, idle_ms):
    """XPENDING + XCLAIM: pick up messages other consumers dropped."""
    try:
        pending = r.xpending_range(stream, group, "-", "+", count=10)
    except redis.ResponseError:
        return

    for entry in pending:
        msg_id = entry["message_id"]
        if isinstance(msg_id, bytes):
            msg_id = msg_id.decode()
        idle = entry.get("time_since_delivered", 0)
        if idle < idle_ms:
            continue
        try:
            claimed = r.xclaim(stream, group, consumer_name, min_idle_time=idle_ms, message_ids=[msg_id])
            for claimed_id, fields in claimed:
                decoded = {
                    (k.decode() if isinstance(k, bytes) else k):
                    (v.decode() if isinstance(v, bytes) else v)
                    for k, v in fields.items()
                }
                handler(claimed_id, decoded)
                r.xack(stream, group, claimed_id)
        except Exception:
            logger.exception("reclaim failed for %s", msg_id)
