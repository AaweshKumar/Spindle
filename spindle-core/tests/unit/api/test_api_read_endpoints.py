from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from pydantic import SecretStr

from spindle_core.api.dependencies import (
    get_runner,
    get_saga_repository,
    get_template_store,
)
from spindle_core.auth.dependencies import get_current_user
from spindle_core.auth.models import User
from spindle_core.config import Settings
from spindle_core.engine.messages import DispatchStep
from spindle_core.engine.saga import SagaState, StepState
from spindle_core.engine.states import SagaStatus, StepStatus
from spindle_core.main import create_app
from spindle_core.persistence.repositories import SagaSummary
from spindle_core.templates.loader import TemplateStore
from spindle_core.templates.models import SagaTemplate, StepRef


def _make_settings(**overrides: Any) -> Settings:
    defaults = dict(
        DATABASE_URL=SecretStr("postgresql://fake/db"),
        REDIS_URL=SecretStr("redis://fake:6379/0"),
        JWT_SECRET=SecretStr("x" * 32),
        ADMIN_USERNAME="admin",
        ADMIN_PASSWORD_HASH=SecretStr("$argon2id$fake"),
        SAGA_TEMPLATES_DIR=Path("/fake/templates"),
        TAPESTRY_PATH=Path("/fake/tapestry.yaml"),
    )
    defaults.update(overrides)
    return Settings.model_construct(**defaults)


def test_get_templates_unauthorized() -> None:
    # Verify unauthenticated request to /templates returns 401
    app = create_app(_make_settings())
    client = TestClient(app, raise_server_exceptions=True)
    res = client.get("/templates")
    assert res.status_code == 401


def test_get_templates_sorted_with_steps() -> None:
    # Verify templates are returned sorted by name/version with structured steps
    app = create_app(_make_settings())
    app.dependency_overrides[get_current_user] = lambda: User(username="admin")

    t_order_v2 = SagaTemplate(
        name="order",
        version=2,
        steps=(StepRef("s1", "typeA"), StepRef("s2", "typeB")),
    )
    t_order_v1 = SagaTemplate(
        name="order",
        version=1,
        steps=(StepRef("s1", "typeA"),),
    )
    t_alpha_v1 = SagaTemplate(
        name="alpha",
        version=1,
        steps=(StepRef("step1", "typeX"),),
    )
    store = TemplateStore([t_order_v2, t_alpha_v1, t_order_v1])
    app.dependency_overrides[get_template_store] = lambda: store

    client = TestClient(app, raise_server_exceptions=True)
    res = client.get("/templates")
    assert res.status_code == 200
    assert res.json() == [
        {
            "name": "alpha",
            "version": 1,
            "steps": [{"id": "step1", "type": "typeX"}],
        },
        {
            "name": "order",
            "version": 1,
            "steps": [{"id": "s1", "type": "typeA"}],
        },
        {
            "name": "order",
            "version": 2,
            "steps": [
                {"id": "s1", "type": "typeA"},
                {"id": "s2", "type": "typeB"},
            ],
        },
    ]


def test_get_templates_empty_store() -> None:
    # Verify empty template store returns empty JSON array
    app = create_app(_make_settings())
    app.dependency_overrides[get_current_user] = lambda: User(username="admin")
    app.dependency_overrides[get_template_store] = lambda: TemplateStore([])

    client = TestClient(app, raise_server_exceptions=True)
    res = client.get("/templates")
    assert res.status_code == 200
    assert res.json() == []


def test_get_saga_unauthorized() -> None:
    # Verify unauthenticated request to /sagas/{id} returns 401
    app = create_app(_make_settings())
    client = TestClient(app, raise_server_exceptions=True)
    res = client.get("/sagas/some-id")
    assert res.status_code == 401


def test_get_saga_detail_with_steps() -> None:
    # Verify saga detail returns step states in order with string enum values
    app = create_app(_make_settings())
    app.dependency_overrides[get_current_user] = lambda: User(username="admin")

    state = SagaState(
        saga_id="saga-compensating",
        status=SagaStatus.COMPENSATING,
        steps=(
            StepState("s1", "TypeA", StepStatus.COMPENSATED),
            StepState("s2", "TypeB", StepStatus.COMPENSATING),
            StepState("s3", "TypeC", StepStatus.FAILED),
            StepState("s4", "TypeD", StepStatus.PENDING),
        ),
    )
    t1 = datetime(2026, 11, 14, 8, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 11, 14, 8, 5, 0, tzinfo=timezone.utc)
    summary = SagaSummary(
        state=state,
        template_name="order-fulfillment",
        template_version=1,
        created_at=t1,
        updated_at=t2,
    )

    class FakeRepo:
        def get_summary(self, saga_id: str) -> SagaSummary | None:
            return summary if saga_id == "saga-compensating" else None

    app.dependency_overrides[get_saga_repository] = lambda: FakeRepo()

    client = TestClient(app, raise_server_exceptions=True)
    res = client.get("/sagas/saga-compensating")
    assert res.status_code == 200
    data = res.json()
    assert set(data.keys()) == {
        "saga_id",
        "status",
        "template_name",
        "template_version",
        "created_at",
        "updated_at",
        "steps",
    }
    assert data["saga_id"] == "saga-compensating"
    assert data["status"] == "COMPENSATING"
    assert data["template_name"] == "order-fulfillment"
    assert data["template_version"] == 1
    c_at = datetime.fromisoformat(data["created_at"])
    u_at = datetime.fromisoformat(data["updated_at"])
    assert c_at.tzinfo is not None and c_at.tzinfo.utcoffset(c_at) is not None
    assert u_at.tzinfo is not None and u_at.tzinfo.utcoffset(u_at) is not None
    assert c_at < u_at
    for step in data["steps"]:
        assert set(step.keys()) == {"step_id", "step_type", "status"}
        assert isinstance(step["status"], str)
    assert data["steps"] == [
        {"step_id": "s1", "step_type": "TypeA", "status": "COMPENSATED"},
        {"step_id": "s2", "step_type": "TypeB", "status": "COMPENSATING"},
        {"step_id": "s3", "step_type": "TypeC", "status": "FAILED"},
        {"step_id": "s4", "step_type": "TypeD", "status": "PENDING"},
    ]


