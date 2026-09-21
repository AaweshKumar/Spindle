from dataclasses import dataclass
# ---- Events (input to decide) ----

@dataclass(frozen=True)
class StepSucceeded:
    saga_id: str
    step_id: str


@dataclass(frozen=True)
class StepFailed:
    saga_id: str
    step_id: str
    reason: str


@dataclass(frozen=True)
class CompensationSucceeded:
    saga_id: str
    step_id: str


@dataclass(frozen=True)
class CompensationFailed:
    saga_id: str
    step_id: str
    reason: str


# ---- Commands (output of decide) ----

@dataclass(frozen=True)
class DispatchStep:
    saga_id: str
    step_id: str
    step_type: str
    idempotency_key: str


@dataclass(frozen=True)
class DispatchCompensation:
    saga_id: str
    step_id: str
    step_type: str
    idempotency_key: str


Event = StepSucceeded | StepFailed | CompensationSucceeded | CompensationFailed
Command = DispatchStep | DispatchCompensation