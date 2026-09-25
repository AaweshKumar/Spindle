# Component: Spindle Architecture
# File: test_machine.py
# Description: Source code module for the Spindle workflow orchestration platform.

import pytest

from spindle_core.engine.errors import InvalidTransition
from spindle_core.engine.machine import decide, start_saga
from spindle_core.engine.messages import (
    CompensationFailed,
    CompensationSucceeded,
    DispatchCompensation,
    DispatchStep,
    StepFailed,
    StepSucceeded,
)
from spindle_core.engine.states import SagaStatus, StepStatus

S = "saga-1"
STEPS = [
    ("reserve", "ReserveInventory"),
    ("charge", "ChargePayment"),
    ("ship", "CreateShipment"),
]
TYPES = dict(STEPS)


# ---- helpers ----

def started():
    state, _ = start_saga(S, STEPS)
    return state


def execute(step_id: str) -> DispatchStep:
    return DispatchStep(S, step_id, TYPES[step_id], f"{S}:{step_id}:execute")


def compensate(step_id: str) -> DispatchCompensation:
    return DispatchCompensation(S, step_id, TYPES[step_id], f"{S}:{step_id}:compensate")


def feed(state, *events):
    """Apply events in order. Returns (final_state, [commands_per_event])."""
    out = []
    for event in events:
        state, commands = decide(state, event)
        out.append(commands)
    return state, out


FAILURE_SEQUENCE = [
    StepSucceeded(S, "reserve"),
    StepSucceeded(S, "charge"),
    StepFailed(S, "ship", "out of stock"),
    CompensationSucceeded(S, "charge"),
    CompensationSucceeded(S, "reserve"),
]


# ---- start_saga ----

def test_start_saga_dispatches_only_the_first_step():
    state, commands = start_saga(S, STEPS)

    assert state.status == SagaStatus.RUNNING
    assert [s.status for s in state.steps] == [
        StepStatus.DISPATCHED, StepStatus.PENDING, StepStatus.PENDING,
    ]
    assert commands == [execute("reserve")]


@pytest.mark.parametrize(
    "steps",
    [[], [("a", "A"), ("a", "B")]],
    ids=["empty", "duplicate_step_ids"],
)
def test_start_saga_rejects_bad_definitions(steps):
    with pytest.raises(ValueError):
        start_saga(S, steps)


# ---- forward path ----

def test_happy_path_completes_saga():
    state, out = feed(
        started(),
        StepSucceeded(S, "reserve"),
        StepSucceeded(S, "charge"),
        StepSucceeded(S, "ship"),
    )

    assert out == [[execute("charge")], [execute("ship")], []]
    assert state.status == SagaStatus.COMPLETED
    assert all(s.status == StepStatus.SUCCEEDED for s in state.steps)


# ---- compensation ----

def test_failure_compensates_in_lifo_order_one_at_a_time():
    state, out = feed(started(), *FAILURE_SEQUENCE)

    assert out == [
        [execute("charge")],
        [execute("ship")],
        [compensate("charge")],   # latest succeeded step first
        [compensate("reserve")],  # only after charge is confirmed undone
        [],
    ]
    assert state.status == SagaStatus.COMPENSATED
    assert [s.status for s in state.steps] == [
        StepStatus.COMPENSATED, StepStatus.COMPENSATED, StepStatus.FAILED,
    ]


def test_failed_step_itself_is_not_compensated():
    state, out = feed(
        started(),
        StepSucceeded(S, "reserve"),
        StepSucceeded(S, "charge"),
        StepFailed(S, "ship", "out of stock"),
    )

    assert out[-1] == [compensate("charge")]
    assert compensate("ship") not in out[-1]
    assert state.status == SagaStatus.COMPENSATING


def test_failure_of_first_step_needs_no_compensation():
    state, out = feed(started(), StepFailed(S, "reserve", "rejected"))

    assert out == [[]]
    assert state.status == SagaStatus.COMPENSATED
    assert [s.status for s in state.steps] == [
        StepStatus.FAILED, StepStatus.PENDING, StepStatus.PENDING,
    ]


