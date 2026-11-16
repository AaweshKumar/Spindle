# Component: Spindle Architecture
# File: test_template_repository.py
# Description: Integration tests for TemplateRepository on an isolated scratch database.

"""Integration tests for TemplateRepository.

Verifies versioned publishing, idempotency, advisory locks, concurrency,
immutability triggers, and conflict resolution on an isolated scratch database.
"""

from __future__ import annotations

import concurrent.futures
import os
import threading
import uuid
import pytest
import psycopg2
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from spindle_core.persistence.errors import (
    InvalidNameError,
    TemplateNotFound,
    VersionConflict,
)
from spindle_core.persistence.template_repository import (
    TemplateRecord,
    TemplateRepository,
)
from spindle_core.schema.models import SagaDefinition

pytestmark = pytest.mark.e2e


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

    scratch_name = f"scratch_tmpl_{uuid.uuid4().hex[:8]}"
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


def _make_saga(name: str, path: str = "/orders") -> SagaDefinition:
    return SagaDefinition.model_validate(
        {
            "name": name,
            "steps": [
                {
                    "name": "s1",
                    "service": "svc",
                    "action": {"method": "POST", "path": path},
                    "timeout_s": 15.0,
                    "compensation": {"method": "DELETE", "path": f"{path}/cancel"},
                }
            ],
        }
    )


def test_immutability_trigger_blocks_update(scratch_db) -> None:
    repo = TemplateRepository(scratch_db)
    tmpl_name = f"tmpl_{uuid.uuid4().hex[:6]}"
    saga = _make_saga(tmpl_name)
    rec, _ = repo.publish(saga)

    with scratch_db() as session:
        with pytest.raises(Exception) as exc_update:
            with session.begin():
                session.execute(
                    text("UPDATE templates SET created_by = 'hacker' WHERE id = :id"),
                    {"id": rec.id},
                )
        assert "immutable" in str(exc_update.value)


def test_immutability_trigger_blocks_delete(scratch_db) -> None:
    repo = TemplateRepository(scratch_db)
    tmpl_name = f"tmpl_{uuid.uuid4().hex[:6]}"
    saga = _make_saga(tmpl_name)
    rec, _ = repo.publish(saga)

    with scratch_db() as session:
        with pytest.raises(Exception) as exc_del:
            with session.begin():
                session.execute(
                    text("DELETE FROM templates WHERE id = :id"),
                    {"id": rec.id},
                )
        assert "immutable" in str(exc_del.value)


def test_duplicate_name_version_rejected_by_db(scratch_db) -> None:
    repo = TemplateRepository(scratch_db)
    tmpl_name = f"tmpl_{uuid.uuid4().hex[:6]}"
    repo.publish(_make_saga(tmpl_name))

    with scratch_db() as session:
        with pytest.raises(Exception) as exc_dup:
            with session.begin():
                session.execute(
                    text(
                        "INSERT INTO templates (name, version, definition, format_version, content_hash) "
                        "VALUES (:name, 1, CAST('{}' AS jsonb), 1, 'dummy_hash')"
                    ),
                    {"name": tmpl_name},
                )
        assert (
            "uq_templates_name_version" in str(exc_dup.value)
            or "duplicate key" in str(exc_dup.value).lower()
        )


def test_unknown_name_with_base_version_raises_conflict(scratch_db) -> None:
    repo = TemplateRepository(scratch_db)
    tmpl_name = f"tmpl_{uuid.uuid4().hex[:6]}"
    with pytest.raises(VersionConflict) as exc_info:
        repo.publish(_make_saga(tmpl_name), base_version=3)
    assert "does not exist" in str(exc_info.value)


def test_publish_aba_yields_versions_1_2_3(scratch_db) -> None:
    repo = TemplateRepository(scratch_db)
    tmpl_name = f"tmpl_{uuid.uuid4().hex[:6]}"

    saga_a = _make_saga(tmpl_name, "/orders/v1")
    saga_b = _make_saga(tmpl_name, "/orders/v2")

    rec1, created1 = repo.publish(saga_a)
    assert created1 is True
    assert rec1.version == 1

    rec2, created2 = repo.publish(saga_b)
    assert created2 is True
    assert rec2.version == 2

    # Publishing A again must yield v3 (non-unique content_hash index allows this)
    rec3, created3 = repo.publish(saga_a)
    assert created3 is True
    assert rec3.version == 3
    assert rec3.content_hash == rec1.content_hash


def test_publish_identical_content_returns_existing(scratch_db) -> None:
    repo = TemplateRepository(scratch_db)
    tmpl_name = f"tmpl_{uuid.uuid4().hex[:6]}"
    saga = _make_saga(tmpl_name, "/orders/static")

    rec1, created1 = repo.publish(saga)
    assert created1 is True
    assert rec1.version == 1

    # Identical publish returns same version and created=False
    rec2, created2 = repo.publish(saga)
    assert created2 is False
    assert rec2.version == 1
    assert rec2.id == rec1.id


