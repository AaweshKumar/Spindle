from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from spindle_core.api.dependencies import (
    get_service_repository,
    get_template_repository,
)
from spindle_core.api.errors import (
    request_validation_error_handler,
    schema_validation_error_handler,
)
from spindle_core.api.workflows import router as workflows_router
from spindle_core.auth.dependencies import get_current_user
from spindle_core.auth.models import User
from spindle_core.persistence.errors import TemplateNotFound, VersionConflict
from spindle_core.persistence.hashing import compute_content_hash
from spindle_core.persistence.template_repository import TemplateRecord
from spindle_core.schema.errors import ValidationError
from spindle_core.schema.models import SagaDefinition


class FakeWorkflowServiceRepo:
    def __init__(self, names: set[str]) -> None:
        self._names = frozenset(names)

    def names(self) -> frozenset[str]:
        return self._names


class FakeWorkflowTemplateRepo:
    def __init__(self) -> None:
        # name -> list of records
        self.templates: dict[str, list[TemplateRecord]] = {}
        self._next_id = 1

    def publish(
        self,
        definition: SagaDefinition,
        *,
        base_version: int | None = None,
        created_by: str | None = None,
    ) -> tuple[TemplateRecord, bool]:
        name = definition.name
        ch, dump = compute_content_hash(definition)
        now = datetime.now(timezone.utc)
        versions = self.templates.get(name, [])

        if not versions:
            if base_version not in (None, 0):
                raise VersionConflict(f"template '{name}' does not exist")
            rec = TemplateRecord(
                id=self._next_id,
                name=name,
                version=1,
                definition=dump,
                format_version=1,
                content_hash=ch,
                created_at=now,
                created_by=created_by,
            )
            self._next_id += 1
            self.templates[name] = [rec]
            return rec, True

        latest = versions[-1]
        if base_version is not None and base_version != latest.version:
            if latest.content_hash == ch and latest.version == base_version + 1:
                return latest, False
            raise VersionConflict(f"stale base_version: expected {base_version}, found {latest.version}")

        if latest.content_hash == ch:
            return latest, False

        rec = TemplateRecord(
            id=self._next_id,
            name=name,
            version=latest.version + 1,
            definition=dump,
            format_version=1,
            content_hash=ch,
            created_at=now,
            created_by=created_by,
        )
        self._next_id += 1
        versions.append(rec)
        return rec, True

    def get(self, name: str, version: int | None = None) -> TemplateRecord:
        versions = self.templates.get(name, [])
        if not versions:
            raise TemplateNotFound(f"workflow '{name}' not found")
        if version is None:
            return versions[-1]
        for v in versions:
            if v.version == version:
                return v
        raise TemplateNotFound(f"workflow '{name}' version {version} not found")

    def list_latest(self) -> list[TemplateRecord]:
        return sorted([v[-1] for v in self.templates.values()], key=lambda r: r.name)

    def list_versions(self, name: str) -> list[TemplateRecord]:
        return self.templates.get(name, [])


def _make_app(
    tmpl_repo: FakeWorkflowTemplateRepo,
    svc_repo: FakeWorkflowServiceRepo,
    current_username: str = "alice_dev",
) -> FastAPI:
    app = FastAPI()
    app.include_router(workflows_router)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    app.add_exception_handler(ValidationError, schema_validation_error_handler)
    app.dependency_overrides[get_current_user] = lambda: User(username=current_username)
    app.dependency_overrides[get_template_repository] = lambda: tmpl_repo
    app.dependency_overrides[get_service_repository] = lambda: svc_repo
    return app


def _valid_saga_body(name: str, step_path: str = "/charge") -> dict[str, Any]:
    return {
        "name": name,
        "steps": [
            {
                "name": "step1",
                "service": "payment",
                "action": {"method": "POST", "path": step_path},
                "timeout_s": 10.0,
                "compensation": {"method": "POST", "path": f"{step_path}/refund"},
            }
        ],
    }


def test_workflow_publish_lifecycle() -> None:
    svc_repo = FakeWorkflowServiceRepo({"payment"})
    tmpl_repo = FakeWorkflowTemplateRepo()
    app = _make_app(tmpl_repo, svc_repo, current_username="alice_dev")
    client = TestClient(app, raise_server_exceptions=True)

    body_v1 = _valid_saga_body("order_flow", "/v1/charge")
    # 1. Publish v1 -> 201
    res1 = client.post("/workflows/order_flow/versions", json=body_v1)
    assert res1.status_code == 201
    d1 = res1.json()
    assert d1["name"] == "order_flow"
    assert d1["version"] == 1
    assert d1["created_by"] == "alice_dev"

    # 2. Identical -> 200 same version
    res2 = client.post("/workflows/order_flow/versions", json=body_v1)
    assert res2.status_code == 200
    assert res2.json()["version"] == 1

    # 3. Changed -> 201 version+1
    body_v2 = _valid_saga_body("order_flow", "/v2/charge")
    res3 = client.post("/workflows/order_flow/versions?base_version=1", json=body_v2)
    assert res3.status_code == 201
    assert res3.json()["version"] == 2

    # 4. Stale base_version -> 409
    body_v3 = _valid_saga_body("order_flow", "/v3/charge")
    res_stale = client.post("/workflows/order_flow/versions?base_version=1", json=body_v3)
    assert res_stale.status_code == 409
    assert res_stale.json() == {"detail": "Version conflict"}