def test_compensation_failure_marks_saga_failed_for_human_intervention():
    state, out = feed(
        started(),
        StepSucceeded(S, "reserve"),
        StepSucceeded(S, "charge"),
        StepFailed(S, "ship", "out of stock"),
        CompensationFailed(S, "charge", "refund rejected"),
    )

    assert out[-1] == []
    assert state.status == SagaStatus.FAILED
    # the stuck step stays COMPENSATING so an operator can see where it stopped
    assert state.steps[1].status == StepStatus.COMPENSATING
    assert state.steps[0].status == StepStatus.SUCCEEDED  # never touched


def test_compensation_failure_on_last_step_marks_saga_failed():
    # Mirrors the test above, but the compensation that fails is the LAST one
    # in the LIFO queue (reserve, the saga's very first step) rather than the
    # first one attempted. Off-by-one bugs in queue draining tend to show up
    # specifically at this boundary, so it's worth its own test rather than
    # assuming the "first compensation fails" case generalizes.
    state, out = feed(
        started(),
        StepSucceeded(S, "reserve"),
        StepSucceeded(S, "charge"),
        StepFailed(S, "ship", "out of stock"),
        CompensationSucceeded(S, "charge"),
        CompensationFailed(S, "reserve", "release rejected"),
    )

    assert out[-1] == []
    assert state.status == SagaStatus.FAILED
    assert state.steps[0].status == StepStatus.COMPENSATING  # reserve: stuck
    assert state.steps[1].status == StepStatus.COMPENSATED   # charge: already undone
    assert state.steps[2].status == StepStatus.FAILED        # ship: original failure


# ---- structural edge cases ----

def test_single_step_saga_completes_without_compensation():
    state, _ = start_saga(S, [("charge", "ChargePayment")])
    state, commands = decide(state, StepSucceeded(S, "charge"))

    assert commands == []
    assert state.status == SagaStatus.COMPLETED
    assert state.steps[0].status == StepStatus.SUCCEEDED


def test_single_step_saga_failure_needs_no_compensation():
    state, _ = start_saga(S, [("charge", "ChargePayment")])
    state, commands = decide(state, StepFailed(S, "charge", "declined"))

    assert commands == []
    assert state.status == SagaStatus.COMPENSATED  # nothing preceded it to undo
    assert state.steps[0].status == StepStatus.FAILED


# ---- idempotency ----

def test_every_event_is_a_noop_when_delivered_twice():
    state = started()
    for event in FAILURE_SEQUENCE:
        state, _ = decide(state, event)
        again, commands = decide(state, event)
        assert again == state
        assert commands == []


def test_compensation_failure_duplicate_is_a_noop():
    state, _ = feed(
        started(),
        StepSucceeded(S, "reserve"),
        StepFailed(S, "charge", "declined"),
        CompensationFailed(S, "reserve", "release rejected"),
    )
    again, commands = decide(state, CompensationFailed(S, "reserve", "release rejected"))

    assert again == state
    assert commands == []


def test_late_step_succeeded_after_compensation_started_is_ignored():
    state, _ = feed(started(), *FAILURE_SEQUENCE)
    late, commands = decide(state, StepSucceeded(S, "reserve"))

    assert late == state
    assert commands == []


# ---- terminal-state immutability ----
# The COMPENSATED case above already proves late/duplicate events are
# swallowed once a saga is terminal. These confirm the SAME contract holds
# for the other two terminal statuses (COMPLETED, FAILED) rather than
# assuming it generalizes.

@pytest.mark.parametrize(
    "event",
    [StepSucceeded(S, "reserve"), StepSucceeded(S, "charge"), StepSucceeded(S, "ship")],
    ids=["duplicate_reserve_succeeded", "duplicate_charge_succeeded", "duplicate_ship_succeeded"],
)
def test_completed_saga_ignores_duplicate_events(event):
    state, _ = feed(
        started(),
        StepSucceeded(S, "reserve"),
        StepSucceeded(S, "charge"),
        StepSucceeded(S, "ship"),
    )
    again, commands = decide(state, event)

    assert again == state
    assert commands == []


