from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import SecretStr

from spindle_core.api.dependencies import (
    get_service_allowed_hosts,
    get_service_repository,
    get_template_repository,
)
from spindle_core.api.errors import (
    request_validation_error_handler,
    schema_validation_error_handler,
)
from spindle_core.api.services import router as services_router
from spindle_core.auth.dependencies import get_current_user
from spindle_core.auth.models import User
from spindle_core.config import Settings
from spindle_core.persistence.errors import ServiceNotFound
from spindle_core.persistence.hashing import compute_content_hash
from spindle_core.persistence.service_repository import ServiceRecord
from spindle_core.schema.errors import ValidationError
from spindle_core.schema.models import ServiceDefinition


class FakeServiceRepo:
    def __init__(self) -> None:
        self.services: dict[str, ServiceRecord] = {}

    def upsert(self, definition: ServiceDefinition) -> tuple[ServiceRecord, bool]:
        ch, dump = compute_content_hash(definition)
        now = datetime.now(timezone.utc)
        name = definition.name
        if name not in self.services:
            rec = ServiceRecord(
                name=name,
                definition=dump,
                content_hash=ch,
                revision=1,
                created_at=now,
                updated_at=now,
            )
            self.services[name] = rec
            return rec, True
        existing = self.services[name]
        if existing.content_hash == ch:
            return existing, False
        rec = ServiceRecord(
            name=name,
            definition=dump,
            content_hash=ch,
            revision=existing.revision + 1,
            created_at=existing.created_at,
            updated_at=now,
        )
        self.services[name] = rec
        return rec, False

    def get(self, name: str) -> ServiceRecord:
        if name not in self.services:
            raise ServiceNotFound(f"service '{name}' not found")
        return self.services[name]

    def list(self) -> list[ServiceRecord]:
        return sorted(self.services.values(), key=lambda r: r.name)

    def delete(self, name: str) -> None:
        if name not in self.services:
            raise ServiceNotFound(f"service '{name}' not found")
        del self.services[name]

    def names(self) -> frozenset[str]:
        return frozenset(self.services.keys())


class FakeTemplateRepo:
    def __init__(self) -> None:
        self.referencing: dict[str, list[str]] = {}

    def names_referencing_service(self, service_name: str) -> list[str]:
        return self.referencing.get(service_name, [])


def _make_app(
    svc_repo: FakeServiceRepo,
    tmpl_repo: FakeTemplateRepo,
    allowed_hosts: str = "mock-services,svc,inventory",
) -> FastAPI:
    app = FastAPI()
    app.include_router(services_router)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    app.add_exception_handler(ValidationError, schema_validation_error_handler)
    app.dependency_overrides[get_current_user] = lambda: User(username="testadmin")
    app.dependency_overrides[get_service_repository] = lambda: svc_repo
    app.dependency_overrides[get_template_repository] = lambda: tmpl_repo
    app.dependency_overrides[get_service_allowed_hosts] = lambda: frozenset(
        h.strip() for h in allowed_hosts.split(",") if h.strip()
    )
    return app


def _valid_service_body(
    name: str, base_url: str = "http://mock-services:9000"
) -> dict[str, Any]:
    return {
        "name": name,
        "base_url": base_url,
        "health": "/health",
        "auth": {"type": "bearer", "secret_ref": "SERVICE_TOKEN"},
    }


def test_service_create_201_and_idempotent_and_updated_200() -> None:
    svc_repo = FakeServiceRepo()
    tmpl_repo = FakeTemplateRepo()
    app = _make_app(svc_repo, tmpl_repo)
    client = TestClient(app, raise_server_exceptions=True)

    body1 = _valid_service_body("svc1", "http://mock-services:9000")
    # 1. Create -> 201
    res1 = client.put("/services/svc1", json=body1)
    assert res1.status_code == 201
    d1 = res1.json()
    assert d1["name"] == "svc1"
    assert d1["revision"] == 1

    # 2. Identical -> 200, revision unchanged
    res2 = client.put("/services/svc1", json=body1)
    assert res2.status_code == 200
    d2 = res2.json()
    assert d2["revision"] == 1

    # 3. Changed -> 200, revision +1
    body2 = _valid_service_body("svc1", "http://svc:8080")
    res3 = client.put("/services/svc1", json=body2)
    assert res3.status_code == 200
    d3 = res3.json()
    assert d3["revision"] == 2


def test_service_list_and_get_and_404() -> None:
    svc_repo = FakeServiceRepo()
    tmpl_repo = FakeTemplateRepo()
    app = _make_app(svc_repo, tmpl_repo)
    client = TestClient(app, raise_server_exceptions=True)

    client.put("/services/b_svc", json=_valid_service_body("b_svc"))
    client.put("/services/a_svc", json=_valid_service_body("a_svc"))

    # List returns all ordered
    res_list = client.get("/services")
    assert res_list.status_code == 200
    names = [s["name"] for s in res_list.json()]
    assert names == ["a_svc", "b_svc"]

    # Get single
    res_get = client.get("/services/a_svc")
    assert res_get.status_code == 200
    assert res_get.json()["name"] == "a_svc"

    # Get 404
    res_404 = client.get("/services/nonexistent")
    assert res_404.status_code == 404
    assert res_404.json() == {"detail": "Service not found"}


