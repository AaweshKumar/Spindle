"""
Unit tests for idempotency-key handling in connectors/caller.py.

Covers:
  - build_request_body exclusion of idempotency_key when field_mapping is empty.
  - call_endpoint promotion of idempotency_key to Idempotency-Key header.
  - ConnectorConsumer.handle_once integration via MagicMock http client.
"""

from __future__ import annotations

import json
from unittest.mock import ANY, MagicMock

import pytest

from spindle_core.connectors.caller import (
    HttpResponse,
    build_request_body,
    call_endpoint,
)
from spindle_core.connectors.consumer import ConnectorConsumer
from spindle_core.tapestry.loader import TapestryStore
from spindle_core.tapestry.models import RequestConfig, StepConfig, SuccessCheck


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok_response() -> HttpResponse:
    return HttpResponse(status_code=200, body=b"{}")


def _make_config(**kwargs) -> RequestConfig:
    return RequestConfig(url="http://svc/action", **kwargs)


FIELDS_WITH_KEY = {
    "type": "DispatchStep",
    "saga_id": "saga-1",
    "step_id": "reserve",
    "step_type": "ReserveInventory",
    "idempotency_key": "saga-1:reserve:execute",
}

FIELDS_WITHOUT_KEY = {
    "type": "DispatchStep",
    "saga_id": "saga-1",
    "step_id": "reserve",
    "step_type": "ReserveInventory",
}

FIELDS_EMPTY_KEY = {
    "type": "DispatchStep",
    "saga_id": "saga-1",
    "step_id": "reserve",
    "idempotency_key": "",
}


# ---------------------------------------------------------------------------
# build_request_body
# ---------------------------------------------------------------------------

class TestBuildRequestBodyIdempotencyExclusion:
    def test_idempotency_key_excluded_when_no_field_mapping(self):
        config = _make_config()
        body = json.loads(build_request_body(config, FIELDS_WITH_KEY))
        assert "idempotency_key" not in body

    def test_other_fields_still_present_when_no_field_mapping(self):
        config = _make_config()
        body = json.loads(build_request_body(config, FIELDS_WITH_KEY))
        assert body["saga_id"] == "saga-1"
        assert body["step_id"] == "reserve"
        assert body["type"] == "DispatchStep"
        assert body["step_type"] == "ReserveInventory"

    def test_idempotency_key_absent_in_fields_no_field_mapping(self):
        config = _make_config()
        body = json.loads(build_request_body(config, FIELDS_WITHOUT_KEY))
        assert "idempotency_key" not in body

    def test_explicit_field_mapping_to_idempotency_key_honours_it(self):
        """An explicit mapping that maps a destination key to idempotency_key still works."""
        config = _make_config(field_mapping={"k": "idempotency_key"})
        body = json.loads(build_request_body(config, FIELDS_WITH_KEY))
        assert body == {"k": "saga-1:reserve:execute"}

    def test_explicit_field_mapping_non_empty_does_not_exclude_idempotency_key(self):
        """Non-empty field_mapping controls which keys appear; exclusion logic does not apply."""
        config = _make_config(field_mapping={"order_id": "saga_id"})
        body = json.loads(build_request_body(config, FIELDS_WITH_KEY))
        # idempotency_key is simply not in the mapping, so it is not in the body
        assert "idempotency_key" not in body
        assert body == {"order_id": "saga-1"}


# ---------------------------------------------------------------------------
# call_endpoint — Idempotency-Key header
# ---------------------------------------------------------------------------

