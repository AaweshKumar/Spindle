# Component: Spindle Architecture
# File: cursor.py
# Description: Keyset cursor encoding and decoding for saga pagination.

"""Opaque keyset cursor, NOT signed."""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
import json


class InvalidCursor(ValueError):
    """Raised when a cursor string cannot be parsed or validated."""


def encode_cursor(created_at: datetime, saga_id: str) -> str:
    """Encode an aware datetime and saga id into a URL-safe base64 cursor."""
    if created_at.tzinfo is None or created_at.tzinfo.utcoffset(created_at) is None:
        raise ValueError("created_at must be timezone-aware")
    payload = json.dumps([created_at.isoformat(), saga_id], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(raw: str) -> tuple[datetime, str]:
    """Decode and validate a URL-safe base64 cursor into an aware datetime and saga id."""
    if not isinstance(raw, str) or not raw or len(raw) > 512:
        raise InvalidCursor("Cursor must be a non-empty string under 512 characters")

    pad_len = (4 - len(raw) % 4) % 4
    padded = raw + ("=" * pad_len)

    try:
        payload = base64.b64decode(padded, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidCursor(f"Invalid base64 payload: {exc}") from None

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidCursor(f"Payload is not valid UTF-8: {exc}") from None

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidCursor(f"Payload is not valid JSON: {exc}") from None

    if not isinstance(data, list) or len(data) != 2:
        raise InvalidCursor("Cursor JSON must be a list of exactly 2 elements")

    if not isinstance(data[0], str) or not isinstance(data[1], str):
        raise InvalidCursor("Cursor JSON list elements must both be strings")

    saga_id = data[1]
    if not saga_id or len(saga_id) > 64:
        raise InvalidCursor("saga_id in cursor must be between 1 and 64 characters")

    try:
        ts = datetime.fromisoformat(data[0])
    except (ValueError, TypeError) as exc:
        raise InvalidCursor(f"Timestamp in cursor could not be parsed: {exc}") from None

    if ts.tzinfo is None or ts.tzinfo.utcoffset(ts) is None:
        raise InvalidCursor("Timestamp in cursor must be timezone-aware")

    return ts, saga_id

