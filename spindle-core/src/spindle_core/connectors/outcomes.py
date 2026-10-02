# Component: Spindle Architecture
# File: outcomes.py
# Description: Call-outcome (success/failure rate) tracking for the connector process.
#
#   Lives entirely in connectors/ — this is Layer-2a-owned semantic data because
#   interpreting an HTTP result as a business success or failure requires knowledge
#   of the Tapestry config (body-field checks, custom ok_status_codes, etc.).
#   Never imported by workers/, orchestrator/, or any layer below.

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass
class CallOutcome:
    """Snapshot of call counts for one step type.  Immutable once returned."""

    success_count: int = 0
    failure_count: int = 0

    @property
    def total(self) -> int:
        return self.success_count + self.failure_count

    @property
    def success_rate(self) -> float | None:
        """Success rate in [0.0, 1.0], or None if no calls have been recorded yet."""
        if self.total == 0:
            return None
        return self.success_count / self.total


class OutcomeTracker:
    """
    Thread-safe per-step-type call-outcome tracker.

    Records whether each HTTP call to a microservice ultimately succeeded or
    failed *after* applying Tapestry SuccessCheck rules (HTTP status + optional
    body-field inspection).  Provides a snapshot of accumulated counts/rates
    per step_type.

    Designed for in-process use within a single connector process.  Not
    persisted; state resets on process restart.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_step_type: dict[str, CallOutcome] = {}

    def record_success(self, step_type: str) -> None:
        with self._lock:
            self._by_step_type.setdefault(step_type, CallOutcome()).success_count += 1

    def record_failure(self, step_type: str) -> None:
        with self._lock:
            self._by_step_type.setdefault(step_type, CallOutcome()).failure_count += 1

    def snapshot(self, step_type: str) -> CallOutcome:
        """Return an isolated copy of the outcome for *step_type* (zeros if unseen)."""
        with self._lock:
            o = self._by_step_type.get(step_type, CallOutcome())
            return CallOutcome(o.success_count, o.failure_count)

    def all_step_types(self) -> list[str]:
        with self._lock:
            return list(self._by_step_type.keys())
