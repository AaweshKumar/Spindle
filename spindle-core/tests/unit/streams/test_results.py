# tests/unit/streams/test_results.py
# Fakes only — no real Redis, no real Postgres.

from __future__ import annotations

import logging

import pytest

from spindle_core.engine import (
    CompensationFailed,
    CompensationSucceeded,
    InvalidTransition,
    StepFailed,
    StepSucceeded,
)
from spindle_core.orchestrator.ports import ConcurrencyConflict
from spindle_core.orchestrator.runner import UnknownSaga
from spindle_core.streams.results import RESULTS_GROUP_NAME, decode_result, make_result_handler

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeRunner:
    """Minimal stand-in for Runner. Configurable to raise on handle_event."""

    def __init__(self, side_effect=None):
        self.calls: list[tuple[str, object]] = []
        self._side_effect = side_effect

    def handle_event(self, saga_id: str, event) -> None:
        self.calls.append((saga_id, event))
        if self._side_effect is not None:
            raise self._side_effect


# ---------------------------------------------------------------------------
# RESULTS_GROUP_NAME
# ---------------------------------------------------------------------------


def test_results_group_name_value():
    assert RESULTS_GROUP_NAME == "spindle-orchestrator"


# ---------------------------------------------------------------------------
# decode_result — happy paths (all four event types)
# ---------------------------------------------------------------------------


def test_decode_step_succeeded():
    event = decode_result({"type": "StepSucceeded", "saga_id": "s1", "step_id": "reserve"})
    assert isinstance(event, StepSucceeded)
    assert event.saga_id == "s1"
    assert event.step_id == "reserve"


def test_decode_step_failed_with_reason():
    event = decode_result(
        {"type": "StepFailed", "saga_id": "s2", "step_id": "charge", "reason": "timeout"}
    )
    assert isinstance(event, StepFailed)
    assert event.saga_id == "s2"
    assert event.step_id == "charge"
    assert event.reason == "timeout"


def test_decode_step_failed_missing_reason_defaults_to_empty():
    # "reason" key absent — connectors omit it when empty (consumer.py:256)
    event = decode_result({"type": "StepFailed", "saga_id": "s3", "step_id": "ship"})
    assert isinstance(event, StepFailed)
    assert event.reason == ""


def test_decode_compensation_succeeded():
    event = decode_result(
        {"type": "CompensationSucceeded", "saga_id": "s4", "step_id": "reserve"}
    )
    assert isinstance(event, CompensationSucceeded)
    assert event.saga_id == "s4"
    assert event.step_id == "reserve"


def test_decode_compensation_failed_with_reason():
    event = decode_result(
        {
            "type": "CompensationFailed",
            "saga_id": "s5",
            "step_id": "charge",
            "reason": "500 error",
        }
    )
    assert isinstance(event, CompensationFailed)
    assert event.reason == "500 error"


def test_decode_compensation_failed_missing_reason_defaults_to_empty():
    event = decode_result({"type": "CompensationFailed", "saga_id": "s6", "step_id": "ship"})
    assert isinstance(event, CompensationFailed)
    assert event.reason == ""


# ---------------------------------------------------------------------------
# decode_result — ValueError cases
# ---------------------------------------------------------------------------


def test_decode_raises_on_unknown_type():
    with pytest.raises(ValueError, match="unknown result type"):
        decode_result({"type": "SomethingElse", "saga_id": "s1", "step_id": "x"})


def test_decode_raises_on_missing_type_key():
    with pytest.raises(ValueError, match="unknown result type"):
        decode_result({"saga_id": "s1", "step_id": "x"})


def test_decode_raises_on_empty_type():
    with pytest.raises(ValueError, match="unknown result type"):
        decode_result({"type": "", "saga_id": "s1", "step_id": "x"})


def test_decode_raises_on_missing_saga_id():
    with pytest.raises(ValueError, match="missing or empty saga_id"):
        decode_result({"type": "StepSucceeded", "step_id": "x"})


def test_decode_raises_on_empty_saga_id():
    with pytest.raises(ValueError, match="missing or empty saga_id"):
        decode_result({"type": "StepSucceeded", "saga_id": "", "step_id": "x"})


def test_decode_raises_on_missing_step_id():
    with pytest.raises(ValueError, match="missing or empty step_id"):
        decode_result({"type": "StepSucceeded", "saga_id": "s1"})


