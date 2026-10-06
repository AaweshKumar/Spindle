"""
Unit tests for connectors/caller.py.
HTTP calls are mocked via the HttpClient Protocol — no live server required.
"""

import json
from unittest.mock import MagicMock

import pytest

from spindle_core.connectors.caller import (
    HttpResponse,
    build_request_body,
    call_endpoint,
    is_success,
)
from spindle_core.tapestry.models import RequestConfig, SuccessCheck


def _ok(body: dict | None = None) -> HttpResponse:
    return HttpResponse(status_code=200, body=json.dumps(body or {}).encode())


def _err(status: int = 500, body: dict | None = None) -> HttpResponse:
    return HttpResponse(status_code=status, body=json.dumps(body or {}).encode())


# ---- build_request_body ----

def test_forwards_all_fields_when_no_mapping():
    config = RequestConfig(url="http://x")
    fields = {"saga_id": "s1", "step_id": "st1", "type": "DispatchStep"}
    body = build_request_body(config, fields)
    assert json.loads(body) == fields


def test_maps_only_declared_fields():
    config = RequestConfig(url="http://x", field_mapping={"order_id": "saga_id"})
    fields = {"saga_id": "abc", "step_id": "xyz"}
    body = build_request_body(config, fields)
    decoded = json.loads(body)
    assert decoded == {"order_id": "abc"}
    assert "step_id" not in decoded


def test_missing_source_field_becomes_empty_string():
    config = RequestConfig(url="http://x", field_mapping={"order_id": "no_such_field"})
    body = build_request_body(config, {})
    assert json.loads(body) == {"order_id": ""}


def test_body_is_valid_json_bytes():
    config = RequestConfig(url="http://x")
    body = build_request_body(config, {"k": "v"})
    assert isinstance(body, bytes)
    json.loads(body)  # must not raise


# ---- is_success — status code checks ----

def test_is_success_true_for_200():
    assert is_success(HttpResponse(200, b"{}"), SuccessCheck()) is True


def test_is_success_true_for_all_2xx():
    check = SuccessCheck()
    for code in [200, 201, 204, 206, 299]:
        assert is_success(HttpResponse(code, b"{}"), check) is True


def test_is_success_false_for_3xx():
    assert is_success(HttpResponse(301, b"{}"), SuccessCheck()) is False


def test_is_success_false_for_4xx():
    assert is_success(HttpResponse(400, b"{}"), SuccessCheck()) is False


def test_is_success_false_for_5xx():
    assert is_success(HttpResponse(500, b"{}"), SuccessCheck()) is False


def test_is_success_respects_custom_ok_status_codes():
    check = SuccessCheck(ok_status_codes=frozenset({201}))
    assert is_success(HttpResponse(200, b"{}"), check) is False
    assert is_success(HttpResponse(201, b"{}"), check) is True


# ---- is_success — body field checks ----

def test_is_success_body_field_exact_match_passes():
    body = json.dumps({"status": "ok"}).encode()
    check = SuccessCheck(body_field="status", body_field_value="ok")
    assert is_success(HttpResponse(200, body), check) is True


def test_is_success_body_field_exact_match_fails_on_wrong_value():
    body = json.dumps({"status": "pending"}).encode()
    check = SuccessCheck(body_field="status", body_field_value="ok")
    assert is_success(HttpResponse(200, body), check) is False


def test_is_success_body_field_exact_match_fails_when_field_missing():
    body = json.dumps({}).encode()
    check = SuccessCheck(body_field="status", body_field_value="ok")
    assert is_success(HttpResponse(200, body), check) is False


def test_is_success_truthy_body_field_passes_when_field_is_true():
    body = json.dumps({"active": True}).encode()
    check = SuccessCheck(body_field="active")
    assert is_success(HttpResponse(200, body), check) is True


def test_is_success_truthy_body_field_fails_when_field_is_false():
    body = json.dumps({"active": False}).encode()
    check = SuccessCheck(body_field="active")
    assert is_success(HttpResponse(200, body), check) is False


def test_is_success_truthy_body_field_fails_when_field_is_zero():
    body = json.dumps({"count": 0}).encode()
    check = SuccessCheck(body_field="count")
    assert is_success(HttpResponse(200, body), check) is False


def test_is_success_non_json_body_with_body_field_returns_false():
    resp = HttpResponse(status_code=200, body=b"not json at all")
    check = SuccessCheck(body_field="status")
    assert is_success(resp, check) is False


def test_is_success_non_dict_json_body_returns_false():
    """A JSON body that parses successfully but is not a dict must fail."""
    resp = HttpResponse(status_code=200, body=b"[]")
    check = SuccessCheck(body_field="status")
    assert is_success(resp, check) is False


def test_is_success_status_check_takes_priority_over_body():
    """A 500 response must fail even if the body would pass the field check."""
    body = json.dumps({"status": "ok"}).encode()
    check = SuccessCheck(body_field="status", body_field_value="ok")
    assert is_success(HttpResponse(500, body), check) is False


# ---- call_endpoint ----

def test_call_endpoint_passes_method_and_url():
    config = RequestConfig(url="http://svc/action", method="PUT")
    mock_client = MagicMock()
    mock_client.request.return_value = _ok()
    call_endpoint(config, {}, mock_client)
    args = mock_client.request.call_args
    assert args.args[0] == "PUT"
    assert args.args[1] == "http://svc/action"


def test_call_endpoint_sets_content_type_header():
    config = RequestConfig(url="http://svc/action")
    mock_client = MagicMock()
    mock_client.request.return_value = _ok()
    call_endpoint(config, {}, mock_client)
    headers = mock_client.request.call_args.kwargs.get("headers", {})
    assert headers.get("Content-Type") == "application/json"


def test_call_endpoint_merges_custom_headers():
    config = RequestConfig(url="http://svc/action", headers={"X-Token": "secret"})
    mock_client = MagicMock()
    mock_client.request.return_value = _ok()
    call_endpoint(config, {}, mock_client)
    headers = mock_client.request.call_args.kwargs.get("headers", {})
    assert headers.get("X-Token") == "secret"
    assert headers.get("Content-Type") == "application/json"


def test_call_endpoint_passes_timeout():
    config = RequestConfig(url="http://svc/action", timeout_seconds=5.0)
    mock_client = MagicMock()
    mock_client.request.return_value = _ok()
    call_endpoint(config, {}, mock_client)
    timeout = mock_client.request.call_args.kwargs.get("timeout")
    assert timeout == 5.0


def test_call_endpoint_propagates_http_client_exception():
    config = RequestConfig(url="http://svc/action")
    mock_client = MagicMock()
    mock_client.request.side_effect = OSError("connection refused")
    with pytest.raises(OSError):
        call_endpoint(config, {}, mock_client)
