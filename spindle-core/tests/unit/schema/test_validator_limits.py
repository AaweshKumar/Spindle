# Component: Spindle Architecture
# File: test_validator_limits.py
# Description: Tests for validator rules 4 and 7: timeouts, budgets, methods, paths.

"""Tests for validator rules 4 and 7.

Rule 4: step timeout_s > 0, retry policy valid, worst-case duration within budget.
Rule 7: HTTP method allowlist, safe path formatting, and service URL validation.
"""

from __future__ import annotations

import pytest

from spindle_core.schema.errors import ValidationError
from spindle_core.schema.validator import validate_saga, validate_service

KNOWN = {"svc", "other"}


def _service_dict(**overrides: object) -> dict:
    svc = {
        "name": "svc",
        "base_url": "https://api.example.com",
        "health": "/health",
        "auth": {"type": "bearer", "secret_ref": "SVC_TOKEN"},
    }
    svc.update(overrides)
    return svc


def _step(name: str = "s1", service: str = "svc", **overrides: object) -> dict:
    step = {
        "name": name,
        "service": service,
        "action": {"method": "POST", "path": "/orders"},
        "timeout_s": 10.0,
        "compensation": {"method": "DELETE", "path": "/orders/cancel"},
    }
    step.update(overrides)
    return step


def _saga(steps: list[dict]) -> dict:
    return {"name": "test_saga", "steps": steps}


def test_forward_step_budget_passes() -> None:
    # 2 attempts of 10s fits within 120s budget
    step = _step(
        timeout_s=10.0,
        retry={"max_attempts": 2},
    )
    saga = validate_saga(_saga([step]), known_services=KNOWN)
    assert saga.steps[0].timeout_s == 10.0


def test_forward_step_budget_fails() -> None:
    # 3 attempts of 60s exceeds 120s budget
    step = _step(
        timeout_s=60.0,
        retry={"max_attempts": 3},
    )
    with pytest.raises(ValidationError) as exc_info:
        validate_saga(_saga([step]), known_services=KNOWN)
    errs = exc_info.value.errors
    assert any(
        e.path == "steps[0].timeout_s" and "exceeds maximum step budget" in e.message
        for e in errs
    )


def test_forward_step_timeout_non_positive_rejected() -> None:
    # timeout_s <= 0 must be rejected
    for bad_t in (0.0, -1.0):
        step = _step(timeout_s=bad_t)
        with pytest.raises(ValidationError) as exc_info:
            validate_saga(_saga([step]), known_services=KNOWN)
        errs = exc_info.value.errors
        assert any(e.path == "steps[0].timeout_s" and "timeout_s must be > 0" in e.message for e in errs)


def test_compensation_step_timeout_non_positive_rejected() -> None:
    # compensation timeout_s <= 0 rejected
    step = _step(
        timeout_s=10.0,
        compensation={"method": "DELETE", "path": "/orders/cancel", "timeout_s": 0.0},
    )
    with pytest.raises(ValidationError) as exc_info:
        validate_saga(_saga([step]), known_services=KNOWN)
    errs = exc_info.value.errors
    assert any(
        e.path == "steps[0].compensation.timeout_s" and "timeout_s must be > 0" in e.message
        for e in errs
    )


def test_bad_retry_and_bad_timeout_both_reported() -> None:
    # Both bad retry (max_attempts=0) and bad timeout (0.0) in the same step
    step = _step(
        timeout_s=0.0,
        retry={"max_attempts": 0},
    )
    with pytest.raises(ValidationError) as exc_info:
        validate_saga(_saga([step]), known_services=KNOWN)
    paths = {e.path for e in exc_info.value.errors}
    assert "steps[0].timeout_s" in paths
    assert "steps[0].retry" in paths


def test_bad_retry_does_not_suppress_other_errors() -> None:
    # Bad retry on step 0 must not suppress unknown service on step 1
    steps = [
        _step(name="s1", retry={"max_attempts": 0}),
        _step(name="s2", service="unknown_svc"),
    ]
    with pytest.raises(ValidationError) as exc_info:
        validate_saga(_saga(steps), known_services=KNOWN)
    paths = {e.path for e in exc_info.value.errors}
    assert "steps[0].retry" in paths
    assert "steps[1].service" in paths