def test_workflow_get_list_versions_and_404() -> None:
    svc_repo = FakeWorkflowServiceRepo({"payment"})
    tmpl_repo = FakeWorkflowTemplateRepo()
    app = _make_app(tmpl_repo, svc_repo)
    client = TestClient(app, raise_server_exceptions=True)

    client.post("/workflows/flow_b/versions", json=_valid_saga_body("flow_b"))
    client.post("/workflows/flow_a/versions", json=_valid_saga_body("flow_a"))
    client.post(
        "/workflows/flow_a/versions?base_version=1",
        json=_valid_saga_body("flow_a", "/v2"),
    )

    # 1. List latest of each (ordered by name)
    res_list = client.get("/workflows")
    assert res_list.status_code == 200
    items = res_list.json()
    assert [i["name"] for i in items] == ["flow_a", "flow_b"]
    assert items[0]["version"] == 2  # flow_a latest is v2

    # 2. Get latest
    res_get = client.get("/workflows/flow_a")
    assert res_get.status_code == 200
    assert res_get.json()["version"] == 2

    # 3. Get 404 for unknown workflow
    res_404 = client.get("/workflows/unknown_flow")
    assert res_404.status_code == 404

    # 4. List versions
    res_versions = client.get("/workflows/flow_a/versions")
    assert res_versions.status_code == 200
    assert [v["version"] for v in res_versions.json()] == [1, 2]

    # 5. List versions 404
    res_v_404 = client.get("/workflows/unknown_flow/versions")
    assert res_v_404.status_code == 404

    # 6. Get specific version
    res_v1 = client.get("/workflows/flow_a/versions/1")
    assert res_v1.status_code == 200
    assert res_v1.json()["version"] == 1

    # 7. Get version 404
    res_v99_404 = client.get("/workflows/flow_a/versions/99")
    assert res_v99_404.status_code == 404


def test_workflow_validation_multierror_and_name_mismatch() -> None:
    svc_repo = FakeWorkflowServiceRepo({"payment"})
    tmpl_repo = FakeWorkflowTemplateRepo()
    app = _make_app(tmpl_repo, svc_repo)
    client = TestClient(app, raise_server_exceptions=False)

    # Unknown service error
    body_unknown_svc = {
        "name": "flow1",
        "steps": [
            {
                "name": "s1",
                "service": "ghost_service",
                "action": {"method": "POST", "path": "/test"},
                "timeout_s": 5.0,
                "compensation": {"method": "POST", "path": "/undo"},
            }
        ],
    }
    res_unk = client.post("/workflows/flow1/versions", json=body_unknown_svc)
    assert res_unk.status_code == 422
    assert res_unk.json() == {
        "detail": "validation failed",
        "errors": [{"path": "steps[0].service", "message": "service 'ghost_service' is unknown; not in known_services"}],
    }

    # 3+ violations all returned in ONE 422, plus name mismatch
    multi_bad_body = {
        "name": "different_name",
        "steps": [
            {
                "name": "s1",
                "service": "unregistered_svc",
                "action": {"method": "INVALID_METHOD", "path": "missing_slash"},
                "timeout_s": -5.0,
                "compensation": {"method": "POST", "path": "/undo"},
            }
        ],
    }
    res_multi = client.post("/workflows/target_flow/versions", json=multi_bad_body)
    assert res_multi.status_code == 422
    errs = res_multi.json()["errors"]
    # Verify at least 3 violations returned, including name mismatch
    assert len(errs) >= 3
    paths = {e["path"] for e in errs}
    assert "name" in paths
    assert "steps[0].service" in paths


def test_workflow_body_size_and_no_leak_in_422() -> None:
    svc_repo = FakeWorkflowServiceRepo({"payment"})
    tmpl_repo = FakeWorkflowTemplateRepo()
    app = _make_app(tmpl_repo, svc_repo)
    client = TestClient(app, raise_server_exceptions=False)

    # 413 on payload too large
    headers = {"Content-Length": "300000"}
    res_413 = client.post("/workflows/flow1/versions", content=b"{}", headers=headers)
    assert res_413.status_code == 413

    # No leak tests
    token = "WORKFLOW_LEAK_SECRET_445566"
    # Wrong-typed field
    res_typed = client.post(
        "/workflows/flow1/versions",
        json={"name": "flow1", "steps": "not-a-list-" + token},
    )
    assert res_typed.status_code == 422
    assert token not in res_typed.text
    assert "input" not in res_typed.text
    assert "ctx" not in res_typed.text
    assert "url" not in res_typed.text

    # Invalid JSON
    res_invalid_json = client.post(
        "/workflows/flow1/versions",
        content=b"invalid json {" + token.encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    assert res_invalid_json.status_code == 422
    assert token not in res_invalid_json.text
    assert "input" not in res_invalid_json.text
    assert "ctx" not in res_invalid_json.text
    assert "url" not in res_invalid_json.text
