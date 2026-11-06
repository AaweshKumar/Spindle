"""
Unit tests for spindle_core/worker_process.py.

All external dependencies are mocked:
  - WorkerSettings is patched so no real env vars are needed.
  - redis.Redis.from_url is patched.
  - load_tapestry_store is patched.
  - run_connector is patched.
  - signal.signal is patched.
"""

from __future__ import annotations

import signal
import threading
from pathlib import Path
from unittest.mock import ANY, MagicMock, call, patch

import pytest

import spindle_core.worker_process as m
from spindle_core.worker_process import WorkerSettings, main


STEP_TYPE = "ReserveInventory"


# ---------------------------------------------------------------------------
# WorkerSettings: basic construction
# ---------------------------------------------------------------------------

class TestWorkerSettings:
    def test_works_with_only_redis_url(self, monkeypatch):
        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
        # Must NOT raise even without DATABASE_URL, JWT_SECRET, etc.
        s = WorkerSettings()
        assert s.REDIS_URL.get_secret_value() == "redis://localhost:6379/0"
        assert s.TAPESTRY_PATH == Path("tapestry.yaml")
        assert s.STEP_TYPE is None
        assert s.HEARTBEAT_INTERVAL_SECONDS == 15.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tapestry_mock(has_step: bool = True):
    t = MagicMock()
    t.get.return_value = MagicMock() if has_step else None
    return t


def _make_settings(step_type: str | None = STEP_TYPE) -> WorkerSettings:
    return WorkerSettings.model_construct(
        REDIS_URL=MagicMock(get_secret_value=lambda: "redis://localhost:6379/0"),
        TAPESTRY_PATH=Path("tapestry.yaml"),
        STEP_TYPE=step_type,
        HEARTBEAT_INTERVAL_SECONDS=15.0,
    )


def _base_patches(settings=None, tapestry=None):
    """Return a dict of patch targets for the main() function."""
    if settings is None:
        settings = _make_settings()
    if tapestry is None:
        tapestry = _make_tapestry_mock(has_step=True)
    return {
        "spindle_core.worker_process.WorkerSettings": MagicMock(return_value=settings),
        "spindle_core.worker_process.load_tapestry_store": MagicMock(return_value=tapestry),
        "spindle_core.worker_process.redis.Redis.from_url": MagicMock(),
        "spindle_core.worker_process.run_connector": MagicMock(),
        "spindle_core.worker_process.signal.signal": MagicMock(),
    }


# ---------------------------------------------------------------------------
# No step type => exit 2, run_connector not called
# ---------------------------------------------------------------------------

class TestMainNoStepType:
    def test_no_step_type_returns_2(self):
        with patch("spindle_core.worker_process.WorkerSettings",
                   return_value=_make_settings(step_type=None)), \
             patch("spindle_core.worker_process.load_tapestry_store",
                   return_value=_make_tapestry_mock()), \
             patch("spindle_core.worker_process.run_connector") as mock_rc, \
             patch("spindle_core.worker_process.signal.signal"):
            result = main(argv=[])
        assert result == 2
        mock_rc.assert_not_called()

    def test_empty_step_type_returns_2(self):
        with patch("spindle_core.worker_process.WorkerSettings",
                   return_value=_make_settings(step_type="")), \
             patch("spindle_core.worker_process.load_tapestry_store",
                   return_value=_make_tapestry_mock()), \
             patch("spindle_core.worker_process.run_connector") as mock_rc, \
             patch("spindle_core.worker_process.signal.signal"):
            result = main(argv=[])
        assert result == 2
        mock_rc.assert_not_called()


# ---------------------------------------------------------------------------
# CLI --step-type overrides STEP_TYPE env var
# ---------------------------------------------------------------------------

