# Component: Spindle Architecture
# File: test_schema_models.py
# Description: Unit tests for the user-defined saga schema models.

"""Unit tests for ``spindle_core.schema.models``.

Covers: strict field rejection, dual YAML/JSON equality, mode default and
mode restriction, and the SuccessCriteria status range.
"""

from __future__ import annotations

import json

import pytest
import yaml

from spindle_core.schema.models import (
    Action,
    AuthReference,
    Compensation,
    RetryPolicy,
    SagaDefinition,
    ServiceDefinition,
    Step,
    SuccessCriteria,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _service_dict(name: str = "svc") -> dict:
    return {
        "name": name,
        "base_url": "http://svc.example.com",
        "health": "/health",
        "auth": {"type": "bearer", "secret_ref": "SVC_TOKEN"},
    }


def _step_dict(name: str = "s1", service: str = "svc", **overrides: object) -> dict:
    step = {
        "name": name,
        "service": service,
        "action": {"method": "POST", "path": "/orders"},
        "timeout_s": 30.0,
    }
    step.update(overrides)
    return step


def _saga_dict(name: str = "order", steps: list[dict] | None = None) -> dict:
    if steps is None:
        steps = [
            _step_dict(
                name="create",
                service="svc",
                compensation={"method": "DELETE", "path": "/orders/{{steps.create.response.id}}"},
            ),
            _step_dict(
                name="final",
                service="svc",
                non_compensable=True,
            ),
        ]
    return {"name": name, "steps": steps}


# ---------------------------------------------------------------------------
# ServiceDefinition
# ---------------------------------------------------------------------------


def test_service_definition_from_dict() -> None:
    svc = ServiceDefinition.model_validate(_service_dict())
    assert svc.name == "svc"
    assert svc.base_url == "http://svc.example.com"
    assert svc.health == "/health"
    assert svc.auth.type == "bearer"
    assert svc.auth.secret_ref == "SVC_TOKEN"


def test_service_definition_extra_field_rejected() -> None:
    data = _service_dict()
    data["unexpected"] = 1
    with pytest.raises(Exception):
        ServiceDefinition.model_validate(data)


def test_auth_reference_inline_secret_rejected() -> None:
    # Inline secret values must be rejected; only a reference name is allowed.
    data = _service_dict()
    data["auth"] = {"type": "bearer", "secret_ref": "actual-token-value"}
    with pytest.raises(Exception):
        ServiceDefinition.model_validate(data)


def test_auth_reference_unknown_type_rejected() -> None:
    data = _service_dict()
    data["auth"] = {"type": "basic", "secret_ref": "SVC_TOKEN"}
    with pytest.raises(Exception):
        ServiceDefinition.model_validate(data)


# ---------------------------------------------------------------------------
# SagaDefinition / Step
# ---------------------------------------------------------------------------


def test_saga_definition_from_dict() -> None:
    saga = SagaDefinition.model_validate(_saga_dict())
    assert saga.name == "order"
    assert len(saga.steps) == 2
    assert saga.steps[0].name == "create"
    assert saga.steps[1].name == "final"
    assert saga.steps[1].non_compensable is True


def test_saga_definition_extra_field_rejected() -> None:
    data = _saga_dict()
    data["unexpected"] = 1
    with pytest.raises(Exception):
        SagaDefinition.model_validate(data)


def test_step_extra_field_rejected() -> None:
    data = _saga_dict()
    data["steps"][0]["bogus"] = 1
    with pytest.raises(Exception):
        SagaDefinition.model_validate(data)


def test_step_mode_default_is_sync() -> None:
    saga = SagaDefinition.model_validate(_saga_dict())
    assert saga.steps[0].mode == "sync"


def test_step_mode_only_sync_accepted() -> None:
    data = _saga_dict()
    data["steps"][0]["mode"] = "async"
    with pytest.raises(Exception):
        SagaDefinition.model_validate(data)


def test_step_mode_explicit_sync_accepted() -> None:
    data = _saga_dict()
    data["steps"][0]["mode"] = "sync"
    saga = SagaDefinition.model_validate(data)
    assert saga.steps[0].mode == "sync"


def test_success_criteria_status_range() -> None:
    sc = SuccessCriteria(status=[200, 201, 404])
    assert sc.status == [200, 201, 404]


def test_success_criteria_empty_rejected() -> None:
    with pytest.raises(Exception):
        SuccessCriteria(status=[])


def test_success_criteria_out_of_range_rejected() -> None:
    with pytest.raises(Exception):
        SuccessCriteria(status=[99])
    with pytest.raises(Exception):
        SuccessCriteria(status=[600])


def test_retry_policy_defaults() -> None:
    rp = RetryPolicy()
    assert rp.max_attempts == 3
    assert rp.initial_backoff_seconds == 0.5
    assert rp.backoff_multiplier == 2.0
    assert rp.max_backoff_seconds == 10.0


# ---------------------------------------------------------------------------
# Dual YAML/JSON equality
# ---------------------------------------------------------------------------


def test_yaml_and_json_parse_to_equal_models() -> None:
    data = _saga_dict()
    yaml_text = yaml.safe_dump(data)
    json_text = json.dumps(data)

    saga_yaml = SagaDefinition.model_validate(yaml.safe_load(yaml_text))
    saga_json = SagaDefinition.model_validate(json.loads(json_text))

    assert saga_yaml == saga_json


def test_yaml_and_json_parse_to_equal_services() -> None:
    data = _service_dict()
    yaml_text = yaml.safe_dump(data)
    json_text = json.dumps(data)

    svc_yaml = ServiceDefinition.model_validate(yaml.safe_load(yaml_text))
    svc_json = ServiceDefinition.model_validate(json.loads(json_text))

    assert svc_yaml == svc_json


def test_compensation_optional() -> None:
    step = Step.model_validate(_step_dict(name="s1", service="svc"))
    assert step.compensation is None
    assert step.non_compensable is False
    assert step.retry is None
    assert step.success is None
    assert step.idempotency_header is None


def test_action_body_optional() -> None:
    step = Step.model_validate(
        _step_dict(name="s1", service="svc", action={"method": "GET", "path": "/x"})
    )
    assert step.action.body is None