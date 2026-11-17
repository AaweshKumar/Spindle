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


def test_workflow_runs_preconditions_and_501() -> None:
    svc_repo = FakeWorkflowServiceRepo({"payment"})
    tmpl_repo = FakeWorkflowTemplateRepo()
    app = _make_app(tmpl_repo, svc_repo)
    client = TestClient(app, raise_server_exceptions=False)

    wf_body = {
        "name": "order_flow",
        "steps": [
            {
                "name": "s1",
                "service": "payment",
                "action": {
                    "method": "POST",
                    "path": "/charge/{{input.order.id}}",
                    "body": {"amount": "{{input.amount}}"},
                },
                "timeout_s": 5.0,
                "compensation": {"method": "POST", "path": "/refund/{{input.order.id}}"},
            }
        ],
    }
    client.post("/workflows/order_flow/versions", json=wf_body)

    # 1. 404 unknown workflow
    res_404_wf = client.post("/workflows/unknown_wf/runs", json={})
    assert res_404_wf.status_code == 404

    # 2. 404 unknown version
    res_404_v = client.post("/workflows/order_flow/runs", json={"version": 99})
    assert res_404_v.status_code == 404

    # 3. 422 missing input (all missing paths in one response)
    res_missing = client.post("/workflows/order_flow/runs", json={"input": {}})
    assert res_missing.status_code == 422
    err_paths = {e["path"] for e in res_missing.json()["errors"]}
    assert "input.order.id" in err_paths
    assert "input.amount" in err_paths
    for e in res_missing.json()["errors"]:
        assert e["message"] == "required input missing"

    # 4. 422 non-object input
    res_non_obj = client.post("/workflows/order_flow/runs", json={"input": "string_not_dict"})
    assert res_non_obj.status_code == 422
    assert any(e["path"] == "input" for e in res_non_obj.json()["errors"])

    # 5. 422 extra body fields
    res_extra = client.post(
        "/workflows/order_flow/runs",
        json={"version": 1, "input": {}, "extra_field": "forbidden"},
    )
    assert res_extra.status_code == 422
    assert any(e["path"] == "extra_field" for e in res_extra.json()["errors"])

    # 6. 422 oversize input
    oversize_input = {"order": {"id": "x" * 70000}, "amount": 100}
    res_oversize = client.post("/workflows/order_flow/runs", json={"input": oversize_input})
    assert res_oversize.status_code == 422
    assert any(e["message"] == "input too large" for e in res_oversize.json()["errors"])

    # 7. 422 too deep input
    deep_dict: dict = {"bottom": 1}
    for _ in range(35):
        deep_dict = {"nested": deep_dict}
    deep_input = {"order": {"id": "1"}, "amount": 100, "meta": deep_dict}
    res_deep = client.post("/workflows/order_flow/runs", json={"input": deep_input})
    assert res_deep.status_code == 422
    assert any(e["message"] == "nesting too deep" for e in res_deep.json()["errors"])

    # 8. 409 for deleted service
    svc_repo._names = frozenset()  # payment service deleted
    res_deleted_svc = client.post(
        "/workflows/order_flow/runs",
        json={"input": {"order": {"id": "1"}, "amount": 100}},
    )
    assert res_deleted_svc.status_code == 409
    body_409 = res_deleted_svc.json()
    assert body_409["detail"] == "workflow references unregistered services"
    assert body_409["services"] == ["payment"]

    # 9. 501 on valid path
    svc_repo._names = frozenset({"payment"})
    res_501 = client.post(
        "/workflows/order_flow/runs",
        json={"input": {"order": {"id": "1"}, "amount": 100}},
    )
    assert res_501.status_code == 501
    assert res_501.json() == {"detail": "workflow execution is not enabled yet"}


def test_prepare_run_helper_builds_snapshot_and_stripped_input() -> None:
    from spindle_core.api.workflows import prepare_run

    record = TemplateRecord(
        id=1,
        name="my-flow",
        version=3,
        definition={"steps": [{"name": "s1"}]},
        format_version=1,
        content_hash="hash123",
        created_at=datetime.now(timezone.utc),
        created_by="tester",
    )
    input_data = {"user": "bob", "count": 42}

    snapshot, copy_data = prepare_run(record, input_data)
    assert snapshot == {
        "name": "my-flow",
        "version": 3,
        "content_hash": "hash123",
        "definition": {"steps": [{"name": "s1"}]},
    }
    assert copy_data == {"user": "bob", "count": 42}
    assert copy_data is not input_data

    # Nested input key preservation (no envelope stripping)
    nested_input = {"input": {"input": 1}}
    _, copy_nested = prepare_run(record, nested_input)
    assert copy_nested == {"input": {"input": 1}}

    # None input
    _, copy_none = prepare_run(record, None)
    assert copy_none is None


def test_workflow_runs_validation_reraised_when_no_unregistered_services() -> None:
    # If definition fails validate_saga for reasons other than missing service, re-raise as 422
    svc_repo = FakeWorkflowServiceRepo({"payment"})
    tmpl_repo = FakeWorkflowTemplateRepo()
    app = _make_app(tmpl_repo, svc_repo)
    client = TestClient(app, raise_server_exceptions=False)

    # Corrupt definition with empty steps (fails validate_saga, but unregistered service set is empty)
    now = datetime.now(timezone.utc)
    corrupt_rec = TemplateRecord(
        id=99,
        name="corrupt_wf",
        version=1,
        definition={"name": "corrupt_wf", "steps": []},
        format_version=1,
        content_hash="h1",
        created_at=now,
        created_by="tester",
    )
    tmpl_repo.templates["corrupt_wf"] = [corrupt_rec]

    res = client.post("/workflows/corrupt_wf/runs", json={"input": {}})
    assert res.status_code == 422
    assert res.json()["detail"] == "validation failed"
    assert any(e["path"] == "steps" for e in res.json()["errors"])