class TestMainStepTypePriority:
    def test_cli_step_type_overrides_env(self):
        with patch("spindle_core.worker_process.WorkerSettings",
                   return_value=_make_settings(step_type="FromEnv")), \
             patch("spindle_core.worker_process.load_tapestry_store",
                   return_value=_make_tapestry_mock()) as mock_lt, \
             patch("spindle_core.worker_process.run_connector") as mock_rc, \
             patch("spindle_core.worker_process.redis.Redis.from_url",
                   return_value=MagicMock()), \
             patch("spindle_core.worker_process.signal.signal"):
            result = main(argv=["--step-type", "FromCLI"])

        # run_connector should have been called with the CLI step type
        assert mock_rc.called
        call_kwargs = mock_rc.call_args
        assert call_kwargs.args[2] == "FromCLI"

    def test_env_step_type_used_when_no_cli(self):
        with patch("spindle_core.worker_process.WorkerSettings",
                   return_value=_make_settings(step_type="FromEnv")), \
             patch("spindle_core.worker_process.load_tapestry_store",
                   return_value=_make_tapestry_mock()), \
             patch("spindle_core.worker_process.run_connector") as mock_rc, \
             patch("spindle_core.worker_process.redis.Redis.from_url",
                   return_value=MagicMock()), \
             patch("spindle_core.worker_process.signal.signal"):
            result = main(argv=[])

        assert mock_rc.called
        assert mock_rc.call_args.args[2] == "FromEnv"


# ---------------------------------------------------------------------------
# Unknown step type => exit 2, run_connector not called
# ---------------------------------------------------------------------------

class TestMainUnknownStepType:
    def test_unknown_step_type_returns_2(self):
        with patch("spindle_core.worker_process.WorkerSettings",
                   return_value=_make_settings(step_type=STEP_TYPE)), \
             patch("spindle_core.worker_process.load_tapestry_store",
                   return_value=_make_tapestry_mock(has_step=False)), \
             patch("spindle_core.worker_process.run_connector") as mock_rc, \
             patch("spindle_core.worker_process.redis.Redis.from_url",
                   return_value=MagicMock()), \
             patch("spindle_core.worker_process.signal.signal"):
            result = main(argv=[])

        assert result == 2
        mock_rc.assert_not_called()


# ---------------------------------------------------------------------------
# Success => return 0, correct run_connector args
# ---------------------------------------------------------------------------

class TestMainSuccess:
    def _run_success(self):
        mock_redis = MagicMock()
        mock_rc = MagicMock()
        mock_sig = MagicMock()
        settings = _make_settings(step_type=STEP_TYPE)

        with patch("spindle_core.worker_process.WorkerSettings",
                   return_value=settings), \
             patch("spindle_core.worker_process.load_tapestry_store",
                   return_value=_make_tapestry_mock()), \
             patch("spindle_core.worker_process.run_connector", mock_rc), \
             patch("spindle_core.worker_process.redis.Redis.from_url",
                   return_value=mock_redis), \
             patch("spindle_core.worker_process.signal.signal", mock_sig):
            result = main(argv=[])

        return result, mock_rc, mock_redis, mock_sig

    def test_returns_0(self):
        result, *_ = self._run_success()
        assert result == 0

    def test_run_connector_called_with_step_type(self):
        _, mock_rc, _, _ = self._run_success()
        assert mock_rc.call_args.args[2] == STEP_TYPE

    def test_consumer_name_starts_with_worker(self):
        _, mock_rc, _, _ = self._run_success()
        consumer_name = mock_rc.call_args.args[3]
        assert consumer_name.startswith("worker:")

    def test_worker_id_is_step_type_colon_consumer_name(self):
        _, mock_rc, _, _ = self._run_success()
        step_type = mock_rc.call_args.args[2]
        consumer_name = mock_rc.call_args.args[3]
        worker_id = mock_rc.call_args.args[4]
        assert worker_id == f"{step_type}:{consumer_name}"

    def test_heartbeat_interval_from_settings(self):
        _, mock_rc, _, _ = self._run_success()
        interval = mock_rc.call_args.kwargs.get("heartbeat_interval_seconds")
        assert interval == 15.0

    def test_stop_event_passed(self):
        _, mock_rc, _, _ = self._run_success()
        stop_arg = mock_rc.call_args.kwargs.get("stop")
        assert isinstance(stop_arg, threading.Event)

    def test_redis_from_url_decode_responses_true(self):
        with patch("spindle_core.worker_process.WorkerSettings",
                   return_value=_make_settings()), \
             patch("spindle_core.worker_process.load_tapestry_store",
                   return_value=_make_tapestry_mock()), \
             patch("spindle_core.worker_process.run_connector"), \
             patch("spindle_core.worker_process.redis.Redis.from_url") as mock_from_url, \
             patch("spindle_core.worker_process.signal.signal"):
            mock_from_url.return_value = MagicMock()
            main(argv=[])

        _, call_kwargs = mock_from_url.call_args_list[0].args, mock_from_url.call_args.kwargs
        assert call_kwargs.get("decode_responses") is True

    def test_redis_close_called_on_success(self):
        _, _, mock_redis, _ = self._run_success()
        mock_redis.close.assert_called_once()


