"""
Unit tests for connectors/consumer.py (ConnectorConsumer).
Both Redis and the HTTP client are mocked — no live services required.
"""

import json
from unittest.mock import ANY, MagicMock

import pytest

from spindle_core.connectors.caller import HttpResponse
from spindle_core.connectors.consumer import (
    RESULTS_STREAM_KEY,
    ConnectorConsumer,
)
from spindle_core.tapestry.loader import TapestryStore
from spindle_core.tapestry.models import (
    CompensationConfig,
    RequestConfig,
    StepConfig,
    SuccessCheck,
)

STEP_TYPE = "ReserveInventory"


# ---- helpers ----

def _make_step_config(
    *,
    with_compensation: bool = True,
    manual_review: bool = False,
    comp_success_check: SuccessCheck | None = None,
) -> StepConfig:
    if not with_compensation:
        comp = None
    elif manual_review:
        comp = CompensationConfig(requires_manual_review=True)
    else:
        comp = CompensationConfig(
            requires_manual_review=False,
            request=RequestConfig(url="http://svc/release"),
            success_check=comp_success_check,
        )
    return StepConfig(
        step_type=STEP_TYPE,
        execute=RequestConfig(url="http://svc/reserve"),
        execute_success_check=SuccessCheck(),
        compensation=comp,
    )


def _make_consumer(
    step_config: StepConfig | None = None,
    http_response: HttpResponse | None = None,
    empty_store: bool = False,
) -> tuple[ConnectorConsumer, MagicMock, MagicMock]:
    r = MagicMock()
    if empty_store:
        store = TapestryStore([])
    else:
        store = TapestryStore([step_config or _make_step_config()])
    http = MagicMock()
    http.request.return_value = (
        http_response
        if http_response is not None
        else HttpResponse(status_code=200, body=b"{}")
    )
    consumer = ConnectorConsumer(
        redis_client=r,
        tapestry=store,
        step_type=STEP_TYPE,
        consumer_name="test-consumer",
        http_client=http,
    )
    return consumer, r, http


def _result_fields(r: MagicMock) -> dict[str, str]:
    """Extract the fields dict from the most recent r.xadd() call."""
    return r.xadd.call_args.args[1]


DISPATCH_STEP = {
    "type": "DispatchStep",
    "saga_id": "saga-1",
    "step_id": "reserve",
    "step_type": STEP_TYPE,
    "idempotency_key": "saga-1:reserve:execute",
}

DISPATCH_COMP = {
    "type": "DispatchCompensation",
    "saga_id": "saga-1",
    "step_id": "reserve",
    "step_type": STEP_TYPE,
    "idempotency_key": "saga-1:reserve:compensate",
}


# ================================================================
# DispatchStep — execute path
# ================================================================

def test_step_success_publishes_step_succeeded():
    consumer, r, _ = _make_consumer()
    consumer.handle_once("msg-1", DISPATCH_STEP)
    assert _result_fields(r)["type"] == "StepSucceeded"


def test_step_success_carries_correct_saga_and_step_ids():
    consumer, r, _ = _make_consumer()
    consumer.handle_once("msg-1", DISPATCH_STEP)
    fields = _result_fields(r)
    assert fields["saga_id"] == "saga-1"
    assert fields["step_id"] == "reserve"


def test_step_failure_on_http_500_publishes_step_failed():
    consumer, r, _ = _make_consumer(
        http_response=HttpResponse(status_code=500, body=b"server error")
    )
    consumer.handle_once("msg-1", DISPATCH_STEP)
    assert _result_fields(r)["type"] == "StepFailed"


def test_step_failure_includes_reason():
    consumer, r, _ = _make_consumer(
        http_response=HttpResponse(status_code=500, body=b"oops")
    )
    consumer.handle_once("msg-1", DISPATCH_STEP)
    assert "reason" in _result_fields(r)


def test_step_failure_on_http_exception_publishes_step_failed():
    consumer, r, http = _make_consumer()
    http.request.side_effect = OSError("connection refused")
    consumer.handle_once("msg-1", DISPATCH_STEP)
    assert _result_fields(r)["type"] == "StepFailed"


def test_no_tapestry_config_publishes_step_failed():
    consumer, r, _ = _make_consumer(empty_store=True)
    consumer.handle_once("msg-1", DISPATCH_STEP)
    assert _result_fields(r)["type"] == "StepFailed"


def test_step_success_with_body_field_check():
    cfg = StepConfig(
        step_type=STEP_TYPE,
        execute=RequestConfig(url="http://svc/reserve"),
        execute_success_check=SuccessCheck(
            body_field="status", body_field_value="reserved"
        ),
    )
    consumer, r, _ = _make_consumer(
        step_config=cfg,
        http_response=HttpResponse(
            status_code=200,
            body=json.dumps({"status": "reserved"}).encode(),
        ),
    )
    consumer.handle_once("msg-1", DISPATCH_STEP)
    assert _result_fields(r)["type"] == "StepSucceeded"


