import concurrent.futures
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Generator

import pytest
import redis

from spindle_core.streams.keys import (
    DEAD_COMMANDS_STREAM_KEY,
    DEAD_RESULTS_STREAM_KEY,
    RESULTS_GROUP_NAME,
    RESULTS_STREAM_KEY,
    STREAM_KEY,
)

pytestmark = pytest.mark.e2e

API_URL = os.environ.get("SPINDLE_E2E_API_URL", "http://127.0.0.1:8000")
MOCK_URL = os.environ.get("SPINDLE_E2E_MOCK_URL", "http://127.0.0.1:9000")
REDIS_URL = os.environ.get("SPINDLE_E2E_REDIS_URL", "redis://127.0.0.1:6379/0")
USER = os.environ.get("SPINDLE_E2E_USER", "admin")
PASSWORD = os.environ.get("SPINDLE_E2E_PASSWORD", "spindle-dev-only")
BURST_COUNT = int(os.environ.get("SPINDLE_E2E_BURST", "25"))


def http_request(
    url: str,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, Any]:
    """Execute HTTP request bypassing proxies and return status and parsed body."""
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=10) as resp:
            status = resp.status
            raw = resp.read()
    except urllib.error.HTTPError as err:
        status = err.code
        raw = err.read()

    text = raw.decode("utf-8", errors="replace")
    try:
        return status, json.loads(text)
    except Exception:
        return status, text


@pytest.fixture(scope="module")
def stack() -> bool:
    """Verify live stack reachability or skip all integration tests."""
    try:
        status, body = http_request(f"{API_URL}/health")
        if status != 200 or not isinstance(body, dict) or "checks" not in body:
            pytest.skip(f"live stack not reachable: API health returned {status}")
    except Exception as exc:
        pytest.skip(f"live stack not reachable: API health error {exc}")

    try:
        status, _ = http_request(f"{MOCK_URL}/health")
        if status != 200:
            pytest.skip(f"live stack not reachable: mock health returned {status}")
    except Exception as exc:
        pytest.skip(f"live stack not reachable: mock health error {exc}")

    try:
        status, _ = http_request(f"{MOCK_URL}/admin/state")
        if status != 200:
            pytest.skip(f"live stack not reachable: mock admin state returned {status}")
    except Exception as exc:
        pytest.skip(f"live stack not reachable: mock admin state error {exc}")

    try:
        client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
        if not client.ping():
            pytest.skip("live stack not reachable: redis ping failed")
        client.close()
    except Exception as exc:
        pytest.skip(f"live stack not reachable: redis error {exc}")

    return True