def test_service_name_validation() -> None:
    svc_repo = FakeServiceRepo()
    tmpl_repo = FakeTemplateRepo()
    app = _make_app(svc_repo, tmpl_repo)
    client = TestClient(app, raise_server_exceptions=False)

    # 1. Path and body name mismatch -> 422 at path "name"
    res_mismatch = client.put(
        "/services/svc1",
        json=_valid_service_body("svc2"),
    )
    assert res_mismatch.status_code == 422
    assert res_mismatch.json() == {
        "detail": "validation failed",
        "errors": [{"path": "name", "message": "body name must equal path name 'svc1'"}],
    }

    # 2. Bad path name -> 422 at path "name"
    res_bad_name = client.put(
        "/services/-bad_name-",
        json=_valid_service_body("-bad_name-"),
    )
    assert res_bad_name.status_code == 422
    errors = res_bad_name.json()["errors"]
    assert any(e["path"] == "name" for e in errors)


def test_service_delete_204_404_and_referenced_409() -> None:
    svc_repo = FakeServiceRepo()
    tmpl_repo = FakeTemplateRepo()
    app = _make_app(svc_repo, tmpl_repo)
    client = TestClient(app, raise_server_exceptions=True)

    client.put("/services/svc1", json=_valid_service_body("svc1"))

    # 1. Delete referenced service -> 409 with referenced_by
    tmpl_repo.referencing["svc1"] = ["workflow_order", "workflow_shipping"]
    res_del_ref = client.delete("/services/svc1")
    assert res_del_ref.status_code == 409
    body_409 = res_del_ref.json()
    assert "detail" in body_409
    assert body_409["referenced_by"] == ["workflow_order", "workflow_shipping"]

    # 2. Delete unreferenced service -> 204
    tmpl_repo.referencing["svc1"] = []
    res_del_ok = client.delete("/services/svc1")
    assert res_del_ok.status_code == 204

    # 3. Delete nonexistent service -> 404
    res_del_404 = client.delete("/services/svc1")
    assert res_del_404.status_code == 404


def test_service_allowed_hosts_rejection() -> None:
    svc_repo = FakeServiceRepo()
    tmpl_repo = FakeTemplateRepo()
    # Allowed hosts: mock-services, svc, inventory
    app = _make_app(svc_repo, tmpl_repo, allowed_hosts="mock-services,svc,inventory")
    client = TestClient(app, raise_server_exceptions=False)

    # Disallowed host -> 422 at path base_url
    res = client.put(
        "/services/svc1",
        json=_valid_service_body("svc1", base_url="http://evil-host.com:9000"),
    )
    assert res.status_code == 422
    assert res.json() == {
        "detail": "validation failed",
        "errors": [{"path": "base_url", "message": "host 'evil-host.com' is not in the allowed hosts"}],
    }


def test_service_body_size_limit() -> None:
    svc_repo = FakeServiceRepo()
    tmpl_repo = FakeTemplateRepo()
    app = _make_app(svc_repo, tmpl_repo)
    client = TestClient(app, raise_server_exceptions=False)

    headers = {"Content-Length": "300000"}
    res = client.put("/services/svc1", content=b"{}", headers=headers)
    assert res.status_code == 413


def test_service_no_leak_in_422() -> None:
    svc_repo = FakeServiceRepo()
    tmpl_repo = FakeTemplateRepo()
    app = _make_app(svc_repo, tmpl_repo)
    client = TestClient(app, raise_server_exceptions=False)

    token = "LEAK_PROBE_SECRET_98765"
    # 1. In secret_ref
    body_secret = _valid_service_body("svc1")
    body_secret["auth"] = {"type": "bearer", "secret_ref": "invalid_ref_" + token}
    res_secret = client.put("/services/svc1", json=body_secret)
    assert res_secret.status_code == 422
    assert token not in res_secret.text
    assert "input" not in res_secret.text
    assert "ctx" not in res_secret.text
    assert "url" not in res_secret.text

    # 2. In wrong-typed field
    body_type = _valid_service_body("svc1")
    body_type["health"] = 12345
    res_type = client.put("/services/svc1", json=body_type)
    assert res_type.status_code == 422
    assert token not in res_type.text
    assert "input" not in res_type.text
    assert "ctx" not in res_type.text
    assert "url" not in res_type.text

    # 3. In invalid JSON
    res_invalid_json = client.put(
        "/services/svc1",
        content=b"invalid json {" + token.encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    assert res_invalid_json.status_code == 422
    assert token not in res_invalid_json.text
    assert "input" not in res_invalid_json.text
    assert "ctx" not in res_invalid_json.text
    assert "url" not in res_invalid_json.text