# ---------------------------------------------------------------------------
# SIGTERM / SIGINT registration
# ---------------------------------------------------------------------------

class TestMainSignalHandlers:
    def test_sigterm_registered(self):
        mock_sig = MagicMock()
        with patch("spindle_core.worker_process.WorkerSettings",
                   return_value=_make_settings()), \
             patch("spindle_core.worker_process.load_tapestry_store",
                   return_value=_make_tapestry_mock()), \
             patch("spindle_core.worker_process.run_connector"), \
             patch("spindle_core.worker_process.redis.Redis.from_url",
                   return_value=MagicMock()), \
             patch("spindle_core.worker_process.signal.signal", mock_sig):
            main(argv=[])

        registered = {c.args[0] for c in mock_sig.call_args_list}
        assert signal.SIGTERM in registered

    def test_sigint_registered(self):
        mock_sig = MagicMock()
        with patch("spindle_core.worker_process.WorkerSettings",
                   return_value=_make_settings()), \
             patch("spindle_core.worker_process.load_tapestry_store",
                   return_value=_make_tapestry_mock()), \
             patch("spindle_core.worker_process.run_connector"), \
             patch("spindle_core.worker_process.redis.Redis.from_url",
                   return_value=MagicMock()), \
             patch("spindle_core.worker_process.signal.signal", mock_sig):
            main(argv=[])

        registered = {c.args[0] for c in mock_sig.call_args_list}
        assert signal.SIGINT in registered

    def test_signal_handler_sets_stop_event(self):
        """The captured signal handler must set the stop Event passed to run_connector."""
        captured_handler = {}
        captured_stop = {}

        def fake_signal(signum, handler):
            captured_handler[signum] = handler

        def fake_run_connector(*args, **kwargs):
            captured_stop["ev"] = kwargs.get("stop")

        with patch("spindle_core.worker_process.WorkerSettings",
                   return_value=_make_settings()), \
             patch("spindle_core.worker_process.load_tapestry_store",
                   return_value=_make_tapestry_mock()), \
             patch("spindle_core.worker_process.run_connector", side_effect=fake_run_connector), \
             patch("spindle_core.worker_process.redis.Redis.from_url",
                   return_value=MagicMock()), \
             patch("spindle_core.worker_process.signal.signal", side_effect=fake_signal):
            main(argv=[])

        stop_ev = captured_stop.get("ev")
        assert stop_ev is not None
        handler = captured_handler.get(signal.SIGTERM)
        assert handler is not None
        assert not stop_ev.is_set()
        handler(signal.SIGTERM, None)
        assert stop_ev.is_set()


# ---------------------------------------------------------------------------
# run_connector raising => exit 1, redis.close still called
# ---------------------------------------------------------------------------

class TestMainRunConnectorRaises:
    def test_exception_returns_1(self):
        mock_redis = MagicMock()
        with patch("spindle_core.worker_process.WorkerSettings",
                   return_value=_make_settings()), \
             patch("spindle_core.worker_process.load_tapestry_store",
                   return_value=_make_tapestry_mock()), \
             patch("spindle_core.worker_process.run_connector",
                   side_effect=RuntimeError("boom")), \
             patch("spindle_core.worker_process.redis.Redis.from_url",
                   return_value=mock_redis), \
             patch("spindle_core.worker_process.signal.signal"):
            result = main(argv=[])

        assert result == 1

    def test_redis_close_called_even_on_exception(self):
        mock_redis = MagicMock()
        with patch("spindle_core.worker_process.WorkerSettings",
                   return_value=_make_settings()), \
             patch("spindle_core.worker_process.load_tapestry_store",
                   return_value=_make_tapestry_mock()), \
             patch("spindle_core.worker_process.run_connector",
                   side_effect=RuntimeError("boom")), \
             patch("spindle_core.worker_process.redis.Redis.from_url",
                   return_value=mock_redis), \
             patch("spindle_core.worker_process.signal.signal"):
            main(argv=[])

        mock_redis.close.assert_called_once()