def test_publish_version_conflict_and_retry_after_success(scratch_db) -> None:
    repo = TemplateRepository(scratch_db)
    tmpl_name = f"tmpl_{uuid.uuid4().hex[:6]}"

    # Unknown name with base_version != 0 raises VersionConflict
    with pytest.raises(VersionConflict) as exc_info:
        repo.publish(_make_saga(tmpl_name), base_version=3)
    assert "does not exist" in str(exc_info.value)

    # Initial publish
    rec1, created1 = repo.publish(_make_saga(tmpl_name, "/orders/v1"))
    assert rec1.version == 1

    # Stale base_version raises VersionConflict
    with pytest.raises(VersionConflict) as exc_stale:
        repo.publish(_make_saga(tmpl_name, "/orders/v2"), base_version=0)
    assert "version mismatch" in str(exc_stale.value)

    # Retry-after-success case: base_version=0, but content matches v1 and latest is v1 (base_version + 1)
    rec_retry, created_retry = repo.publish(_make_saga(tmpl_name, "/orders/v1"), base_version=0)
    assert created_retry is False
    assert rec_retry.version == 1


def test_concurrency_parallel_publishes_produce_sequential_versions(scratch_db) -> None:
    repo = TemplateRepository(scratch_db)
    tmpl_name = f"tmpl_{uuid.uuid4().hex[:6]}"
    barrier = threading.Barrier(8)

    # Concurrently publish 8 different definitions without base_version
    def _publish_worker(i: int):
        saga = _make_saga(tmpl_name, f"/orders/step_{i}")
        barrier.wait()
        return repo.publish(saga, created_by=f"worker_{i}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(_publish_worker, i) for i in range(8)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    # Exactly 8 versions produced, 1..8
    versions = sorted(r[0].version for r in results)
    assert versions == list(range(1, 9))
    assert all(r[1] is True for r in results)

    # Check list_versions returns all 8 in ascending order
    history = repo.list_versions(tmpl_name)
    assert len(history) == 8
    assert [h.version for h in history] == list(range(1, 9))


def test_concurrency_same_base_version_one_winner(scratch_db) -> None:
    repo = TemplateRepository(scratch_db)
    tmpl_name = f"tmpl_{uuid.uuid4().hex[:6]}"

    # Publish v1
    repo.publish(_make_saga(tmpl_name, "/orders/v1"))
    barrier = threading.Barrier(2)

    # Concurrently attempt 2 publishes with base_version=1 and different content
    def _try_publish(path_suffix: str):
        saga = _make_saga(tmpl_name, f"/orders/{path_suffix}")
        barrier.wait()
        return repo.publish(saga, base_version=1)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(_try_publish, "diff_a")
        f2 = executor.submit(_try_publish, "diff_b")

    successes = []
    conflicts = []
    for f in (f1, f2):
        try:
            res = f.result()
            successes.append(res)
        except VersionConflict as exc:
            conflicts.append(exc)

    assert len(successes) == 1
    assert len(conflicts) == 1
    assert successes[0][0].version == 2


def test_concurrency_identical_concurrent_publishes_one_row(scratch_db) -> None:
    repo = TemplateRepository(scratch_db)
    tmpl_name = f"tmpl_{uuid.uuid4().hex[:6]}"
    saga = _make_saga(tmpl_name, "/orders/static")
    barrier = threading.Barrier(8)

    def _identical_worker():
        barrier.wait()
        return repo.publish(saga)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(_identical_worker) for _ in range(8)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    # All returned version 1
    assert all(r[0].version == 1 for r in results)
    # Exactly one was created=True, others created=False
    created_flags = [r[1] for r in results]
    assert created_flags.count(True) == 1
    assert created_flags.count(False) == 7

    history = repo.list_versions(tmpl_name)
    assert len(history) == 1


def test_template_get_and_list_latest(scratch_db) -> None:
    repo = TemplateRepository(scratch_db)
    tmpl_name = f"tmpl_{uuid.uuid4().hex[:6]}"

    repo.publish(_make_saga(tmpl_name, "/v1"))
    repo.publish(_make_saga(tmpl_name, "/v2"))

    # get latest
    latest = repo.get(tmpl_name)
    assert latest.version == 2

    # get specific version
    v1 = repo.get(tmpl_name, version=1)
    assert v1.version == 1

    # get nonexistent version
    with pytest.raises(TemplateNotFound):
        repo.get(tmpl_name, version=99)

    # get nonexistent template
    with pytest.raises(TemplateNotFound):
        repo.get("nonexistent_template")

    # list_latest includes tmpl_name with version 2
    latest_list = repo.list_latest()
    matching = [t for t in latest_list if t.name == tmpl_name]
    assert len(matching) == 1
    assert matching[0].version == 2


def test_template_invalid_name_and_no_definition_leak_in_errors(scratch_db) -> None:
    repo = TemplateRepository(scratch_db)

    # Invalid name rejected early without leaking submitted name
    bad_name = "bad secret token with spaces"
    with pytest.raises(InvalidNameError) as exc_name:
        repo.get(bad_name)
    assert str(exc_name.value) == "invalid template name"
    assert bad_name not in str(exc_name.value)

    with pytest.raises(InvalidNameError):
        repo.list_versions("-starts_with_dash")

    # Error message must never leak definition text
    secret_payload_marker = "SUPER_SECRET_INTERNAL_TOKEN_XYZ"
    bad_saga = _make_saga(f"tmpl_{uuid.uuid4().hex[:6]}", f"/{secret_payload_marker}")

    with pytest.raises(VersionConflict) as exc_info:
        repo.publish(bad_saga, base_version=99)

    err_text = str(exc_info.value)
    assert secret_payload_marker not in err_text
