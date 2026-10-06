"""
Unit tests for workers/registry.py (LivenessRegistry).

All Redis interactions are mocked — no real Redis required.
"""

from unittest.mock import MagicMock, call

import pytest

from spindle_core.workers.registry import (
    DEFAULT_TTL_SECONDS,
    LivenessRegistry,
    _KEY_PREFIX,
)


def _make_registry(ttl: int = DEFAULT_TTL_SECONDS) -> tuple[LivenessRegistry, MagicMock]:
    r = MagicMock()
    return LivenessRegistry(r, ttl_seconds=ttl), r


# ---- heartbeat ----

def test_heartbeat_calls_redis_set():
    reg, r = _make_registry()
    reg.heartbeat("worker-1")
    r.set.assert_called_once()


def test_heartbeat_uses_correct_key():
    reg, r = _make_registry()
    reg.heartbeat("worker-1")
    key = r.set.call_args.args[0]
    assert key == f"{_KEY_PREFIX}worker-1"


def test_heartbeat_uses_default_ttl():
    reg, r = _make_registry()
    reg.heartbeat("worker-1")
    ex = r.set.call_args.kwargs.get("ex")
    assert ex == DEFAULT_TTL_SECONDS


def test_heartbeat_uses_custom_ttl():
    reg, r = _make_registry(ttl=60)
    reg.heartbeat("worker-1")
    ex = r.set.call_args.kwargs.get("ex")
    assert ex == 60


def test_heartbeat_value_is_float_string():
    reg, r = _make_registry()
    reg.heartbeat("worker-1")
    value_str = r.set.call_args.args[1]
    # Must be parseable as a float (POSIX timestamp)
    float(value_str)


def test_heartbeat_different_worker_ids_produce_different_keys():
    reg, r = _make_registry()
    reg.heartbeat("alpha")
    reg.heartbeat("beta")
    keys = [c.args[0] for c in r.set.call_args_list]
    assert len(set(keys)) == 2
    assert f"{_KEY_PREFIX}alpha" in keys
    assert f"{_KEY_PREFIX}beta" in keys


# ---- is_alive ----

def test_is_alive_true_when_key_exists():
    reg, r = _make_registry()
    r.exists.return_value = 1
    assert reg.is_alive("worker-1") is True
    r.exists.assert_called_once_with(f"{_KEY_PREFIX}worker-1")


def test_is_alive_false_when_key_missing():
    reg, r = _make_registry()
    r.exists.return_value = 0
    assert reg.is_alive("worker-1") is False


def test_is_alive_false_when_exists_returns_zero():
    """Redis exists() returns the count of keys that exist (0 or 1 here)."""
    reg, r = _make_registry()
    r.exists.return_value = 0
    assert reg.is_alive("w") is False


# ---- last_seen ----

def test_last_seen_returns_float_from_bytes():
    reg, r = _make_registry()
    r.get.return_value = b"1700000000.123"
    result = reg.last_seen("worker-1")
    assert result == pytest.approx(1700000000.123)
    r.get.assert_called_once_with(f"{_KEY_PREFIX}worker-1")


def test_last_seen_returns_float_from_string():
    reg, r = _make_registry()
    r.get.return_value = "1700000001.0"
    result = reg.last_seen("worker-1")
    assert result == pytest.approx(1700000001.0)


def test_last_seen_returns_none_when_key_expired():
    reg, r = _make_registry()
    r.get.return_value = None
    assert reg.last_seen("worker-1") is None


# ---- no upward imports ----

def test_registry_has_no_connectors_import():
    """Verify workers/registry.py never imports from connectors or tapestry."""
    import spindle_core.workers.registry as mod
    import inspect
    src = inspect.getsource(mod)
    assert "connectors" not in src
    assert "tapestry" not in src
