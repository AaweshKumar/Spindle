from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from pydantic import SecretStr

from spindle_core.api.cursor import decode_cursor, encode_cursor
from spindle_core.api.dependencies import get_saga_repository
from spindle_core.auth.dependencies import get_current_user
from spindle_core.auth.models import User
from spindle_core.config import Settings
from spindle_core.engine.saga import SagaState, StepState
from spindle_core.engine.states import SagaStatus, StepStatus
from spindle_core.main import create_app
from spindle_core.persistence.repositories import SagaSummary


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


class FakeListRepo:
    def __init__(self, rows: list[SagaSummary] | None = None) -> None:
        self.rows = rows if rows is not None else []
        self.recorded_kwargs: dict[str, Any] = {}
        self.call_count = 0

    def list_summaries(
        self,
        *,
        status: SagaStatus | None = None,
        limit: int,
        after: tuple[datetime, str] | None = None,
    ) -> list[SagaSummary]:
        self.call_count += 1
        self.recorded_kwargs = {"status": status, "limit": limit, "after": after}
        return self.rows


def _make_summary(saga_id: str, status: SagaStatus = SagaStatus.RUNNING, created_offset_seconds: int = 0) -> SagaSummary:
    t0 = datetime(2026, 11, 14, 8, 0, 0, tzinfo=timezone.utc)
    t = t0 + timedelta(seconds=created_offset_seconds)
    state = SagaState(
        saga_id=saga_id,
        status=status,
        steps=(StepState("s1", "TypeA", StepStatus.SUCCEEDED),),
    )
    return SagaSummary(
        state=state,
        template_name="order-fulfillment",
        template_version=1,
        created_at=t,
        updated_at=t + timedelta(seconds=1),
    )


def test_list_sagas_unauthorized() -> None:
    # Verify GET /sagas returns 401 when no auth header is provided
    app = create_app(_make_settings())
    client = TestClient(app, raise_server_exceptions=True)
    res = client.get("/sagas")
    assert res.status_code == 401


def test_list_sagas_default_request() -> None:
    # Verify default GET /sagas requests limit=51 and returns items with full key set
    app = create_app(_make_settings())
    app.dependency_overrides[get_current_user] = lambda: User(username="admin")
    rows = [_make_summary("s1", created_offset_seconds=10), _make_summary("s2", created_offset_seconds=5)]
    repo = FakeListRepo(rows)
    app.dependency_overrides[get_saga_repository] = lambda: repo

    client = TestClient(app, raise_server_exceptions=True)
    res = client.get("/sagas")
    assert res.status_code == 200
    assert repo.recorded_kwargs == {"status": None, "limit": 51, "after": None}
    data = res.json()
    assert data["next_cursor"] is None
    assert len(data["items"]) == 2
    assert data["items"][0]["saga_id"] == "s1"
    assert data["items"][1]["saga_id"] == "s2"
    expected_keys = {"saga_id", "status", "template_name", "template_version", "created_at", "updated_at", "steps"}
    for item in data["items"]:
        assert set(item.keys()) == expected_keys


def test_list_sagas_has_more_returns_next_cursor() -> None:
    # Verify pagination cursor is constructed from the last item on the page
    app = create_app(_make_settings())
    app.dependency_overrides[get_current_user] = lambda: User(username="admin")
    rows = [
        _make_summary("s1", created_offset_seconds=30),
        _make_summary("s2", created_offset_seconds=20),
        _make_summary("s3", created_offset_seconds=10),
    ]
    repo = FakeListRepo(rows)
    app.dependency_overrides[get_saga_repository] = lambda: repo

    client = TestClient(app, raise_server_exceptions=True)
    res = client.get("/sagas?limit=2")
    assert res.status_code == 200
    data = res.json()
    assert len(data["items"]) == 2
    assert data["items"][0]["saga_id"] == "s1"
    assert data["items"][1]["saga_id"] == "s2"
    assert data["next_cursor"] is not None
    decoded_dt, decoded_id = decode_cursor(data["next_cursor"])
    assert decoded_id == "s2"
    assert decoded_dt == rows[1].created_at


