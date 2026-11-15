import http.client
import math
import socket
import ssl
from urllib.error import HTTPError, URLError

import pytest

from spindle_core.callspec.placeholders import PlaceholderError
from spindle_core.callspec.retry import (
    NO_RETRY,
    RetryPolicy,
    is_retryable_exception,
    is_retryable_status,
)


def test_retry_policy_defaults():
    policy = RetryPolicy()
    assert policy.delays() == [0.5, 1.0]
    assert policy.delay_before_retry(1) == 0.5
    assert policy.delay_before_retry(2) == 1.0
    assert policy.worst_case_seconds(30) == 91.5
    assert policy.fits_budget(30) is True
    assert policy.fits_budget(60) is False


def test_retry_policy_cap():
    policy = RetryPolicy(
        max_attempts=6,
        initial_backoff_seconds=1,
        backoff_multiplier=3,
        max_backoff_seconds=5,
    )
    assert policy.delays() == [1.0, 3.0, 5.0, 5.0, 5.0]


def test_no_retry_policy():
    assert NO_RETRY.delays() == []
    assert NO_RETRY.worst_case_seconds(10) == 10.0
    assert NO_RETRY.fits_budget(60) is True
    assert NO_RETRY.fits_budget(121) is False


@pytest.mark.parametrize("attempt", [0, -1, 3])
def test_delay_before_retry_out_of_range(attempt: int):
    policy = RetryPolicy(max_attempts=3)
    with pytest.raises(ValueError):
        policy.delay_before_retry(attempt)


@pytest.mark.parametrize(
    ("kwargs", "field_name"),
    [
        ({"max_attempts": 0}, "max_attempts"),
        ({"max_attempts": 11}, "max_attempts"),
        ({"max_attempts": True}, "max_attempts"),
        ({"max_attempts": 1.5}, "max_attempts"),
        ({"initial_backoff_seconds": 0}, "initial_backoff_seconds"),
        ({"initial_backoff_seconds": -1}, "initial_backoff_seconds"),
        ({"initial_backoff_seconds": 61}, "initial_backoff_seconds"),
        ({"initial_backoff_seconds": float("nan")}, "initial_backoff_seconds"),
        ({"initial_backoff_seconds": float("inf")}, "initial_backoff_seconds"),
        ({"backoff_multiplier": 0.9}, "backoff_multiplier"),
        ({"backoff_multiplier": 11}, "backoff_multiplier"),
        ({"initial_backoff_seconds": 10.0, "max_backoff_seconds": 5.0}, "max_backoff_seconds"),
        ({"max_backoff_seconds": 301}, "max_backoff_seconds"),
    ],
)
def test_invalid_policy_raises_naming_field(kwargs: dict, field_name: str):
    with pytest.raises(ValueError) as exc_info:
        RetryPolicy(**kwargs)
    assert field_name in str(exc_info.value)


@pytest.mark.parametrize("timeout", [0, -1])
def test_worst_case_seconds_invalid_timeout(timeout):
    policy = RetryPolicy()
    with pytest.raises(ValueError):
        policy.worst_case_seconds(timeout)


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 599])
def test_is_retryable_status_true(status: int):
    assert is_retryable_status(status) is True


@pytest.mark.parametrize(
    "status",
    [200, 204, 301, 307, 400, 401, 403, 404, 408, 409, 422, 428, 600],
)
def test_is_retryable_status_false(status: int):
    assert is_retryable_status(status) is False


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError(),
        socket.timeout(),
        ConnectionRefusedError(),
        ConnectionResetError(),
        BrokenPipeError(),
        http.client.RemoteDisconnected("x"),
        http.client.IncompleteRead(b"a", 5),
        socket.gaierror(-2, "x"),
        URLError(socket.timeout()),
        URLError(ConnectionRefusedError()),
        URLError(URLError(TimeoutError())),
    ],
)
def test_is_retryable_exception_true(exc: BaseException):
    assert is_retryable_exception(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("x"),
        KeyError("k"),
        RuntimeError("x"),
        URLError("unknown url type"),
        HTTPError("http://x", 503, "msg", {}, None),
        ssl.SSLCertVerificationError(),
        PlaceholderError("x"),
    ],
)
def test_is_retryable_exception_false(exc: BaseException):
    assert is_retryable_exception(exc) is False
