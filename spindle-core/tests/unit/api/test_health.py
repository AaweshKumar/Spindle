"""Unit tests for GET /health (SPEC B).

Tests 1–3: basic routing and secret-scrubbing.
Tests 4–10: thread state, probe seams, edge cases, lifespan integration.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from spindle_core.config import Settings
from spindle_core.main import create_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_settings(**overrides) -> Settings:
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


def _alive_thread():
    """Return a MagicMock that looks like a live thread."""
    t = MagicMock()
    t.is_alive.return_value = True
    return t


def _dead_thread():
    t = MagicMock()
    t.is_alive.return_value = False
    return t


def _client_all_ok(settings: Settings) -> TestClient:
    """TestClient with all 4 checks green (no lifespan)."""
    app = create_app(settings)
    app.state.relay_thread = _alive_thread()
    app.state.router_thread = _alive_thread()
    client = TestClient(app, raise_server_exceptions=True)
    return client


# ---------------------------------------------------------------------------
# Test 1 — all checks ok → 200
# ---------------------------------------------------------------------------

def test_health_all_ok_returns_200():
    settings = _make_settings()
    with (
        patch("spindle_core.api.health._check_postgres", return_value=True),
        patch("spindle_core.api.health._check_redis", return_value=True),
    ):
        client = _client_all_ok(settings)
        resp = client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "status": "ok",
        "checks": {
            "postgres": "ok",
            "redis": "ok",
            "outbox_relay": "ok",
            "command_router": "ok",
        },
    }
    assert resp.headers["cache-control"] == "no-store"
    assert "Authorization" not in resp.request.headers


# ---------------------------------------------------------------------------
# Test 2 — each of the 4 checks fails alone → 503, only that check "fail"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("failing_check", ["postgres", "redis", "outbox_relay", "command_router"])
def test_health_one_check_fails_returns_503(failing_check):
    settings = _make_settings()

    app = create_app(settings)
    app.state.relay_thread = _alive_thread()
    app.state.router_thread = _alive_thread()

    pg_ok = failing_check != "postgres"
    rd_ok = failing_check != "redis"

    if failing_check == "outbox_relay":
        app.state.relay_thread = _dead_thread()
    if failing_check == "command_router":
        app.state.router_thread = _dead_thread()

    with (
        patch("spindle_core.api.health._check_postgres", return_value=pg_ok),
        patch("spindle_core.api.health._check_redis", return_value=rd_ok),
    ):
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/health")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unavailable"
    assert body["checks"][failing_check] == "fail"
    # all other three should be "ok"
    for key, val in body["checks"].items():
        if key != failing_check:
            assert val == "ok", f"{key} should be ok, got {val}"


# ---------------------------------------------------------------------------
# Test 3 — _check_redis raises with a secret in exception message: not leaked
# ---------------------------------------------------------------------------

def test_health_redis_exception_does_not_leak_secret(caplog):
    settings = _make_settings(
        REDIS_URL=SecretStr("redis://:s3cretpw@secret-host-xyz:6379/0"),
    )
    app = create_app(settings)
    app.state.relay_thread = _alive_thread()
    app.state.router_thread = _alive_thread()

    with (
        patch("spindle_core.api.health._check_postgres", return_value=True),
        patch(
            "spindle_core.api.health.redis_lib.Redis.from_url",
            side_effect=ConnectionError("cannot connect to secret-host-xyz:6379 pw=s3cretpw"),
        ),
        caplog.at_level(logging.WARNING, logger="spindle_core.api.health"),
    ):
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/health")

    assert resp.status_code == 503
    body = resp.json()
    assert body["checks"]["redis"] == "fail"
    assert "secret-host-xyz" not in resp.text
    assert "s3cretpw" not in resp.text
    assert "secret-host-xyz" not in caplog.text
    assert "s3cretpw" not in caplog.text
    assert "ConnectionError" in caplog.text


# ---------------------------------------------------------------------------
# Test 4 — 4 checks always evaluated even when first fails
# ---------------------------------------------------------------------------

def test_health_all_checks_evaluated_even_when_first_fails():
    settings = _make_settings()
    pg_calls = []
    rd_calls = []

    def _pg(s):
        pg_calls.append(1)
        return False  # postgres fails

    def _rd(s):
        rd_calls.append(1)
        return False  # redis also fails

    app = create_app(settings)
    app.state.relay_thread = _dead_thread()
    app.state.router_thread = _dead_thread()

    with (
        patch("spindle_core.api.health._check_postgres", side_effect=_pg),
        patch("spindle_core.api.health._check_redis", side_effect=_rd),
    ):
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/health")

    assert resp.status_code == 503
    assert len(pg_calls) == 1, "postgres check not called"
    assert len(rd_calls) == 1, "redis check not called even though postgres failed"
    body = resp.json()
    assert all(v == "fail" for v in body["checks"].values())


# ---------------------------------------------------------------------------
# Test 5 — missing thread attrs → 503, both "fail", not 500
# ---------------------------------------------------------------------------

def test_health_missing_thread_attrs_returns_503_not_500():
    settings = _make_settings()
    # Create app WITHOUT setting relay_thread / router_thread
    app = create_app(settings)
    # app.state has no relay_thread / router_thread attributes
    with (
        patch("spindle_core.api.health._check_postgres", return_value=True),
        patch("spindle_core.api.health._check_redis", return_value=True),
    ):
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/health")

    assert resp.status_code == 503
    body = resp.json()
    assert body["checks"]["outbox_relay"] == "fail"
    assert body["checks"]["command_router"] == "fail"


# ---------------------------------------------------------------------------
# Test 6 — is_alive() returns False → thread check "fail"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dead_thread", ["relay", "router"])
def test_health_dead_thread_returns_fail(dead_thread):
    settings = _make_settings()
    app = create_app(settings)
    app.state.relay_thread = _dead_thread() if dead_thread == "relay" else _alive_thread()
    app.state.router_thread = _dead_thread() if dead_thread == "router" else _alive_thread()

    with (
        patch("spindle_core.api.health._check_postgres", return_value=True),
        patch("spindle_core.api.health._check_redis", return_value=True),
    ):
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/health")

    assert resp.status_code == 503
    body = resp.json()
    expected_key = "outbox_relay" if dead_thread == "relay" else "command_router"
    assert body["checks"][expected_key] == "fail"


# ---------------------------------------------------------------------------
# Test 7 — _check_postgres: dedicated engine, connect_timeout=2, dispose() called
# ---------------------------------------------------------------------------

def test_check_postgres_uses_dedicated_engine_and_always_disposes():
    import spindle_core.api.health as health_mod

    settings = _make_settings()

    # Success case
    mock_engine = MagicMock()
    mock_conn = MagicMock()
    mock_engine.connect.return_value.__enter__ = lambda s: mock_conn
    mock_engine.connect.return_value.__exit__ = lambda s, *a: False

    with patch("spindle_core.api.health.create_engine", return_value=mock_engine) as mock_ce:
        result = health_mod._check_postgres(settings)

    assert result is True
    _, kwargs = mock_ce.call_args
    assert kwargs.get("connect_args", {}).get("connect_timeout") == 2
    mock_engine.dispose.assert_called_once()

    # Failure case — engine raises on connect
    mock_engine2 = MagicMock()
    mock_engine2.connect.side_effect = Exception("boom")

    with patch("spindle_core.api.health.create_engine", return_value=mock_engine2):
        result2 = health_mod._check_postgres(settings)

    assert result2 is False
    mock_engine2.dispose.assert_called_once()


# ---------------------------------------------------------------------------
# Test 8 — _check_redis: dedicated client, timeouts=2, close() called
# ---------------------------------------------------------------------------

def test_check_redis_uses_dedicated_client_and_always_closes():
    import spindle_core.api.health as health_mod

    settings = _make_settings()

    # Success case
    mock_client = MagicMock()
    mock_client.ping.return_value = True

    with patch("spindle_core.api.health.redis_lib.Redis.from_url", return_value=mock_client) as mock_fu:
        result = health_mod._check_redis(settings)

    assert result is True
    _, kwargs = mock_fu.call_args
    assert kwargs.get("socket_connect_timeout") == 2
    assert kwargs.get("socket_timeout") == 2
    mock_client.close.assert_called_once()

    # Failure case
    mock_client2 = MagicMock()
    mock_client2.ping.side_effect = Exception("refused")

    with patch("spindle_core.api.health.redis_lib.Redis.from_url", return_value=mock_client2):
        result2 = health_mod._check_redis(settings)

    assert result2 is False
    mock_client2.close.assert_called_once()


# ---------------------------------------------------------------------------
# Test 9 — response always contains all 4 keys even when multiple fail
# ---------------------------------------------------------------------------

def test_health_response_always_has_all_four_keys():
    settings = _make_settings()
    app = create_app(settings)
    # No thread attrs → both thread checks fail
    with (
        patch("spindle_core.api.health._check_postgres", return_value=False),
        patch("spindle_core.api.health._check_redis", return_value=False),
    ):
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/health")

    body = resp.json()
    assert set(body["checks"].keys()) == {"postgres", "redis", "outbox_relay", "command_router"}


# ---------------------------------------------------------------------------
# Test 10 — lifespan exposes relay_thread / router_thread on app.state
# ---------------------------------------------------------------------------

def test_lifespan_exposes_threads_on_app_state():
    """During lifespan both thread attrs exist on app.state and are alive; dead after."""
    from fastapi import FastAPI
    from spindle_core.api.lifespan import make_lifespan

    settings = _make_settings()

    fake_redis = MagicMock()
    fake_redis.xreadgroup.return_value = []

    async def _run():
        with (
            patch("spindle_core.api.lifespan.create_db_engine", return_value=MagicMock()),
            patch("spindle_core.api.lifespan.make_session_factory", return_value=MagicMock()),
            patch("spindle_core.api.lifespan.redis_lib.Redis.from_url", return_value=fake_redis),
            patch("spindle_core.api.lifespan.load_tapestry_store", return_value=MagicMock()),
            patch("spindle_core.api.lifespan.TemplateStore.from_dir", return_value=MagicMock()),
            patch("spindle_core.api.lifespan.PgSagaRepository", return_value=MagicMock()),
            patch("spindle_core.api.lifespan.RedisCommandBus", return_value=MagicMock()),
            patch(
                "spindle_core.api.lifespan.OutboxRelay",
                return_value=MagicMock(run_forever=lambda stop: stop.wait()),
            ),
        ):
            app = FastAPI()
            async with make_lifespan(settings)(app):
                assert isinstance(app.state.relay_thread, threading.Thread)
                assert app.state.relay_thread.is_alive()
                assert app.state.relay_thread.name == "outbox-relay"

                assert isinstance(app.state.router_thread, threading.Thread)
                assert app.state.router_thread.is_alive()
                assert app.state.router_thread.name == "command-router"

                relay = app.state.relay_thread
                router = app.state.router_thread

            assert not relay.is_alive()
            assert not router.is_alive()

    asyncio.run(_run())

