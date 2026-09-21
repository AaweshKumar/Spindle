from enum import StrEnum


class SagaStatus(StrEnum):
    RUNNING = "RUNNING"            # forward progress, steps executing
    COMPENSATING = "COMPENSATING"  # a step failed, undoing completed steps in LIFO order
    COMPLETED = "COMPLETED"        # terminal: all steps succeeded
    COMPENSATED = "COMPENSATED"    # terminal: failure happened, everything was undone cleanly
    FAILED = "FAILED"              # terminal: a compensation itself failed, needs a human


class StepStatus(StrEnum):
    PENDING = "PENDING"            # not yet dispatched
    DISPATCHED = "DISPATCHED"      # command issued, result not yet known
    SUCCEEDED = "SUCCEEDED"        # executed OK, will need compensation if the saga fails later
    FAILED = "FAILED"              # executed and failed
    COMPENSATING = "COMPENSATING"  # compensation dispatched, result not yet known
    COMPENSATED = "COMPENSATED"    # successfully undone