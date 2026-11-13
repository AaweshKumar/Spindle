from __future__ import annotations

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
from spindle_core.orchestrator.ports import LoadedSaga
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
    loaded = LoadedSaga(state=state, version=3)

    class FakeRepo:
        def load(self, saga_id: str) -> LoadedSaga | None:
            return loaded if saga_id == "saga-compensating" else None

    app.dependency_overrides[get_saga_repository] = lambda: FakeRepo()

    client = TestClient(app, raise_server_exceptions=True)
    res = client.get("/sagas/saga-compensating")
    assert res.status_code == 200
    data = res.json()
    assert set(data.keys()) == {"saga_id", "status", "steps"}
    assert data["saga_id"] == "saga-compensating"
    assert data["status"] == "COMPENSATING"
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
        def load(self, saga_id: str) -> LoadedSaga | None:
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

        def start(self, state: SagaState, commands: list[Any]) -> None:
            self.recorded_state = state
            self.recorded_commands = list(commands)

    runner = FakeRunner()
    app.dependency_overrides[get_runner] = lambda: runner

    client = TestClient(app, raise_server_exceptions=True)
    res = client.post("/sagas", json={"template": "order-flow"})
    assert res.status_code == 201
    data = res.json()
    assert set(data.keys()) == {"saga_id", "status"}
    assert data["status"] == "RUNNING"
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
    assert "steps" in schema["components"]["schemas"]["SagaDetailResponse"]["properties"]

    sagas_post_201 = schema["paths"]["/sagas"]["post"]["responses"]["201"]
    post_ref = sagas_post_201["content"]["application/json"]["schema"]["$ref"]
    assert post_ref.endswith("/SagaResponse")

