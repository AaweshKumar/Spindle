"""
Unit tests for the dead_letter extension of make_result_handler and
make_dead_letter_writer (streams/results.py).

Redis is replaced with a MagicMock — no live services required.
Style mirrors test_results.py.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

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
from spindle_core.streams import keys as keys_module
from spindle_core.streams.results import (
    RESULTS_GROUP_NAME,
    make_dead_letter_writer,
    make_result_handler,
)

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


_STEP_SUCCEEDED = {
    "type": "StepSucceeded",
    "saga_id": "saga-1",
    "step_id": "reserve",
}

_STEP_FAILED = {
    "type": "StepFailed",
    "saga_id": "saga-1",
    "step_id": "reserve",
    "reason": "timeout",
}

_COMP_SUCCEEDED = {
    "type": "CompensationSucceeded",
    "saga_id": "saga-1",
    "step_id": "reserve",
}

_COMP_FAILED = {
    "type": "CompensationFailed",
    "saga_id": "saga-1",
    "step_id": "reserve",
    "reason": "500 error",
}


# ---------------------------------------------------------------------------
# Constants — keys module sources of truth
# ---------------------------------------------------------------------------


def test_results_group_name_in_keys():
    assert keys_module.RESULTS_GROUP_NAME == "spindle-orchestrator"


def test_dead_results_stream_key_in_keys():
    assert keys_module.DEAD_RESULTS_STREAM_KEY == "spindle:dead:results"


def test_results_group_name_re_exported_from_results():
    """results.RESULTS_GROUP_NAME must be the same object as keys.RESULTS_GROUP_NAME."""
    assert RESULTS_GROUP_NAME is keys_module.RESULTS_GROUP_NAME


# ---------------------------------------------------------------------------
# dead_letter=None — behaviour identical to today (backward compatibility)
# ---------------------------------------------------------------------------


def test_no_dead_letter_malformed_returns_normally():
    runner = FakeRunner()
    handle = make_result_handler(runner, dead_letter=None)
    # Does not raise
    handle("msg-1", {"type": "BogusType", "saga_id": "s1", "step_id": "x"})
    assert len(runner.calls) == 0


def test_no_dead_letter_unknown_saga_returns_normally():
    runner = FakeRunner(side_effect=UnknownSaga("saga-X"))
    handle = make_result_handler(runner, dead_letter=None)
    result = handle("msg-1", _STEP_SUCCEEDED)
    assert result is None


def test_no_dead_letter_invalid_transition_returns_normally():
    runner = FakeRunner(side_effect=InvalidTransition("bad state"))
    handle = make_result_handler(runner, dead_letter=None)
    result = handle("msg-1", _STEP_SUCCEEDED)
    assert result is None


# ---------------------------------------------------------------------------
# dead_letter given — ValueError / UnknownSaga / InvalidTransition
# ---------------------------------------------------------------------------


def test_dead_letter_called_on_malformed():
    runner = FakeRunner()
    dl_calls: list[tuple[str, dict, str]] = []

    def _dl(mid, fields, reason):
        dl_calls.append((mid, fields, reason))

    handle = make_result_handler(runner, dead_letter=_dl)
    handle("msg-1", {"type": "BogusType", "saga_id": "s1", "step_id": "x"})

    assert len(dl_calls) == 1
    mid, fields, reason = dl_calls[0]
    assert mid == "msg-1"
    assert fields["type"] == "BogusType"
    assert "malformed" in reason


def test_dead_letter_called_on_unknown_saga():
    runner = FakeRunner(side_effect=UnknownSaga("saga-X"))
    dl_calls: list[tuple[str, dict, str]] = []

    def _dl(mid, fields, reason):
        dl_calls.append((mid, fields, reason))

    handle = make_result_handler(runner, dead_letter=_dl)
    handle("msg-1", _STEP_SUCCEEDED)

    assert len(dl_calls) == 1
    mid, fields, reason = dl_calls[0]
    assert mid == "msg-1"
    assert fields == _STEP_SUCCEEDED
    assert "UnknownSaga" in reason


def test_dead_letter_called_on_invalid_transition():
    runner = FakeRunner(side_effect=InvalidTransition("bad state"))
    dl_calls: list[tuple[str, dict, str]] = []

    def _dl(mid, fields, reason):
        dl_calls.append((mid, fields, reason))

    handle = make_result_handler(runner, dead_letter=_dl)
    handle("msg-1", _STEP_SUCCEEDED)

    assert len(dl_calls) == 1
    mid, fields, reason = dl_calls[0]
    assert mid == "msg-1"
    assert "InvalidTransition" in reason


def test_dead_letter_called_exactly_once_per_malformed_message():
    runner = FakeRunner()
    call_count = 0

    def _dl(mid, fields, reason):
        nonlocal call_count
        call_count += 1

    handle = make_result_handler(runner, dead_letter=_dl)
    handle("msg-1", {"type": "Bad"})
    assert call_count == 1


# ---------------------------------------------------------------------------
# ConcurrencyConflict and RuntimeError are NEVER dead-lettered
# ---------------------------------------------------------------------------


def test_concurrency_conflict_reraised_dead_letter_not_called():
    runner = FakeRunner(side_effect=ConcurrencyConflict("gave up"))
    dl_calls: list = []

    handle = make_result_handler(runner, dead_letter=lambda m, f, r: dl_calls.append(r))
    with pytest.raises(ConcurrencyConflict):
        handle("msg-1", _STEP_SUCCEEDED)
    assert dl_calls == []


def test_runtime_error_reraised_dead_letter_not_called():
    runner = FakeRunner(side_effect=RuntimeError("unexpected"))
    dl_calls: list = []

    handle = make_result_handler(runner, dead_letter=lambda m, f, r: dl_calls.append(r))
    with pytest.raises(RuntimeError):
        handle("msg-1", _STEP_SUCCEEDED)
    assert dl_calls == []


# ---------------------------------------------------------------------------
# dead_letter raising propagates out of handle
# ---------------------------------------------------------------------------


def test_dead_letter_raising_propagates():
    runner = FakeRunner()

    def _dl(mid, fields, reason):
        raise OSError("disk full")

    handle = make_result_handler(runner, dead_letter=_dl)
    with pytest.raises(OSError, match="disk full"):
        handle("msg-1", {"type": "BogusType", "saga_id": "s1", "step_id": "x"})


# ---------------------------------------------------------------------------
# Happy path — all 4 event types: runner called, dead_letter NOT called
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fields,expected_cls", [
    (_STEP_SUCCEEDED, StepSucceeded),
    (_STEP_FAILED, StepFailed),
    (_COMP_SUCCEEDED, CompensationSucceeded),
    (_COMP_FAILED, CompensationFailed),
])
def test_happy_path_runner_called_dead_letter_not_called(fields, expected_cls):
    runner = FakeRunner()
    dl_calls: list = []

    handle = make_result_handler(runner, dead_letter=lambda m, f, r: dl_calls.append(r))
    handle("msg-1", fields)

    assert len(runner.calls) == 1
    assert isinstance(runner.calls[0][1], expected_cls)
    assert dl_calls == []


# ---------------------------------------------------------------------------
# make_dead_letter_writer
# ---------------------------------------------------------------------------


def test_dead_letter_writer_xadds_to_dead_results_stream():
    r = MagicMock()
    writer = make_dead_letter_writer(r)
    writer("msg-1", {"type": "StepFailed", "saga_id": "s1", "step_id": "x"}, "some reason")

    r.xadd.assert_called_once()
    stream = r.xadd.call_args.args[0]
    assert stream == "spindle:dead:results"


def test_dead_letter_writer_original_fields_preserved():
    r = MagicMock()
    writer = make_dead_letter_writer(r)
    original = {"type": "StepFailed", "saga_id": "s1", "step_id": "x"}
    writer("msg-1", original, "my reason")

    fields = r.xadd.call_args.args[1]
    assert fields["type"] == "StepFailed"
    assert fields["saga_id"] == "s1"
    assert fields["step_id"] == "x"


def test_dead_letter_writer_reason_set():
    r = MagicMock()
    writer = make_dead_letter_writer(r)
    writer("msg-1", {"type": "StepFailed", "saga_id": "s1", "step_id": "x"}, "the reason")

    fields = r.xadd.call_args.args[1]
    assert fields["reason"] == "the reason"


def test_dead_letter_writer_original_reason_preserved_as_original_reason():
    """When original fields already have 'reason', it must be saved as 'original_reason'."""
    r = MagicMock()
    writer = make_dead_letter_writer(r)
    original = {
        "type": "StepFailed",
        "saga_id": "s1",
        "step_id": "x",
        "reason": "original failure reason",
    }
    writer("msg-1", original, "dead-letter reason")

    fields = r.xadd.call_args.args[1]
    assert fields["reason"] == "dead-letter reason"
    assert fields["original_reason"] == "original failure reason"


def test_dead_letter_writer_does_not_mutate_original_fields():
    """The writer must not mutate the caller's dict."""
    r = MagicMock()
    writer = make_dead_letter_writer(r)
    original = {"type": "StepFailed", "saga_id": "s1", "step_id": "x"}
    writer("msg-1", original, "some reason")
    assert "reason" not in original  # original is unchanged


def test_dead_letter_writer_redis_error_propagates():
    import redis as redis_lib

    r = MagicMock()
    r.xadd.side_effect = redis_lib.ConnectionError("lost")
    writer = make_dead_letter_writer(r)
    with pytest.raises(redis_lib.ConnectionError):
        writer("msg-1", {"type": "StepFailed", "saga_id": "s1", "step_id": "x"}, "reason")

