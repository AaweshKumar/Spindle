# Component: Spindle Architecture
# File: streams/router.py
# Description: Command router — fans out commands from spindle:commands to
#              per-step-type work streams (spindle:work:{step_type}).
#
#   Reads DispatchStep / DispatchCompensation messages from STREAM_KEY via the
#   standard consume_loop / ensure_group helpers, then:
#     • Valid command with known step_type → XADD to WORK_STREAM_PREFIX+step_type.
#     • Valid command with empty or unknown step_type → XADD a failure event
#       (StepFailed / CompensationFailed) to RESULTS_STREAM_KEY.
#     • Unparseable message → XADD original fields + "reason" to
#       DEAD_COMMANDS_STREAM_KEY.
#   Redis / infra errors are re-raised so consume_loop does NOT ack the message.
#
#   Layering: imports only from streams/ (L1) and tapestry/ (L2a, same layer as
#   connectors/).  Never imports from connectors/, api/, orchestrator/, or engine/.

from __future__ import annotations

import logging
import threading

import redis

from .consumer import consume_loop, ensure_group
from .keys import (
    DEAD_COMMANDS_STREAM_KEY,
    RESULTS_STREAM_KEY,
    ROUTER_GROUP_NAME,
    STREAM_KEY,
    WORK_STREAM_PREFIX,
)

logger = logging.getLogger(__name__)

# The set of command type-name strings the router recognises.
_DISPATCH_TYPES: frozenset[str] = frozenset({"DispatchStep", "DispatchCompensation"})

# Map command type → result failure event type published on bad step_type.
_FAILURE_EVENT: dict[str, str] = {
    "DispatchStep": "StepFailed",
    "DispatchCompensation": "CompensationFailed",
}


class CommandRouter:
    """
    Fan-out router for the spindle:commands stream.

    Instantiate once per process; call ``run()`` to block and process messages,
    or ``route_once(message_id, fields)`` to handle a single message (used in
    tests and for manual invocation).

    The TapestryStore is used read-only to validate that step_type is known.
    No tapestry/ imports are needed here — the store is injected.
    """

    def __init__(
        self,
        redis_client: redis.Redis,
        tapestry,  # TapestryStore — typed as object to avoid circular import risk
        consumer_name: str,
    ) -> None:
        self._r = redis_client
        self._tapestry = tapestry
        self._consumer_name = consumer_name

    # -- public API --

    def run(self, stop: threading.Event | None = None) -> None:
        """Block and route messages until stopped or the thread is killed.

        Passes ``stop`` to ``consume_loop``: when the event is set the loop
        exits at the next block_ms boundary and this method returns normally.
        When ``stop`` is None (default) the loop runs forever — matching the
        original behaviour for callers that manage shutdown via the process
        lifetime.
        """
        logger.info(
            "command-router starting: stream=%r group=%r consumer=%r",
            STREAM_KEY,
            ROUTER_GROUP_NAME,
            self._consumer_name,
        )
        consume_loop(
            self._r,
            self._consumer_name,
            self._route_message,
            stream=STREAM_KEY,
            group=ROUTER_GROUP_NAME,
            stop=stop,
        )

    def route_once(self, message_id: str, fields: dict[str, str]) -> None:
        """
        Route a single message outside the consume loop.

        Primarily for tests and manual invocation.  Obeys the same error
        contract as the handler passed to consume_loop: raises on Redis /
        infra errors, returns normally for all data-quality problems.
        """
        self._route_message(message_id, fields)

    # -- internal --

    def _route_message(self, message_id: str, fields: dict[str, str]) -> None:
        msg_type = fields.get("type", "")
        saga_id = fields.get("saga_id", "")
        step_id = fields.get("step_id", "")

        # ------------------------------------------------------------------ #
        # Behaviour 4: unparseable — unknown/missing type, or empty identity  #
        # ------------------------------------------------------------------ #
        if msg_type not in _DISPATCH_TYPES or not saga_id or not step_id:
            reason = (
                f"unroutable command: type={msg_type!r} "
                f"saga_id={saga_id!r} step_id={step_id!r}"
            )
            logger.error(
                "dead-lettering command id=%s: %s", message_id, reason
            )
            dead_fields = dict(fields)
            dead_fields["reason"] = reason
            # Re-raise on Redis error — message must not be acked.
            self._r.xadd(DEAD_COMMANDS_STREAM_KEY, dead_fields)
            return

        step_type = fields.get("step_type", "")

        # ------------------------------------------------------------------ #
        # Behaviour 3: parseable but step_type empty or unknown to Tapestry   #
        # ------------------------------------------------------------------ #
        if not step_type or self._tapestry.get(step_type) is None:
            failure_event = _FAILURE_EVENT[msg_type]
            reason = (
                f"router: step_type={step_type!r} is not registered in TapestryStore"
                if step_type
                else "router: step_type is empty"
            )
            logger.error(
                "publishing %s for unknown step_type=%r: saga=%s step=%s",
                failure_event,
                step_type,
                saga_id,
                step_id,
            )
            result_fields: dict[str, str] = {
                "type": failure_event,
                "saga_id": saga_id,
                "step_id": step_id,
                "reason": reason,
            }
            # Re-raise on Redis error — message must not be acked.
            self._r.xadd(RESULTS_STREAM_KEY, result_fields)
            return

        # ------------------------------------------------------------------ #
        # Behaviour 2: valid command — fan-out to work stream                 #
        # ------------------------------------------------------------------ #
        work_stream = f"{WORK_STREAM_PREFIX}{step_type}"
        logger.debug(
            "routing %s saga=%s step=%s step_type=%s → %s",
            msg_type,
            saga_id,
            step_id,
            step_type,
            work_stream,
        )
        # Re-raise on Redis error — message must not be acked.
        self._r.xadd(work_stream, fields)

