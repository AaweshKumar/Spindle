# Component: Spindle Architecture
# File: step_result_repository.py
# Description: Step result repository with insert-only semantics.

"""Repository for recording and retrieving step execution and compensation results."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from spindle_core.persistence.models import StepResultModel

MAX_RESPONSE_BYTES = 65536


class ResponseTooLarge(ValueError):
    """Raised when a step result response exceeds MAX_RESPONSE_BYTES."""


class StepResultRepository:
    """Repository for step execution and compensation results."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def put(self, saga_id: str, step_id: str, kind: str, response: Any) -> bool:
        """Insert a step result response with insert-only ON CONFLICT DO NOTHING semantics.

        Returns True if inserted, False if a result for (saga_id, step_id, kind) already existed.
        Raises ValueError for invalid kind.
        Raises ResponseTooLarge if canonical JSON exceeds 65536 bytes (message contains sizes only).
        """
        if kind not in ("execute", "compensate"):
            raise ValueError(f"invalid kind: {kind!r}")

        try:
            canonical_bytes = json.dumps(
                response, separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("response must be JSON-serializable") from exc

        if len(canonical_bytes) > MAX_RESPONSE_BYTES:
            raise ResponseTooLarge(
                f"response size {len(canonical_bytes)} bytes exceeds limit of {MAX_RESPONSE_BYTES} bytes"
            )

        stmt = text(
            """
            INSERT INTO saga_step_results (saga_id, step_id, kind, response, created_at)
            VALUES (:saga_id, :step_id, :kind, CAST(:response AS jsonb), now())
            ON CONFLICT (saga_id, step_id, kind) DO NOTHING
            RETURNING 1;
            """
        )

        with self._session_factory() as session:
            with session.begin():
                res = session.execute(
                    stmt,
                    {
                        "saga_id": saga_id,
                        "step_id": step_id,
                        "kind": kind,
                        "response": canonical_bytes.decode("utf-8"),
                    },
                ).fetchone()
                return res is not None

    def get_all(self, saga_id: str) -> dict[tuple[str, str], Any]:
        """Return all recorded step results for a saga mapped by (step_id, kind)."""
        with self._session_factory() as session:
            rows = session.execute(
                select(StepResultModel).where(StepResultModel.saga_id == saga_id)
            ).scalars().all()
            return {(r.step_id, r.kind): r.response for r in rows}
