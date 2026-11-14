from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy.dialects.postgresql import dialect as pg_dialect

from spindle_core.engine import SagaState, SagaStatus
from spindle_core.engine.saga import StepState
from spindle_core.engine.states import StepStatus
from spindle_core.persistence.models import OutboxModel, SagaModel
from spindle_core.persistence.repositories import PgSagaRepository, SagaSummary


def _make_session(rows=None):
    """Return a (session_factory, mock_session) pair; session.execute returns rows."""
    mock_session = MagicMock()
    mock_session.execute.return_value.scalars.return_value.all.return_value = rows or []
    mock_session.begin.return_value.__enter__ = MagicMock(return_value=None)
    mock_session.begin.return_value.__exit__ = MagicMock(return_value=False)
    factory = MagicMock()
    factory.return_value.__enter__.return_value = mock_session
    factory.return_value.__exit__ = MagicMock(return_value=False)
    return factory, mock_session


def _compile(stmt) -> str:
    return str(stmt.compile(dialect=pg_dialect()))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_list_summaries_base_sql() -> None:
    # Verify base query has ORDER BY DESC and LIMIT without filters
    factory, mock_session = _make_session()
    repo = PgSagaRepository(factory)
    repo.list_summaries(limit=50)
    stmt = mock_session.execute.call_args.args[0]
    sql = _compile(stmt)
    assert "ORDER BY sagas.created_at DESC, sagas.saga_id DESC" in sql
    assert "LIMIT" in sql
    assert "sagas.status =" not in sql
    assert "(sagas.created_at, sagas.saga_id) <" not in sql


def test_list_summaries_status_filter() -> None:
    # Verify status filter is included with correct bound value
    factory, mock_session = _make_session()
    repo = PgSagaRepository(factory)
    repo.list_summaries(status=SagaStatus.COMPENSATED, limit=10)
    stmt = mock_session.execute.call_args.args[0]
    compiled = stmt.compile(dialect=pg_dialect())
    sql = str(compiled)
    params = compiled.params
    assert "sagas.status =" in sql
    # Find the param value that equals COMPENSATED
    assert any(v == "COMPENSATED" for v in params.values())


def test_list_summaries_after_filter() -> None:
    # Verify keyset cursor generates tuple comparison with correct bound values
    factory, mock_session = _make_session()
    repo = PgSagaRepository(factory)
    ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    cursor_id = "saga-abc"
    repo.list_summaries(limit=25, after=(ts, cursor_id))
    stmt = mock_session.execute.call_args.args[0]
    compiled = stmt.compile(dialect=pg_dialect())
    sql = str(compiled)
    params = compiled.params
    assert "(sagas.created_at, sagas.saga_id) <" in sql
    assert ts in params.values() or any(v == ts for v in params.values())
    assert cursor_id in params.values()


def test_list_summaries_limit_validation() -> None:
    # Verify limit=0 and limit=1001 raise ValueError without touching session
    factory, mock_session = _make_session()
    repo = PgSagaRepository(factory)
    with pytest.raises(ValueError):
        repo.list_summaries(limit=0)
    with pytest.raises(ValueError):
        repo.list_summaries(limit=1001)
    mock_session.execute.assert_not_called()


def _make_saga_model(saga_id: str, status: str, steps_data: list) -> MagicMock:
    m = MagicMock(spec=SagaModel)
    m.saga_id = saga_id
    m.state = {
        "saga_id": saga_id,
        "status": status,
        "steps": steps_data,
    }
    m.status = status
    m.version = 1
    m.template_name = "order-fulfillment"
    m.template_version = 1
    m.created_at = datetime(2026, 1, 10, 0, 0, tzinfo=timezone.utc)
    m.updated_at = datetime(2026, 1, 10, 1, 0, tzinfo=timezone.utc)
    return m


def test_list_summaries_rows_to_summary() -> None:
    # Verify row mapping preserves order, parses steps, and carries metadata
    steps_data = [
        {"step_id": "step1", "step_type": "TypeA", "status": "SUCCEEDED"},
        {"step_id": "step2", "step_type": "TypeB", "status": "PENDING"},
    ]
    row1 = _make_saga_model("s1", "COMPLETED", steps_data)
    row2 = _make_saga_model("s2", "RUNNING", [])
    factory, mock_session = _make_session(rows=[row1, row2])
    repo = PgSagaRepository(factory)
    result = repo.list_summaries(limit=10)
    assert len(result) == 2
    assert result[0].state.saga_id == "s1"
    assert result[1].state.saga_id == "s2"
    assert result[0].state.steps[0].step_id == "step1"
    assert result[0].state.steps[0].status == StepStatus.SUCCEEDED
    assert result[0].template_name == "order-fulfillment"
    assert result[0].template_version == 1
    assert result[0].created_at == datetime(2026, 1, 10, 0, 0, tzinfo=timezone.utc)


