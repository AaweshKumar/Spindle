from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from spindle_core.api.dependencies import (
    get_service_allowed_hosts,
    get_service_repository,
    get_template_repository,
)
from spindle_core.auth.dependencies import get_current_user
from spindle_core.auth.models import User
from spindle_core.config import Settings
from spindle_core.main import create_app


def _make_settings(**overrides: Any) -> Settings:
    defaults = dict(
        DATABASE_URL=SecretStr("postgresql://fake/db"),
        REDIS_URL=SecretStr("redis://fake:6379/0"),
        JWT_SECRET=SecretStr("x" * 32),
        ADMIN_USERNAME="admin",
        ADMIN_PASSWORD_HASH=SecretStr("$argon2id$fake"),
        SAGA_TEMPLATES_DIR=Path("/fake/templates"),
        TAPESTRY_PATH=Path("/fake/tapestry.yaml"),
        SERVICE_ALLOWED_HOSTS="mock-services,svc,inventory",
    )
    defaults.update(overrides)
    return Settings.model_construct(**defaults)


# DoD 5: Parametrized 401 test without get_current_user override
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PUT", "/services/test"),
        ("GET", "/services"),
        ("GET", "/services/test"),
        ("DELETE", "/services/test"),
        ("POST", "/workflows/test/versions"),
        ("GET", "/workflows"),
        ("GET", "/workflows/test"),
        ("GET", "/workflows/test/versions"),
        ("GET", "/workflows/test/versions/1"),
        ("POST", "/validate/service"),
        ("POST", "/validate/workflow"),
    ],
)
def test_all_new_routes_return_401_without_token(method: str, path: str) -> None:
    app = create_app(_make_settings())
    # App is built WITHOUT get_current_user override
    client = TestClient(app, raise_server_exceptions=False)
    res = client.request(method, path)
    assert res.status_code == 401, f"Expected 401 for {method} {path}, got {res.status_code}"


# Amendment 6: Characterization test for legacy POST /sagas
def test_legacy_post_sagas_retains_default_fastapi_422_shape() -> None:
    from spindle_core.api.dependencies import get_runner, get_template_store

    app = create_app(_make_settings())
    app.dependency_overrides[get_current_user] = lambda: User(username="admin")
    app.dependency_overrides[get_template_store] = lambda: None
    app.dependency_overrides[get_runner] = lambda: None
    client = TestClient(app, raise_server_exceptions=False)

    token = "CHARACTERIZATION_LEAK_TOKEN_123"
    res = client.post(
        "/sagas",
        json={"template": "bad template name!", "bogus": token},
    )
    assert res.status_code == 422
    body = res.json()
    assert isinstance(body.get("detail"), list)
    assert token in res.text
    # Default FastAPI handler echoes "input"
    assert "input" in body["detail"][0]


