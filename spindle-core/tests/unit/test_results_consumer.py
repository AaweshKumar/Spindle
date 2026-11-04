"""
Unit tests for spindle_core.results_consumer (run() and main()).

No real Redis or Postgres — MagicMock throughout.
Style mirrors test_router.py.
"""

from __future__ import annotations

import signal
import threading
from unittest.mock import MagicMock, patch, call

import pytest

from spindle_core.streams.keys import RESULTS_GROUP_NAME, RESULTS_STREAM_KEY


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeRunner:
    """Minimal stand-in for Runner."""

    def __init__(self, side_effect=None):
        self.calls: list[tuple[str, object]] = []
        self._side_effect = side_effect

    def handle_event(self, saga_id: str, event) -> None:
        self.calls.append((saga_id, event))
        if self._side_effect is not None:
            raise self._side_effect


# ---------------------------------------------------------------------------
# run() — consume_loop is called with correct stream, group, stop
# ---------------------------------------------------------------------------


def test_run_calls_consume_loop_with_correct_stream_and_group():
    """run() must pass stream=RESULTS_STREAM_KEY and group=RESULTS_GROUP_NAME explicitly."""
    from spindle_core import results_consumer

    r = MagicMock()
    runner = FakeRunner()
    stop = threading.Event()
    stop.set()

    with patch.object(results_consumer, "consume_loop") as mock_loop:
        results_consumer.run(stop, redis_client=r, runner=runner, consumer_name="test-c")

    mock_loop.assert_called_once()
    kwargs = mock_loop.call_args
    # Positional: (redis_client, consumer_name, handler)
    # Keyword: stream=..., group=..., stop=...
    # Accept both positional and keyword args
    all_kwargs = kwargs.kwargs
    # stream and group must NOT be the consume_loop defaults
    assert all_kwargs.get("stream") == RESULTS_STREAM_KEY
    assert all_kwargs.get("group") == RESULTS_GROUP_NAME
    assert all_kwargs.get("stream") != "spindle:commands"
    assert all_kwargs.get("group") != "spindle-workers"


def test_run_passes_stop_event_to_consume_loop():
    from spindle_core import results_consumer

    r = MagicMock()
    runner = FakeRunner()
    stop = threading.Event()
    stop.set()

    with patch.object(results_consumer, "consume_loop") as mock_loop:
        results_consumer.run(stop, redis_client=r, runner=runner, consumer_name="test-c")

    assert mock_loop.call_args.kwargs.get("stop") is stop


def test_run_passes_callable_handler_to_consume_loop():
    from spindle_core import results_consumer

    r = MagicMock()
    runner = FakeRunner()
    stop = threading.Event()
    stop.set()

    with patch.object(results_consumer, "consume_loop") as mock_loop:
        results_consumer.run(stop, redis_client=r, runner=runner, consumer_name="test-c")

    # Third positional arg is the handler
    handler = mock_loop.call_args.args[2]
    assert callable(handler)


# ---------------------------------------------------------------------------
# End-to-end through handler (consume_loop captured, invoked directly)
# ---------------------------------------------------------------------------


def test_step_succeeded_reaches_runner():
    """A StepSucceeded wire dict delivered to the handler calls runner.handle_event."""
    from spindle_core import results_consumer
    from spindle_core.engine import StepSucceeded

    r = MagicMock()
    runner = FakeRunner()
    stop = threading.Event()
    stop.set()

    captured_handler = None

    def _fake_loop(redis_client, consumer_name, handler, **kwargs):
        nonlocal captured_handler
        captured_handler = handler

    with patch.object(results_consumer, "consume_loop", side_effect=_fake_loop):
        results_consumer.run(stop, redis_client=r, runner=runner, consumer_name="test-c")

    assert captured_handler is not None
    captured_handler(
        "msg-1",
        {"type": "StepSucceeded", "saga_id": "saga-A", "step_id": "reserve"},
    )
    assert len(runner.calls) == 1
    assert isinstance(runner.calls[0][1], StepSucceeded)


def test_unparseable_message_goes_to_dead_results_stream():
    """An unparseable dict (bad type) must end up XADDed to spindle:dead:results."""
    from spindle_core import results_consumer

    r = MagicMock()
    runner = FakeRunner()
    stop = threading.Event()
    stop.set()

    captured_handler = None

    def _fake_loop(redis_client, consumer_name, handler, **kwargs):
        nonlocal captured_handler
        captured_handler = handler

    with patch.object(results_consumer, "consume_loop", side_effect=_fake_loop):
        results_consumer.run(stop, redis_client=r, runner=runner, consumer_name="test-c")

    assert captured_handler is not None
    captured_handler("msg-2", {"type": "GARBAGE", "saga_id": "s1", "step_id": "x"})

    r.xadd.assert_called_once()
    stream = r.xadd.call_args.args[0]
    assert stream == "spindle:dead:results"


# ---------------------------------------------------------------------------
# run() returns promptly when stop is pre-set (real consume_loop, MagicMock redis)
# ---------------------------------------------------------------------------


def test_run_returns_promptly_when_stop_is_set():
    """With stop pre-set, run() must return within 3 seconds."""
    from spindle_core.results_consumer import run

    r = MagicMock()
    r.xreadgroup.return_value = []
    r.xautoclaim.return_value = ["0-0", []]

    runner = FakeRunner()
    stop = threading.Event()
    stop.set()

    done = threading.Event()
    exc_holder: list[BaseException] = []

    def _target():
        try:
            run(stop, redis_client=r, runner=runner, consumer_name="test-c", )
        except Exception as e:
            exc_holder.append(e)
        finally:
            done.set()

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    finished = done.wait(timeout=3.0)
    assert finished, "run() did not return within 3s when stop was pre-set"
    assert not exc_holder, f"run() raised: {exc_holder[0]}"