def test_get_summary_missing_returns_none() -> None:
    # Verify None is returned for a missing saga_id
    factory, mock_session = _make_session()
    mock_session.get.return_value = None
    repo = PgSagaRepository(factory)
    assert repo.get_summary("nonexistent") is None


def test_get_summary_present_returns_summary() -> None:
    # Verify present row is mapped to SagaSummary correctly
    row = _make_saga_model("s1", "RUNNING", [])
    factory, mock_session = _make_session()
    mock_session.get.return_value = row
    repo = PgSagaRepository(factory)
    result = repo.get_summary("s1")
    assert isinstance(result, SagaSummary)
    assert result.state.saga_id == "s1"
    assert result.template_name == "order-fulfillment"
    assert result.template_version == 1


def test_save_insert_records_status_and_template() -> None:
    # Verify insert path sets status, template_name, template_version, version=1
    factory, mock_session = _make_session()
    repo = PgSagaRepository(factory)
    state = SagaState(saga_id="new1", status=SagaStatus.RUNNING, steps=())
    repo.save("new1", state, [], expected_version=0, template_name="order-fulfillment", template_version=1)
    added = mock_session.add.call_args.args[0]
    assert isinstance(added, SagaModel)
    assert added.status == "RUNNING"
    assert added.template_name == "order-fulfillment"
    assert added.template_version == 1
    assert added.version == 1


def test_save_update_includes_status_and_updated_at() -> None:
    # Verify update path sets status, updated_at=now(), and bumps version
    mock_result = MagicMock()
    mock_result.rowcount = 1
    factory, mock_session = _make_session()
    mock_session.execute.return_value = mock_result
    repo = PgSagaRepository(factory)
    state = SagaState(saga_id="s1", status=SagaStatus.COMPENSATING, steps=())
    repo.save("s1", state, [], expected_version=2)
    stmt = mock_session.execute.call_args.args[0]
    sql = _compile(stmt)
    # The compiled UPDATE has SET status=..., updated_at=now(), version=...
    assert "status=" in sql
    assert "updated_at=now()" in sql
    assert "version=" in sql
    # status bound value must equal the enum value
    compiled = stmt.compile(dialect=pg_dialect())
    assert any(v == "COMPENSATING" for v in compiled.params.values())


def test_save_validation_one_template_arg() -> None:
    # Verify ValueError raised with only one template arg without touching session
    factory, mock_session = _make_session()
    repo = PgSagaRepository(factory)
    state = SagaState(saga_id="s1", status=SagaStatus.RUNNING, steps=())
    with pytest.raises(ValueError):
        repo.save("s1", state, [], expected_version=0, template_name="order-fulfillment")
    with pytest.raises(ValueError):
        repo.save("s1", state, [], expected_version=0, template_version=1)
    # template given with non-zero expected_version
    with pytest.raises(ValueError):
        repo.save("s1", state, [], expected_version=1, template_name="x", template_version=1)
    factory.assert_not_called()


def test_schema_new_columns_and_indexes() -> None:
    # Verify new columns exist with correct nullability and indexes have exact names/order
    cols = {c.name: c for c in SagaModel.__table__.columns}
    for col_name in ("status", "created_at", "updated_at"):
        assert col_name in cols, f"missing column: {col_name}"
        assert not cols[col_name].nullable, f"{col_name} should be NOT NULL"
    for col_name in ("template_name", "template_version"):
        assert col_name in cols, f"missing column: {col_name}"
        assert cols[col_name].nullable, f"{col_name} should be nullable"

    idx_names = {i.name: i for i in SagaModel.__table__.indexes}
    assert "ix_sagas_created_at" in idx_names
    assert "ix_sagas_status_created_at" in idx_names
    created_at_idx_cols = [c.name for c in idx_names["ix_sagas_created_at"].columns]
    assert created_at_idx_cols == ["created_at", "saga_id"]
    status_idx_cols = [c.name for c in idx_names["ix_sagas_status_created_at"].columns]
    assert status_idx_cols == ["status", "created_at", "saga_id"]

    outbox_idxs = {i.name: i for i in OutboxModel.__table__.indexes}
    assert "ix_outbox_unpublished" in outbox_idxs
    partial_idx = outbox_idxs["ix_outbox_unpublished"]
    where = partial_idx.dialect_options.get("postgresql", {}).get("where")
    assert where is not None, "ix_outbox_unpublished must have a postgresql_where clause"
