# Component: Spindle Architecture
# File: test_example_order_fulfillment.py
# Description: Unit and integration tests for the order-fulfillment saga example.

"""Tests for order-fulfillment example: mock services, tapestry config, and saga execution."""

from __future__ import annotations

import concurrent.futures
import importlib.util
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from spindle_core.api.dependencies import load_tapestry_store
from spindle_core.connectors.consumer import ConnectorConsumer
from spindle_core.templates.loader import TemplateStore

REPO_ROOT = Path(__file__).resolve().parents[4]

mock_services_path = REPO_ROOT / "examples" / "order-fulfillment" / "mock_services.py"
spec = importlib.util.spec_from_file_location("mock_services", mock_services_path)
assert spec is not None and spec.loader is not None
mock_services = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mock_services)


@pytest.fixture
def mock_server(monkeypatch):
    """Start mock services on an ephemeral loopback port in a daemon thread."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    server = mock_services.create_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}"
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


@pytest.fixture
def tapestry_store(mock_server, tmp_path):
    """Load tapestry pointing to the ephemeral mock server."""
    orig_path = REPO_ROOT / "examples" / "order-fulfillment" / "tapestry.yaml"
    text = orig_path.read_text(encoding="utf-8")
    replaced = text.replace("http://mock-services:9000", mock_server)
    tmp_file = tmp_path / "tapestry.yaml"
    tmp_file.write_text(replaced, encoding="utf-8")
    return load_tapestry_store(tmp_file)


def run_connector_step(
    store,
    step_type: str,
    msg_type: str,
    saga_id: str,
    step_id: str,
    idempotency_key: str,
) -> dict[str, str]:
    """Execute a step through real ConnectorConsumer and return published result."""
    redis_mock = MagicMock()
    consumer = ConnectorConsumer(
        redis_client=redis_mock,
        tapestry=store,
        step_type=step_type,
        consumer_name="t",
    )
    fields = {
        "type": msg_type,
        "saga_id": saga_id,
        "step_id": step_id,
        "step_type": step_type,
        "idempotency_key": idempotency_key,
    }
    consumer.handle_once("1", fields)
    assert redis_mock.xadd.called, "redis.xadd was not called"
    return redis_mock.xadd.call_args.args[1]


def make_http_request(
    url: str,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    method: str = "POST",
) -> tuple[int, bytes]:
    """Send raw HTTP request using urllib and return (status_code, body)."""
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def get_admin_state(base_url: str) -> dict:
    """Fetch black-box state from mock services."""
    code, body = make_http_request(f"{base_url}/admin/state", method="GET")
    assert code == 200
    return json.loads(body.decode("utf-8"))


def set_admin_fail(base_url: str, shop: str, enabled: bool) -> dict:
    """Toggle failure mode for a mock shop."""
    body = json.dumps({"shop": shop, "enabled": enabled}).encode("utf-8")
    code, resp_bytes = make_http_request(
        f"{base_url}/admin/fail",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    assert code == 200
    return json.loads(resp_bytes.decode("utf-8"))


def post_admin_reset(base_url: str) -> dict:
    """Reset mock services state."""
    code, resp_bytes = make_http_request(
        f"{base_url}/admin/reset",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    assert code == 200
    return json.loads(resp_bytes.decode("utf-8"))


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------


def test_tapestry_configuration_structure():
    """Case 1: The original tapestry.yaml parses and all step definitions match spec."""
    orig_path = REPO_ROOT / "examples" / "order-fulfillment" / "tapestry.yaml"
    text = orig_path.read_text(encoding="utf-8")
    assert "http://mock-services:9000" in text

    store = load_tapestry_store(orig_path)
    expected_steps = ["CreateOrder", "ReserveInventory", "ChargePayment", "ShipOrder"]
    assert store.all_step_types() == expected_steps

    expected_words = {
        "CreateOrder": "created",
        "ReserveInventory": "reserved",
        "ChargePayment": "charged",
        "ShipOrder": "shipped",
    }

    for step_type in expected_steps:
        step_def = store.get(step_type)
        assert step_def is not None
        assert step_def.execute.method == "POST"
        assert step_def.execute.field_mapping == {"order_id": "saga_id", "step_id": "step_id"}
        assert step_def.execute_success_check.body_field == "status"
        assert step_def.execute_success_check.body_field_value == expected_words[step_type]
        assert step_def.compensation is not None
        assert step_def.compensation.requires_manual_review is False
        assert step_def.compensation.request is not None
        assert bool(step_def.compensation.request.url)
        assert step_def.compensation.success_check is not None
        assert step_def.compensation.success_check.ok_status_codes == frozenset({200})


def test_template_store_loading():
    """Case 2: TemplateStore loads exactly one template with exact step pairs."""
    orig_path = REPO_ROOT / "examples" / "order-fulfillment" / "tapestry.yaml"
    store = load_tapestry_store(orig_path)
    templates_dir = REPO_ROOT / "examples" / "order-fulfillment" / "templates"
    yaml_files = list(templates_dir.glob("*.yaml"))
    assert len(yaml_files) == 1
    assert yaml_files[0].name == "order-fulfillment@v1.yaml"

    template_store = TemplateStore.from_dir(templates_dir, store)
    tmpl = template_store.get("order-fulfillment")
    assert tmpl is not None
    assert [(s.id, s.type) for s in tmpl.steps] == [
        ("create-order", "CreateOrder"),
        ("reserve-inventory", "ReserveInventory"),
        ("charge-payment", "ChargePayment"),
        ("ship-order", "ShipOrder"),
    ]


def test_happy_path_all_steps_succeed(tapestry_store, mock_server):
    """Case 3: All 4 forward steps succeed and active orders are recorded in all shops."""
    saga_id = "saga-happy-100"
    steps = [
        ("CreateOrder", "create-order"),
        ("ReserveInventory", "reserve-inventory"),
        ("ChargePayment", "charge-payment"),
        ("ShipOrder", "ship-order"),
    ]
    for step_type, step_id in steps:
        res = run_connector_step(
            tapestry_store,
            step_type=step_type,
            msg_type="DispatchStep",
            saga_id=saga_id,
            step_id=step_id,
            idempotency_key=f"key-{step_id}",
        )
        assert res["type"] == "StepSucceeded", f"{step_type} failed: {res.get('reason')}"

    state = get_admin_state(mock_server)
    for shop in ["orders", "inventory", "payments", "shipping"]:
        assert saga_id in state["shops"][shop]["active"]
        assert state["shops"][shop]["applied_count"] == 1


def test_duplicate_ticket_idempotent_replay(tapestry_store, mock_server):
    """Case 4: Same ChargePayment dispatch twice yields StepSucceeded, applied 1, replay 1."""
    saga_id = "saga-dup-200"
    res1 = run_connector_step(
        tapestry_store,
        step_type="ChargePayment",
        msg_type="DispatchStep",
        saga_id=saga_id,
        step_id="charge-payment",
        idempotency_key="key-dup-charge",
    )
    assert res1["type"] == "StepSucceeded"

    res2 = run_connector_step(
        tapestry_store,
        step_type="ChargePayment",
        msg_type="DispatchStep",
        saga_id=saga_id,
        step_id="charge-payment",
        idempotency_key="key-dup-charge",
    )
    assert res2["type"] == "StepSucceeded"

    state = get_admin_state(mock_server)
    assert state["shops"]["payments"]["applied_count"] == 1
    assert state["shops"]["payments"]["replay_count"] == 1


def test_payment_failure_toggle(tapestry_store, mock_server):
    """Case 5: Payments failing returns StepFailed with 'declined' and active empty."""
    set_admin_fail(mock_server, "payments", True)
    saga_id = "saga-pay-fail"
    res = run_connector_step(
        tapestry_store,
        step_type="ChargePayment",
        msg_type="DispatchStep",
        saga_id=saga_id,
        step_id="charge-payment",
        idempotency_key="key-pay-fail",
    )
    assert res["type"] == "StepFailed"
    assert "declined" in res.get("reason", "")

    state = get_admin_state(mock_server)
    assert saga_id not in state["shops"]["payments"]["active"]
    assert state["shops"]["payments"]["active"] == []


def test_inventory_failure_toggle(tapestry_store, mock_server):
    """Case 6: Inventory failing returns StepFailed with 500 error."""
    set_admin_fail(mock_server, "inventory", True)
    saga_id = "saga-inv-fail"
    res = run_connector_step(
        tapestry_store,
        step_type="ReserveInventory",
        msg_type="DispatchStep",
        saga_id=saga_id,
        step_id="reserve-inventory",
        idempotency_key="key-inv-fail",
    )
    assert res["type"] == "StepFailed"
    assert "500" in res.get("reason", "")


def test_compensation_idempotent_undo(tapestry_store, mock_server):
    """Case 7: Reserve then DispatchCompensation twice -> CompensationSucceeded, active empty, undone 1."""
    saga_id = "saga-comp-700"
    res_fwd = run_connector_step(
        tapestry_store,
        step_type="ReserveInventory",
        msg_type="DispatchStep",
        saga_id=saga_id,
        step_id="reserve-inventory",
        idempotency_key="key-comp-fwd",
    )
    assert res_fwd["type"] == "StepSucceeded"

    state_mid = get_admin_state(mock_server)
    assert saga_id in state_mid["shops"]["inventory"]["active"]

    res_comp1 = run_connector_step(
        tapestry_store,
        step_type="ReserveInventory",
        msg_type="DispatchCompensation",
        saga_id=saga_id,
        step_id="reserve-inventory",
        idempotency_key="key-comp-undo-1",
    )
    assert res_comp1["type"] == "CompensationSucceeded"

    res_comp2 = run_connector_step(
        tapestry_store,
        step_type="ReserveInventory",
        msg_type="DispatchCompensation",
        saga_id=saga_id,
        step_id="reserve-inventory",
        idempotency_key="key-comp-undo-2",
    )
    assert res_comp2["type"] == "CompensationSucceeded"

    state_end = get_admin_state(mock_server)
    assert saga_id not in state_end["shops"]["inventory"]["active"]
    assert state_end["shops"]["inventory"]["undone_count"] == 1


def test_undo_succeeds_even_when_shop_failing(tapestry_store, mock_server):
    """Case 8: Undo while failing still succeeds and clears active order."""
    saga_id = "saga-undofail-800"
    res_fwd = run_connector_step(
        tapestry_store,
        step_type="ReserveInventory",
        msg_type="DispatchStep",
        saga_id=saga_id,
        step_id="reserve-inventory",
        idempotency_key="key-undofail-fwd",
    )
    assert res_fwd["type"] == "StepSucceeded"

    set_admin_fail(mock_server, "inventory", True)

    res_comp = run_connector_step(
        tapestry_store,
        step_type="ReserveInventory",
        msg_type="DispatchCompensation",
        saga_id=saga_id,
        step_id="reserve-inventory",
        idempotency_key="key-undofail-comp",
    )
    assert res_comp["type"] == "CompensationSucceeded"

    state = get_admin_state(mock_server)
    assert saga_id not in state["shops"]["inventory"]["active"]
    assert state["shops"]["inventory"]["undone_count"] == 1


def test_mock_input_validation(mock_server):
    """Case 9: Raw HTTP validation for missing key, empty order, invalid json, 404, payload limit."""
    # forward without Idempotency-Key -> 400
    code, _ = make_http_request(
        f"{mock_server}/orders/create",
        data=b'{"order_id":"val-1"}',
        headers={"Content-Type": "application/json"},
    )
    assert code == 400

    # empty order_id -> 400
    code, _ = make_http_request(
        f"{mock_server}/orders/create",
        data=b'{"order_id":""}',
        headers={"Content-Type": "application/json", "Idempotency-Key": "k-val"},
    )
    assert code == 400

    # invalid JSON -> 400
    code, _ = make_http_request(
        f"{mock_server}/orders/create",
        data=b'not-json',
        headers={"Content-Type": "application/json", "Idempotency-Key": "k-val"},
    )
    assert code == 400

    # unknown path -> 404
    code, _ = make_http_request(
        f"{mock_server}/nonexistent/endpoint",
        data=b'{"order_id":"val-1"}',
        headers={"Content-Type": "application/json", "Idempotency-Key": "k-val"},
    )
    assert code == 404

    # Content-Length over 65536 -> 413
    big_body = json.dumps({"order_id": "val-1", "padding": "x" * 70000}).encode("utf-8")
    code, _ = make_http_request(
        f"{mock_server}/orders/create",
        data=big_body,
        headers={"Content-Type": "application/json", "Idempotency-Key": "k-val"},
    )
    assert code == 413


def test_admin_reset_restores_initial_state_and_seq_increases(monkeypatch):
    """Case 10: /admin/reset restores creation-time failing set and clears state; seq strictly increases."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    srv = mock_services.create_server("127.0.0.1", 0, fail_shops=["payments"])
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    base_url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        make_http_request(
            f"{base_url}/orders/create",
            data=b'{"order_id":"r-1"}',
            headers={"Content-Type": "application/json", "Idempotency-Key": "k-r1"},
        )
        make_http_request(
            f"{base_url}/inventory/reserve",
            data=b'{"order_id":"r-1"}',
            headers={"Content-Type": "application/json", "Idempotency-Key": "k-r2"},
        )
        make_http_request(
            f"{base_url}/inventory/release",
            data=b'{"order_id":"r-1"}',
            headers={"Content-Type": "application/json"},
        )

        st1 = get_admin_state(base_url)
        calls1 = st1["calls"]
        assert len(calls1) == 3
        seqs1 = [c["seq"] for c in calls1]
        assert seqs1 == sorted(seqs1) and len(set(seqs1)) == len(seqs1)
        assert all(seqs1[i] < seqs1[i + 1] for i in range(len(seqs1) - 1))

        post_admin_reset(base_url)

        st2 = get_admin_state(base_url)
        assert st2["fail"] == ["payments"]
        assert st2["calls"] == []
        for s in mock_services.SHOPS:
            assert st2["shops"][s]["active"] == []
            assert st2["shops"][s]["applied_count"] == 0
            assert st2["shops"][s]["undone_count"] == 0
            assert st2["shops"][s]["replay_count"] == 0

        make_http_request(
            f"{base_url}/orders/create",
            data=b'{"order_id":"r-2"}',
            headers={"Content-Type": "application/json", "Idempotency-Key": "k-r3"},
        )
        make_http_request(
            f"{base_url}/orders/create",
            data=b'{"order_id":"r-3"}',
            headers={"Content-Type": "application/json", "Idempotency-Key": "k-r4"},
        )
        st3 = get_admin_state(base_url)
        calls2 = st3["calls"]
        assert len(calls2) == 2
        seqs2 = [c["seq"] for c in calls2]
        assert seqs2[0] < seqs2[1]
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2.0)


def test_concurrency_smoke_one_apply_19_replays(mock_server):
    """Case 11: 20 concurrent threads send same forward request -> exactly 1 apply, 19 replays."""
    url = f"{mock_server}/orders/create"
    data = b'{"order_id":"conc-order"}'
    headers = {"Content-Type": "application/json", "Idempotency-Key": "conc-key-common"}

    def _worker():
        return make_http_request(url, data=data, headers=headers)

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(_worker) for _ in range(20)]
        results = [f.result() for f in futures]

    for status_code, body in results:
        assert status_code == 200
        parsed = json.loads(body.decode("utf-8"))
        assert parsed == {"status": "created", "order_id": "conc-order"}

    state = get_admin_state(mock_server)
    assert state["shops"]["orders"]["applied_count"] == 1
    assert state["shops"]["orders"]["replay_count"] == 19

