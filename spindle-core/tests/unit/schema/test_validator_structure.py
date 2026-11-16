# Component: Spindle Architecture
# File: test_validator_structure.py
# Description: Validator tests for saga structural rules 1, 2, 3, 6, 8.

"""Validator tests for saga structural rules.

Rule 1: step names unique within a saga.
Rule 2: each step has compensation XOR non_compensable: true.
Rule 3: non_compensable steps only as the final step.
Rule 6: step.service must be in known_services.
Rule 8: unknown fields rejected.
"""

from __future__ import annotations

import pytest

from spindle_core.schema.errors import ValidationError
from spindle_core.schema.validator import validate_saga

KNOWN = {"svc", "other"}


def _compensation(method: str = "DELETE", path: str = "/x") -> dict:
    return {"method": method, "path": path}


def _step(name: str, service: str = "svc", **overrides: object) -> dict:
    step = {
        "name": name,
        "service": service,
        "action": {"method": "POST", "path": "/orders"},
        "timeout_s": 30.0,
        "compensation": _compensation(),
    }
    if overrides.get("non_compensable"):
        step.pop("compensation")      # rule 2: XOR, so drop the default
    step.update(overrides)
    return step


def _saga(steps: list[dict]) -> dict:
    return {"name": "s", "steps": steps}


# ---------------------------------------------------------------------------
# Rule 1: unique step names
# ---------------------------------------------------------------------------


def test_rule1_unique_names_passing() -> None:
    saga = _saga([_step("a"), _step("b")])
    assert validate_saga(saga, known_services=KNOWN).name == "s"


def test_rule1_duplicate_names_failing() -> None:
    saga = _saga([_step("a"), _step("a")])
    with pytest.raises(ValidationError) as exc:
        validate_saga(saga, known_services=KNOWN)
    assert any("duplicate step name" in e.message for e in exc.value.errors)
    assert any(e.path.startswith("steps[1].name") for e in exc.value.errors)


# ---------------------------------------------------------------------------
# Rule 2: compensation XOR non_compensable
# ---------------------------------------------------------------------------


def test_rule2_compensation_only_passing() -> None:
    saga = _saga([_step("a")])
    validate_saga(saga, known_services=KNOWN)


def test_rule2_non_compensable_only_passing() -> None:
    saga = _saga([_step("a", non_compensable=True)])
    validate_saga(saga, known_services=KNOWN)


def test_rule2_both_failing() -> None:
    saga = _saga([_step("a", compensation=_compensation(), non_compensable=True)])
    with pytest.raises(ValidationError) as exc:
        validate_saga(saga, known_services=KNOWN)
    assert any("both compensation and non_compensable" in e.message for e in exc.value.errors)


def test_rule2_neither_failing() -> None:
    saga = _saga([_step("a", compensation=None, non_compensable=False)])
    with pytest.raises(ValidationError) as exc:
        validate_saga(saga, known_services=KNOWN)
    assert any("must declare compensation OR non_compensable" in e.message for e in exc.value.errors)


# ---------------------------------------------------------------------------
# Rule 3: non_compensable only on final step
# ---------------------------------------------------------------------------


def test_rule3_non_compensable_on_final_passing() -> None:
    saga = _saga([_step("a"), _step("b", non_compensable=True)])
    validate_saga(saga, known_services=KNOWN)


def test_rule3_non_compensable_not_final_failing() -> None:
    saga = _saga([_step("a", non_compensable=True), _step("b")])
    with pytest.raises(ValidationError) as exc:
        validate_saga(saga, known_services=KNOWN)
    assert any("only allowed as the final step" in e.message for e in exc.value.errors)
    assert any(e.path.startswith("steps[0].non_compensable") for e in exc.value.errors)


# ---------------------------------------------------------------------------
# Rule 6: service must be in known_services
# ---------------------------------------------------------------------------


def test_rule6_known_service_passing() -> None:
    saga = _saga([_step("a", service="other")])
    validate_saga(saga, known_services=KNOWN)


def test_rule6_unknown_service_failing() -> None:
    saga = _saga([_step("a", service="ghost")])
    with pytest.raises(ValidationError) as exc:
        validate_saga(saga, known_services=KNOWN)
    assert any("unknown" in e.message and "ghost" in e.message for e in exc.value.errors)
    assert any(e.path.startswith("steps[0].service") for e in exc.value.errors)


# ---------------------------------------------------------------------------
# Rule 8: unknown fields rejected
# ---------------------------------------------------------------------------


def test_rule8_unknown_field_rejected() -> None:
    saga = _saga([_step("a")])
    saga["bogus"] = 1
    with pytest.raises(ValidationError) as exc:
        validate_saga(saga, known_services=KNOWN)
    assert any("extra" in e.message.lower() or "unexpected" in e.message.lower() for e in exc.value.errors)


def test_rule8_unknown_step_field_rejected() -> None:
    saga = _saga([_step("a")])
    saga["steps"][0]["bogus"] = 1
    with pytest.raises(ValidationError) as exc:
        validate_saga(saga, known_services=KNOWN)
    assert any("extra" in e.message.lower() or "unexpected" in e.message.lower() for e in exc.value.errors)