@pytest.mark.parametrize(
    "event",
    [StepSucceeded(S, "reserve"), StepSucceeded(S, "charge")],
    ids=["duplicate_reserve_succeeded", "duplicate_charge_succeeded"],
)
def test_failed_saga_ignores_late_or_duplicate_events(event):
    # Once an operator has been paged for human intervention, stray
    # redelivered events from earlier in the saga's history must not keep
    # mutating state out from under them.
    state, _ = feed(
        started(),
        StepSucceeded(S, "reserve"),
        StepSucceeded(S, "charge"),
        StepFailed(S, "ship", "out of stock"),
        CompensationFailed(S, "charge", "refund rejected"),
    )
    again, commands = decide(state, event)

    assert again == state
    assert commands == []


# ---- invalid transitions ----

@pytest.mark.parametrize(
    "event",
    [
        StepSucceeded("other-saga", "reserve"),
        StepSucceeded(S, "no-such-step"),
        StepSucceeded(S, "charge"),                 # still PENDING
        StepFailed(S, "ship", "x"),                 # still PENDING
        CompensationSucceeded(S, "reserve"),        # saga isn't compensating
        CompensationFailed(S, "reserve", "x"),      # saga isn't compensating
    ],
    ids=[
        "wrong_saga_id",
        "unknown_step",
        "succeeded_for_pending_step",
        "failed_for_pending_step",
        "compensation_succeeded_while_running",
        "compensation_failed_while_running",
    ],
)
def test_contradictory_events_raise(event):
    with pytest.raises(InvalidTransition):
        decide(started(), event)


def test_step_failed_after_already_succeeded_raises():
    # reserve already resolved SUCCEEDED (saga still RUNNING, non-terminal);
    # a later FAILED for the same step contradicts the recorded outcome and
    # must not be silently accepted.
    state, _ = feed(started(), StepSucceeded(S, "reserve"))

    with pytest.raises(InvalidTransition):
        decide(state, StepFailed(S, "reserve", "contradicts recorded success"))


def test_step_succeeded_after_already_failed_raises():
    # ship already resolved FAILED and the saga is mid-compensation
    # (COMPENSATING, not yet terminal) — distinct from the terminal-swallow
    # tests above, this checks a step's own resolution can't be contradicted
    # while the saga is still actively processing.
    state, _ = feed(
        started(),
        StepSucceeded(S, "reserve"),
        StepSucceeded(S, "charge"),
        StepFailed(S, "ship", "out of stock"),
    )

    with pytest.raises(InvalidTransition):
        decide(state, StepSucceeded(S, "ship"))


@pytest.mark.parametrize(
    "event",
    [
        CompensationSucceeded(S, "reserve"),
        CompensationFailed(S, "reserve", "x"),
    ],
    ids=[
        "compensation_succeeded_out_of_lifo_order",
        "compensation_failed_out_of_lifo_order",
    ],
)
def test_compensation_ack_rejected_when_step_not_yet_dispatched_for_compensation(event):
    # charge is the step currently being compensated (LIFO); reserve is still
    # SUCCEEDED and hasn't been dispatched for compensation yet. An ack for
    # reserve here would mean compensating out of order — this is a stronger
    # check than "saga isn't compensating at all" above, since the saga IS
    # compensating, just not this step yet.
    state, _ = feed(
        started(),
        StepSucceeded(S, "reserve"),
        StepSucceeded(S, "charge"),
        StepFailed(S, "ship", "out of stock"),
    )

    with pytest.raises(InvalidTransition):
        decide(state, event)


# ---- purity / determinism ----

def test_decide_does_not_modify_its_input():
    state = started()
    decide(state, StepSucceeded(S, "reserve"))

    assert state == started()


def test_replay_is_deterministic():
    state_a, out_a = feed(started(), *FAILURE_SEQUENCE)
    state_b, out_b = feed(started(), *FAILURE_SEQUENCE)

    assert state_a == state_b
    assert out_a == out_b  # same idempotency keys, so worker-side dedupe works after a crash