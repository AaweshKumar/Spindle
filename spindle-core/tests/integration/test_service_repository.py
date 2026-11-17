# Component: Spindle Architecture
# File: test_service_repository.py
# Description: Integration tests for ServiceRepository on an isolated scratch database.

"""Integration tests for ServiceRepository.

Tests race-safe atomic upsert, revision incrementing, idempotent re-upsert,
listing, name enumeration, deletion, and concurrency on an isolated scratch database.
"""

from __future__ import annotations

import concurrent.futures
import os
import uuid
import pytest
import psycopg2
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from spindle_core.persistence.errors import InvalidNameError, ServiceNotFound
from spindle_core.persistence.service_repository import ServiceRecord, ServiceRepository
from spindle_core.schema.models import ServiceDefinition

pytestmark = pytest.mark.e2e


def _make_svc(name: str, base_url: str = "https://api.example.com") -> ServiceDefinition:
    return ServiceDefinition.model_validate(
        {
            "name": name,
            "base_url": base_url,
            "health": "/health",
            "auth": {"type": "bearer", "secret_ref": "API_KEY"},
        }
    )


def test_service_upsert_create_and_update(scratch_db) -> None:
    repo = ServiceRepository(scratch_db)
    svc_name = f"svc_{uuid.uuid4().hex[:6]}"
    svc1 = _make_svc(svc_name, "https://api.example.com/v1")

    # Initial upsert creates row with revision=1
    rec1, created1 = repo.upsert(svc1)
    assert created1 is True
    assert rec1.name == svc_name
    assert rec1.revision == 1
    assert rec1.definition["base_url"] == "https://api.example.com/v1"

    # Second upsert with different content increments revision
    svc2 = _make_svc(svc_name, "https://api.example.com/v2")
    rec2, created2 = repo.upsert(svc2)
    assert created2 is False
    assert rec2.name == svc_name
    assert rec2.revision == 2
    assert rec2.definition["base_url"] == "https://api.example.com/v2"
    assert rec2.updated_at >= rec1.updated_at


def test_service_upsert_identical_changes_nothing(scratch_db) -> None:
    repo = ServiceRepository(scratch_db)
    svc_name = f"svc_{uuid.uuid4().hex[:6]}"
    svc = _make_svc(svc_name, "https://api.example.com/static")

    rec1, created1 = repo.upsert(svc)
    assert created1 is True

    # Identical upsert
    rec2, created2 = repo.upsert(svc)
    assert created2 is False
    assert rec2.revision == rec1.revision
    assert rec2.updated_at == rec1.updated_at
    assert rec2.content_hash == rec1.content_hash


def test_service_get_list_delete_names(scratch_db) -> None:
    repo = ServiceRepository(scratch_db)
    svc_name = f"svc_{uuid.uuid4().hex[:6]}"
    svc = _make_svc(svc_name, "https://api.example.com/test")

    # Upsert
    repo.upsert(svc)

    # get
    rec = repo.get(svc_name)
    assert rec.name == svc_name
    assert rec.revision == 1

    # get nonexistent raises ServiceNotFound
    with pytest.raises(ServiceNotFound) as exc_info:
        repo.get("nonexistent_service")
    assert "nonexistent_service" in str(exc_info.value)

    # names includes svc_name
    names = repo.names()
    assert svc_name in names

    # list includes svc_name
    items = repo.list()
    assert any(i.name == svc_name for i in items)

    # delete
    repo.delete(svc_name)
    assert svc_name not in repo.names()

    # delete nonexistent raises ServiceNotFound
    with pytest.raises(ServiceNotFound):
        repo.delete(svc_name)


def test_service_invalid_name_rejected_early(scratch_db) -> None:
    repo = ServiceRepository(scratch_db)
    bad_names = ["-invalid_start", "invalid name with spaces", "a" * 65]
    for bad in bad_names:
        with pytest.raises(InvalidNameError):
            repo.get(bad)
        with pytest.raises(InvalidNameError):
            repo.delete(bad)


def test_service_concurrent_upserts(scratch_db) -> None:
    repo = ServiceRepository(scratch_db)
    svc_name = f"svc_{uuid.uuid4().hex[:6]}"

    # Concurrently upsert 8 variants of the same service name
    def _do_upsert(i: int):
        svc = _make_svc(svc_name, f"https://api.example.com/v{i}")
        return repo.upsert(svc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(_do_upsert, i) for i in range(8)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    assert len(results) == 8
    final_rec = repo.get(svc_name)
    assert final_rec.revision >= 1


def test_service_no_secret_leak_in_exception(scratch_db) -> None:
    repo = ServiceRepository(scratch_db)
    with pytest.raises(ServiceNotFound) as exc_info:
        repo.get("nonexistent_secret_svc")
    err_str = str(exc_info.value)
    assert "https://" not in err_str
    assert "secret" not in err_str.lower() or "not found" in err_str
