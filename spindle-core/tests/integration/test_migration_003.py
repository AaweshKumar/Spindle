# Component: Spindle Architecture
# File: test_migration_003.py
# Description: Integration test verifying migration 003 upgrade, downgrade, and re-upgrade.

"""Integration test for Alembic migration 003.

Verifies that migration 003 applies cleanly to a scratch database, rolls back
successfully, and re-applies to head revision 003.
"""

from __future__ import annotations

import os
import uuid
import pytest
import psycopg2
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

pytestmark = pytest.mark.e2e


def _get_pg_env() -> dict[str, str]:
    return {
        "user": os.environ.get("POSTGRES_USER", "spindle"),
        "password": os.environ.get("POSTGRES_PASSWORD", "spindle-dev-only"),
        "host": os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        "port": os.environ.get("POSTGRES_PORT", "5433"),
        "maint_db": os.environ.get("POSTGRES_DB", "spindle"),
    }


def test_migration_003_upgrade_downgrade_upgrade() -> None:
    env = _get_pg_env()
    maint_url = (
        f"postgresql://{env['user']}:{env['password']}@{env['host']}:{env['port']}/{env['maint_db']}"
    )

    try:
        conn = psycopg2.connect(maint_url, connect_timeout=3)
        conn.autocommit = True
    except Exception as exc:
        pytest.skip(f"Postgres server not reachable on {env['host']}:{env['port']}: {exc}")

    scratch_name = f"scratch_mig_{uuid.uuid4().hex[:8]}"
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
    assert "003" in [r.revision for r in script.walk_revisions()]

    try:
        # Upgrade to head (003)
        command.upgrade(cfg, "head")

        # Downgrade to base
        command.downgrade(cfg, "base")

        # Re-upgrade to head (003)
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