# Amendment 6 & DoD 4: No leak test across all body-accepting authoring routes
def test_no_leak_on_service_write_route() -> None:
    class DummyRepo:
        def names(self) -> frozenset[str]:
            return frozenset({"payment"})

    app = create_app(_make_settings())
    app.dependency_overrides[get_current_user] = lambda: User(username="admin")
    app.dependency_overrides[get_service_repository] = lambda: DummyRepo()
    app.dependency_overrides[get_service_allowed_hosts] = lambda: frozenset({"mock-services"})

    client = TestClient(app, raise_server_exceptions=False)
    token = "SECRET_PROBE_TOKEN_998877"

    # Case 1: Token in secret_ref
    body_secret = {
        "name": "test_service",
        "base_url": "http://mock-services:9000",
        "health": "/health",
        "auth": {"type": "bearer", "secret_ref": "lowercase_invalid_" + token},
    }
    res_secret = client.put("/services/test_service", json=body_secret)
    assert res_secret.status_code == 422
    assert token not in res_secret.text
    assert "input" not in res_secret.text
    assert "ctx" not in res_secret.text
    assert "url" not in res_secret.text

    # Case 2: Wrong-typed field
    body_typed = {
        "name": "test_service",
        "base_url": "http://mock-services:9000",
        "health": 12345,
        "auth": {"type": "bearer", "secret_ref": "SVC_KEY"},
        "extra_field": token,
    }
    res_typed = client.put("/services/test_service", json=body_typed)
    assert res_typed.status_code == 422
    assert token not in res_typed.text
    assert "input" not in res_typed.text
    assert "ctx" not in res_typed.text
    assert "url" not in res_typed.text

    # Case 3: Invalid JSON body
    res_inv = client.put(
        "/services/test_service",
        content=b"invalid json {" + token.encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    assert res_inv.status_code == 422
    assert token not in res_inv.text
    assert "input" not in res_inv.text
    assert "ctx" not in res_inv.text
    assert "url" not in res_inv.text


def test_no_leak_on_workflow_write_route() -> None:
    class DummyRepo:
        def names(self) -> frozenset[str]:
            return frozenset({"payment"})

    app = create_app(_make_settings())
    app.dependency_overrides[get_current_user] = lambda: User(username="admin")
    app.dependency_overrides[get_service_repository] = lambda: DummyRepo()
    app.dependency_overrides[get_template_repository] = lambda: DummyRepo()

    client = TestClient(app, raise_server_exceptions=False)
    token = "SECRET_PROBE_TOKEN_998877"

    # Case 1: Token in secret_ref
    body_secret = {
        "name": "test_workflow",
        "steps": [
            {
                "name": "s1",
                "service": "payment",
                "action": {"method": "POST", "path": "/pay"},
                "timeout_s": 10.0,
                "compensation": {"method": "POST", "path": "/refund"},
            }
        ],
        "secret_ref": token,
    }
    res_secret = client.post("/workflows/test_workflow/versions", json=body_secret)
    assert res_secret.status_code == 422
    assert token not in res_secret.text
    assert "input" not in res_secret.text
    assert "ctx" not in res_secret.text
    assert "url" not in res_secret.text

    # Case 2: Wrong-typed field
    body_typed = {
        "name": "test_workflow",
        "steps": "not-a-list-" + token,
    }
    res_typed = client.post("/workflows/test_workflow/versions", json=body_typed)
    assert res_typed.status_code == 422
    assert token not in res_typed.text
    assert "input" not in res_typed.text
    assert "ctx" not in res_typed.text
    assert "url" not in res_typed.text

    # Case 3: Invalid JSON body
    res_inv = client.post(
        "/workflows/test_workflow/versions",
        content=b"invalid json {" + token.encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    assert res_inv.status_code == 422
    assert token not in res_inv.text
    assert "input" not in res_inv.text
    assert "ctx" not in res_inv.text
    assert "url" not in res_inv.text


def test_no_leak_on_validate_service_route() -> None:
    app = create_app(_make_settings())
    app.dependency_overrides[get_current_user] = lambda: User(username="admin")
    app.dependency_overrides[get_service_allowed_hosts] = lambda: frozenset({"mock-services"})

    client = TestClient(app, raise_server_exceptions=False)
    token = "VALIDATE_PROBE_TOKEN_112233"

    # Case 1: Token in secret_ref inside valid envelope -> 200 {"valid": false} without leak
    body_secret = {
        "name": "test_service",
        "base_url": "http://mock-services:9000",
        "health": "/health",
        "auth": {"type": "bearer", "secret_ref": "lowercase_invalid_" + token},
    }
    res_secret = client.post("/validate/service", json=body_secret)
    assert res_secret.status_code == 200
    assert res_secret.json()["valid"] is False
    assert token not in res_secret.text
    assert "input" not in res_secret.text
    assert "ctx" not in res_secret.text
    assert "url" not in res_secret.text

    # Case 2: Wrong-typed envelope (body is string containing token) -> 422 without leak
    res_str = client.post("/validate/service", json="invalid_envelope_" + token)
    assert res_str.status_code == 422
    assert token not in res_str.text
    assert "input" not in res_str.text
    assert "ctx" not in res_str.text
    assert "url" not in res_str.text

    # Case 3: Invalid JSON body -> 422 without leak
    res_inv = client.post(
        "/validate/service",
        content=b"invalid json {" + token.encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    assert res_inv.status_code == 422
    assert token not in res_inv.text
    assert "input" not in res_inv.text
    assert "ctx" not in res_inv.text
    assert "url" not in res_inv.text


def test_no_leak_on_validate_workflow_route() -> None:
    class DummyRepo:
        def names(self) -> frozenset[str]:
            return frozenset({"payment"})

    app = create_app(_make_settings())
    app.dependency_overrides[get_current_user] = lambda: User(username="admin")
    app.dependency_overrides[get_service_repository] = lambda: DummyRepo()

    client = TestClient(app, raise_server_exceptions=False)
    token = "VALIDATE_PROBE_TOKEN_112233"

    # Case 1: Token in secret_ref inside valid envelope -> 200 {"valid": false} without leak
    body_secret = {
        "name": "test_workflow",
        "steps": [
            {
                "name": "s1",
                "service": "payment",
                "action": {"method": "POST", "path": "/run"},
                "timeout_s": 10.0,
                "compensation": {"method": "POST", "path": "/undo"},
            }
        ],
        "secret_ref": token,
    }
    res_secret = client.post("/validate/workflow", json=body_secret)
    assert res_secret.status_code == 200
    assert res_secret.json()["valid"] is False
    assert token not in res_secret.text
    assert "input" not in res_secret.text
    assert "ctx" not in res_secret.text
    assert "url" not in res_secret.text

    # Case 2: Wrong-typed envelope (body is string containing token) -> 422 without leak
    res_str = client.post("/validate/workflow", json="invalid_envelope_" + token)
    assert res_str.status_code == 422
    assert token not in res_str.text
    assert "input" not in res_str.text
    assert "ctx" not in res_str.text
    assert "url" not in res_str.text

    # Case 3: Invalid JSON body -> 422 without leak
    res_inv = client.post(
        "/validate/workflow",
        content=b"invalid json {" + token.encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    assert res_inv.status_code == 422
    assert token not in res_inv.text
    assert "input" not in res_inv.text
    assert "ctx" not in res_inv.text
    assert "url" not in res_inv.text


