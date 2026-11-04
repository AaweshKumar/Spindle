# Component: Spindle Architecture
# File: streams/results.py
# Description: Decode engine Events from the results stream and dispatch them
#              to the orchestrator Runner.

from __future__ import annotations

import logging
from collections.abc import Callable

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
from .keys import DEAD_RESULTS_STREAM_KEY, RESULTS_GROUP_NAME  # noqa: F401 — intentional re-export

logger = logging.getLogger(__name__)

# DESIGN NOTE (D2): RESULTS_STREAM_KEY and RESULTS_GROUP_NAME now live in streams/keys.py
# (single source of truth for all stream/group identifiers).  RESULTS_GROUP_NAME is
# imported from there above and re-exported so callers can still import it from this module.
# Do not duplicate constants here: streams/ is L1 and must not import from connectors/ (L2a).

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


def make_result_handler(
    runner: Runner,
    dead_letter: Callable[[str, dict[str, str], str], None] | None = None,
):
    """Return a handler compatible with streams.consumer.consume_loop.

    Acked (swallowed) exceptions:
        ValueError       — malformed message (unknown type, missing saga_id/step_id)
        UnknownSaga      — event arrived for a saga that was never started
        InvalidTransition — engine rejected the event as illegal for current state

    Re-raised (message retried via pending-entry reclaim):
        ConcurrencyConflict — optimistic-lock failure after all attempts
        Any other Exception — unexpected error; surface to the caller

    When ``dead_letter`` is provided, the three acked exception classes above
    additionally invoke ``dead_letter(message_id, fields, reason)`` before
    returning.  ConcurrencyConflict and all other exceptions are never
    dead-lettered and are re-raised exactly as when ``dead_letter`` is None.
    If ``dead_letter`` itself raises, the exception propagates (message is not acked).

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
            if dead_letter is not None:
                dead_letter(message_id, fields, f"malformed result: {exc}")
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
            if dead_letter is not None:
                dead_letter(
                    message_id,
                    fields,
                    f"{type(exc).__name__}: {exc}",
                )
            return
        except ConcurrencyConflict:
            # Exhausted all retry attempts inside Runner; let the stream re-deliver.
            raise
        except Exception:
            # Unexpected: surface so the caller can decide (re-raise = reclaim retry).
            raise

    return handle


def make_dead_letter_writer(
    redis_client,
) -> Callable[[str, dict[str, str], str], None]:
    """Return a callable that XADDs failed result messages to DEAD_RESULTS_STREAM_KEY.

    Signature: (message_id: str, fields: dict[str, str], reason: str) -> None

    The original fields are copied to the dead-letter entry.  If the original
    fields already contain a "reason" key its value is preserved under
    "original_reason" to avoid losing it.  Redis errors propagate to the caller
    so the message is not acked.
    """

    def _write(message_id: str, fields: dict[str, str], reason: str) -> None:
        dead_fields: dict[str, str] = dict(fields)
        if "reason" in dead_fields:
            dead_fields["original_reason"] = dead_fields["reason"]
        dead_fields["reason"] = reason
        redis_client.xadd(DEAD_RESULTS_STREAM_KEY, dead_fields)

    return _write
