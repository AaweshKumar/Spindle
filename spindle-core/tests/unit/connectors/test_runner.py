"""
Unit tests for connectors/runner.py.
"""

import threading
from unittest.mock import MagicMock, patch

from spindle_core.connectors.runner import run_connector


@patch("spindle_core.connectors.runner.LivenessRegistry")
@patch("spindle_core.connectors.runner.ConnectorConsumer")
def test_runner_heartbeat_first_before_wait(mock_consumer_cls, mock_registry_cls):
    mock_registry = MagicMock()
    mock_registry_cls.return_value = mock_registry
    mock_consumer = MagicMock()
    mock_consumer_cls.return_value = mock_consumer
    
    heartbeat_called = threading.Event()
    
    def fake_heartbeat(*args):
        heartbeat_called.set()
        
    mock_registry.heartbeat.side_effect = fake_heartbeat
    
    def fake_run():
        # Assert that the first heartbeat happens before a large interval elapses.
        # We wait for a very short time. If the loop was waiting first, this would timeout.
        assert heartbeat_called.wait(timeout=1.0) is True, "Heartbeat not called immediately"
        
    mock_consumer.run.side_effect = fake_run
    
    run_connector(
        redis_client=MagicMock(),
        tapestry=MagicMock(),
        step_type="Test",
        consumer_name="test-consumer",
        worker_id="worker-1",
        heartbeat_interval_seconds=10.0,
    )
    
    assert mock_registry.heartbeat.call_count == 1


@patch("spindle_core.connectors.runner.LivenessRegistry")
@patch("spindle_core.connectors.runner.ConnectorConsumer")
def test_runner_no_final_heartbeat_after_stop(mock_consumer_cls, mock_registry_cls):
    mock_registry = MagicMock()
    mock_registry_cls.return_value = mock_registry
    mock_consumer = MagicMock()
    mock_consumer_cls.return_value = mock_consumer
    
    heartbeat_called = threading.Event()
    
    def fake_heartbeat(*args):
        heartbeat_called.set()
        
    mock_registry.heartbeat.side_effect = fake_heartbeat
    
    def fake_run():
        # Let one heartbeat happen, then exit so `stop` gets set immediately
        heartbeat_called.wait(timeout=1.0)
        
    mock_consumer.run.side_effect = fake_run
    
    run_connector(
        redis_client=MagicMock(),
        tapestry=MagicMock(),
        step_type="Test",
        consumer_name="test-consumer",
        worker_id="worker-1",
        # Use large interval so the loop does not trigger a second heartbeat naturally
        heartbeat_interval_seconds=10.0,
    )
    
    # We should have exactly 1 heartbeat. If there was a final heartbeat block
    # outside the loop, the call_count would be 2.
    assert mock_registry.heartbeat.call_count == 1