def test_get_saga_not_found() -> None:
    # Verify missing saga returns 404 with exact detail message
    app = create_app(_make_settings())
    app.dependency_overrides[get_current_user] = lambda: User(username="admin")

    class EmptyRepo:
        def get_summary(self, saga_id: str) -> SagaSummary | None:
            return None

    app.dependency_overrides[get_saga_repository] = lambda: EmptyRepo()

    client = TestClient(app, raise_server_exceptions=True)
    res = client.get("/sagas/unknown")
    assert res.status_code == 404
    assert res.json() == {"detail": "Saga not found"}


def test_post_sagas_regression() -> None:
    # Verify saga start response stays backward compatible with exact keys
    app = create_app(_make_settings())
    app.dependency_overrides[get_current_user] = lambda: User(username="admin")

    template = SagaTemplate(
        name="order-flow",
        version=1,
        steps=(StepRef("step1", "Type1"), StepRef("step2", "Type2")),
    )
    store = TemplateStore([template])
    app.dependency_overrides[get_template_store] = lambda: store

    class FakeRunner:
        def __init__(self) -> None:
            self.recorded_state: SagaState | None = None
            self.recorded_commands: list[Any] | None = None
            self.recorded_kwargs: dict[str, Any] = {}

        def start(self, state: SagaState, commands: list[Any], **kwargs: Any) -> None:
            self.recorded_state = state
            self.recorded_commands = list(commands)
            self.recorded_kwargs = kwargs

    runner = FakeRunner()
    app.dependency_overrides[get_runner] = lambda: runner

    client = TestClient(app, raise_server_exceptions=True)
    res = client.post("/sagas", json={"template": "order-flow"})
    assert res.status_code == 201
    data = res.json()
    assert set(data.keys()) == {"saga_id", "status"}
    assert data["status"] == "RUNNING"
    assert runner.recorded_kwargs.get("template_name") == "order-flow"
    assert runner.recorded_kwargs.get("template_version") == 1
    assert runner.recorded_state is not None
    assert runner.recorded_state.steps[0].status == StepStatus.DISPATCHED
    assert runner.recorded_commands is not None
    assert len(runner.recorded_commands) == 1
    assert isinstance(runner.recorded_commands[0], DispatchStep)


def test_openapi_schemas_and_security() -> None:
    # Verify openapi reflects security and response schemas accurately
    app = create_app(_make_settings())
    schema = app.openapi()

    templates_get = schema["paths"]["/templates"]["get"]
    assert bool(templates_get.get("security"))

    sagas_get_200 = schema["paths"]["/sagas/{saga_id}"]["get"]["responses"]["200"]
    ref = sagas_get_200["content"]["application/json"]["schema"]["$ref"]
    assert ref.endswith("SagaDetailResponse")
    detail_props = schema["components"]["schemas"]["SagaDetailResponse"]["properties"]
    assert "steps" in detail_props
    assert "template_name" in detail_props
    assert "template_version" in detail_props
    assert "created_at" in detail_props
    assert "updated_at" in detail_props

    list_get = schema["paths"]["/sagas"]["get"]
    assert bool(list_get.get("security"))
    list_200_ref = list_get["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert list_200_ref.endswith("SagaListResponse")
    query_params = {p["name"]: p for p in list_get.get("parameters", []) if p.get("in") == "query"}
    assert "status" in query_params
    assert "limit" in query_params
    assert "cursor" in query_params
    limit_schema = query_params["limit"].get("schema", {})
    assert limit_schema.get("minimum") == 1
    assert limit_schema.get("maximum") == 200
    assert limit_schema.get("default") == 50

    sagas_post_201 = schema["paths"]["/sagas"]["post"]["responses"]["201"]
    post_ref = sagas_post_201["content"]["application/json"]["schema"]["$ref"]
    assert post_ref.endswith("/SagaResponse")

