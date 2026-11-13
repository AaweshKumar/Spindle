from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Generator

import pytest

pytestmark = pytest.mark.e2e

API_URL = os.environ.get("SPINDLE_E2E_API_URL", "http://127.0.0.1:8000")
MOCK_URL = os.environ.get("SPINDLE_E2E_MOCK_URL", "http://127.0.0.1:9000")
USER = os.environ.get("SPINDLE_E2E_USER", "admin")
PASSWORD = os.environ.get("SPINDLE_E2E_PASSWORD", "spindle-dev-only")


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


def wait_status(
    token: str, saga_id: str, target_status: str, timeout: float = 30.0
) -> dict[str, Any]:
    """Poll saga status until target status is reached or timeout expires."""
    deadline = time.monotonic() + timeout
    headers = {"Authorization": f"Bearer {token}"}
    last_status = "UNKNOWN"
    while time.monotonic() < deadline:
        status, body = http_request(f"{API_URL}/sagas/{saga_id}", headers=headers)
        assert status == 200, f"GET /sagas/{saga_id} failed with {status}: {body}"
        assert isinstance(body, dict) and "status" in body
        cur = body["status"]
        last_status = cur
        if cur == target_status:
            return body
        time.sleep(0.25)
    pytest.fail(f"saga {saga_id} timed out waiting for {target_status}; last was {last_status}")


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


def test_get_templates_live(token: str) -> None:
    # Verify unauthenticated call is rejected and authenticated call lists templates
    status, _ = http_request(f"{API_URL}/templates")
    assert status == 401

    headers = {"Authorization": f"Bearer {token}"}
    status, body = http_request(f"{API_URL}/templates", headers=headers)
    assert status == 200 and isinstance(body, list)

    target = next(
        (t for t in body if t.get("name") == "order-fulfillment" and t.get("version") == 1),
        None,
    )
    assert target is not None
    expected_steps = [
        {"id": "create-order", "type": "CreateOrder"},
        {"id": "reserve-inventory", "type": "ReserveInventory"},
        {"id": "charge-payment", "type": "ChargePayment"},
        {"id": "ship-order", "type": "ShipOrder"},
    ]
    assert target.get("steps") == expected_steps


def test_happy_saga_step_details(token: str) -> None:
    # Verify successful saga execution produces all step details with SUCCEEDED status
    saga_id = start_saga(token)
    data = wait_status(token, saga_id, "COMPLETED")
    steps = data.get("steps", [])

    assert [s.get("step_id") for s in steps] == [
        "create-order",
        "reserve-inventory",
        "charge-payment",
        "ship-order",
    ]
    assert [s.get("step_type") for s in steps] == [
        "CreateOrder",
        "ReserveInventory",
        "ChargePayment",
        "ShipOrder",
    ]
    assert [s.get("status") for s in steps] == [
        "SUCCEEDED",
        "SUCCEEDED",
        "SUCCEEDED",
        "SUCCEEDED",
    ]


def test_payments_declined_step_details(token: str) -> None:
    # Verify compensation flow reflects step states accurately after payment decline
    set_mock_fail("payments", True)
    saga_id = start_saga(token)
    data = wait_status(token, saga_id, "COMPENSATED")
    steps = data.get("steps", [])

    assert [s.get("status") for s in steps] == [
        "COMPENSATED",
        "COMPENSATED",
        "FAILED",
        "PENDING",
    ]