# ---------------------------------------------------------------------------
# main() tests
# ---------------------------------------------------------------------------


def _make_main_patches(fake_redis=None, run_side_effect=None):
    """Return a list of context managers to patch main() dependencies."""
    if fake_redis is None:
        fake_redis = MagicMock()

    fake_engine = MagicMock()
    fake_session_factory = MagicMock()
    fake_runner = MagicMock()
    fake_repo = MagicMock()
    fake_settings = MagicMock()
    fake_settings.DATABASE_URL.get_secret_value.return_value = "postgresql://fake/db"
    fake_settings.REDIS_URL.get_secret_value.return_value = "redis://fake:6379/0"

    patches = [
        patch("spindle_core.results_consumer.get_settings", return_value=fake_settings),
        patch("spindle_core.results_consumer.create_db_engine", return_value=fake_engine),
        patch("spindle_core.results_consumer.make_session_factory", return_value=fake_session_factory),
        patch("spindle_core.results_consumer.PgSagaRepository", return_value=fake_repo),
        patch("spindle_core.results_consumer.Runner", return_value=fake_runner),
        patch("spindle_core.results_consumer.redis.Redis.from_url", return_value=fake_redis),
        patch("spindle_core.results_consumer.run", side_effect=run_side_effect),
        patch("spindle_core.results_consumer.signal.signal"),
    ]
    return patches, fake_redis, fake_engine


def test_main_returns_0_on_success():
    patches, _, _ = _make_main_patches()
    from spindle_core.results_consumer import main

    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
        result = main()
    assert result == 0


def test_main_registers_sigterm_and_sigint():
    registered_signals = []

    def _fake_signal(signum, handler):
        registered_signals.append(signum)

    patches, _, _ = _make_main_patches()
    from spindle_core.results_consumer import main

    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
        with patch("spindle_core.results_consumer.signal.signal", side_effect=_fake_signal):
            main()

    assert signal.SIGTERM in registered_signals
    assert signal.SIGINT in registered_signals


def test_main_signal_handler_sets_stop():
    """A captured signal handler must call stop.set()."""
    captured_handler = {}

    def _fake_signal(signum, handler):
        captured_handler[signum] = handler

    patches, _, _ = _make_main_patches()
    from spindle_core.results_consumer import main

    # We need run() to capture the stop event and not proceed — make it do nothing
    captured_stop: list[threading.Event] = []

    def _fake_run(stop, **kwargs):
        captured_stop.append(stop)

    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        with patch("spindle_core.results_consumer.run", side_effect=_fake_run):
            with patch("spindle_core.results_consumer.signal.signal", side_effect=_fake_signal):
                main()

    assert signal.SIGTERM in captured_handler
    assert len(captured_stop) == 1
    stop = captured_stop[0]
    assert not stop.is_set()
    # Fire the SIGTERM handler
    captured_handler[signal.SIGTERM](signal.SIGTERM, None)
    assert stop.is_set()


def test_main_consumer_name_starts_with_results():
    captured_names: list[str] = []

    def _fake_run(stop, *, redis_client, runner, consumer_name):
        captured_names.append(consumer_name)

    patches, _, _ = _make_main_patches()
    from spindle_core.results_consumer import main

    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[7]:
        with patch("spindle_core.results_consumer.run", side_effect=_fake_run):
            main()

    assert len(captured_names) == 1
    assert captured_names[0].startswith("results:")


def test_main_redis_from_url_called_with_decode_responses_true():
    from spindle_core.results_consumer import main

    with patch("spindle_core.results_consumer.get_settings") as mock_settings, \
         patch("spindle_core.results_consumer.create_db_engine", return_value=MagicMock()), \
         patch("spindle_core.results_consumer.make_session_factory", return_value=MagicMock()), \
         patch("spindle_core.results_consumer.PgSagaRepository", return_value=MagicMock()), \
         patch("spindle_core.results_consumer.Runner", return_value=MagicMock()), \
         patch("spindle_core.results_consumer.redis.Redis.from_url", return_value=MagicMock()) as mock_from_url, \
         patch("spindle_core.results_consumer.run"), \
         patch("spindle_core.results_consumer.signal.signal"):
        fake_settings = MagicMock()
        fake_settings.DATABASE_URL.get_secret_value.return_value = "postgresql://fake/db"
        fake_settings.REDIS_URL.get_secret_value.return_value = "redis://fake:6379/0"
        mock_settings.return_value = fake_settings
        main()

    mock_from_url.assert_called_once()
    assert mock_from_url.call_args.kwargs.get("decode_responses") is True


def test_main_redis_close_and_engine_dispose_called_on_success():
    fake_redis = MagicMock()
    fake_engine = MagicMock()
    patches, _, _ = _make_main_patches(fake_redis=fake_redis)
    from spindle_core.results_consumer import main

    with patches[0], \
         patch("spindle_core.results_consumer.create_db_engine", return_value=fake_engine), \
         patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
        main()

    fake_redis.close.assert_called_once()
    fake_engine.dispose.assert_called_once()


def test_main_redis_close_and_engine_dispose_called_when_run_raises():
    fake_redis = MagicMock()
    fake_engine = MagicMock()
    patches, _, _ = _make_main_patches(fake_redis=fake_redis, run_side_effect=RuntimeError("boom"))
    from spindle_core.results_consumer import main

    with patches[0], \
         patch("spindle_core.results_consumer.create_db_engine", return_value=fake_engine), \
         patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
        result = main()

    assert result == 1
    fake_redis.close.assert_called_once()
    fake_engine.dispose.assert_called_once()

