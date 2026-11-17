import os
import uuid
import psycopg2
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _get_pg_env() -> dict[str, str]:
    return {
        "user": os.environ.get("POSTGRES_USER", "spindle"),
        "password": os.environ.get("POSTGRES_PASSWORD", "spindle-dev-only"),
        "host": os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        "port": os.environ.get("POSTGRES_PORT", "5433"),
        "maint_db": os.environ.get("POSTGRES_DB", "spindle"),
    }


@pytest.fixture(scope="module")
def scratch_db():
    env = _get_pg_env()
    maint_url = (
        f"postgresql://{env['user']}:{env['password']}@{env['host']}:{env['port']}/{env['maint_db']}"
    )

    try:
        conn = psycopg2.connect(maint_url, connect_timeout=3)
        conn.autocommit = True
    except Exception as exc:
        pytest.skip(f"Postgres server not reachable on {env['host']}:{env['port']}: {exc}")

    scratch_name = f"scratch_{uuid.uuid4().hex[:8]}"
    cur = conn.cursor()
    cur.execute(f"CREATE DATABASE {scratch_name};")
    conn.close()

    scratch_sa_url = (
        f"postgresql+psycopg2://{env['user']}:{env['password']}@{env['host']}:{env['port']}/{scratch_name}"
    )
    old_db_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = scratch_sa_url

    engine = create_engine(scratch_sa_url, pool_pre_ping=True)
    factory = sessionmaker(bind=engine)

    # Run migrations to head
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    cfg.config_file_name = None
    command.upgrade(cfg, "head")

    try:
        yield factory
    finally:
        engine.dispose()
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
