import base64
from datetime import datetime, timedelta, timezone

import pytest

from spindle_core.api.cursor import InvalidCursor, decode_cursor, encode_cursor


def test_round_trip_preserves_instant_and_microseconds() -> None:
    # Verify exact round-trip preserves microseconds and timezone instant
    dt = datetime(2026, 11, 14, 8, 30, 45, 123456, tzinfo=timezone.utc)
    cursor = encode_cursor(dt, "saga-123")
    decoded_dt, decoded_id = decode_cursor(cursor)
    assert decoded_dt == dt
    assert decoded_dt.microsecond == 123456
    assert decoded_id == "saga-123"


def test_round_trip_non_utc_offset() -> None:
    # Verify non-UTC timezone offsets round-trip to the same instant
    offset = timezone(timedelta(hours=5, minutes=30))
    dt = datetime(2026, 11, 14, 8, 30, 45, 654321, tzinfo=offset)
    cursor = encode_cursor(dt, "saga-offset")
    decoded_dt, decoded_id = decode_cursor(cursor)
    assert decoded_dt == dt
    assert decoded_id == "saga-offset"


def test_encoded_value_url_safe_no_padding() -> None:
    # Verify encoded string is url-safe and strips standard base64 padding
    dt = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    cursor = encode_cursor(dt, "saga-1")
    assert "+" not in cursor
    assert "/" not in cursor
    assert "=" not in cursor


def test_encode_naive_datetime_raises_value_error() -> None:
    # Verify encoding rejects naive datetimes without timezone info
    naive = datetime(2026, 1, 1, 0, 0, 0)
    with pytest.raises(ValueError):
        encode_cursor(naive, "saga-1")


@pytest.mark.parametrize(
    "invalid_raw",
    [
        "",
        "not base64!",
        base64.urlsafe_b64encode(b"\xff\xfe\xfd").decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(b"not json text").decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(b'{"key": "value"}').decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(b'["2026-01-01T00:00:00+00:00"]').decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(b'["2026-01-01T00:00:00+00:00", "id", "extra"]').decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(b'[1, "x"]').decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(b'["2026-01-01T00:00:00+00:00", 5]').decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(b'["2026-01-01T00:00:00", "x"]').decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(b'["not-a-timestamp", "x"]').decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(b'["2026-01-01T00:00:00+00:00", ""]').decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(b'["2026-01-01T00:00:00+00:00", "' + b"a" * 65 + b'"]').decode("ascii").rstrip("="),
        "a" * 600,
    ],
)
def test_decode_invalid_inputs_raise_invalid_cursor(invalid_raw: str) -> None:
    # Verify malformed or out-of-spec cursor strings always raise InvalidCursor
    with pytest.raises(InvalidCursor):
        decode_cursor(invalid_raw)


def test_hand_built_valid_cursor() -> None:
    # Verify hand-crafted URL-safe base64 json cursor decodes as expected
    raw = base64.urlsafe_b64encode(b'["2026-05-01T12:00:00+00:00","saga-handbuilt"]').decode().rstrip("=")
    ts, sid = decode_cursor(raw)
    assert ts == datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert sid == "saga-handbuilt"