@pytest.fixture(scope="module")
def token(stack: bool) -> str:
    """Authenticate with API and return bearer access token."""
    data = urllib.parse.urlencode({"username": USER, "password": PASSWORD}).encode("utf-8")
    status, body = http_request(
        f"{API_URL}/auth/token",
        method="POST",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert status == 200, f"auth token failed ({status}): {body}"
    assert isinstance(body, dict) and "access_token" in body
    return body["access_token"]


@pytest.fixture(scope="module")
def r(stack: bool) -> Generator[redis.Redis, None, None]:
    """Provide Redis client instance for stream operations."""
    client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    yield client
    client.close()


@pytest.fixture(autouse=True)
def clean_mock(stack: bool) -> Generator[None, None, None]:
    """Reset mock state before and after each test."""
    http_request(f"{MOCK_URL}/admin/reset", method="POST")
    try:
        yield
    finally:
        http_request(f"{MOCK_URL}/admin/reset", method="POST")


def start_saga(token: str) -> str:
    """Dispatch saga creation and return created saga_id."""
    payload = json.dumps({"template": "order-fulfillment"}).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    status, body = http_request(
        f"{API_URL}/sagas",
        method="POST",
        data=payload,
        headers=headers,
    )
    assert status == 201, f"POST /sagas failed with {status}: {body}"
    assert isinstance(body, dict) and "saga_id" in body
    return body["saga_id"]


def wait_terminal(
    token: str, saga_id: str, timeout: float = 30.0
) -> tuple[str, list[str]]:
    """Poll saga until terminal status is reached or timeout expires."""
    terminal_statuses = {"COMPLETED", "COMPENSATED", "FAILED"}
    seen: list[str] = []
    last_status = "UNKNOWN"
    deadline = time.monotonic() + timeout
    headers = {"Authorization": f"Bearer {token}"}
    while time.monotonic() < deadline:
        status, body = http_request(f"{API_URL}/sagas/{saga_id}", headers=headers)
        assert status == 200, f"GET /sagas/{saga_id} failed with {status}: {body}"
        assert isinstance(body, dict) and "status" in body
        cur = body["status"]
        last_status = cur
        if not seen or seen[-1] != cur:
            seen.append(cur)
        if cur in terminal_statuses:
            return cur, seen
        time.sleep(0.25)
    pytest.fail(f"saga {saga_id} timed out after {timeout}s; last status was {last_status}")


def mock_calls(saga_id: str) -> list[tuple[str, int, bool]]:
    """Retrieve and sort mock HTTP calls matching given saga_id."""
    status, body = http_request(f"{MOCK_URL}/admin/state")
    assert status == 200, f"mock /admin/state returned {status}"
    assert isinstance(body, dict)
    calls = body.get("calls", [])
    matching = [c for c in calls if c.get("order_id") == saga_id]
    matching.sort(key=lambda c: c.get("seq", 0))
    return [(c["path"], c["status"], c["replay"]) for c in matching]


def drain_results(r: redis.Redis, timeout: float = 20.0) -> None:
    """Ensure all pending and lagging items in results group are processed."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            groups = r.xinfo_groups(RESULTS_STREAM_KEY)
            group = next((g for g in groups if g.get("name") == RESULTS_GROUP_NAME), None)
            if group is not None:
                pending = group.get("pending", 0)
                lag = group.get("lag") or 0
                if pending == 0 and lag == 0:
                    time.sleep(0.3)
                    return
        except redis.ResponseError:
            time.sleep(0.1)
        time.sleep(0.2)
    pytest.fail(f"drain_results timed out waiting for group {RESULTS_GROUP_NAME}")


def dead_entries(
    r: redis.Redis, stream: str, saga_id: str
) -> list[dict[str, str]]:
    """Read up to 500 latest entries from dead stream matching saga_id."""
    try:
        entries = r.xrevrange(stream, max="+", min="-", count=500)
        return [fields for _, fields in entries if fields.get("saga_id") == saga_id]
    except redis.ResponseError:
        return []


def wait_dead(
    r: redis.Redis, stream: str, saga_id: str, timeout: float = 20.0
) -> list[dict[str, str]]:
    """Poll dead stream until matching entries appear or timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        items = dead_entries(r, stream, saga_id)
        if items:
            return items
        time.sleep(0.2)
    pytest.fail(f"wait_dead timed out for saga_id={saga_id} on {stream}")


def new_marker() -> str:
    """Generate unique test marker for isolating stream messages."""
    return f"e2e-{uuid.uuid4().hex}"


def set_mock_fail(shop: str, enabled: bool) -> None:
    """Configure simulated failure for a target shop on mock service."""
    payload = json.dumps({"shop": shop, "enabled": enabled}).encode("utf-8")
    status, body = http_request(
        f"{MOCK_URL}/admin/fail",
        method="POST",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    assert status == 200, f"POST /admin/fail failed with {status}: {body}"


def test_happy_path(token: str, r: redis.Redis) -> None:
    # Verify standard saga execution completes all forward steps successfully
    saga_id = start_saga(token)
    final_status, _ = wait_terminal(token, saga_id)
    assert final_status == "COMPLETED"
    expected = [
        ("/orders/create", 200, False),
        ("/inventory/reserve", 200, False),
        ("/payments/charge", 200, False),
        ("/shipping/ship", 200, False),
    ]
    assert mock_calls(saga_id) == expected
    status, body = http_request(
        f"{API_URL}/sagas/{saga_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert status == 200 and body.get("status") == "COMPLETED"


def test_payments_declined(token: str, r: redis.Redis) -> None:
    # Verify payments decline triggers compensation of earlier completed steps
    set_mock_fail("payments", True)
    saga_id = start_saga(token)
    final_status, _ = wait_terminal(token, saga_id)
    assert final_status == "COMPENSATED"
    expected = [
        ("/orders/create", 200, False),
        ("/inventory/reserve", 200, False),
        ("/payments/charge", 200, False),
        ("/inventory/release", 200, False),
        ("/orders/cancel", 200, False),
    ]
    assert mock_calls(saga_id) == expected


def test_inventory_500(token: str, r: redis.Redis) -> None:
    # Verify inventory 500 error compensates only previously completed order step
    set_mock_fail("inventory", True)
    saga_id = start_saga(token)
    final_status, _ = wait_terminal(token, saga_id)
    assert final_status == "COMPENSATED"
    expected = [
        ("/orders/create", 200, False),
        ("/inventory/reserve", 500, False),
        ("/orders/cancel", 200, False),
    ]
    assert mock_calls(saga_id) == expected


def test_shipping_failure(token: str, r: redis.Redis) -> None:
    # Verify shipping failure compensates all prior steps in reverse order
    set_mock_fail("shipping", True)
    saga_id = start_saga(token)
    final_status, _ = wait_terminal(token, saga_id)
    assert final_status == "COMPENSATED"
    expected = [
        ("/orders/create", 200, False),
        ("/inventory/reserve", 200, False),
        ("/payments/charge", 200, False),
        ("/shipping/ship", 500, False),
        ("/payments/refund", 200, False),
        ("/inventory/release", 200, False),
        ("/orders/cancel", 200, False),
    ]
    assert mock_calls(saga_id) == expected


def test_first_step_failure(token: str, r: redis.Redis) -> None:
    # Verify initial step failure terminates without invoking compensation
    set_mock_fail("orders", True)
    saga_id = start_saga(token)
    final_status, _ = wait_terminal(token, saga_id)
    assert final_status == "COMPENSATED"
    expected = [("/orders/create", 500, False)]
    assert mock_calls(saga_id) == expected


def test_duplicate_result_is_no_op(token: str, r: redis.Redis) -> None:
    # Verify duplicate result event does not mutate state or dead-letter
    saga_id = start_saga(token)
    final_status, _ = wait_terminal(token, saga_id)
    assert final_status == "COMPLETED"
    calls_before = mock_calls(saga_id)
    r.xadd(RESULTS_STREAM_KEY, {"type": "StepSucceeded", "saga_id": saga_id, "step_id": "ship-order"})
    drain_results(r)
    status, body = http_request(
        f"{API_URL}/sagas/{saga_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert status == 200 and body.get("status") == "COMPLETED"
    assert dead_entries(r, DEAD_RESULTS_STREAM_KEY, saga_id) == []
    assert mock_calls(saga_id) == calls_before


def test_invalid_transition(token: str, r: redis.Redis) -> None:
    # Verify illegal state transition event routes to dead letter stream
    saga_id = start_saga(token)
    final_status, _ = wait_terminal(token, saga_id)
    assert final_status == "COMPLETED"
    r.xadd(
        RESULTS_STREAM_KEY,
        {"type": "CompensationSucceeded", "saga_id": saga_id, "step_id": "create-order"},
    )
    entries = wait_dead(r, DEAD_RESULTS_STREAM_KEY, saga_id)
    assert any(e.get("reason", "").startswith("InvalidTransition") for e in entries)
    status, body = http_request(
        f"{API_URL}/sagas/{saga_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert status == 200 and body.get("status") == "COMPLETED"


def test_unknown_saga(r: redis.Redis) -> None:
    # Verify event for non-existent saga is routed to dead letter stream
    marker = new_marker()
    r.xadd(RESULTS_STREAM_KEY, {"type": "StepSucceeded", "saga_id": marker, "step_id": "x"})
    entries = wait_dead(r, DEAD_RESULTS_STREAM_KEY, marker)
    assert any(e.get("reason", "").startswith("UnknownSaga") for e in entries)


@pytest.mark.parametrize(
    "case_fields",
    [
        {"type": "Bogus", "step_id": "x"},
        {"type": "StepSucceeded"},
    ],
)
def test_malformed_result(r: redis.Redis, case_fields: dict[str, str]) -> None:
    # Verify malformed or incomplete result payload is routed to dead letters
    marker = new_marker()
    fields = dict(case_fields)
    fields["saga_id"] = marker
    r.xadd(RESULTS_STREAM_KEY, fields)
    entries = wait_dead(r, DEAD_RESULTS_STREAM_KEY, marker)
    assert any(e.get("reason", "").startswith("malformed result:") for e in entries)


@pytest.mark.parametrize(
    "dispatch_type,expected_event",
    [
        ("DispatchStep", "StepFailed"),
        ("DispatchCompensation", "CompensationFailed"),
    ],
)
def test_router_unknown_step_type(
    r: redis.Redis, dispatch_type: str, expected_event: str
) -> None:
    # Verify unknown step type causes router to emit failure result to dead stream
    marker = new_marker()
    fields = {
        "type": dispatch_type,
        "saga_id": marker,
        "step_id": "s1",
        "step_type": "NoSuchType",
        "idempotency_key": f"{marker}:s1:execute",
    }
    r.xadd(STREAM_KEY, fields)
    entries = wait_dead(r, DEAD_RESULTS_STREAM_KEY, marker)
    assert any(
        e.get("type") == expected_event
        and e.get("reason", "").startswith("UnknownSaga")
        and "not registered in TapestryStore" in e.get("original_reason", "")
        for e in entries
    )


def test_unroutable_command(r: redis.Redis) -> None:
    # Verify command missing required type field is sent to dead commands stream
    marker = new_marker()
    r.xadd(STREAM_KEY, {"saga_id": marker, "step_id": "s1"})
    entries = wait_dead(r, DEAD_COMMANDS_STREAM_KEY, marker)
    assert any("unroutable command" in e.get("reason", "") for e in entries)


def test_burst(token: str) -> None:
    # Verify concurrent sagas execute to completion without replaying steps
    n = BURST_COUNT
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(n, 16)) as executor:
        saga_ids = list(executor.map(lambda _: start_saga(token), range(n)))

    deadline = time.monotonic() + 60.0 + 0.1 * n
    for saga_id in saga_ids:
        remaining = max(1.0, deadline - time.monotonic())
        final_status, _ = wait_terminal(token, saga_id, timeout=remaining)
        assert final_status == "COMPLETED"

    status, body = http_request(f"{MOCK_URL}/admin/state")
    assert status == 200 and isinstance(body, dict)
    shops = body.get("shops", {})
    for shop in ("orders", "inventory", "payments", "shipping"):
        shop_data = shops.get(shop, {})
        assert shop_data.get("applied_count") == n, f"{shop} applied_count != {n}"
        assert shop_data.get("replay_count") == 0, f"{shop} replay_count != 0"

