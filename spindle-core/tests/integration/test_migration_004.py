"""Integration test for Alembic migration 004.

Verifies that migration 004 applies cleanly to a database containing legacy-style
sagas rows, enforces CHECK constraints and foreign keys, survives downgrade,
and re-applies cleanly.
"""

from __future__ import annotations

import os
import uuid
import psycopg2
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

def _get_pg_env() -> dict[str, str]:
    return {
        "user": os.environ.get("POSTGRES_USER", "spindle"),
        "password": os.environ.get("POSTGRES_PASSWORD", "spindle-dev-only"),
        "host": os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        "port": os.environ.get("POSTGRES_PORT", "5433"),
        "maint_db": os.environ.get("POSTGRES_DB", "spindle"),
    }


pytestmark = pytest.mark.e2e


def test_migration_004_legacy_row_and_round_trip() -> None:
    env = _get_pg_env()
    maint_url = (
        f"postgresql://{env['user']}:{env['password']}@{env['host']}:{env['port']}/{env['maint_db']}"
    )

    try:
        conn = psycopg2.connect(maint_url, connect_timeout=3)
        conn.autocommit = True
    except Exception as exc:
        pytest.skip(f"Postgres server not reachable on {env['host']}:{env['port']}: {exc}")

    scratch_name = f"scratch_mig004_{uuid.uuid4().hex[:8]}"
    cur = conn.cursor()
    cur.execute(f"CREATE DATABASE {scratch_name};")
    conn.close()

    scratch_sa_url = (
        f"postgresql+psycopg2://{env['user']}:{env['password']}@{env['host']}:{env['port']}/{scratch_name}"
    )
    old_db_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = scratch_sa_url

    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    cfg.config_file_name = None
    script = ScriptDirectory.from_config(cfg)
    assert script.get_current_head() == "004"

    try:
        # 1. Upgrade to 003 first
        command.upgrade(cfg, "003")

        # 2. Insert a legacy row (no source, definition_snapshot, or input columns exist yet)
        pg_conn = psycopg2.connect(
            f"postgresql://{env['user']}:{env['password']}@{env['host']}:{env['port']}/{scratch_name}"
        )
        pg_conn.autocommit = True
        c = pg_conn.cursor()
        c.execute(
            """
            INSERT INTO sagas (saga_id, state, version, status, template_name, template_version)
            VALUES ('legacy-saga-1', '{"saga_id": "legacy-saga-1", "status": "RUNNING", "steps": []}'::jsonb, 1, 'RUNNING', 'order-fulfillment', 1);
            """
        )

        # 3. Upgrade to head (004)
        command.upgrade(cfg, "head")

        # 4. Verify legacy row has default source='template' and NULL snapshot/input
        c.execute("SELECT source, definition_snapshot, input FROM sagas WHERE saga_id = 'legacy-saga-1';")
        row = c.fetchone()
        assert row is not None
        assert row[0] == "template"
        assert row[1] is None
        assert row[2] is None

        # 5. Verify check constraint: invalid source rejected
        with pytest.raises(psycopg2.IntegrityError):
            c.execute(
                """
                INSERT INTO sagas (saga_id, state, version, status, source)
                VALUES ('bad-source', '{"saga_id": "bad-source", "status": "RUNNING", "steps": []}'::jsonb, 1, 'RUNNING', 'invalid');
                """
            )

        # 6. Verify check constraint: workflow requires definition_snapshot
        with pytest.raises(psycopg2.IntegrityError):
            c.execute(
                """
                INSERT INTO sagas (saga_id, state, version, status, source, definition_snapshot)
                VALUES ('bad-wf-no-snap', '{"saga_id": "bad-wf-no-snap", "status": "RUNNING", "steps": []}'::jsonb, 1, 'RUNNING', 'workflow', NULL);
                """
            )

        # 7. Verify check constraint: template cannot have definition_snapshot
        with pytest.raises(psycopg2.IntegrityError):
            c.execute(
                """
                INSERT INTO sagas (saga_id, state, version, status, source, definition_snapshot)
                VALUES ('bad-tmpl-with-snap', '{"saga_id": "bad-tmpl-with-snap", "status": "RUNNING", "steps": []}'::jsonb, 1, 'RUNNING', 'template', '{"steps": []}'::jsonb);
                """
            )

        # 8. Valid workflow saga insert
        c.execute(
            """
            INSERT INTO sagas (saga_id, state, version, status, source, definition_snapshot, input)
            VALUES ('wf-saga-1', '{"saga_id": "wf-saga-1", "status": "RUNNING", "steps": []}'::jsonb, 1, 'RUNNING', 'workflow', '{"steps": []}'::jsonb, '{"param": 1}'::jsonb);
            """
        )

        # 9. Verify saga_step_results insert and kind constraint
        c.execute(
            """
            INSERT INTO saga_step_results (saga_id, step_id, kind, response)
            VALUES ('wf-saga-1', 'step-1', 'execute', '{"status": 200}'::jsonb);
            """
        )
        with pytest.raises(psycopg2.IntegrityError):
            c.execute(
                """
                INSERT INTO saga_step_results (saga_id, step_id, kind, response)
                VALUES ('wf-saga-1', 'step-1', 'invalid-kind', '{"status": 200}'::jsonb);
                """
            )

        # 10. Verify FK cascade
        c.execute("DELETE FROM sagas WHERE saga_id = 'wf-saga-1';")
        c.execute("SELECT count(*) FROM saga_step_results WHERE saga_id = 'wf-saga-1';")
        assert c.fetchone()[0] == 0

        pg_conn.close()

        # 11. Downgrade to 003
        command.downgrade(cfg, "003")

        # 12. Verify legacy row still exists after downgrade
        pg_conn = psycopg2.connect(
            f"postgresql://{env['user']}:{env['password']}@{env['host']}:{env['port']}/{scratch_name}"
        )
        pg_conn.autocommit = True
        c = pg_conn.cursor()
        c.execute("SELECT saga_id, status FROM sagas WHERE saga_id = 'legacy-saga-1';")
        assert c.fetchone() == ("legacy-saga-1", "RUNNING")

        # Verify saga_step_results table is gone
        c.execute("SELECT to_regclass('saga_step_results');")
        assert c.fetchone()[0] is None
        pg_conn.close()

        # 13. Re-upgrade to head (004)
        command.upgrade(cfg, "head")

    finally:
        if old_db_url is not None:
            os.environ["DATABASE_URL"] = old_db_url
        else:
            os.environ.pop("DATABASE_URL", None)

        try:
            cleanup_conn = psycopg2.connect(maint_url, connect_timeout=3)
            cleanup_conn.autocommit = True
            c_cur = cleanup_conn.cursor()
            c_cur.execute(
                f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname = '{scratch_name}' AND pid <> pg_backend_pid();"
            )
            c_cur.execute(f"DROP DATABASE IF EXISTS {scratch_name};")
            cleanup_conn.close()
        except Exception:
            pass
