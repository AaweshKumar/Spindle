# Component: Spindle Architecture
# File: test_validator_multierror.py
# Description: Tests for multi-error collection, Pydantic conversion, parsing, and secrecy.

"""Tests for multi-error collection, parser behavior, and security invariants.

- Aggregate collection: 3+ violations in one saga returned together.
- Pydantic structural errors converted to FieldError; raw exceptions never leak.
- parse_saga and parse_service parse YAML and JSON text interchangeably.
- Malformed syntax yields ValidationError instead of parser exceptions.
- ValidationError serialization (pickle, copy) preserves errors.
- Secret leaks prevented in auth error messages and representations.
- Zero I/O during validation.
"""

from __future__ import annotations

import copy
import json
import pickle
import socket
from unittest.mock import patch
import pytest
import yaml

from spindle_core.schema.errors import FieldError, ValidationError
from spindle_core.schema.validator import (
    parse_saga,
    parse_service,
    validate_saga,
    validate_service,
)

KNOWN = {"svc", "other"}


def _valid_service_dict() -> dict:
    return {
        "name": "svc",
        "base_url": "https://api.example.com",
        "health": "/health",
        "auth": {"type": "bearer", "secret_ref": "SVC_TOKEN"},
    }


def _valid_step_dict(name: str = "s1") -> dict:
    return {
        "name": name,
        "service": "svc",
        "action": {"method": "POST", "path": "/orders"},
        "timeout_s": 15.0,
        "compensation": {"method": "DELETE", "path": "/orders/cancel"},
    }


def _valid_saga_dict() -> dict:
    return {
        "name": "order_flow",
        "steps": [_valid_step_dict("step1")],
    }


def test_saga_multiple_violations_collected() -> None:
    # A saga with 4 violations across rules 4, 6, and 7
    bad_saga = {
        "name": "broken_saga",
        "steps": [
            {
                "name": "step1",
                "service": "nonexistent_service",
                "action": {"method": "BAD_METHOD", "path": "missing_leading_slash"},
                "timeout_s": -5.0,
                "compensation": {"method": "DELETE", "path": "/cancel"},
            }
        ],
    }
    with pytest.raises(ValidationError) as exc_info:
        validate_saga(bad_saga, known_services=KNOWN)
    errs = exc_info.value.errors
    assert len(errs) >= 4
    paths = {e.path for e in errs}
    assert "steps[0].service" in paths
    assert "steps[0].action.method" in paths
    assert "steps[0].action.path" in paths
    assert "steps[0].timeout_s" in paths


def test_pydantic_structural_errors_converted_to_field_error() -> None:
    # Unknown field, wrong type, and missing field converted to FieldError; no pydantic error leaks
    cases = [
        ({"unexpected_root": 123}, "unexpected_root"),
        ({"name": 12345, "steps": []}, "name"),
        ({"name": "saga_without_steps"}, "steps"),
    ]
    for bad_data, expected_path in cases:
        with pytest.raises(ValidationError) as exc_info:
            validate_saga(bad_data, known_services=KNOWN)
        assert not isinstance(exc_info.value, yaml.YAMLError)
        assert any(e.path == expected_path for e in exc_info.value.errors)


def test_pydantic_structural_error_bracket_path_with_dot() -> None:
    # Structural error on steps[2].compensation must use dotted bracket notation
    steps = [
        _valid_step_dict("s0"),
        _valid_step_dict("s1"),
        {
            "name": "s2",
            "service": "svc",
            "action": {"method": "POST", "path": "/do"},
            "timeout_s": 10.0,
            "compensation": "not-a-dict",
        },
    ]
    with pytest.raises(ValidationError) as exc_info:
        validate_saga({"name": "flow", "steps": steps}, known_services=KNOWN)
    assert any(e.path == "steps[2].compensation" for e in exc_info.value.errors)


def test_parse_saga_and_service_accept_yaml_and_json() -> None:
    # Identical definition in JSON and YAML yields equal models
    saga_data = _valid_saga_dict()
    service_data = _valid_service_dict()

    saga_json = json.dumps(saga_data)
    saga_yaml = yaml.dump(saga_data)
    assert parse_saga(saga_json, known_services=KNOWN) == parse_saga(
        saga_yaml, known_services=KNOWN
    )

    svc_json = json.dumps(service_data)
    svc_yaml = yaml.dump(service_data)
    assert parse_service(svc_json) == parse_service(svc_yaml)


def test_malformed_yaml_and_json_yields_validation_error() -> None:
    # Malformed text raises ValidationError with path="", not a raw parser error
    bad_payloads = [
        "{unclosed_json: [",
        "::\ninvalid : yaml : [",
    ]
    for bad_text in bad_payloads:
        with pytest.raises(ValidationError) as exc_info:
            parse_saga(bad_text, known_services=KNOWN)
        assert any(
            e.path == "" and "invalid YAML/JSON text" in e.message
            for e in exc_info.value.errors
        )

        with pytest.raises(ValidationError) as exc_info:
            parse_service(bad_text)
        assert any(
            e.path == "" and "invalid YAML/JSON text" in e.message
            for e in exc_info.value.errors
        )


def test_validation_error_pickle_and_copy_roundtrip() -> None:
    # ValidationError preserves .errors across pickle and copy round-trips
    errs = [
        FieldError(path="steps[0].timeout_s", message="budget exceeded"),
        FieldError(path="base_url", message="invalid URL"),
    ]
    orig_exc = ValidationError(errs)

    # Pickle round trip
    pickled_exc = pickle.loads(pickle.dumps(orig_exc))
    assert pickled_exc.errors == orig_exc.errors
    assert str(pickled_exc) == str(orig_exc)

    # Shallow copy
    copied_exc = copy.copy(orig_exc)
    assert copied_exc.errors == orig_exc.errors

    # Deep copy
    deepcopy_exc = copy.deepcopy(orig_exc)
    assert deepcopy_exc.errors == orig_exc.errors


def test_secret_leak_prevention_in_auth_validation() -> None:
    # Secret tokens must NEVER appear in str(exc), repr(exc), message, or path
    secret_tokens = ["sk-live-abc123xyz", "token-forbidden-inline-value-999"]
    for token in secret_tokens:
        bad_svc = _valid_service_dict()
        bad_svc["auth"] = {"type": "bearer", "secret_ref": token}

        with pytest.raises(ValidationError) as exc_info:
            validate_service(bad_svc)

        exc_str = str(exc_info.value)
        exc_repr = repr(exc_info.value)
        assert token not in exc_str
        assert token not in exc_repr
        for err in exc_info.value.errors:
            assert token not in err.message
            assert token not in err.path

        # Also verify via parse_service with JSON payload
        with pytest.raises(ValidationError) as exc_info:
            parse_service(json.dumps(bad_svc))
        assert token not in str(exc_info.value)
        assert token not in repr(exc_info.value)
        for err in exc_info.value.errors:
            assert token not in err.message
            assert token not in err.path


def test_validators_perform_no_io() -> None:
    # Assert validators perform zero network I/O by patching socket creation
    with patch("socket.socket", side_effect=RuntimeError("Network I/O forbidden")):
        svc = validate_service(_valid_service_dict())
        assert svc.name == "svc"

        saga = validate_saga(_valid_saga_dict(), known_services=KNOWN)
        assert saga.name == "order_flow"

        parsed_svc = parse_service(json.dumps(_valid_service_dict()))
        assert parsed_svc.name == "svc"

        parsed_saga = parse_saga(json.dumps(_valid_saga_dict()), known_services=KNOWN)
        assert parsed_saga.name == "order_flow"
