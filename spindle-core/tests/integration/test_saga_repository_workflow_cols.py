from __future__ import annotations

import pytest
from sqlalchemy import text

from spindle_core.engine import SagaState, SagaStatus
from spindle_core.persistence.models import SagaModel
from spindle_core.persistence.repositories import PgSagaRepository

pytestmark = pytest.mark.e2e


def test_saga_repository_workflow_columns(scratch_db) -> None:
    session_factory = scratch_db
    repo = PgSagaRepository(session_factory)

    # a. PgSagaRepository.save() for a legacy saga (no workflow kwargs) succeeds;
    # a raw SELECT shows source='template', definition_snapshot IS NULL, input IS NULL
    # (SQL NULL, not JSON null: assert via `definition_snapshot IS NULL`).
    state_legacy = SagaState(saga_id="saga-legacy-1", status=SagaStatus.RUNNING, steps=())
    repo.save(
        "saga-legacy-1",
        state_legacy,
        [],
        expected_version=0,
        template_name="order-fulfillment",
        template_version=1,
    )

    with session_factory() as session:
        row = session.execute(
            text(
                """
                SELECT source,
                       (definition_snapshot IS NULL) AS snap_is_sql_null,
                       (input IS NULL) AS input_is_sql_null,
                       template_name,
                       template_version
                FROM sagas
                WHERE saga_id = 'saga-legacy-1'
                """
            )
        ).fetchone()
        assert row is not None
        assert row[0] == "template"
        assert row[1] is True, "definition_snapshot must be SQL NULL, not JSON null"
        assert row[2] is True, "input must be SQL NULL, not JSON null"
        assert row[3] == "order-fulfillment"
        assert row[4] == 1

    # b. save() with source='workflow', a snapshot and input succeeds; reading back
    # returns them, and sagas.template_name/version are stored.
    snapshot = {"name": "wf-checkout", "version": 2, "definition": {"steps": []}}
    input_payload = {"user_id": "u123", "total": 99.9}
    state_wf = SagaState(saga_id="saga-wf-1", status=SagaStatus.RUNNING, steps=())

    repo.save(
        "saga-wf-1",
        state_wf,
        [],
        expected_version=0,
        template_name="wf-checkout",
        template_version=2,
        source="workflow",
        definition_snapshot=snapshot,
        input_data=input_payload,
    )

    with session_factory() as session:
        wf_model = session.get(SagaModel, "saga-wf-1")
        assert wf_model is not None
        assert wf_model.source == "workflow"
        assert wf_model.definition_snapshot == snapshot
        assert wf_model.input == input_payload
        assert wf_model.template_name == "wf-checkout"
        assert wf_model.template_version == 2

    # c. a workflow save with input_data=None stores SQL NULL for input.
    state_wf_no_input = SagaState(saga_id="saga-wf-2", status=SagaStatus.RUNNING, steps=())
    repo.save(
        "saga-wf-2",
        state_wf_no_input,
        [],
        expected_version=0,
        template_name="wf-checkout",
        template_version=2,
        source="workflow",
        definition_snapshot=snapshot,
        input_data=None,
    )

    with session_factory() as session:
        row2 = session.execute(
            text(
                """
                SELECT (input IS NULL) AS input_is_sql_null,
                       (definition_snapshot IS NULL) AS snap_is_sql_null
                FROM sagas
                WHERE saga_id = 'saga-wf-2'
                """
            )
        ).fetchone()
        assert row2 is not None
        assert row2[0] is True, "input must be SQL NULL when input_data=None"
        assert row2[1] is False, "definition_snapshot must be non-null for source='workflow'"
