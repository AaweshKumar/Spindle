import pytest
from unittest.mock import MagicMock
from sqlalchemy.exc import IntegrityError
from spindle_core.persistence.repositories import PgSagaRepository, SagaModel
from spindle_core.orchestrator.ports import ConcurrencyConflict
from spindle_core.engine import SagaState, SagaStatus

def test_save_duplicate_raises_concurrency_conflict():
    mock_session = MagicMock()
    mock_session.begin.return_value.__enter__ = MagicMock()
    
    table = SagaModel.__table__
    pk_name = table.primary_key.name
    constraint_name = pk_name if pk_name is not None else f"{table.name}_pkey"
    
    class FakeOrig:
        pgcode = "23505"
        class Diag:
            pass
        diag = Diag()
        diag.constraint_name = constraint_name
    
    exc = IntegrityError("test", {}, FakeOrig())
    mock_session.begin.return_value.__exit__ = MagicMock(side_effect=exc)
    
    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__enter__.return_value = mock_session
    
    repo = PgSagaRepository(mock_session_factory)
    state = SagaState("s1", SagaStatus.RUNNING, ())
    
    with pytest.raises(ConcurrencyConflict) as exc_info:
        repo.save("s1", state, [], 0)
        
    assert exc_info.value.__cause__ is exc

def test_save_different_pgcode_propagates():
    mock_session = MagicMock()
    mock_session.begin.return_value.__enter__ = MagicMock()
    
    table = SagaModel.__table__
    pk_name = table.primary_key.name
    constraint_name = pk_name if pk_name is not None else f"{table.name}_pkey"
    
    class FakeOrig:
        pgcode = "11111"
        class Diag:
            pass
        diag = Diag()
        diag.constraint_name = constraint_name
        
    exc = IntegrityError("test", {}, FakeOrig())
    mock_session.begin.return_value.__exit__ = MagicMock(side_effect=exc)
    
    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__enter__.return_value = mock_session
    
    repo = PgSagaRepository(mock_session_factory)
    state = SagaState("s1", SagaStatus.RUNNING, ())
    
    with pytest.raises(IntegrityError):
        repo.save("s1", state, [], 0)

def test_save_different_constraint_propagates():
    mock_session = MagicMock()
    mock_session.begin.return_value.__enter__ = MagicMock()
    
    class FakeOrig:
        pgcode = "23505"
        class Diag:
            pass
        diag = Diag()
        diag.constraint_name = "other_constraint"
        
    exc = IntegrityError("test", {}, FakeOrig())
    mock_session.begin.return_value.__exit__ = MagicMock(side_effect=exc)
    
    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__enter__.return_value = mock_session
    
    repo = PgSagaRepository(mock_session_factory)
    state = SagaState("s1", SagaStatus.RUNNING, ())
    
    with pytest.raises(IntegrityError):
        repo.save("s1", state, [], 0)

def test_expected_version_greater_than_0_path_unchanged():
    mock_session = MagicMock()
    mock_session.begin.return_value.__enter__ = MagicMock()
    
    mock_result = MagicMock()
    mock_result.rowcount = 0
    mock_session.execute.return_value = mock_result
    
    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__enter__.return_value = mock_session
    
    repo = PgSagaRepository(mock_session_factory)
    state = SagaState("s1", SagaStatus.RUNNING, ())
    
    with pytest.raises(ConcurrencyConflict) as exc_info:
        repo.save("s1", state, [], 1)
    
    assert "version mismatch" in str(exc_info.value)

def test_fetch_unsent_has_no_for_update():
    from sqlalchemy.dialects import postgresql
    
    mock_session = MagicMock()
    mock_session.execute.return_value.scalars.return_value.all.return_value = []
    
    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__enter__.return_value = mock_session
    
    repo = PgSagaRepository(mock_session_factory)
    repo.fetch_unsent(10)
    
    stmt = mock_session.execute.call_args[0][0]
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    
    assert "FOR UPDATE" not in sql
    assert "published = false" in sql.lower()
    assert "ORDER BY outbox.id" in sql
    assert "LIMIT" in sql.upper()


def test_save_validation_source_and_snapshot():
    mock_session_factory = MagicMock()
    repo = PgSagaRepository(mock_session_factory)
    state = SagaState("s1", SagaStatus.RUNNING, ())

    # Invalid source
    with pytest.raises(ValueError, match="invalid source"):
        repo.save("s1", state, [], 0, source="unknown")

    # Workflow without snapshot
    with pytest.raises(ValueError, match="definition_snapshot is required when source='workflow'"):
        repo.save("s1", state, [], 0, source="workflow", definition_snapshot=None)

    # Template with snapshot
    with pytest.raises(ValueError, match="definition_snapshot cannot be set when source='template'"):
        repo.save("s1", state, [], 0, source="template", definition_snapshot={"steps": []})

    # Non-zero expected_version with workflow metadata
    with pytest.raises(ValueError, match="workflow metadata can only be set when creating a new saga"):
        repo.save("s1", state, [], 1, source="workflow", definition_snapshot={"steps": []})

    with pytest.raises(ValueError, match="workflow metadata can only be set when creating a new saga"):
        repo.save("s1", state, [], 1, input_data={"foo": "bar"})


def test_save_workflow_stores_fields_on_create():
    mock_session = MagicMock()
    mock_session.begin.return_value.__enter__ = MagicMock()
    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__enter__.return_value = mock_session

    repo = PgSagaRepository(mock_session_factory)
    state = SagaState("s1", SagaStatus.RUNNING, ())

    snapshot = {"name": "wf", "version": 1, "definition": {}}
    input_payload = {"key": "value"}

    repo.save(
        "s1",
        state,
        [],
        0,
        template_name="wf",
        template_version=1,
        source="workflow",
        definition_snapshot=snapshot,
        input_data=input_payload,
    )

    added = mock_session.add.call_args[0][0]
    assert isinstance(added, SagaModel)
    assert added.saga_id == "s1"
    assert added.template_name == "wf"
    assert added.template_version == 1
    assert added.source == "workflow"
    assert added.definition_snapshot == snapshot
    assert added.input == input_payload

