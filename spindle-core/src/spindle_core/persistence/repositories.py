import json
from collections.abc import Sequence
from dataclasses import asdict

from sqlalchemy import select, update
from sqlalchemy.orm import Session as SASession

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
    ) -> None:
        with self._session_factory() as session:
            with session.begin():
                if expected_version == 0:
                    # insert: saga must not exist yet
                    session.add(SagaModel(
                        saga_id=saga_id,
                        state=_state_to_json(new_state),
                        version=1,
                    ))
                else:
                    # update with OCC check
                    result = session.execute(
                        update(SagaModel)
                        .where(SagaModel.saga_id == saga_id, SagaModel.version == expected_version)
                        .values(state=_state_to_json(new_state), version=expected_version + 1)
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

    # -- OutboxStore --

    def fetch_unsent(self, limit: int) -> list[OutboxRecord]:
        with self._session_factory() as session:
            rows = session.execute(
                select(OutboxModel)
                .where(OutboxModel.published == False)
                .order_by(OutboxModel.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
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