def test_decode_raises_on_empty_step_id():
    with pytest.raises(ValueError, match="missing or empty step_id"):
        decode_result({"type": "StepSucceeded", "saga_id": "s1", "step_id": ""})


# ---------------------------------------------------------------------------
# make_result_handler — happy path
# ---------------------------------------------------------------------------


def test_handler_happy_path_calls_runner():
    runner = FakeRunner()
    handle = make_result_handler(runner)
    handle("msg-1", {"type": "StepSucceeded", "saga_id": "saga-A", "step_id": "reserve"})
    assert len(runner.calls) == 1
    saga_id, event = runner.calls[0]
    assert saga_id == "saga-A"
    assert isinstance(event, StepSucceeded)
    assert event.step_id == "reserve"


def test_handler_happy_path_returns_none():
    runner = FakeRunner()
    handle = make_result_handler(runner)
    result = handle("msg-2", {"type": "CompensationSucceeded", "saga_id": "s1", "step_id": "charge"})
    assert result is None


# ---------------------------------------------------------------------------
# make_result_handler — ack (swallow) cases
# ---------------------------------------------------------------------------


def test_handler_acks_malformed_unknown_type(caplog):
    runner = FakeRunner()
    handle = make_result_handler(runner)
    with caplog.at_level(logging.WARNING, logger="spindle_core.streams.results"):
        handle("msg-3", {"type": "BoguType", "saga_id": "s1", "step_id": "x"})
    assert len(runner.calls) == 0
    assert any(
        "s1" in r.message and "x" in r.message
        for r in caplog.records
        if r.levelno == logging.WARNING
    ), "Expected WARNING containing saga_id='s1' and step_id='x'"


def test_handler_acks_malformed_missing_saga_id(caplog):
    runner = FakeRunner()
    handle = make_result_handler(runner)
    with caplog.at_level(logging.WARNING, logger="spindle_core.streams.results"):
        handle("msg-4", {"type": "StepSucceeded", "step_id": "x"})
    assert len(runner.calls) == 0
    # saga_id falls back to "<unknown>"; step_id "x" is present in fields
    assert any(
        "<unknown>" in r.message and "x" in r.message
        for r in caplog.records
        if r.levelno == logging.WARNING
    ), "Expected WARNING containing '<unknown>' for saga_id and step_id='x'"


def test_handler_acks_malformed_missing_step_id(caplog):
    runner = FakeRunner()
    handle = make_result_handler(runner)
    with caplog.at_level(logging.WARNING, logger="spindle_core.streams.results"):
        handle("msg-5", {"type": "StepSucceeded", "saga_id": "s1"})
    assert len(runner.calls) == 0
    # step_id falls back to "<unknown>"
    assert any(
        "s1" in r.message and "<unknown>" in r.message
        for r in caplog.records
        if r.levelno == logging.WARNING
    ), "Expected WARNING containing saga_id='s1' and '<unknown>' for step_id"


def test_handler_acks_unknown_saga(caplog):
    runner = FakeRunner(side_effect=UnknownSaga("saga-X"))
    handle = make_result_handler(runner)
    with caplog.at_level(logging.WARNING, logger="spindle_core.streams.results"):
        result = handle("msg-6", {"type": "StepSucceeded", "saga_id": "saga-X", "step_id": "x"})
    assert result is None  # did not raise
    assert any(
        "saga-X" in r.message and "x" in r.message
        for r in caplog.records
        if r.levelno == logging.WARNING
    ), "Expected WARNING containing saga_id='saga-X' and step_id='x'"


def test_handler_acks_invalid_transition(caplog):
    runner = FakeRunner(side_effect=InvalidTransition("bad state"))
    handle = make_result_handler(runner)
    with caplog.at_level(logging.WARNING, logger="spindle_core.streams.results"):
        result = handle("msg-7", {"type": "StepSucceeded", "saga_id": "s1", "step_id": "x"})
    assert result is None
    assert any(
        "s1" in r.message and "x" in r.message
        for r in caplog.records
        if r.levelno == logging.WARNING
    ), "Expected WARNING containing saga_id='s1' and step_id='x'"


# ---------------------------------------------------------------------------
# make_result_handler — re-raise cases
# ---------------------------------------------------------------------------


def test_handler_reraises_concurrency_conflict():
    runner = FakeRunner(side_effect=ConcurrencyConflict("gave up"))
    handle = make_result_handler(runner)
    with pytest.raises(ConcurrencyConflict):
        handle("msg-8", {"type": "StepSucceeded", "saga_id": "s1", "step_id": "x"})


