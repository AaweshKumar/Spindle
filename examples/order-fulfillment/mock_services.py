# Component: Spindle Architecture
# File: mock_services.py
# Description: Mock external shop services for order-fulfillment example.

"""Mock HTTP services for the order-fulfillment saga example.

Simulates four shops: orders, inventory, payments, and shipping.
Supports idempotency keys, failure simulation toggles, and compensation undo routes.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
from collections import deque
from collections.abc import Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger(__name__)

SHOPS: tuple[str, ...] = ("orders", "inventory", "payments", "shipping")

FORWARD_PATHS: dict[str, tuple[str, str]] = {
    "/orders/create": ("orders", "created"),
    "/inventory/reserve": ("inventory", "reserved"),
    "/payments/charge": ("payments", "charged"),
    "/shipping/ship": ("shipping", "shipped"),
}

UNDO_PATHS: dict[str, str] = {
    "/orders/cancel": "orders",
    "/inventory/release": "inventory",
    "/payments/refund": "payments",
    "/shipping/cancel": "shipping",
}

ALL_GET_PATHS: frozenset[str] = frozenset({"/health", "/admin/state"})
ALL_POST_PATHS: frozenset[str] = frozenset(
    set(FORWARD_PATHS.keys()) | set(UNDO_PATHS.keys()) | {"/admin/fail", "/admin/reset"}
)
ALL_PATHS: frozenset[str] = frozenset(ALL_GET_PATHS | ALL_POST_PATHS)


class MockState:
    """In-memory state for mock shop services guarded by a single lock."""

    def __init__(self, fail_shops: Iterable[str] = ()) -> None:
        self.lock = threading.Lock()
        self.initial_fail_shops: set[str] = set(fail_shops)
        self.fail_shops: set[str] = set(fail_shops)
        self.shops: dict[str, dict] = {
            shop: {
                "active": set(),
                "applied_count": 0,
                "undone_count": 0,
                "replay_count": 0,
            }
            for shop in SHOPS
        }
        self.ticket_book: dict[tuple[str, str], tuple[int, dict]] = {}
        self.calls: deque[dict] = deque(maxlen=500)
        self._seq: int = 0

    def next_seq(self) -> int:
        """Increment and return the next call sequence number."""
        self._seq += 1
        return self._seq

    def reset(self) -> None:
        """Clear all active state, ticket book, and calls, restoring initial failure set."""
        self.fail_shops = set(self.initial_fail_shops)
        for shop in SHOPS:
            self.shops[shop] = {
                "active": set(),
                "applied_count": 0,
                "undone_count": 0,
                "replay_count": 0,
            }
        self.ticket_book.clear()
        self.calls.clear()


class MockRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for mock services."""

    state: MockState

    def log_message(self, format: str, *args: object) -> None:
        """Override default stderr logging to keep http.server quiet."""
        pass

    def _send_json(self, status_code: int, data: dict) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        logger.info("%s %s -> %s", self.command, self.path, status_code)

    def _read_json_body(self) -> dict | None:
        """Validate Content-Length and parse JSON body, sending error response on failure."""
        cl_header = self.headers.get("Content-Length")
        if cl_header is None:
            self._send_json(411, {"error": "Content-Length header required"})
            return None
        try:
            length = int(cl_header)
            if length < 0:
                raise ValueError
        except ValueError:
            self._send_json(400, {"error": "Invalid Content-Length"})
            return None

        if length > 65536:
            self._send_json(413, {"error": "Payload too large"})
            return None

        raw_body = self.rfile.read(length)
        try:
            data = json.loads(raw_body.decode("utf-8"))
        except Exception:
            self._send_json(400, {"error": "Invalid JSON"})
            return None

        if not isinstance(data, dict):
            self._send_json(400, {"error": "Body must be a JSON object"})
            return None
        return data

    def do_GET(self) -> None:
        """Handle GET requests."""
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
            return

        if self.path == "/admin/state":
            with self.state.lock:
                data = {
                    "fail": sorted(list(self.state.fail_shops)),
                    "shops": {
                        shop: {
                            "active": sorted(list(self.state.shops[shop]["active"])),
                            "applied_count": self.state.shops[shop]["applied_count"],
                            "undone_count": self.state.shops[shop]["undone_count"],
                            "replay_count": self.state.shops[shop]["replay_count"],
                        }
                        for shop in SHOPS
                    },
                    "calls": list(self.state.calls),
                }
            self._send_json(200, data)
            return

        if self.path in ALL_POST_PATHS:
            self._send_json(405, {"error": "Method not allowed"})
            return

        self._send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        """Handle POST requests."""
        if self.path in FORWARD_PATHS:
            self._handle_forward()
            return

        if self.path in UNDO_PATHS:
            self._handle_undo()
            return

        if self.path == "/admin/fail":
            self._handle_admin_fail()
            return

        if self.path == "/admin/reset":
            with self.state.lock:
                self.state.reset()
            self._send_json(200, {"status": "reset"})
            return

        if self.path in ALL_GET_PATHS:
            self._send_json(405, {"error": "Method not allowed"})
            return

        self._send_json(404, {"error": "Not found"})

    def do_PUT(self) -> None:
        """Handle PUT requests as 405 or 404."""
        self._handle_wrong_method()

    def do_DELETE(self) -> None:
        """Handle DELETE requests as 405 or 404."""
        self._handle_wrong_method()

    def _handle_wrong_method(self) -> None:
        if self.path in ALL_PATHS:
            self._send_json(405, {"error": "Method not allowed"})
        else:
            self._send_json(404, {"error": "Not found"})

    def _handle_forward(self) -> None:
        body = self._read_json_body()
        if body is None:
            return

        order_id = body.get("order_id")
        if not isinstance(order_id, str) or not order_id:
            self._send_json(400, {"error": "order_id must be a non-empty string"})
            return

        key = self.headers.get("Idempotency-Key")
        if not key or not key.strip():
            self._send_json(400, {"error": "Idempotency-Key header is required"})
            return
        key = key.strip()

        shop, success_word = FORWARD_PATHS[self.path]
        with self.state.lock:
            cached = self.state.ticket_book.get((self.path, key))
            if cached is not None:
                status_code, resp_body = cached
                self.state.shops[shop]["replay_count"] += 1
                self.state.calls.append({
                    "seq": self.state.next_seq(),
                    "path": self.path,
                    "order_id": order_id,
                    "key": key,
                    "status": status_code,
                    "replay": True,
                })
                self._send_json(status_code, resp_body)
                return

            if shop in self.state.fail_shops:
                if shop == "payments":
                    status_code = 200
                    resp_body = {"status": "declined", "order_id": order_id}
                else:
                    status_code = 500
                    resp_body = {"status": "error", "order_id": order_id}
                self.state.calls.append({
                    "seq": self.state.next_seq(),
                    "path": self.path,
                    "order_id": order_id,
                    "key": key,
                    "status": status_code,
                    "replay": False,
                })
                self._send_json(status_code, resp_body)
                return

            # Apply
            shop_data = self.state.shops[shop]
            if order_id not in shop_data["active"]:
                shop_data["active"].add(order_id)
                shop_data["applied_count"] += 1
            status_code = 200
            resp_body = {"status": success_word, "order_id": order_id}
            self.state.ticket_book[(self.path, key)] = (status_code, resp_body)
            self.state.calls.append({
                "seq": self.state.next_seq(),
                "path": self.path,
                "order_id": order_id,
                "key": key,
                "status": status_code,
                "replay": False,
            })
            self._send_json(status_code, resp_body)

    def _handle_undo(self) -> None:
        body = self._read_json_body()
        if body is None:
            return

        order_id = body.get("order_id")
        if not isinstance(order_id, str) or not order_id:
            self._send_json(400, {"error": "order_id must be a non-empty string"})
            return

        key = self.headers.get("Idempotency-Key")
        if key is not None:
            key = key.strip()

        shop = UNDO_PATHS[self.path]
        with self.state.lock:
            shop_data = self.state.shops[shop]
            if order_id in shop_data["active"]:
                shop_data["active"].remove(order_id)
                shop_data["undone_count"] += 1
            status_code = 200
            resp_body = {"status": "undone", "order_id": order_id}
            self.state.calls.append({
                "seq": self.state.next_seq(),
                "path": self.path,
                "order_id": order_id,
                "key": key,
                "status": status_code,
                "replay": False,
            })
            self._send_json(status_code, resp_body)

    def _handle_admin_fail(self) -> None:
        body = self._read_json_body()
        if body is None:
            return

        shop = body.get("shop")
        enabled = body.get("enabled")
        if shop not in SHOPS or type(enabled) is not bool:
            self._send_json(400, {"error": "Invalid shop or enabled value"})
            return

        with self.state.lock:
            if enabled:
                self.state.fail_shops.add(shop)
            else:
                self.state.fail_shops.discard(shop)
            self._send_json(200, {"status": "ok", "fail": sorted(list(self.state.fail_shops))})