def test_list_sagas_exactly_limit_rows_next_cursor_null() -> None:
    # Verify exactly limit rows yields next_cursor null
    app = create_app(_make_settings())
    app.dependency_overrides[get_current_user] = lambda: User(username="admin")
    rows = [_make_summary("s1", created_offset_seconds=20), _make_summary("s2", created_offset_seconds=10)]
    repo = FakeListRepo(rows)
    app.dependency_overrides[get_saga_repository] = lambda: repo

    client = TestClient(app, raise_server_exceptions=True)
    res = client.get("/sagas?limit=2")
    assert res.status_code == 200
    data = res.json()
    assert len(data["items"]) == 2
    assert data["next_cursor"] is None


def test_list_sagas_cursor_passed_to_repo() -> None:
    # Verify valid cursor string is decoded and forwarded as after tuple
    app = create_app(_make_settings())
    app.dependency_overrides[get_current_user] = lambda: User(username="admin")
    repo = FakeListRepo([])
    app.dependency_overrides[get_saga_repository] = lambda: repo

    dt = datetime(2026, 11, 14, 8, 30, 0, tzinfo=timezone.utc)
    c = encode_cursor(dt, "saga-after")
    client = TestClient(app, raise_server_exceptions=True)
    res = client.get(f"/sagas?cursor={c}")
    assert res.status_code == 200
    assert repo.recorded_kwargs["after"] == (dt, "saga-after")


def test_list_sagas_status_filter() -> None:
    # Verify valid status enum forwarded and bogus status rejected with 422
    app = create_app(_make_settings())
    app.dependency_overrides[get_current_user] = lambda: User(username="admin")
    repo = FakeListRepo([])
    app.dependency_overrides[get_saga_repository] = lambda: repo

    client = TestClient(app, raise_server_exceptions=True)
    res = client.get("/sagas?status=COMPENSATED")
    assert res.status_code == 200
    assert repo.recorded_kwargs["status"] == SagaStatus.COMPENSATED

    res_bogus = client.get("/sagas?status=bogus")
    assert res_bogus.status_code == 422
    assert repo.call_count == 1


def test_list_sagas_limit_validation() -> None:
    # Verify limit bounds: 0, 201, abc give 422 without repo call; 200 passes
    app = create_app(_make_settings())
    app.dependency_overrides[get_current_user] = lambda: User(username="admin")
    repo = FakeListRepo([])
    app.dependency_overrides[get_saga_repository] = lambda: repo

    client = TestClient(app, raise_server_exceptions=True)
    for bad_limit in ["0", "201", "abc"]:
        r = client.get(f"/sagas?limit={bad_limit}")
        assert r.status_code == 422
    assert repo.call_count == 0

    r_max = client.get("/sagas?limit=200")
    assert r_max.status_code == 200
    assert repo.recorded_kwargs["limit"] == 201


def test_list_sagas_invalid_cursor() -> None:
    # Verify malformed cursor returns 422 Invalid cursor without calling repo
    app = create_app(_make_settings())
    app.dependency_overrides[get_current_user] = lambda: User(username="admin")
    repo = FakeListRepo([])
    app.dependency_overrides[get_saga_repository] = lambda: repo

    client = TestClient(app, raise_server_exceptions=True)
    r = client.get("/sagas?cursor=garbage")
    assert r.status_code == 422
    assert r.json() == {"detail": "Invalid cursor"}
    assert repo.call_count == 0


def test_list_sagas_empty_result() -> None:
    # Verify empty repository result returns empty items list and null next_cursor
    app = create_app(_make_settings())
    app.dependency_overrides[get_current_user] = lambda: User(username="admin")
    repo = FakeListRepo([])
    app.dependency_overrides[get_saga_repository] = lambda: repo

    client = TestClient(app, raise_server_exceptions=True)
    r = client.get("/sagas")
    assert r.status_code == 200
    assert r.json() == {"items": [], "next_cursor": None}

