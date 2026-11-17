from __future__ import annotations

import pytest
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel

from spindle_core.api.errors import (
    enforce_body_size,
    format_loc,
    request_validation_error_handler,
    schema_validation_error_handler,
)
from spindle_core.schema.errors import FieldError, ValidationError


def test_format_loc() -> None:
    assert format_loc(("body", "name")) == "name"
    assert format_loc(("body", "steps", 0, "service")) == "steps[0].service"
    assert format_loc(("body", "steps", 1, "action", "method")) == "steps[1].action.method"
    assert format_loc(("body",)) == ""
    assert format_loc(("query", "base_version")) == "base_version"
    assert format_loc(("path", "name")) == "name"
    assert format_loc(("header", "x-token")) == "x-token"
    assert format_loc(("steps", 2, "tags", 0)) == "steps[2].tags[0]"


def test_enforce_body_size() -> None:
    class DummyRequest:
        def __init__(self, length: str | None) -> None:
            self.headers = {"content-length": length} if length else {}

    # Within bounds: no exception
    enforce_body_size(DummyRequest("100"))  # type: ignore[arg-type]
    enforce_body_size(DummyRequest("262144"))  # type: ignore[arg-type]
    enforce_body_size(DummyRequest(None))  # type: ignore[arg-type]
    enforce_body_size(DummyRequest("not-an-int"))  # type: ignore[arg-type]

    # Exceeds bounds: 413
    with pytest.raises(HTTPException) as exc:
        enforce_body_size(DummyRequest("262145"))  # type: ignore[arg-type]
    assert exc.value.status_code == 413


class _TestPayload(BaseModel):
    name: str
    count: int


def test_authoring_422_handler_sanitizes_body() -> None:
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    app.add_exception_handler(ValidationError, schema_validation_error_handler)

    @app.post("/services/test")
    def create_service(payload: _TestPayload = Body(...)):
        return {"status": "ok"}

    @app.post("/legacy/test")
    def legacy_endpoint(payload: _TestPayload = Body(...)):
        return {"status": "ok"}

    client = TestClient(app, raise_server_exceptions=True)

    secret_marker = "SUPER_SECRET_TOKEN_DO_NOT_LEAK"
    # 1. Authoring path: 422 shape with errors list, no input/ctx/url
    res_auth = client.post(
        "/services/test",
        json={"name": "test", "count": "not-an-int-with-" + secret_marker},
    )
    assert res_auth.status_code == 422
    body_auth = res_auth.json()
    assert body_auth["detail"] == "validation failed"
    assert "errors" in body_auth
    assert body_auth["errors"] == [
        {
            "path": "count",
            "message": "Input should be a valid integer, unable to parse string as an integer",
        }
    ]
    # Verify no leak in raw response text
    raw_text = res_auth.text
    assert secret_marker not in raw_text
    assert "input" not in raw_text
    assert "ctx" not in raw_text
    assert "url" not in raw_text

    # 2. Legacy path: delegates to default FastAPI handler unchanged
    res_leg = client.post(
        "/legacy/test",
        json={"name": "test", "count": "not-an-int-with-" + secret_marker},
    )
    assert res_leg.status_code == 422
    body_leg = res_leg.json()
    assert isinstance(body_leg["detail"], list)
    assert secret_marker in res_leg.text
    assert "input" in body_leg["detail"][0]


def test_schema_validation_error_handler() -> None:
    app = FastAPI()
    app.add_exception_handler(ValidationError, schema_validation_error_handler)

    @app.get("/workflows/fail")
    def fail():
        raise ValidationError(
            [
                FieldError(path="steps[0].service", message="unknown service 'foo'"),
                FieldError(path="name", message="invalid name"),
            ]
        )

    client = TestClient(app, raise_server_exceptions=True)
    res = client.get("/workflows/fail")
    assert res.status_code == 422
    assert res.json() == {
        "detail": "validation failed",
        "errors": [
            {"path": "steps[0].service", "message": "unknown service 'foo'"},
            {"path": "name", "message": "invalid name"},
        ],
    }


def test_invalid_json_emits_empty_path_and_no_digits() -> None:
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)

    @app.post("/services/test")
    def create_service(payload: _TestPayload = Body(...)):
        return {"status": "ok"}

    client = TestClient(app, raise_server_exceptions=False)
    res = client.post(
        "/services/test",
        content=b"{invalid json syntax",
        headers={"Content-Type": "application/json"},
    )
    assert res.status_code == 422
    body = res.json()
    assert body["detail"] == "validation failed"
    assert body["errors"] == [{"path": "", "message": "invalid JSON"}]
    assert not any(c.isdigit() for c in res.text)

