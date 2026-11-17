from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from spindle_core.api.dependencies import (
    get_service_allowed_hosts,
    get_service_repository,
)
from spindle_core.api.errors import (
    request_validation_error_handler,
    schema_validation_error_handler,
)
from spindle_core.api.validation import router as validation_router
from spindle_core.auth.dependencies import get_current_user
from spindle_core.auth.models import User
from spindle_core.schema.errors import ValidationError


class CountingFakeServiceRepo:
    def __init__(self, names: set[str]) -> None:
        self._names = frozenset(names)
        self.write_count = 0

    def names(self) -> frozenset[str]:
        return self._names

    def upsert(self, *args: Any, **kwargs: Any) -> Any:
        self.write_count += 1
        raise AssertionError("upsert should not be called in validation endpoints")


def _make_app(
    svc_repo: CountingFakeServiceRepo,
    allowed_hosts: str = "mock-services,svc,inventory",
) -> FastAPI:
    app = FastAPI()
    app.include_router(validation_router)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    app.add_exception_handler(ValidationError, schema_validation_error_handler)
    app.dependency_overrides[get_current_user] = lambda: User(username="testadmin")
    app.dependency_overrides[get_service_repository] = lambda: svc_repo
    app.dependency_overrides[get_service_allowed_hosts] = lambda: frozenset(
        h.strip() for h in allowed_hosts.split(",") if h.strip()
    )
    return app


def _valid_service_body(name: str = "svc1", host: str = "mock-services") -> dict[str, Any]:
    return {
        "name": name,
        "base_url": f"http://{host}:9000",
        "health": "/health",
        "auth": {"type": "bearer", "secret_ref": "SVC_KEY"},
    }


def _valid_workflow_body(name: str = "flow1", service: str = "payment") -> dict[str, Any]:
    return {
        "name": name,
        "steps": [
            {
                "name": "s1",
                "service": service,
                "action": {"method": "POST", "path": "/run"},
                "timeout_s": 10.0,
                "compensation": {"method": "POST", "path": "/undo"},
            }
        ],
    }


def test_validate_service_success_and_failure_no_writes() -> None:
    svc_repo = CountingFakeServiceRepo({"payment"})
    app = _make_app(svc_repo, allowed_hosts="mock-services")
    client = TestClient(app, raise_server_exceptions=True)

    # 1. Valid service -> 200 {"valid": True, "errors": []}
    res_ok = client.post("/validate/service", json=_valid_service_body("svc1", "mock-services"))
    assert res_ok.status_code == 200
    assert res_ok.json() == {"valid": True, "errors": []}

    # 2. Invalid service (host disallowed) -> 200 {"valid": False, "errors": [...]}
    res_bad = client.post("/validate/service", json=_valid_service_body("svc1", "evil.com"))
    assert res_bad.status_code == 200
    body_bad = res_bad.json()
    assert body_bad["valid"] is False
    assert len(body_bad["errors"]) > 0
    assert body_bad["errors"][0]["path"] == "base_url"

    # Confirm no writes were performed
    assert svc_repo.write_count == 0


def test_validate_workflow_success_and_failure_no_writes() -> None:
    svc_repo = CountingFakeServiceRepo({"payment"})
    app = _make_app(svc_repo)
    client = TestClient(app, raise_server_exceptions=True)

    # 1. Valid workflow -> 200 {"valid": True, "errors": []}
    res_ok = client.post("/validate/workflow", json=_valid_workflow_body("flow1", "payment"))
    assert res_ok.status_code == 200
    assert res_ok.json() == {"valid": True, "errors": []}

    # 2. Invalid workflow (unknown service) -> 200 {"valid": False, "errors": [...]}
    res_bad = client.post("/validate/workflow", json=_valid_workflow_body("flow1", "unknown_service"))
    assert res_bad.status_code == 200
    body_bad = res_bad.json()
    assert body_bad["valid"] is False
    assert len(body_bad["errors"]) > 0
    assert body_bad["errors"][0]["path"] == "steps[0].service"

    # Confirm no writes were performed
    assert svc_repo.write_count == 0


def test_validate_malformed_envelope_and_body_size() -> None:
    svc_repo = CountingFakeServiceRepo({"payment"})
    app = _make_app(svc_repo)
    client = TestClient(app, raise_server_exceptions=False)

    # Body not an object (array) -> 422
    res_arr = client.post("/validate/service", json=[1, 2, 3])
    assert res_arr.status_code == 422

    # Body not an object (string) -> 422
    res_str = client.post("/validate/workflow", json="not a json object")
    assert res_str.status_code == 422

    # Invalid JSON syntax -> 422
    res_inv = client.post(
        "/validate/service",
        content=b"{invalid json",
        headers={"Content-Type": "application/json"},
    )
    assert res_inv.status_code == 422

    # Payload size > 262144 -> 413
    res_413 = client.post(
        "/validate/service",
        content=b"{}",
        headers={"Content-Length": "300000"},
    )
    assert res_413.status_code == 413


def test_validate_no_leak_in_422() -> None:
    svc_repo = CountingFakeServiceRepo({"payment"})
    app = _make_app(svc_repo)
    client = TestClient(app, raise_server_exceptions=False)

    token = "VALIDATE_LEAK_SECRET_778899"
    # Invalid JSON on /validate/service
    res1 = client.post(
        "/validate/service",
        content=b"invalid json {" + token.encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    assert res1.status_code == 422
    assert token not in res1.text
    assert "input" not in res1.text
    assert "ctx" not in res1.text
    assert "url" not in res1.text

    # Invalid JSON on /validate/workflow
    res2 = client.post(
        "/validate/workflow",
        content=b"invalid json {" + token.encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    assert res2.status_code == 422
    assert token not in res2.text
    assert "input" not in res2.text
    assert "ctx" not in res2.text
    assert "url" not in res2.text