def test_step_failure_when_body_field_check_fails():
    cfg = StepConfig(
        step_type=STEP_TYPE,
        execute=RequestConfig(url="http://svc/reserve"),
        execute_success_check=SuccessCheck(
            body_field="status", body_field_value="reserved"
        ),
    )
    consumer, r, _ = _make_consumer(
        step_config=cfg,
        http_response=HttpResponse(
            status_code=200,
            body=json.dumps({"status": "pending"}).encode(),
        ),
    )
    consumer.handle_once("msg-1", DISPATCH_STEP)
    assert _result_fields(r)["type"] == "StepFailed"


# ================================================================
# DispatchCompensation — compensation path
# ================================================================

def test_compensation_success_publishes_compensation_succeeded():
    consumer, r, _ = _make_consumer()
    consumer.handle_once("msg-1", DISPATCH_COMP)
    assert _result_fields(r)["type"] == "CompensationSucceeded"


def test_compensation_manual_review_publishes_compensation_failed():
    consumer, r, _ = _make_consumer(step_config=_make_step_config(manual_review=True))
    consumer.handle_once("msg-1", DISPATCH_COMP)
    fields = _result_fields(r)
    assert fields["type"] == "CompensationFailed"
    assert "manual review" in fields.get("reason", "")


def test_compensation_no_config_publishes_compensation_failed():
    consumer, r, _ = _make_consumer(
        step_config=_make_step_config(with_compensation=False)
    )
    consumer.handle_once("msg-1", DISPATCH_COMP)
    assert _result_fields(r)["type"] == "CompensationFailed"


def test_compensation_http_exception_publishes_compensation_failed():
    consumer, r, http = _make_consumer()
    http.request.side_effect = OSError("timeout")
    consumer.handle_once("msg-1", DISPATCH_COMP)
    assert _result_fields(r)["type"] == "CompensationFailed"


def test_compensation_bad_status_publishes_compensation_failed():
    consumer, r, _ = _make_consumer(
        http_response=HttpResponse(status_code=503, body=b"unavailable")
    )
    consumer.handle_once("msg-1", DISPATCH_COMP)
    assert _result_fields(r)["type"] == "CompensationFailed"


def test_compensation_uses_comp_success_check_when_provided():
    """If comp.success_check is set, it overrides execute_success_check."""
    comp_check = SuccessCheck(body_field="comp_ok", body_field_value="yes")
    cfg = _make_step_config(comp_success_check=comp_check)
    consumer, r, _ = _make_consumer(
        step_config=cfg,
        http_response=HttpResponse(
            status_code=200,
            body=json.dumps({"comp_ok": "yes"}).encode(),
        ),
    )
    consumer.handle_once("msg-1", DISPATCH_COMP)
    assert _result_fields(r)["type"] == "CompensationSucceeded"


def test_no_tapestry_config_for_compensation_publishes_failed():
    consumer, r, _ = _make_consumer(empty_store=True)
    consumer.handle_once("msg-1", DISPATCH_COMP)
    assert _result_fields(r)["type"] == "CompensationFailed"


# ================================================================
# Unknown message type
# ================================================================

def test_unknown_message_type_does_not_publish_and_does_not_raise():
    consumer, r, _ = _make_consumer()
    consumer.handle_once("msg-1", {"type": "UnknownThing", "saga_id": "x", "step_id": "y"})
    r.xadd.assert_not_called()


# ================================================================
# Results stream key
# ================================================================

def test_result_published_to_correct_stream_key():
    consumer, r, _ = _make_consumer()
    consumer.handle_once("msg-1", DISPATCH_STEP)
    r.xadd.assert_called_once_with(RESULTS_STREAM_KEY, ANY)


# ================================================================
# Outcome tracking
# ================================================================

def test_outcome_tracker_records_success_on_step_success():
    consumer, r, _ = _make_consumer()
    consumer.handle_once("msg-1", DISPATCH_STEP)
    snap = consumer._tracker.snapshot(STEP_TYPE)
    assert snap.success_count == 1
    assert snap.failure_count == 0


def test_outcome_tracker_records_failure_on_step_failure():
    consumer, r, _ = _make_consumer(
        http_response=HttpResponse(status_code=500, body=b"err")
    )
    consumer.handle_once("msg-1", DISPATCH_STEP)
    snap = consumer._tracker.snapshot(STEP_TYPE)
    assert snap.failure_count == 1
    assert snap.success_count == 0


def test_outcome_tracker_accumulates_across_messages():
    consumer, r, http = _make_consumer()
    # 2 successes
    consumer.handle_once("msg-1", DISPATCH_STEP)
    consumer.handle_once("msg-2", DISPATCH_STEP)
    # 1 failure
    http.request.return_value = HttpResponse(500, b"err")
    consumer.handle_once("msg-3", DISPATCH_STEP)
    snap = consumer._tracker.snapshot(STEP_TYPE)
    assert snap.success_count == 2
    assert snap.failure_count == 1