def test_action_method_allowlist_passes_and_fails() -> None:
    # Passing action methods
    for method in ("GET", "post", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
        step = _step(action={"method": method, "path": "/orders"})
        saga = validate_saga(_saga([step]), known_services=KNOWN)
        assert saga.steps[0].action.method == method

    # Failing action method
    step = _step(action={"method": "INVALID", "path": "/orders"})
    with pytest.raises(ValidationError) as exc_info:
        validate_saga(_saga([step]), known_services=KNOWN)
    errs = exc_info.value.errors
    assert any(e.path == "steps[0].action.method" and "not allowed" in e.message for e in errs)


def test_compensation_method_allowlist_passes_and_fails() -> None:
    # Passing compensation method
    step = _step(compensation={"method": "POST", "path": "/cancel"})
    saga = validate_saga(_saga([step]), known_services=KNOWN)
    assert saga.steps[0].compensation.method == "POST"

    # Failing compensation method
    step = _step(compensation={"method": "PURGE", "path": "/cancel"})
    with pytest.raises(ValidationError) as exc_info:
        validate_saga(_saga([step]), known_services=KNOWN)
    errs = exc_info.value.errors
    assert any(
        e.path == "steps[0].compensation.method" and "not allowed" in e.message
        for e in errs
    )


def test_action_path_rules_passes_and_fails() -> None:
    # Passing action path
    step = _step(action={"method": "POST", "path": "/api/v1/orders"})
    saga = validate_saga(_saga([step]), known_services=KNOWN)
    assert saga.steps[0].action.path == "/api/v1/orders"

    # Failing action paths: no leading slash, scheme, leading double slash
    for bad_path, expected_msg in [
        ("api/v1/orders", "path must start with '/'"),
        ("https://example.com/api", "path must not contain scheme"),
        ("//api/v1/orders", "path must not start with '//'"),
    ]:
        step = _step(action={"method": "POST", "path": bad_path})
        with pytest.raises(ValidationError) as exc_info:
            validate_saga(_saga([step]), known_services=KNOWN)
        errs = exc_info.value.errors
        assert any(
            e.path == "steps[0].action.path" and expected_msg in e.message
            for e in errs
        )


def test_compensation_path_rules_passes_and_fails() -> None:
    # Passing compensation path
    step = _step(compensation={"method": "DELETE", "path": "/orders/123"})
    saga = validate_saga(_saga([step]), known_services=KNOWN)
    assert saga.steps[0].compensation.path == "/orders/123"

    # Failing compensation paths
    for bad_path, expected_msg in [
        ("orders/123", "path must start with '/'"),
        ("http://example.com/orders", "path must not contain scheme"),
        ("//orders/123", "path must not start with '//'"),
    ]:
        step = _step(compensation={"method": "DELETE", "path": bad_path})
        with pytest.raises(ValidationError) as exc_info:
            validate_saga(_saga([step]), known_services=KNOWN)
        errs = exc_info.value.errors
        assert any(
            e.path == "steps[0].compensation.path" and expected_msg in e.message
            for e in errs
        )


def test_service_base_url_rules_and_allowed_hosts() -> None:
    # Valid service base_url passes
    svc = validate_service(_service_dict(base_url="https://api.example.com"))
    assert svc.base_url == "https://api.example.com"

    # check_url_template errors map to FieldError at base_url
    with pytest.raises(ValidationError) as exc_info:
        validate_service(_service_dict(base_url="ftp://api.example.com"))
    assert any(e.path == "base_url" for e in exc_info.value.errors)

    # allowed_hosts honored: passes when host is allowed
    svc_allowed = validate_service(
        _service_dict(base_url="https://api.example.com"),
        allowed_hosts=frozenset({"api.example.com"}),
    )
    assert svc_allowed.base_url == "https://api.example.com"

    # allowed_hosts honored: fails when host is not allowed
    with pytest.raises(ValidationError) as exc_info:
        validate_service(
            _service_dict(base_url="https://other.example.com"),
            allowed_hosts=frozenset({"api.example.com"}),
        )
    assert any(
        e.path == "base_url" and "not in the allowed hosts" in e.message
        for e in exc_info.value.errors
    )


def test_service_health_path_rules_passes_and_fails() -> None:
    # Valid health path passes
    svc = validate_service(_service_dict(health="/healthz"))
    assert svc.health == "/healthz"

    # Failing health paths
    for bad_path, expected_msg in [
        ("health", "path must start with '/'"),
        ("http://example.com/health", "path must not contain scheme"),
        ("//health", "path must not start with '//'"),
    ]:
        with pytest.raises(ValidationError) as exc_info:
            validate_service(_service_dict(health=bad_path))
        errs = exc_info.value.errors
        assert any(
            e.path == "health" and expected_msg in e.message
            for e in errs
        )