class _MockHTTPServer(ThreadingHTTPServer):
    # Default backlog of 5 drops simultaneous connects under concurrency
    request_queue_size = 128


def create_server(host: str, port: int, fail_shops: Iterable[str] = ()) -> ThreadingHTTPServer:
    """Create and return a ThreadingHTTPServer configured with mock shop services."""
    state = MockState(fail_shops)

    class BoundHandler(MockRequestHandler):
        pass

    BoundHandler.state = state
    server = _MockHTTPServer((host, port), BoundHandler)
    server.state = state  # type: ignore[attr-defined]
    return server


def main() -> int:
    """Run mock services server configured from environment variables."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    host = os.environ.get("MOCK_HOST", "0.0.0.0")
    port_str = os.environ.get("MOCK_PORT", "9000")
    try:
        port = int(port_str)
        if not (1 <= port <= 65535):
            raise ValueError
    except ValueError:
        sys.stderr.write(f"Invalid MOCK_PORT: {port_str}\n")
        return 2

    fail_env = os.environ.get("MOCK_FAIL", "")
    fail_shops: list[str] = []
    if fail_env.strip():
        for s in fail_env.split(","):
            s = s.strip()
            if not s:
                continue
            if s not in SHOPS:
                sys.stderr.write(f"Unknown shop in MOCK_FAIL: {s}\n")
                return 2
            fail_shops.append(s)

    server = create_server(host, port, fail_shops)

    def _sig_handler(signum: int, frame: object) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)

    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

