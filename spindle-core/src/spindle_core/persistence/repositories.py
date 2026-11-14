from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import func, select, tuple_, update
from sqlalchemy.exc import IntegrityError

from spindle_core.engine import (
    Command,
    DispatchCompensation,
    DispatchStep,
    SagaState,
    SagaStatus,
    StepState,
    StepStatus,
)
from spindle_core.orchestrator.ports import (
    ConcurrencyConflict,
    LoadedSaga,
    OutboxRecord,
)

from .models import OutboxModel, SagaModel


# -- serialisation helpers --

def _state_to_json(state: SagaState) -> dict:
    return {
        "saga_id": state.saga_id,
        "status": state.status.value,
        "steps": [
            {"step_id": s.step_id, "step_type": s.step_type, "status": s.status.value}
            for s in state.steps
        ],
    }


def _state_from_json(data: dict) -> SagaState:
    return SagaState(
        saga_id=data["saga_id"],
        status=SagaStatus(data["status"]),
        steps=tuple(
            StepState(s["step_id"], s["step_type"], StepStatus(s["status"]))
            for s in data["steps"]
        ),
    )


def _command_to_json(cmd: Command) -> dict:
    kind = type(cmd).__name__
    return {"type": kind, **asdict(cmd)}


def _command_from_json(data: dict) -> Command:
    kind = data["type"]
    if kind == "DispatchStep":
        return DispatchStep(data["saga_id"], data["step_id"], data["step_type"], data["idempotency_key"])
    if kind == "DispatchCompensation":
        return DispatchCompensation(data["saga_id"], data["step_id"], data["step_type"], data["idempotency_key"])
    raise ValueError(f"unknown command type: {kind}")


# -- summary dataclass --

@dataclass(frozen=True)
class SagaSummary:
    """Lightweight projection of a saga row for list and detail views."""

    state: SagaState
    template_name: str | None
    template_version: int | None
    created_at: datetime
    updated_at: datetime


def _row_to_summary(row: SagaModel) -> SagaSummary:
    return SagaSummary(
        state=_state_from_json(row.state),
        template_name=row.template_name,
        template_version=row.template_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# -- repository --

class PgSagaRepository:
    """Implements SagaRepository and OutboxStore protocols using SQLAlchemy sessions."""

    def __init__(self, session_factory):
        self._session_factory = session_factory

    # -- SagaRepository --

    def load(self, saga_id: str) -> LoadedSaga | None:
        with self._session_factory() as session:
            row = session.get(SagaModel, saga_id)
            if row is None:
                return None
            return LoadedSaga(state=_state_from_json(row.state), version=row.version)

    def save(
        self,
        saga_id: str,
        new_state: SagaState,
        commands: Sequence[Command],
        expected_version: int,
        *,
        template_name: str | None = None,
        template_version: int | None = None,
    ) -> None:
        """Atomically write state and commands; optionally store template metadata on insert."""
        if (template_name is None) != (template_version is None):
            raise ValueError("template_name and template_version must both be given or both omitted")
        if template_name is not None and expected_version != 0:
            raise ValueError("template metadata can only be set when creating a new saga (expected_version=0)")

        with self._session_factory() as session:
            try:
                with session.begin():
                    if expected_version == 0:
                        # insert: saga must not exist yet
                        session.add(SagaModel(
                            saga_id=saga_id,
                            state=_state_to_json(new_state),
                            version=1,
                            status=new_state.status.value,
                            template_name=template_name,
                            template_version=template_version,
                        ))
                    else:
                        # update with OCC check; bump updated_at to track transitions
                        result = session.execute(
                            update(SagaModel)
                            .where(SagaModel.saga_id == saga_id, SagaModel.version == expected_version)
                            .values(
                                state=_state_to_json(new_state),
                                version=expected_version + 1,
                                status=new_state.status.value,
                                updated_at=func.now(),
                            )
                        )
                        if result.rowcount == 0:
                            raise ConcurrencyConflict(f"saga {saga_id} version mismatch")

                    # outbox rows in the same transaction
                    for cmd in commands:
                        session.add(OutboxModel(
                            saga_id=saga_id,
                            payload=_command_to_json(cmd),
                            published=False,
                        ))
            except IntegrityError as exc:
                table = SagaModel.__table__
                pk_name = table.primary_key.name
                expected_constraint_name = pk_name if pk_name is not None else f"{table.name}_pkey"
                
                if (
                    getattr(exc.orig, "pgcode", None) == "23505"
                    and getattr(getattr(exc.orig, "diag", None), "constraint_name", None) == expected_constraint_name
                ):
                    raise ConcurrencyConflict(f"saga {saga_id} already exists") from exc
                raise

    def get_summary(self, saga_id: str) -> SagaSummary | None:
        """Return a SagaSummary for the given saga, or None if not found."""
        with self._session_factory() as session:
            row = session.get(SagaModel, saga_id)
            if row is None:
                return None
            return _row_to_summary(row)

    def list_summaries(
        self,
        *,
        status: SagaStatus | None = None,
        limit: int,
        after: tuple[datetime, str] | None = None,
    ) -> list[SagaSummary]:
        """Return up to limit sagas ordered newest first with optional keyset and status filter."""
        if not (1 <= limit <= 1000):
            raise ValueError("limit must be between 1 and 1000 inclusive")

        stmt = select(SagaModel)
        if status is not None:
            stmt = stmt.where(SagaModel.status == status.value)
        if after is not None:
            # keyset pagination: rows strictly before the cursor position
            stmt = stmt.where(
                tuple_(SagaModel.created_at, SagaModel.saga_id) < tuple_(after[0], after[1])
            )
        stmt = stmt.order_by(SagaModel.created_at.desc(), SagaModel.saga_id.desc()).limit(limit)

        with self._session_factory() as session:
            rows = session.execute(stmt).scalars().all()
            return [_row_to_summary(r) for r in rows]

    # -- OutboxStore --

    def fetch_unsent(self, limit: int) -> list[OutboxRecord]:
        """
        single relay, at-least-once, duplicates possible after a crash between publish and mark_sent.
        """
        with self._session_factory() as session:
            rows = session.execute(
                select(OutboxModel)
                .where(OutboxModel.published == False)
                .order_by(OutboxModel.id)
                .limit(limit)
            ).scalars().all()
            # detach before session closes
            return [
                OutboxRecord(id=r.id, command=_command_from_json(r.payload))
                for r in rows
            ]

    def mark_sent(self, record_id: int) -> None:
        with self._session_factory() as session:
            with session.begin():
                session.execute(
                    update(OutboxModel)
                    .where(OutboxModel.id == record_id)
                    .values(published=True)
                )
