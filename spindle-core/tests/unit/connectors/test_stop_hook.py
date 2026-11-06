"""
Unit tests for the stop-event hook added in B1 and B2.

Covers:
  - ConnectorConsumer.run passes stop to consume_loop.
  - run_connector passes its stop arg to ConnectorConsumer, or creates one if None.
  - Setting stop makes the heartbeat thread exit and run_connector return.
  - Real ConnectorConsumer + real consume_loop terminates when stop is set.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, call, patch

import pytest

from spindle_core.connectors.consumer import WORK_GROUP_NAME, ConnectorConsumer
from spindle_core.connectors.runner import run_connector
from spindle_core.tapestry.loader import TapestryStore
from spindle_core.tapestry.models import RequestConfig, StepConfig, SuccessCheck


STEP_TYPE = "StopHookTest"
STREAM_KEY = f"spindle:work:{STEP_TYPE}"


# ---------------------------------------------------------------------------
# B1: ConnectorConsumer.run passes stop to consume_loop
# ---------------------------------------------------------------------------

class TestConnectorConsumerRunPassesStop:

    def _make_consumer(self, stop=None) -> ConnectorConsumer:
        r = MagicMock()
        step_config = StepConfig(
            step_type=STEP_TYPE,
            execute=RequestConfig(url="http://svc/act"),
            execute_success_check=SuccessCheck(),
        )
        store = TapestryStore([step_config])
        return ConnectorConsumer(
            redis_client=r,
            tapestry=store,
            step_type=STEP_TYPE,
            consumer_name="test",
            stop=stop,
        )

    def test_run_passes_explicit_stop_event_to_consume_loop(self):
        ev = threading.Event()
        consumer = self._make_consumer(stop=ev)
        with patch("spindle_core.connectors.consumer.consume_loop") as mock_cl:
            consumer.run()
        kwargs = mock_cl.call_args.kwargs
        assert kwargs.get("stop") is ev

    def test_run_passes_none_stop_when_not_given(self):
        consumer = self._make_consumer(stop=None)
        with patch("spindle_core.connectors.consumer.consume_loop") as mock_cl:
            consumer.run()
        kwargs = mock_cl.call_args.kwargs
        assert kwargs.get("stop") is None

    def test_run_passes_correct_stream(self):
        ev = threading.Event()
        consumer = self._make_consumer(stop=ev)
        with patch("spindle_core.connectors.consumer.consume_loop") as mock_cl:
            consumer.run()
        kwargs = mock_cl.call_args.kwargs
        assert kwargs.get("stream") == STREAM_KEY

    def test_run_passes_correct_group(self):
        ev = threading.Event()
        consumer = self._make_consumer(stop=ev)
        with patch("spindle_core.connectors.consumer.consume_loop") as mock_cl:
            consumer.run()
        kwargs = mock_cl.call_args.kwargs
        assert kwargs.get("group") == WORK_GROUP_NAME


# ---------------------------------------------------------------------------
# B2: run_connector passes stop to ConnectorConsumer
# ---------------------------------------------------------------------------

class TestRunConnectorStopPropagation:

    def _run_with_mocks(self, stop=None):
        """Call run_connector with ConnectorConsumer and LivenessRegistry patched."""
        with (
            patch("spindle_core.connectors.runner.LivenessRegistry") as mock_reg_cls,
            patch("spindle_core.connectors.runner.ConnectorConsumer") as mock_cc_cls,
        ):
            mock_reg = MagicMock()
            mock_reg_cls.return_value = mock_reg
            mock_consumer = MagicMock()
            mock_cc_cls.return_value = mock_consumer

            # Make heartbeat fire once quickly then consumer.run exits
            heartbeat_called = threading.Event()

            def fake_heartbeat(*args):
                heartbeat_called.set()

            mock_reg.heartbeat.side_effect = fake_heartbeat

            def fake_run():
                heartbeat_called.wait(timeout=1.0)

            mock_consumer.run.side_effect = fake_run

            kwargs = dict(
                redis_client=MagicMock(),
                tapestry=MagicMock(),
                step_type=STEP_TYPE,
                consumer_name="test-consumer",
                worker_id=f"{STEP_TYPE}:test-consumer",
                heartbeat_interval_seconds=10.0,
            )
            if stop is not None:
                kwargs["stop"] = stop

            run_connector(**kwargs)

            return mock_cc_cls, mock_consumer

    def test_explicit_stop_event_passed_to_consumer_constructor(self):
        ev = threading.Event()
        mock_cc_cls, _ = self._run_with_mocks(stop=ev)
        init_kwargs = mock_cc_cls.call_args.kwargs
        assert init_kwargs.get("stop") is ev

    def test_no_stop_given_creates_non_none_event_for_consumer(self):
        mock_cc_cls, _ = self._run_with_mocks(stop=None)
        init_kwargs = mock_cc_cls.call_args.kwargs
        # A new Event should have been created and passed
        passed_stop = init_kwargs.get("stop")
        assert passed_stop is not None
        assert isinstance(passed_stop, threading.Event)

    def test_consumer_run_called_with_no_args(self):
        _, mock_consumer = self._run_with_mocks(stop=None)
        mock_consumer.run.assert_called_once_with()


# ---------------------------------------------------------------------------
# Stop event terminates heartbeat and run_connector returns
# ---------------------------------------------------------------------------

class TestStopEventTerminatesLoop:

    def test_setting_stop_causes_run_connector_to_return_within_3s(self):
        """Consumer.run() waits on stop; setting it must unblock run_connector."""
        stop = threading.Event()

        with (
            patch("spindle_core.connectors.runner.LivenessRegistry") as mock_reg_cls,
            patch("spindle_core.connectors.runner.ConnectorConsumer") as mock_cc_cls,
        ):
            mock_reg = MagicMock()
            mock_reg_cls.return_value = mock_reg
            mock_consumer = MagicMock()
            mock_cc_cls.return_value = mock_consumer

            # fake consumer.run waits on the stop event (simulates consume_loop)
            def fake_run():
                stop.wait(timeout=5.0)

            mock_consumer.run.side_effect = fake_run

            finished = threading.Event()

            def _run():
                run_connector(
                    redis_client=MagicMock(),
                    tapestry=MagicMock(),
                    step_type=STEP_TYPE,
                    consumer_name="tc",
                    worker_id=f"{STEP_TYPE}:tc",
                    heartbeat_interval_seconds=60.0,
                    stop=stop,
                )
                finished.set()

            t = threading.Thread(target=_run, daemon=True)
            t.start()

            # Give it a moment to start
            time.sleep(0.05)
            stop.set()
            assert finished.wait(timeout=3.0), "run_connector did not return within 3 s after stop set"


# ---------------------------------------------------------------------------
# Real ConnectorConsumer + real consume_loop via MagicMock redis
# ---------------------------------------------------------------------------

class TestRealConsumerStopHook:

    def test_real_consumer_exits_when_stop_set(self):
        """Real ConnectorConsumer + real consume_loop; stop set from another thread."""
        import redis as redis_mod
        r = MagicMock(spec=redis_mod.Redis)
        r.xreadgroup.return_value = []
        # Must be a redis.ResponseError with "BUSYGROUP" so ensure_group swallows it
        busygroup_err = redis_mod.ResponseError("BUSYGROUP Consumer Group name already exists")
        r.xgroup_create.side_effect = busygroup_err

        step_config = StepConfig(
            step_type=STEP_TYPE,
            execute=RequestConfig(url="http://svc/act"),
            execute_success_check=SuccessCheck(),
        )
        store = TapestryStore([step_config])
        stop = threading.Event()

        consumer = ConnectorConsumer(
            redis_client=r,
            tapestry=store,
            step_type=STEP_TYPE,
            consumer_name="real-test",
            stop=stop,
        )

        finished = threading.Event()

        def _run():
            consumer.run()
            finished.set()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        time.sleep(0.05)
        stop.set()
        assert finished.wait(timeout=3.0), "real consumer.run did not exit within 3 s after stop set"
