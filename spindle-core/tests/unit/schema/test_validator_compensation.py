# Component: Spindle Architecture
# File: test_validator_compensation.py
# Description: Validator tests for compensation inheritance and budget rules.

"""Validator tests for compensation inheritance and step-budget rules.

DoD 4: inherited timeout/retry, own override, override cannot bypass fits_budget,
success not inherited, error paths use steps[i].compensation.*.
DoD 5: a bad retry field plus a bad timeout in the same step reports both.
"""

from __future__ import annotations

import pytest

from spindle_core.schema.errors import ValidationError
from spindle_core.schema.validator import validate_saga

KNOWN = {"svc"}


def _step(name: str = "s1", **overrides: object) -> dict:
    step = {
        "name": name,
        "service": "svc",
        "action": {"method": "POST", "path": "/orders"},
        "timeout_s": 10.0,
        "compensation": {"method": "DELETE", "path": "/orders"},
    }
    step.update(overrides)
    return step


def _saga(steps: list[dict]) -> dict:
    return {"name": "s", "steps": steps}


# ---------------------------------------------------------------------------
# Inheritance
# ---------------------------------------------------------------------------


def test_compensation_inherits_step_timeout() -> None:
    # Compensation without timeout_s inherits the step's timeout_s (10s) and
    # the step's retry (none), so fits_budget passes.
    saga = _saga([_step()])
    validate_saga(saga, known_services=KNOWN)


def test_compensation_inherits_step_retry() -> None:
    saga = _saga(
        [_step(retry={"max_attempts": 3, "initial_backoff_seconds": 1.0, "backoff_multiplier": 2.0, "max_backoff_seconds": 5.0})]
    )
    # Inherited retry must also fit the budget at the step timeout.
    validate_saga(saga, known_services=KNOWN)


def test_compensation_own_override() -> None:
    saga = _saga([_step(compensation={"method": "DELETE", "path": "/orders", "timeout_s": 5.0})])
    validate_saga(saga, known_services=KNOWN)


def test_compensation_success_not_inherited() -> None:
    # Compensation with its own success criteria is independent of the step.
    saga = _saga(
        [_step(compensation={"method": "DELETE", "path": "/orders", "success": {"status": [204]}})]
    )
    validate_saga(saga, known_services=KNOWN)


# ---------------------------------------------------------------------------
# Budget enforcement
# ---------------------------------------------------------------------------


def test_compensation_override_cannot_bypass_budget() -> None:
    # A huge retry policy on the compensation must still fail fits_budget.
    saga = _saga(
        [
            _step(
                timeout_s=1.0,
                compensation={
                    "method": "DELETE",
                    "path": "/orders",
                    "timeout_s": 1.0,
                    "retry": {
                        "max_attempts": 10,
                        "initial_backoff_seconds": 30.0,
                        "backoff_multiplier": 2.0,
                        "max_backoff_seconds": 100.0,
                    },
                },
            )
        ]
    )
    with pytest.raises(ValidationError) as exc:
        validate_saga(saga, known_services=KNOWN)
    # The error lands on the compensation retry path.
    assert any(e.path.startswith("steps[0].compensation") for e in exc.value.errors)


def test_compensation_budget_error_path() -> None:
    saga = _saga(
        [
            _step(
                timeout_s=1.0,
                compensation={
                    "method": "DELETE",
                    "path": "/orders",
                    "timeout_s": 1.0,
                    "retry": {
                        "max_attempts": 10,
                        "initial_backoff_seconds": 30.0,
                        "backoff_multiplier": 2.0,
                        "max_backoff_seconds": 100.0,
                    },
                },
            )
        ]
    )
    with pytest.raises(ValidationError) as exc:
        validate_saga(saga, known_services=KNOWN)
    paths = {e.path for e in exc.value.errors}
    assert any(p.startswith("steps[0].compensation.retry") for p in paths)


# ---------------------------------------------------------------------------
# DoD 5: bad retry + bad timeout in the same step
# ---------------------------------------------------------------------------


def test_bad_retry_and_bad_timeout_in_same_step() -> None:
    saga = _saga(
        [
            _step(
                timeout_s=-1.0,
                retry={
                    "max_attempts": 100,
                    "initial_backoff_seconds": 30.0,
                    "backoff_multiplier": 2.0,
                    "max_backoff_seconds": 100.0,
                },
            )
        ]
    )
    with pytest.raises(ValidationError) as exc:
        validate_saga(saga, known_services=KNOWN)
    paths = {e.path for e in exc.value.errors}
    assert any(p.startswith("steps[0].timeout_s") for p in paths)
    assert any(p.startswith("steps[0].retry") for p in paths)
    assert len(exc.value.errors) >= 2