def test_handler_reraises_arbitrary_exception():
    runner = FakeRunner(side_effect=RuntimeError("unexpected"))
    handle = make_result_handler(runner)
    with pytest.raises(RuntimeError, match="unexpected"):
        handle("msg-9", {"type": "StepSucceeded", "saga_id": "s1", "step_id": "x"})


# ---------------------------------------------------------------------------
# make_result_handler — duplicate event (runner returns normally = ack)
# ---------------------------------------------------------------------------


def test_handler_duplicate_event_is_acked():
    """Runner.handle_event returns normally on duplicate (engine sees no state change).
    Handler should also return normally — duplicate is acked, not retried."""
    runner = FakeRunner()  # no side_effect => returns None
    handle = make_result_handler(runner)
    handle("msg-10", {"type": "StepSucceeded", "saga_id": "s1", "step_id": "reserve"})
    handle("msg-11", {"type": "StepSucceeded", "saga_id": "s1", "step_id": "reserve"})
    assert len(runner.calls) == 2  # both delivered; runner handles idempotency internally


# ---------------------------------------------------------------------------
# Contract test: round-trip through ConnectorConsumer._publish_result -> decode_result
# (imports from connectors only inside tests/, per spec)
# ---------------------------------------------------------------------------


class FakeRedis:
    """Captures xadd calls without touching real Redis."""

    def __init__(self):
        self.xadd_calls: list[tuple[str, dict]] = []

    def xadd(self, stream: str, fields: dict) -> str:
        self.xadd_calls.append((stream, dict(fields)))
        return b"0-1"


def _make_consumer(fake_redis, step_type="reserve") -> "ConnectorConsumer":  # noqa: F821
    from spindle_core.connectors.consumer import ConnectorConsumer
    from spindle_core.tapestry.loader import TapestryStore

    store = TapestryStore.from_dict({})
    return ConnectorConsumer(
        redis_client=fake_redis,
        tapestry=store,
        step_type=step_type,
        consumer_name="test-consumer",
    )


@pytest.mark.parametrize(
    "event_type,reason,expected_cls",
    [
        # *Succeeded — no reason field on the event class; key must be absent from wire payload
        ("StepSucceeded", "", StepSucceeded),
        ("CompensationSucceeded", "", CompensationSucceeded),
        # *Failed with non-empty reason — "reason" key present in wire payload
        ("StepFailed", "timeout", StepFailed),
        ("CompensationFailed", "503", CompensationFailed),
        # *Failed with empty reason — "reason" key ABSENT from wire payload (consumer.py:256);
        # decode_result must return reason="" (the BLOCKER case)
        ("StepFailed", "", StepFailed),
        ("CompensationFailed", "", CompensationFailed),
    ],
)
def test_contract_round_trip(event_type, reason, expected_cls):
    """_publish_result payload round-trips through decode_result into the correct event.

    For *Failed rows (empty or non-empty reason):
        - event.reason == reason (always checked)
        - "reason" key absent from payload when reason is empty (consumer.py:256)
        - "reason" key present and correct when reason is non-empty

    For *Succeeded rows:
        - "reason" key absent from payload (consumer never writes it for success)
    """
    fake_r = FakeRedis()
    consumer = _make_consumer(fake_r)

    # Call _publish_result directly — the internal method that writes to Redis.
    consumer._publish_result(event_type, "saga-Z", "step-Y", reason=reason)

    assert len(fake_r.xadd_calls) == 1
    _stream, fields = fake_r.xadd_calls[0]

    is_failed = event_type in ("StepFailed", "CompensationFailed")

    if not reason:
        # Empty reason: "reason" key must be absent from the wire payload regardless of event type.
        # For *Succeeded this is expected; for *Failed this is the BLOCKER case being proven here.
        assert "reason" not in fields, (
            f"'reason' key must be absent when reason='' in {event_type} payload (consumer.py:256)"
        )
    else:
        # Non-empty reason: key must be present and correct.
        assert "reason" in fields, f"'reason' key must be present for {event_type} with reason={reason!r}"
        assert fields["reason"] == reason

    # Round-trip decode.
    event = decode_result(fields)
    assert isinstance(event, expected_cls)
    assert event.saga_id == "saga-Z"
    assert event.step_id == "step-Y"

    # For all *Failed events, verify the decoded reason equals what was published.
    if is_failed:
        assert event.reason == reason  # type: ignore[attr-defined]
