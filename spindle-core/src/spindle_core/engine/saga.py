from dataclasses import dataclass

from spindle_core.engine.states import SagaStatus, StepStatus


@dataclass(frozen=True)
class StepState:
    step_id: str
    step_type: str
    status: StepStatus


@dataclass(frozen=True)
class SagaState:
    saga_id: str
    status: SagaStatus
    steps: tuple[StepState, ...]