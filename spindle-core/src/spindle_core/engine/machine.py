from collections.abc import Sequence
from dataclasses import replace

from spindle_core.engine.errors import InvalidTransition
from spindle_core.engine.messages import (
    Command,
    CompensationFailed,
    CompensationSucceeded,
    DispatchCompensation,
    DispatchStep,
    Event,
    StepFailed,
    StepSucceeded,
)
from spindle_core.engine.saga import SagaState, StepState
from spindle_core.engine.states import SagaStatus, StepStatus

Result = tuple[SagaState, list[Command]]

# Statuses meaning "this step already succeeded (and maybe moved on since)".
_SUCCEEDED_OR_LATER = {
    StepStatus.SUCCEEDED,
    StepStatus.COMPENSATING,
    StepStatus.COMPENSATED,
}


def _key(saga_id: str, step_id: str, action: str) -> str:
    return f"{saga_id}:{step_id}:{action}"


def _index_of(state: SagaState, step_id: str) -> int:
    for i, step in enumerate(state.steps):
        if step.step_id == step_id:
            return i
    raise InvalidTransition(f"unknown step {step_id!r} in saga {state.saga_id!r}")


def _with_step(
    steps: tuple[StepState, ...], i: int, status: StepStatus
) -> tuple[StepState, ...]:
    """Return a new steps tuple with steps[i] set to `status`. Never mutates."""
    return steps[:i] + (replace(steps[i], status=status),) + steps[i + 1 :]


def _compensate_latest(state: SagaState) -> Result:
    """Undo the latest SUCCEEDED step, or finish the saga if none remain."""
    for i in range(len(state.steps) - 1, -1, -1):
        step = state.steps[i]
        if step.status == StepStatus.SUCCEEDED:
            new_state = replace(
                state,
                status=SagaStatus.COMPENSATING,
                steps=_with_step(state.steps, i, StepStatus.COMPENSATING),
            )
            command = DispatchCompensation(
                state.saga_id,
                step.step_id,
                step.step_type,
                _key(state.saga_id, step.step_id, "compensate"),
            )
            return new_state, [command]
    return replace(state, status=SagaStatus.COMPENSATED), []


def start_saga(
    saga_id: str, steps: Sequence[tuple[str, str]]
) -> Result:
    if not steps:
        raise ValueError("a saga needs at least one step")
    if len({step_id for step_id, _ in steps}) != len(steps):
        raise ValueError("step ids must be unique")

    pending = tuple(StepState(sid, stype, StepStatus.PENDING) for sid, stype in steps)
    first = replace(pending[0], status=StepStatus.DISPATCHED)
    state = SagaState(saga_id, SagaStatus.RUNNING, (first,) + pending[1:])
    command = DispatchStep(
        saga_id, first.step_id, first.step_type, _key(saga_id, first.step_id, "execute")
    )
    return state, [command]


def _on_step_succeeded(state: SagaState, i: int) -> Result:
    step = state.steps[i]
    if step.status in _SUCCEEDED_OR_LATER:
        return state, []  # duplicate or late delivery
    if step.status != StepStatus.DISPATCHED:
        raise InvalidTransition(f"StepSucceeded for {step.step_id!r} in {step.status}")

    steps = _with_step(state.steps, i, StepStatus.SUCCEEDED)
    if i + 1 < len(steps):
        nxt = steps[i + 1]
        steps = _with_step(steps, i + 1, StepStatus.DISPATCHED)
        command = DispatchStep(
            state.saga_id,
            nxt.step_id,
            nxt.step_type,
            _key(state.saga_id, nxt.step_id, "execute"),
        )
        return replace(state, steps=steps), [command]
    return replace(state, status=SagaStatus.COMPLETED, steps=steps), []


def _on_step_failed(state: SagaState, i: int) -> Result:
    step = state.steps[i]
    if step.status == StepStatus.FAILED:
        return state, []  # duplicate
    if step.status != StepStatus.DISPATCHED:
        raise InvalidTransition(f"StepFailed for {step.step_id!r} in {step.status}")

    failed = replace(state, steps=_with_step(state.steps, i, StepStatus.FAILED))
    return _compensate_latest(failed)


def _on_compensation_succeeded(state: SagaState, i: int) -> Result:
    step = state.steps[i]
    if step.status == StepStatus.COMPENSATED:
        return state, []  # duplicate
    if state.status != SagaStatus.COMPENSATING or step.status != StepStatus.COMPENSATING:
        raise InvalidTransition(
            f"CompensationSucceeded for {step.step_id!r}: "
            f"saga {state.status}, step {step.status}"
        )

    done = replace(state, steps=_with_step(state.steps, i, StepStatus.COMPENSATED))
    return _compensate_latest(done)


def _on_compensation_failed(state: SagaState, i: int) -> Result:
    step = state.steps[i]
    if state.status == SagaStatus.FAILED and step.status == StepStatus.COMPENSATING:
        return state, []  # duplicate
    if state.status != SagaStatus.COMPENSATING or step.status != StepStatus.COMPENSATING:
        raise InvalidTransition(
            f"CompensationFailed for {step.step_id!r}: "
            f"saga {state.status}, step {step.status}"
        )
    # The step stays COMPENSATING: in a FAILED saga, that marks the stuck step.
    return replace(state, status=SagaStatus.FAILED), []


def decide(state: SagaState, event: Event) -> Result:
    if event.saga_id != state.saga_id:
        raise InvalidTransition(f"event for {event.saga_id}, state is {state.saga_id}")

    i = _index_of(state, event.step_id)

    match event:
        case StepSucceeded():
            return _on_step_succeeded(state, i)
        case StepFailed():
            return _on_step_failed(state, i)
        case CompensationSucceeded():
            return _on_compensation_succeeded(state, i)
        case CompensationFailed():
            return _on_compensation_failed(state, i)

    raise InvalidTransition(f"unhandled event: {type(event).__name__}")