class TestCallEndpointIdempotencyHeader:
    def _call(self, fields, config_kwargs=None) -> dict:
        config = _make_config(**(config_kwargs or {}))
        client = MagicMock()
        client.request.return_value = _ok_response()
        call_endpoint(config, fields, client)
        return client.request.call_args.kwargs.get("headers", {})

    def test_header_set_from_execute_style_key(self):
        headers = self._call(FIELDS_WITH_KEY)
        assert headers.get("Idempotency-Key") == "saga-1:reserve:execute"

    def test_header_set_from_compensate_style_key(self):
        fields = {**FIELDS_WITH_KEY, "idempotency_key": "saga-1:reserve:compensate"}
        headers = self._call(fields)
        assert headers.get("Idempotency-Key") == "saga-1:reserve:compensate"

    def test_header_absent_when_key_missing(self):
        headers = self._call(FIELDS_WITHOUT_KEY)
        assert "Idempotency-Key" not in headers

    def test_header_absent_when_key_is_empty_string(self):
        headers = self._call(FIELDS_EMPTY_KEY)
        assert "Idempotency-Key" not in headers

    def test_config_header_idempotency_key_replaced(self):
        """A config.headers entry with any casing for idempotency-key is replaced."""
        headers = self._call(
            FIELDS_WITH_KEY,
            config_kwargs={"headers": {"idempotency-key": "old-value"}},
        )
        # Must have exactly one idempotency header with the message value
        idem_headers = {k: v for k, v in headers.items() if k.lower() == "idempotency-key"}
        assert len(idem_headers) == 1
        assert list(idem_headers.values())[0] == "saga-1:reserve:execute"

    def test_config_header_uppercase_casing_replaced(self):
        headers = self._call(
            FIELDS_WITH_KEY,
            config_kwargs={"headers": {"IDEMPOTENCY-KEY": "old-value"}},
        )
        idem_headers = {k: v for k, v in headers.items() if k.lower() == "idempotency-key"}
        assert len(idem_headers) == 1
        assert list(idem_headers.values())[0] == "saga-1:reserve:execute"

    def test_content_type_still_present(self):
        headers = self._call(FIELDS_WITH_KEY)
        assert headers.get("Content-Type") == "application/json"

    def test_custom_headers_still_present(self):
        headers = self._call(
            FIELDS_WITH_KEY,
            config_kwargs={"headers": {"X-Custom": "my-value"}},
        )
        assert headers.get("X-Custom") == "my-value"

    def test_timeout_unchanged(self):
        config = _make_config(timeout_seconds=7.0)
        client = MagicMock()
        client.request.return_value = _ok_response()
        call_endpoint(config, FIELDS_WITH_KEY, client)
        assert client.request.call_args.kwargs.get("timeout") == 7.0

    def test_method_unchanged(self):
        config = _make_config(method="PUT")
        client = MagicMock()
        client.request.return_value = _ok_response()
        call_endpoint(config, FIELDS_WITH_KEY, client)
        assert client.request.call_args.args[0] == "PUT"

    def test_url_unchanged(self):
        config = _make_config()
        client = MagicMock()
        client.request.return_value = _ok_response()
        call_endpoint(config, FIELDS_WITH_KEY, client)
        assert client.request.call_args.args[1] == "http://svc/action"


# ---------------------------------------------------------------------------
# ConnectorConsumer.handle_once integration
# ---------------------------------------------------------------------------

class TestConnectorConsumerIdempotencyIntegration:
    """Integration test: handle_once sends correct header and body via http mock."""

    STEP_TYPE = "ReserveInventory"
    DISPATCH_STEP = {
        "type": "DispatchStep",
        "saga_id": "saga-1",
        "step_id": "reserve",
        "step_type": STEP_TYPE,
        "idempotency_key": "saga-1:reserve:execute",
    }

    def _make_consumer(self) -> tuple[ConnectorConsumer, MagicMock, MagicMock]:
        r = MagicMock()
        step_config = StepConfig(
            step_type=self.STEP_TYPE,
            execute=RequestConfig(url="http://svc/reserve"),
            execute_success_check=SuccessCheck(),
        )
        store = TapestryStore([step_config])
        http = MagicMock()
        http.request.return_value = _ok_response()
        consumer = ConnectorConsumer(
            redis_client=r,
            tapestry=store,
            step_type=self.STEP_TYPE,
            consumer_name="test-consumer",
            http_client=http,
        )
        return consumer, r, http

    def test_idempotency_header_sent_to_http(self):
        consumer, _, http = self._make_consumer()
        consumer.handle_once("msg-1", self.DISPATCH_STEP)
        headers = http.request.call_args.kwargs.get("headers", {})
        assert headers.get("Idempotency-Key") == "saga-1:reserve:execute"

    def test_idempotency_key_not_in_body(self):
        consumer, _, http = self._make_consumer()
        consumer.handle_once("msg-1", self.DISPATCH_STEP)
        body_bytes = http.request.call_args.kwargs.get("body", b"")
        body = json.loads(body_bytes)
        assert "idempotency_key" not in body

    def test_other_fields_in_body(self):
        consumer, _, http = self._make_consumer()
        consumer.handle_once("msg-1", self.DISPATCH_STEP)
        body_bytes = http.request.call_args.kwargs.get("body", b"")
        body = json.loads(body_bytes)
        assert body["saga_id"] == "saga-1"
        assert body["step_id"] == "reserve"

