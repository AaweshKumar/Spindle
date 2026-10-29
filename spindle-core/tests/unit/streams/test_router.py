"""
Unit tests for streams/router.py (CommandRouter).

Redis is replaced with a MagicMock — no live services required.
The test double pattern mirrors test_consumer.py and test_results.py.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, call

import pytest

from spindle_core.streams.keys import (
    DEAD_COMMANDS_STREAM_KEY,
    RESULTS_STREAM_KEY,
    ROUTER_GROUP_NAME,
    WORK_STREAM_PREFIX,
)
from spindle_core.streams.router import CommandRouter
from spindle_core.tapestry.loader import TapestryStore
from spindle_core.tapestry.models import RequestConfig, StepConfig

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

_KNOWN_STEP_TYPE = "ReserveInventory"


def _make_tapestry(step_types: list[str] | None = None) -> TapestryStore:
    """Return a TapestryStore populated with the given step types (or one default)."""
    types = step_types if step_types is not None else [_KNOWN_STEP_TYPE]
    configs = [
        StepConfig(step_type=st, execute=RequestConfig(url=f"http://svc/{st}"))
        for st in types
    ]
    return TapestryStore(configs)


def _make_router(
    tapestry: TapestryStore | None = None,
) -> tuple[CommandRouter, MagicMock]:
    """Return (router, fake_redis)."""
    r = MagicMock()
    store = tapestry if tapestry is not None else _make_tapestry()
    router = CommandRouter(
        redis_client=r,
        tapestry=store,
        consumer_name="test-router",
    )
    return router, r


# Canonical wire-format messages produced by _serialize_command / RedisCommandBus.
_DISPATCH_STEP = {
    "type": "DispatchStep",
    "saga_id": "saga-1",
    "step_id": "reserve",
    "step_type": _KNOWN_STEP_TYPE,
    "idempotency_key": "saga-1:reserve:execute",
}

_DISPATCH_COMP = {
    "type": "DispatchCompensation",
    "saga_id": "saga-1",
    "step_id": "reserve",
    "step_type": _KNOWN_STEP_TYPE,
    "idempotency_key": "saga-1:reserve:compensate",
}


def _last_xadd_stream(r: MagicMock) -> str:
    """Stream name passed to the most recent r.xadd() call."""
    return r.xadd.call_args.args[0]


def _last_xadd_fields(r: MagicMock) -> dict[str, str]:
    """Fields dict passed to the most recent r.xadd() call."""
    return r.xadd.call_args.args[1]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_router_group_name_value():
    assert ROUTER_GROUP_NAME == "spindle-router"


def test_work_stream_prefix_value():
    assert WORK_STREAM_PREFIX == "spindle:work:"


def test_dead_commands_stream_key_value():
    assert DEAD_COMMANDS_STREAM_KEY == "spindle:dead:commands"


# ---------------------------------------------------------------------------
# Behaviour 2 — valid DispatchStep routed to work stream
# ---------------------------------------------------------------------------


def test_dispatch_step_routes_to_work_stream():
    router, r = _make_router()
    router.route_once("msg-1", _DISPATCH_STEP)
    assert _last_xadd_stream(r) == f"{WORK_STREAM_PREFIX}{_KNOWN_STEP_TYPE}"


def test_dispatch_step_fields_forwarded_unchanged():
    router, r = _make_router()
    router.route_once("msg-1", _DISPATCH_STEP)
    assert _last_xadd_fields(r) == _DISPATCH_STEP


def test_dispatch_compensation_routes_to_work_stream():
    router, r = _make_router()
    router.route_once("msg-1", _DISPATCH_COMP)
    assert _last_xadd_stream(r) == f"{WORK_STREAM_PREFIX}{_KNOWN_STEP_TYPE}"


def test_dispatch_compensation_fields_forwarded_unchanged():
    router, r = _make_router()
    router.route_once("msg-1", _DISPATCH_COMP)
    assert _last_xadd_fields(r) == _DISPATCH_COMP


def test_work_stream_suffix_is_step_type():
    """The suffix of the work stream key must equal the step_type field."""
    router, r = _make_router()
    router.route_once("msg-1", _DISPATCH_STEP)
    stream = _last_xadd_stream(r)
    assert stream.endswith(_KNOWN_STEP_TYPE)


def test_different_step_types_route_to_different_streams():
    types = ["Alpha", "Beta"]
    tapestry = _make_tapestry(types)
    router, r = _make_router(tapestry)

    msg_alpha = {**_DISPATCH_STEP, "step_type": "Alpha"}
    msg_beta = {**_DISPATCH_STEP, "step_type": "Beta"}

    router.route_once("msg-1", msg_alpha)
    router.route_once("msg-2", msg_beta)

    calls = r.xadd.call_args_list
    assert calls[0].args[0] == f"{WORK_STREAM_PREFIX}Alpha"
    assert calls[1].args[0] == f"{WORK_STREAM_PREFIX}Beta"


# ---------------------------------------------------------------------------
# Behaviour 3 — parseable but step_type unknown → failure event on results
# ---------------------------------------------------------------------------


def test_unknown_step_type_dispatch_step_emits_step_failed():
    router, r = _make_router()
    bad = {**_DISPATCH_STEP, "step_type": "NonExistent"}
    router.route_once("msg-1", bad)
    assert _last_xadd_stream(r) == RESULTS_STREAM_KEY
    assert _last_xadd_fields(r)["type"] == "StepFailed"


def test_unknown_step_type_dispatch_compensation_emits_compensation_failed():
    router, r = _make_router()
    bad = {**_DISPATCH_COMP, "step_type": "NonExistent"}
    router.route_once("msg-1", bad)
    assert _last_xadd_stream(r) == RESULTS_STREAM_KEY
    assert _last_xadd_fields(r)["type"] == "CompensationFailed"


def test_unknown_step_type_failure_carries_saga_and_step_ids():
    router, r = _make_router()
    bad = {**_DISPATCH_STEP, "step_type": "Ghost"}
    router.route_once("msg-1", bad)
    fields = _last_xadd_fields(r)
    assert fields["saga_id"] == "saga-1"
    assert fields["step_id"] == "reserve"


def test_unknown_step_type_failure_names_step_type_in_reason():
    router, r = _make_router()
    bad = {**_DISPATCH_STEP, "step_type": "MissingStep"}
    router.route_once("msg-1", bad)
    reason = _last_xadd_fields(r)["reason"]
    assert "MissingStep" in reason


def test_empty_step_type_dispatch_step_emits_step_failed():
    router, r = _make_router()
    bad = {**_DISPATCH_STEP, "step_type": ""}
    router.route_once("msg-1", bad)
    assert _last_xadd_stream(r) == RESULTS_STREAM_KEY
    assert _last_xadd_fields(r)["type"] == "StepFailed"


def test_empty_step_type_dispatch_compensation_emits_compensation_failed():
    router, r = _make_router()
    bad = {**_DISPATCH_COMP, "step_type": ""}
    router.route_once("msg-1", bad)
    assert _last_xadd_stream(r) == RESULTS_STREAM_KEY
    assert _last_xadd_fields(r)["type"] == "CompensationFailed"


# ---------------------------------------------------------------------------
# Behaviour 4 — unparseable → dead-letter stream
# ---------------------------------------------------------------------------


def test_missing_type_field_goes_to_dead_letter():
    router, r = _make_router()
    router.route_once("msg-1", {"saga_id": "s1", "step_id": "x", "step_type": "T"})
    assert _last_xadd_stream(r) == DEAD_COMMANDS_STREAM_KEY


def test_unknown_type_goes_to_dead_letter():
    router, r = _make_router()
    router.route_once("msg-1", {"type": "StartSaga", "saga_id": "s1", "step_id": "x"})
    assert _last_xadd_stream(r) == DEAD_COMMANDS_STREAM_KEY


def test_empty_saga_id_goes_to_dead_letter():
    router, r = _make_router()
    bad = {**_DISPATCH_STEP, "saga_id": ""}
    router.route_once("msg-1", bad)
    assert _last_xadd_stream(r) == DEAD_COMMANDS_STREAM_KEY


def test_empty_step_id_goes_to_dead_letter():
    router, r = _make_router()
    bad = {**_DISPATCH_STEP, "step_id": ""}
    router.route_once("msg-1", bad)
    assert _last_xadd_stream(r) == DEAD_COMMANDS_STREAM_KEY


def test_dead_letter_includes_reason_field():
    router, r = _make_router()
    router.route_once("msg-1", {"type": "Junk"})
    fields = _last_xadd_fields(r)
    assert "reason" in fields


def test_dead_letter_preserves_original_fields():
    router, r = _make_router()
    original = {"type": "Junk", "extra_key": "val"}
    router.route_once("msg-1", original)
    fields = _last_xadd_fields(r)
    assert fields["type"] == "Junk"
    assert fields["extra_key"] == "val"


def test_unparseable_does_not_raise():
    """Behaviour 4: bad data must never propagate as an exception."""
    router, r = _make_router()
    # Completely empty message — should not raise
    router.route_once("msg-1", {})


# ---------------------------------------------------------------------------
# Behaviour 5 — Redis errors propagate (message must NOT be acked)
# ---------------------------------------------------------------------------


def test_redis_error_on_valid_command_propagates():
    """xadd raises on a valid command → exception must re-raise (no ack)."""
    router, r = _make_router()
    r.xadd.side_effect = redis_error()
    with pytest.raises(Exception, match="REDIS"):
        router.route_once("msg-1", _DISPATCH_STEP)


def test_redis_error_on_unknown_step_type_propagates():
    """xadd raises when publishing failure event → must re-raise."""
    router, r = _make_router()
    r.xadd.side_effect = redis_error()
    bad = {**_DISPATCH_STEP, "step_type": "Unknown"}
    with pytest.raises(Exception, match="REDIS"):
        router.route_once("msg-1", bad)


def test_redis_error_on_dead_letter_propagates():
    """xadd raises when publishing to dead-letter → must re-raise."""
    router, r = _make_router()
    r.xadd.side_effect = redis_error()
    with pytest.raises(Exception, match="REDIS"):
        router.route_once("msg-1", {"type": "Junk"})


def redis_error():
    """Return a ResponseError that matches 'REDIS' in its message."""
    import redis as _redis
    return _redis.ResponseError("REDIS SIMULATED ERROR")


# ---------------------------------------------------------------------------
# Behaviour — xadd called exactly once per message (no double-writes)
# ---------------------------------------------------------------------------


def test_valid_command_xadd_called_once():
    router, r = _make_router()
    router.route_once("msg-1", _DISPATCH_STEP)
    assert r.xadd.call_count == 1


def test_unknown_step_type_xadd_called_once():
    router, r = _make_router()
    bad = {**_DISPATCH_STEP, "step_type": "Nope"}
    router.route_once("msg-1", bad)
    assert r.xadd.call_count == 1


def test_dead_letter_xadd_called_once():
    router, r = _make_router()
    router.route_once("msg-1", {})
    assert r.xadd.call_count == 1


# ---------------------------------------------------------------------------
# Lifespan integration — thread starts and stops within join timeout
# ---------------------------------------------------------------------------


def test_router_thread_stops_when_run_raises_immediately():
    """
    Simulate the lifespan pattern: start a thread running _run_router_supervised,
    set the stop event, join within _ROUTER_JOIN_TIMEOUT.

    We make router.run() raise immediately so the supervised loop observes
    stop.is_set() before restarting and exits cleanly.
    """
    from spindle_core.api.lifespan import _run_router_supervised

    router, r = _make_router()
    # Make run() exit immediately (simulate stop already set before first restart)
    call_count = {"n": 0}

    def fake_run(stop=None):
        call_count["n"] += 1
        raise RuntimeError("simulated crash")

    router.run = fake_run  # type: ignore[method-assign]

    stop = threading.Event()
    t = threading.Thread(
        target=_run_router_supervised,
        args=(router, stop),
        daemon=True,
    )
    t.start()
    # Give the thread one iteration then stop it.
    stop.set()
    t.join(timeout=5.0)
    assert not t.is_alive(), "router thread did not stop within 5 s"


def test_run_router_supervised_exits_cleanly_when_stop_set_before_start():
    """_run_router_supervised exits without calling run() if stop is pre-set."""
    from spindle_core.api.lifespan import _run_router_supervised

    router, r = _make_router()
    called = {"n": 0}

    def fake_run(stop=None):
        called["n"] += 1

    router.run = fake_run  # type: ignore[method-assign]

    stop = threading.Event()
    stop.set()

    t = threading.Thread(
        target=_run_router_supervised, args=(router, stop), daemon=True
    )
    t.start()
    t.join(timeout=2.0)
    assert not t.is_alive()
    assert called["n"] == 0, "run() should not have been called"


# ---------------------------------------------------------------------------
# Stop-hook: consume_loop exits when stop is set
# ---------------------------------------------------------------------------


def test_consume_loop_exits_when_stop_is_set():
    """consume_loop returns within roughly one block interval when stop is set."""
    from spindle_core.streams.consumer import consume_loop

    r = MagicMock()
    r.xreadgroup.return_value = []
    stop = threading.Event()

    t = threading.Thread(
        target=consume_loop,
        kwargs=dict(
            r=r,
            consumer_name="test-consumer",
            handler=MagicMock(),
            block_ms=50,
            stop=stop,
        ),
        daemon=True,
    )
    t.start()
    assert t.is_alive()
    stop.set()
    t.join(timeout=1.0)
    assert not t.is_alive(), "consume_loop did not exit within 1s after stop.set()"


def test_consume_loop_stop_none_preserves_behaviour():
    """consume_loop with stop=None behaves as before."""
    from spindle_core.streams.consumer import consume_loop

    r = MagicMock()
    r.xreadgroup.side_effect = [
        [("spindle:commands", [("msg-1", {"type": "DispatchStep"})])],
        RuntimeError("stop loop"),
    ]

    handled = []

    def _handler(msg_id, fields):
        handled.append(msg_id)

    with pytest.raises(RuntimeError, match="stop loop"):
        consume_loop(r, "test-consumer", _handler)

    assert handled == ["msg-1"]
    r.xack.assert_called_once()


# ---------------------------------------------------------------------------
# Stop-hook: _run_router_supervised with real CommandRouter exits cleanly
# ---------------------------------------------------------------------------


def test_run_router_supervised_with_real_router_exits_on_stop(caplog):
    """_run_router_supervised with real CommandRouter exits on stop without restart or crash log."""
    import logging
    from spindle_core.api.lifespan import _run_router_supervised

    r = MagicMock()
    r.xreadgroup.return_value = []
    router, _ = _make_router()
    router._r = r

    stop = threading.Event()
    t = threading.Thread(
        target=_run_router_supervised,
        args=(router, stop),
        daemon=True,
        name="command-router",
    )
    with caplog.at_level(logging.ERROR):
        t.start()
        assert t.is_alive()
        stop.set()
        t.join(timeout=2.0)
        assert not t.is_alive(), "supervisor did not exit within 2s after stop.set()"

    assert "command-router crashed" not in caplog.text


# ---------------------------------------------------------------------------
# Lifespan integration: command-router thread alive during lifespan, stopped after
# ---------------------------------------------------------------------------


def test_lifespan_router_thread_alive_during_lifespan_and_stopped_after(caplog):
    """Lifespan test: thread 'command-router' is alive during lifespan, dead after, no warning."""
    import asyncio
    import logging
    from pathlib import Path
    from unittest.mock import patch
    from pydantic import SecretStr

    from spindle_core.config import Settings
    from spindle_core.api.lifespan import make_lifespan

    settings = Settings.model_construct(
        DATABASE_URL=SecretStr("postgresql://fake/db"),
        REDIS_URL=SecretStr("redis://fake:6379/0"),
        JWT_SECRET=SecretStr("x" * 32),
        ADMIN_USERNAME="admin",
        ADMIN_PASSWORD_HASH=SecretStr("$argon2id$fake"),
        SAGA_TEMPLATES_DIR=Path("/fake/templates"),
        TAPESTRY_PATH=Path("/fake/tapestry.yaml"),
    )

    fake_redis = MagicMock()
    fake_redis.xreadgroup.return_value = []
    fake_tapestry = MagicMock()

    async def _run():
        with (
            patch("spindle_core.api.lifespan.create_db_engine", return_value=MagicMock()),
            patch("spindle_core.api.lifespan.make_session_factory", return_value=MagicMock()),
            patch("spindle_core.api.lifespan.redis_lib.Redis.from_url", return_value=fake_redis),
            patch("spindle_core.api.lifespan.load_tapestry_store", return_value=fake_tapestry),
            patch("spindle_core.api.lifespan.TemplateStore.from_dir", return_value=MagicMock()),
            patch("spindle_core.api.lifespan.PgSagaRepository", return_value=MagicMock()),
            patch("spindle_core.api.lifespan.RedisCommandBus", return_value=MagicMock()),
            patch("spindle_core.api.lifespan.OutboxRelay", return_value=MagicMock(run_forever=lambda stop: stop.wait())),
        ):
            lifespan = make_lifespan(settings)
            app = MagicMock()
            app.state = MagicMock()

            async with lifespan(app):
                router_thread = next(
                    (t for t in threading.enumerate() if t.name == "command-router"),
                    None,
                )
                assert router_thread is not None, "command-router thread not found during lifespan"
                assert router_thread.is_alive(), "command-router thread not alive during lifespan"

            assert not router_thread.is_alive(), "command-router thread still alive after lifespan"

    with caplog.at_level(logging.WARNING, logger="spindle_core.api.lifespan"):
        asyncio.run(_run())

    assert "command-router thread did not stop" not in caplog.text


# ---------------------------------------------------------------------------
# Back-compat: WORK_STREAM_PREFIX still importable from connectors package
# ---------------------------------------------------------------------------


def test_work_stream_prefix_importable_from_connectors():
    from spindle_core.connectors import WORK_STREAM_PREFIX as wsp  # noqa: F401
    assert wsp == "spindle:work:"


def test_work_stream_prefix_importable_from_connectors_consumer():
    from spindle_core.connectors.consumer import WORK_STREAM_PREFIX as wsp  # noqa: F401
    assert wsp == "spindle:work:"


# ---------------------------------------------------------------------------
# Layering: streams/router.py must not import from connectors/
# ---------------------------------------------------------------------------


def test_router_module_does_not_import_connectors():
    import importlib
    import sys

    # Reload the router module in isolation and check its imports.
    mod = sys.modules.get("spindle_core.streams.router")
    assert mod is not None  # already imported by the test above
    # Walk the module's __dict__ for any reference to the connectors package.
    for name, obj in mod.__dict__.items():
        if hasattr(obj, "__module__") and obj.__module__ and "connectors" in obj.__module__:
            pytest.fail(
                f"streams/router.py imports from connectors/ via attribute {name!r}"
            )

