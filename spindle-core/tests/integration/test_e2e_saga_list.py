from __future__ import annotations

from datetime import datetime
import json
import os
import time
from typing import Any, Generator
import urllib.error
import urllib.parse
import urllib.request

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


def test_list_pagination_newest_first(token: str) -> None:
    # Verify 7 sagas paged limit=3 follow reverse creation order without duplicates
    created_ids: list[str] = []
    for _ in range(7):
        sid = start_saga(token)
        wait_status(token, sid, "COMPLETED")
        created_ids.append(sid)

    seen_items: list[dict[str, Any]] = []
    cursor: str | None = None
    pages = 0
    headers = {"Authorization": f"Bearer {token}"}

    while pages < 20:
        pages += 1
        url = f"{API_URL}/sagas?limit=3" + (f"&cursor={cursor}" if cursor else "")
        status, body = http_request(url, headers=headers)
        assert status == 200, f"GET /sagas failed with {status}: {body}"
        items = body["items"]
        seen_items.extend(items)
        cursor = body.get("next_cursor")
        if set(created_ids).issubset({it["saga_id"] for it in seen_items}):
            break
        if not cursor:
            break

    seen_ids = [it["saga_id"] for it in seen_items]
    assert len(seen_ids) == len(set(seen_ids)), "duplicate saga_id found across pages"
    filtered_ids = [it["saga_id"] for it in seen_items if it["saga_id"] in created_ids]
    assert filtered_ids == list(reversed(created_ids))

    for i in range(len(seen_items) - 1):
        t_cur = datetime.fromisoformat(seen_items[i]["created_at"])
        t_next = datetime.fromisoformat(seen_items[i + 1]["created_at"])
        assert t_cur >= t_next, f"created_at not non-increasing: {t_cur} < {t_next}"


def test_list_status_filter(token: str) -> None:
    # Verify status query param isolates COMPENSATED and COMPLETED sagas
    set_mock_fail("payments", True)
    declined_id = start_saga(token)
    wait_status(token, declined_id, "COMPENSATED")
    set_mock_fail("payments", False)

    happy_id = start_saga(token)
    wait_status(token, happy_id, "COMPLETED")

    headers = {"Authorization": f"Bearer {token}"}
    status, body = http_request(f"{API_URL}/sagas?status=COMPENSATED&limit=200", headers=headers)
    assert status == 200
    items = body["items"]
    item_ids = {it["saga_id"] for it in items}
    assert declined_id in item_ids
    assert happy_id not in item_ids
    for it in items:
        assert it["status"] == "COMPENSATED"

    status, body = http_request(f"{API_URL}/sagas?status=COMPLETED&limit=200", headers=headers)
    assert status == 200
    items = body["items"]
    item_ids = {it["saga_id"] for it in items}
    assert happy_id in item_ids
    assert declined_id not in item_ids
    for it in items:
        assert it["status"] == "COMPLETED"


def test_detail_includes_template_and_timestamps(token: str) -> None:
    # Verify template metadata and tz-aware timestamps with updated_at advancement
    happy_id = start_saga(token)
    detail = wait_status(token, happy_id, "COMPLETED")
    assert detail["template_name"] == "order-fulfillment"
    assert detail["template_version"] == 1
    c_at = datetime.fromisoformat(detail["created_at"])
    u_at = datetime.fromisoformat(detail["updated_at"])
    assert c_at.tzinfo is not None and c_at.tzinfo.utcoffset(c_at) is not None
    assert u_at.tzinfo is not None and u_at.tzinfo.utcoffset(u_at) is not None
    assert u_at > c_at


def test_list_item_matches_detail(token: str) -> None:
    # Verify list representation matches GET /sagas/{id} payload exactly
    saga_id = start_saga(token)
    detail = wait_status(token, saga_id, "COMPLETED")
    headers = {"Authorization": f"Bearer {token}"}
    status, body = http_request(f"{API_URL}/sagas?limit=200", headers=headers)
    assert status == 200
    match = next((it for it in body["items"] if it["saga_id"] == saga_id), None)
    assert match is not None, f"saga {saga_id} not found in first 200 items"
    assert match == detail


def test_list_rejects_bad_input(token: str) -> None:
    # Verify 401 unauthenticated and 422 for malformed query parameters
    status, _ = http_request(f"{API_URL}/sagas")
    assert status == 401

    headers = {"Authorization": f"Bearer {token}"}
    for query in ["status=bogus", "limit=0", "limit=201", "cursor=garbage"]:
        status, _ = http_request(f"{API_URL}/sagas?{query}", headers=headers)
        assert status == 422, f"expected 422 for {query}, got {status}"

