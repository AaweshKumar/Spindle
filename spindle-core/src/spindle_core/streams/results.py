# Component: Spindle Architecture
# File: streams/results.py
# Description: Decode engine Events from the results stream and dispatch them
#              to the orchestrator Runner.

from __future__ import annotations

import logging

from spindle_core.engine import (
    CompensationFailed,
    CompensationSucceeded,
    InvalidTransition,
    StepFailed,
    StepSucceeded,
)
from spindle_core.engine.messages import Event
from spindle_core.orchestrator.ports import ConcurrencyConflict
from spindle_core.orchestrator.runner import Runner, UnknownSaga

logger = logging.getLogger(__name__)

# DESIGN NOTE (D2): the results STREAM KEY is deliberately NOT defined here. It is single-sourced from
# connectors.consumer.RESULTS_STREAM_KEY and passed to consume_loop(stream=...) by the composition root.
# Do not add a duplicate constant: streams (L1) may not import connectors (L2a), and duplicating risks drift.
RESULTS_GROUP_NAME = "spindle-orchestrator"

# Wire-format field names, matching connectors/consumer.py:251-257.
_TYPE = "type"
_SAGA_ID = "saga_id"
_STEP_ID = "step_id"
_REASON = "reason"

# Known type-name sets used to validate the "type" wire field.
# StepSucceeded and CompensationSucceeded have no `reason` field (engine/messages.py:9-11, 22-24).
_SUCCESS_TYPES: frozenset[str] = frozenset({"StepSucceeded", "CompensationSucceeded"})
_FAILED_TYPES: frozenset[str] = frozenset({"StepFailed", "CompensationFailed"})
_KNOWN_TYPES: frozenset[str] = _SUCCESS_TYPES | _FAILED_TYPES


def decode_result(fields: dict[str, str]) -> Event:
    """Decode a Redis stream message into an engine Event.

    Wire format (connectors/consumer.py:251-257):
      type     — event class name
      saga_id  — str
      step_id  — str
      reason   — str, present only when non-empty

    Raises:
        ValueError: unknown type, or missing/empty saga_id or step_id.
    """
    event_type = fields.get(_TYPE, "")
    if event_type not in _KNOWN_TYPES:
        raise ValueError(f"unknown result type: {event_type!r}")

    saga_id = fields.get(_SAGA_ID, "")
    if not saga_id:
        raise ValueError(f"missing or empty saga_id in result message (type={event_type!r})")

    step_id = fields.get(_STEP_ID, "")
    if not step_id:
        raise ValueError(f"missing or empty step_id in result message (type={event_type!r})")

    # A missing "reason" on a *Failed event decodes to "".
    reason = fields.get(_REASON, "")

    if event_type == "StepSucceeded":
        return StepSucceeded(saga_id=saga_id, step_id=step_id)
    if event_type == "StepFailed":
        return StepFailed(saga_id=saga_id, step_id=step_id, reason=reason)
    if event_type == "CompensationSucceeded":
        return CompensationSucceeded(saga_id=saga_id, step_id=step_id)
    # event_type == "CompensationFailed" — only remaining branch
    return CompensationFailed(saga_id=saga_id, step_id=step_id, reason=reason)


def make_result_handler(runner: Runner):
    """Return a handler compatible with streams.consumer.consume_loop.

    Acked (swallowed) exceptions:
        ValueError       — malformed message (unknown type, missing saga_id/step_id)
        UnknownSaga      — event arrived for a saga that was never started
        InvalidTransition — engine rejected the event as illegal for current state

    Re-raised (message retried via pending-entry reclaim):
        ConcurrencyConflict — optimistic-lock failure after all attempts
        Any other Exception — unexpected error; surface to the caller

    Signature matches consume_loop's handler parameter:
        callable(message_id: str, fields: dict[str, str]) -> None
    """

    def handle(message_id: str, fields: dict[str, str]) -> None:
        saga_id = fields.get(_SAGA_ID, "<unknown>")
        step_id = fields.get(_STEP_ID, "<unknown>")
        try:
            event = decode_result(fields)
        except ValueError as exc:
            logger.warning(
                "results handler: malformed message id=%s saga_id=%s step_id=%s error=%s",
                message_id,
                saga_id,
                step_id,
                exc,
            )
            return

        try:
            runner.handle_event(event.saga_id, event)
        except (UnknownSaga, InvalidTransition) as exc:
            logger.warning(
                "results handler: acking unrecoverable event id=%s saga_id=%s step_id=%s error=%s",
                message_id,
                event.saga_id,
                event.step_id,
                exc,
            )
            return
        except ConcurrencyConflict:
            # Exhausted all retry attempts inside Runner; let the stream re-deliver.
            raise
        except Exception:
            # Unexpected: surface so the caller can decide (re-raise = reclaim retry).
            raise

    return handle
