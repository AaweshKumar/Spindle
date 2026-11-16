# Component: Spindle Architecture
# File: test_validator_templates.py
# Description: Validator tests for template reference rules (rule 5).

"""Validator tests for template reference rules.

Rule 5: template references resolve only to input.* or steps.<earlier>.response.*.
Later/nonexistent step references are errors, as are saga/secret roots.
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
        "timeout_s": 30.0,
        "compensation": {"method": "DELETE", "path": "/orders"},
    }
    step.update(overrides)
    return step


def _saga(steps: list[dict]) -> dict:
    return {"name": "s", "steps": steps}


# ---------------------------------------------------------------------------
# Allowed roots
# ---------------------------------------------------------------------------


def test_rule5_input_root_passing() -> None:
    saga = _saga([_step(action={"method": "POST", "path": "/orders/{{input.id}}"})])
    validate_saga(saga, known_services=KNOWN)


def test_rule5_steps_earlier_response_passing() -> None:
    saga = _saga(
        [
            _step("create"),
            _step("done", action={"method": "POST", "path": "/x/{{steps.create.response.id}}"}),
        ]
    )
    validate_saga(saga, known_services=KNOWN)


def test_rule5_compensation_self_reference_passing() -> None:
    # Compensation of step i may reference step i's own response.
    saga = _saga(
        [_step("s1", compensation={"method": "DELETE", "path": "/x/{{steps.s1.response.id}}"})]
    )
    validate_saga(saga, known_services=KNOWN)


# ---------------------------------------------------------------------------
# Rejected roots
# ---------------------------------------------------------------------------


def test_rule5_saga_root_rejected() -> None:
    saga = _saga([_step(action={"method": "POST", "path": "/x/{{saga.id}}"})])
    with pytest.raises(ValidationError) as exc:
        validate_saga(saga, known_services=KNOWN)
    assert any("'saga'" in e.message for e in exc.value.errors)


def test_rule5_secret_root_rejected() -> None:
    saga = _saga([_step(action={"method": "POST", "path": "/x/{{secret.K}}"})])
    with pytest.raises(ValidationError) as exc:
        validate_saga(saga, known_services=KNOWN)
    assert any("'secret'" in e.message for e in exc.value.errors)


def test_rule5_unknown_root_rejected() -> None:
    saga = _saga([_step(action={"method": "POST", "path": "/x/{{env.FOO}}"})])
    with pytest.raises(ValidationError) as exc:
        validate_saga(saga, known_services=KNOWN)
    assert any("unknown placeholder namespace" in e.message for e in exc.value.errors)


# ---------------------------------------------------------------------------
# Step reference errors
# ---------------------------------------------------------------------------


def test_rule5_later_step_reference_rejected() -> None:
    saga = _saga(
        [
            _step("a", action={"method": "POST", "path": "/x/{{steps.b.response.id}}"}),
            _step("b"),
        ]
    )
    with pytest.raises(ValidationError) as exc:
        validate_saga(saga, known_services=KNOWN)
    assert any("later step" in e.message for e in exc.value.errors)


def test_rule5_unknown_step_reference_rejected() -> None:
    saga = _saga([_step(action={"method": "POST", "path": "/x/{{steps.ghost.response.id}}"})])
    with pytest.raises(ValidationError) as exc:
        validate_saga(saga, known_services=KNOWN)
    assert any("unknown" in e.message or "later step" in e.message for e in exc.value.errors)


def test_rule5_malformed_steps_reference_rejected() -> None:
    saga = _saga([_step(action={"method": "POST", "path": "/x/{{steps.a}}"})])
    with pytest.raises(ValidationError) as exc:
        validate_saga(saga, known_services=KNOWN)
    assert any("steps" in e.message and "response" in e.message for e in exc.value.errors)


def test_rule5_body_template_checked() -> None:
    saga = _saga(
        [_step(action={"method": "POST", "path": "/x", "body": {"k": "{{saga.id}}"}})]
    )
    with pytest.raises(ValidationError) as exc:
        validate_saga(saga, known_services=KNOWN)
    assert any("'saga'" in e.message for e in exc.value.errors)


def test_rule5_action_self_reference_rejected() -> None:
    # Action may not reference its own step (only compensation may).
    saga = _saga([_step("s1", action={"method": "POST", "path": "/x/{{steps.s1.response.id}}"})])
    with pytest.raises(ValidationError) as exc:
        validate_saga(saga, known_services=KNOWN)
    assert any("own response" in e.message for e in exc.value.errors)