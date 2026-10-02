# Component: Spindle Architecture
# File: caller.py
# Description: HTTP request builder/sender and response success checker for
#   the Spindle connector.  Builds a request from a Tapestry RequestConfig,
#   sends it, and evaluates the response against a SuccessCheck.
#
#   Uses a lightweight HttpClient Protocol so the caller is unit-testable
#   without a live HTTP server.  Default implementation uses stdlib
#   urllib.request — no new dependencies.

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from spindle_core.tapestry.models import RequestConfig, SuccessCheck

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Minimal HTTP abstraction (Protocol) — keeps the caller testable
# ---------------------------------------------------------------------------

@dataclass
class HttpResponse:
    """Minimal HTTP response wrapper shared across implementations."""
    status_code: int
    body: bytes

    def json(self) -> dict:
        return json.loads(self.body)

    def text(self) -> str:
        return self.body.decode(errors="replace")


@runtime_checkable
class HttpClient(Protocol):
    """
    Minimal HTTP client protocol.  Concrete implementations swap freely
    (urllib, httpx, requests, test doubles) without touching call sites.
    """

    def request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> HttpResponse: ...


class UrllibHttpClient:
    """
    Default HttpClient implementation using stdlib urllib.request.
    Zero new dependencies.
    """

    def request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> HttpResponse:
        req = urllib.request.Request(url, data=body, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return HttpResponse(status_code=resp.status, body=resp.read())
        except urllib.error.HTTPError as exc:
            # HTTPError is an IOBase so we can read the body for diagnostics.
            return HttpResponse(status_code=exc.code, body=exc.read())


# ---------------------------------------------------------------------------
# Request building
# ---------------------------------------------------------------------------

def build_request_body(config: RequestConfig, message_fields: dict[str, str]) -> bytes:
    """
    Produce the JSON-encoded HTTP request body from *message_fields*.

    If config.field_mapping is non-empty, only the mapped keys are included
    (http_body_key ← message_fields[source_field]).  Missing source fields
    become the empty string rather than raising.

    If config.field_mapping is empty, the full *message_fields* dict is
    forwarded as-is.
    """
    if config.field_mapping:
        body_dict: dict[str, str] = {
            dest: message_fields.get(src, "")
            for dest, src in config.field_mapping.items()
        }
    else:
        body_dict = dict(message_fields)
    return json.dumps(body_dict).encode()


# ---------------------------------------------------------------------------
# Response success detection
# ---------------------------------------------------------------------------

def is_success(response: HttpResponse, check: SuccessCheck) -> bool:
    """
    Return True iff *response* passes all criteria in *check*.

    Evaluation order:
    1. HTTP status must be in check.ok_status_codes (fast path).
    2. If check.body_field is set, the JSON response body is parsed and
       the field inspected per check.body_field_value semantics.
    """
    if response.status_code not in check.ok_status_codes:
        return False

    if check.body_field is None:
        return True

    try:
        body = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning(
            "response body is not valid JSON; treating as failure "
            "(status=%d, body_prefix=%r)",
            response.status_code,
            response.body[:100],
        )
        return False

    value = body.get(check.body_field)
    if check.body_field_value is not None:
        return str(value) == check.body_field_value
    # body_field_value is None → field must be present and truthy
    return bool(value)


# ---------------------------------------------------------------------------
# Entry point used by the consumer
# ---------------------------------------------------------------------------

def call_endpoint(
    config: RequestConfig,
    message_fields: dict[str, str],
    client: HttpClient,
) -> HttpResponse:
    """Build and send the HTTP request described by *config*."""
    body = build_request_body(config, message_fields)
    headers = {"Content-Type": "application/json", **config.headers}
    logger.debug("HTTP %s %s", config.method, config.url)
    return client.request(
        config.method,
        config.url,
        body=body,
        headers=headers,
        timeout=config.timeout_seconds,
    )
