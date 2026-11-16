# Component: Spindle Architecture
# File: service_repository.py
# Description: Service definition repository with race-safe atomic upsert.

"""Service repository for Spindle service definitions.

Provides race-safe atomic upsert, retrieval, deletion, and name enumeration
backed by PostgreSQL.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from typing import TYPE_CHECKING

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session, sessionmaker

from spindle_core.persistence.constants import NAME_RE
from spindle_core.persistence.errors import InvalidNameError, ServiceNotFound
from spindle_core.persistence.hashing import compute_content_hash
from spindle_core.persistence.models import ServiceModel

if TYPE_CHECKING:
    from spindle_core.schema.models import ServiceDefinition


@dataclass(frozen=True)
class ServiceRecord:
    """Frozen dataclass representing a persisted service definition."""

    name: str
    definition: dict
    content_hash: str
    revision: int
    created_at: datetime
    updated_at: datetime


class ServiceRepository:
    """Repository for managing registered services."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def upsert(self, definition: ServiceDefinition) -> tuple[ServiceRecord, bool]:
        """Atomically insert or update a service definition.

        Returns (stored_record, created).
        - If new name: created=True, revision=1.
        - If existing name with different content_hash: created=False, revision increments.
        - If identical content_hash: no write, revision and updated_at unchanged, created=False.
        """
        name = definition.name
        if not NAME_RE.fullmatch(name):
            raise InvalidNameError("invalid service name")

        content_hash, dump = compute_content_hash(definition)
        dump_json = json.dumps(dump)

        stmt = text(
            """
            INSERT INTO services (name, definition, content_hash, revision, created_at, updated_at)
            VALUES (:name, CAST(:definition AS jsonb), :content_hash, 1, now(), now())
            ON CONFLICT (name) DO UPDATE
            SET definition = EXCLUDED.definition,
                content_hash = EXCLUDED.content_hash,
                revision = services.revision + 1,
                updated_at = now()
            WHERE services.content_hash != EXCLUDED.content_hash
            RETURNING name, definition, content_hash, revision, created_at, updated_at, (xmax = 0) AS created;
            """
        )

        with self._session_factory() as session:
            with session.begin():
                result = session.execute(
                    stmt,
                    {
                        "name": name,
                        "definition": dump_json,
                        "content_hash": content_hash,
                    },
                ).fetchone()

                if result is not None:
                    rec = ServiceRecord(
                        name=result[0],
                        definition=result[1],
                        content_hash=result[2],
                        revision=result[3],
                        created_at=result[4],
                        updated_at=result[5],
                    )
                    created = bool(result[6])
                    return rec, created

                # Identical content hash: no write performed
                row = session.execute(
                    select(ServiceModel).where(ServiceModel.name == name)
                ).scalar_one()
                rec = ServiceRecord(
                    name=row.name,
                    definition=row.definition,
                    content_hash=row.content_hash,
                    revision=row.revision,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                )
                return rec, False

    def get(self, name: str) -> ServiceRecord:
        """Retrieve a service by name, or raise ServiceNotFound."""
        if not NAME_RE.fullmatch(name):
            raise InvalidNameError("invalid service name")

        with self._session_factory() as session:
            row = session.execute(
                select(ServiceModel).where(ServiceModel.name == name)
            ).scalar_one_or_none()
            if row is None:
                raise ServiceNotFound(f"service '{name}' not found")
            return ServiceRecord(
                name=row.name,
                definition=row.definition,
                content_hash=row.content_hash,
                revision=row.revision,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )

    def list(self) -> list[ServiceRecord]:
        """List all services ordered by name ascending."""
        with self._session_factory() as session:
            rows = session.execute(
                select(ServiceModel).order_by(ServiceModel.name.asc())
            ).scalars().all()
            return [
                ServiceRecord(
                    name=r.name,
                    definition=r.definition,
                    content_hash=r.content_hash,
                    revision=r.revision,
                    created_at=r.created_at,
                    updated_at=r.updated_at,
                )
                for r in rows
            ]

    def delete(self, name: str) -> None:
        """Delete a service by name, or raise ServiceNotFound."""
        if not NAME_RE.fullmatch(name):
            raise InvalidNameError("invalid service name")

        with self._session_factory() as session:
            with session.begin():
                res = session.execute(
                    delete(ServiceModel).where(ServiceModel.name == name)
                )
                if res.rowcount == 0:
                    raise ServiceNotFound(f"service '{name}' not found")

    def names(self) -> frozenset[str]:
        """Return the set of all registered service names."""
        with self._session_factory() as session:
            names = session.execute(select(ServiceModel.name)).scalars().all()
            return frozenset(names)
