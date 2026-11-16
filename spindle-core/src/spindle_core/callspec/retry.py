# Component: Spindle Architecture
# File: retry.py
# Description: Retry policy, status classification, and transient error detection.

from __future__ import annotations

import http.client
import math
import socket
import urllib.error
from dataclasses import dataclass

MAX_STEP_BUDGET_SECONDS: float = 120.0
RETRYABLE_STATUS: frozenset[int] = frozenset({429, *range(500, 600)})


@dataclass(frozen=True)
class RetryPolicy:
    """Configurable backoff retry policy for outbound HTTP step executions."""

    max_attempts: int = 3
    initial_backoff_seconds: float = 0.5
    backoff_multiplier: float = 2.0
    max_backoff_seconds: float = 10.0

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int):
            raise ValueError("max_attempts must be an integer, not bool")
        if not (1 <= self.max_attempts <= 10):
            raise ValueError("max_attempts must be between 1 and 10")

        for field_name, val in [
            ("initial_backoff_seconds", self.initial_backoff_seconds),
            ("backoff_multiplier", self.backoff_multiplier),
            ("max_backoff_seconds", self.max_backoff_seconds),
        ]:
            if (
                isinstance(val, bool)
                or not isinstance(val, (int, float))
                or math.isnan(val)
                or math.isinf(val)
            ):
                raise ValueError(f"{field_name} must be a finite number")

        if not (0 < self.initial_backoff_seconds <= 60):
            raise ValueError("initial_backoff_seconds must be between 0 and 60")
        if not (1 <= self.backoff_multiplier <= 10):
            raise ValueError("backoff_multiplier must be between 1 and 10")
        if not (self.initial_backoff_seconds <= self.max_backoff_seconds <= 300):
            raise ValueError("max_backoff_seconds must be >= initial_backoff_seconds and <= 300")

    def delay_before_retry(self, failed_attempt: int) -> float:
        """Return the backoff delay in seconds following failed_attempt."""
        if isinstance(failed_attempt, bool) or not isinstance(failed_attempt, int):
            raise ValueError("failed_attempt must be an integer")
        if failed_attempt < 1 or failed_attempt >= self.max_attempts:
            raise ValueError(
                f"failed_attempt {failed_attempt} out of range [1, {self.max_attempts - 1}]"
            )
        delay = self.initial_backoff_seconds * (self.backoff_multiplier ** (failed_attempt - 1))
        return float(min(delay, self.max_backoff_seconds))

    def delays(self) -> list[float]:
        """Return list of backoff delays for each retry attempt."""
        return [self.delay_before_retry(a) for a in range(1, self.max_attempts)]

    def worst_case_seconds(self, per_attempt_timeout: float) -> float:
        """Calculate worst-case duration including all attempts and backoff delays."""
        if (
            isinstance(per_attempt_timeout, bool)
            or not isinstance(per_attempt_timeout, (int, float))
            or math.isnan(per_attempt_timeout)
            or math.isinf(per_attempt_timeout)
            or per_attempt_timeout <= 0
        ):
            raise ValueError("per_attempt_timeout must be a positive finite number")
        return float(self.max_attempts * per_attempt_timeout + sum(self.delays()))

    def fits_budget(self, per_attempt_timeout: float) -> bool:
        """Return True iff worst-case time is within MAX_STEP_BUDGET_SECONDS."""
        return self.worst_case_seconds(per_attempt_timeout) <= MAX_STEP_BUDGET_SECONDS


NO_RETRY = RetryPolicy(max_attempts=1)


def is_retryable_status(code: int) -> bool:
    """Return True if the HTTP response status code indicates a retryable condition."""
    return code in RETRYABLE_STATUS


RETRYABLE_EXCEPTIONS = (
    TimeoutError,
    ConnectionError,
    http.client.RemoteDisconnected,
    http.client.IncompleteRead,
    socket.gaierror,
)


def is_retryable_exception(exc: BaseException) -> bool:
    """Return True if an exception represents a transient network or connection failure."""
    curr: BaseException | Any = exc
    hops = 0
    while True:
        if isinstance(curr, urllib.error.HTTPError):
            return False
        if isinstance(curr, RETRYABLE_EXCEPTIONS):
            return True
        if isinstance(curr, urllib.error.URLError) and isinstance(curr.reason, BaseException):
            hops += 1
            if hops > 5:
                return False
            curr = curr.reason
        else:
            return False
