from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from pydantic import SecretStr

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
        CORS_ALLOW_ORIGINS=["http://localhost:5173"],
    )
    defaults.update(overrides)
    return Settings.model_construct(**defaults)


def test_cors_preflight_allowed_origin() -> None:
    # Verify allowed origin receives preflight approval with requested headers
    client = TestClient(create_app(_make_settings()), raise_server_exceptions=True)
    res = client.options(
        "/sagas",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert res.status_code == 200
    assert res.headers.get("access-control-allow-origin") == "http://localhost:5173"
    allowed_headers = res.headers.get("access-control-allow-headers", "").lower()
    assert "authorization" in allowed_headers


def test_cors_preflight_disallowed_origin() -> None:
    # Verify disallowed origin receives 400 without access control headers
    client = TestClient(create_app(_make_settings()), raise_server_exceptions=True)
    res = client.options(
        "/sagas",
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert res.status_code == 400
    assert "access-control-allow-origin" not in res.headers


def test_cors_simple_request_origin_filtering() -> None:
    # Verify simple request emits allow-origin only for configured origin
    client = TestClient(create_app(_make_settings()), raise_server_exceptions=False)
    res_good = client.get("/health", headers={"Origin": "http://localhost:5173"})
    assert res_good.headers.get("access-control-allow-origin") == "http://localhost:5173"

    res_bad = client.get("/health", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in res_bad.headers


def test_cors_default_empty_origins_denies_all() -> None:
    # Verify default configuration with empty origins denies preflight requests
    defaults = dict(
        DATABASE_URL=SecretStr("postgresql://fake/db"),
        REDIS_URL=SecretStr("redis://fake:6379/0"),
        JWT_SECRET=SecretStr("x" * 32),
        ADMIN_USERNAME="admin",
        ADMIN_PASSWORD_HASH=SecretStr("$argon2id$fake"),
        SAGA_TEMPLATES_DIR=Path("/fake/templates"),
        TAPESTRY_PATH=Path("/fake/tapestry.yaml"),
    )
    client = TestClient(
        create_app(Settings.model_construct(**defaults)),
        raise_server_exceptions=True,
    )
    res = client.options(
        "/sagas",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert res.status_code == 400
    assert "access-control-allow-origin" not in res.headers

