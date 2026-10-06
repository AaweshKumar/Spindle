"""
Unit tests for connectors/outcomes.py (OutcomeTracker).
No mocking required — pure in-memory state.
"""

import pytest

from spindle_core.connectors.outcomes import CallOutcome, OutcomeTracker


# ---- CallOutcome ----

def test_call_outcome_zero_total():
    o = CallOutcome()
    assert o.total == 0


def test_call_outcome_success_rate_none_when_no_calls():
    o = CallOutcome()
    assert o.success_rate is None


def test_call_outcome_success_rate_all_success():
    o = CallOutcome(success_count=5, failure_count=0)
    assert o.success_rate == pytest.approx(1.0)


def test_call_outcome_success_rate_all_failure():
    o = CallOutcome(success_count=0, failure_count=3)
    assert o.success_rate == pytest.approx(0.0)


def test_call_outcome_mixed_success_rate():
    o = CallOutcome(success_count=3, failure_count=1)
    assert o.success_rate == pytest.approx(0.75)


# ---- OutcomeTracker ----

def test_initial_snapshot_is_all_zeros():
    tracker = OutcomeTracker()
    snap = tracker.snapshot("Foo")
    assert snap.success_count == 0
    assert snap.failure_count == 0
    assert snap.total == 0
    assert snap.success_rate is None


def test_record_success_increments_success_count():
    tracker = OutcomeTracker()
    tracker.record_success("Foo")
    tracker.record_success("Foo")
    assert tracker.snapshot("Foo").success_count == 2
    assert tracker.snapshot("Foo").failure_count == 0


def test_record_failure_increments_failure_count():
    tracker = OutcomeTracker()
    tracker.record_failure("Foo")
    assert tracker.snapshot("Foo").failure_count == 1
    assert tracker.snapshot("Foo").success_count == 0


def test_mixed_recording():
    tracker = OutcomeTracker()
    for _ in range(3):
        tracker.record_success("Bar")
    tracker.record_failure("Bar")
    snap = tracker.snapshot("Bar")
    assert snap.total == 4
    assert snap.success_rate == pytest.approx(0.75)


def test_step_types_are_isolated():
    tracker = OutcomeTracker()
    tracker.record_success("A")
    tracker.record_failure("B")
    assert tracker.snapshot("A").success_count == 1
    assert tracker.snapshot("A").failure_count == 0
    assert tracker.snapshot("B").failure_count == 1
    assert tracker.snapshot("B").success_count == 0


def test_snapshot_is_a_copy():
    """Mutating the tracker after snapshotting must not change the snapshot."""
    tracker = OutcomeTracker()
    tracker.record_success("X")
    snap1 = tracker.snapshot("X")
    tracker.record_success("X")
    snap2 = tracker.snapshot("X")
    assert snap1.success_count == 1
    assert snap2.success_count == 2


def test_all_step_types_returns_seen_types():
    tracker = OutcomeTracker()
    tracker.record_success("A")
    tracker.record_failure("B")
    tracker.record_success("C")
    assert set(tracker.all_step_types()) == {"A", "B", "C"}


def test_all_step_types_empty_initially():
    tracker = OutcomeTracker()
    assert tracker.all_step_types() == []
