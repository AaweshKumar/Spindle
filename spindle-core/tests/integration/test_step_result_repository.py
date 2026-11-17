from __future__ import annotations

import pytest
from sqlalchemy import text

from spindle_core.persistence.step_result_repository import (
    ResponseTooLarge,
    StepResultRepository,
)

pytestmark = pytest.mark.e2e


def test_step_result_repository_crud_and_conflict(scratch_db) -> None:
    session_factory = scratch_db
    repo = StepResultRepository(session_factory)

    # 1. Insert parent saga row so foreign key is satisfied
    with session_factory() as session:
        with session.begin():
            session.execute(
                text(
                    """
                    INSERT INTO sagas (saga_id, state, version, status, source)
                    VALUES ('s-res-1', '{"saga_id": "s-res-1", "status": "RUNNING", "steps": []}'::jsonb, 1, 'RUNNING', 'template');
                    """
                )
            )

    # 2. First put succeeds
    inserted = repo.put("s-res-1", "step1", "execute", {"result": "ok", "count": 1})
    assert inserted is True

    # 3. Duplicate put returns False and does not overwrite
    inserted_dup = repo.put("s-res-1", "step1", "execute", {"result": "different"})
    assert inserted_dup is False

    # 4. Compensation kind for same step succeeds
    inserted_comp = repo.put("s-res-1", "step1", "compensate", {"undone": True})
    assert inserted_comp is True

    # 5. get_all returns both entries with initial content preserved
    all_results = repo.get_all("s-res-1")
    assert len(all_results) == 2
    assert all_results[("step1", "execute")] == {"result": "ok", "count": 1}
    assert all_results[("step1", "compensate")] == {"undone": True}

    # 6. get_all for nonexistent saga returns empty dict
    assert repo.get_all("nonexistent") == {}
