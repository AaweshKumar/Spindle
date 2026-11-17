# Component: Spindle Architecture
# File: template_repository.py
# Description: Saga template repository with optimistic concurrency and advisory locks.

"""Template repository for Spindle saga workflow definitions.

Provides versioned template storage, advisory-locked publication, idempotency,
and list/get retrieval backed by PostgreSQL.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from typing import TYPE_CHECKING

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from spindle_core.persistence.constants import NAME_RE
from spindle_core.persistence.errors import (
    InvalidNameError,
    TemplateNotFound,
    VersionConflict,
)
from spindle_core.persistence.hashing import compute_content_hash
from spindle_core.persistence.models import TemplateModel

if TYPE_CHECKING:
    from spindle_core.schema.models import SagaDefinition


@dataclass(frozen=True)
class TemplateRecord:
    """Frozen dataclass representing a persisted saga template."""

    id: int
    name: str
    version: int
    definition: dict
    format_version: int
    content_hash: str
    created_at: datetime
    created_by: str | None


def _row_to_record(row: TemplateModel) -> TemplateRecord:
    return TemplateRecord(
        id=row.id,
        name=row.name,
        version=row.version,
        definition=row.definition,
        format_version=row.format_version,
        content_hash=row.content_hash,
        created_at=row.created_at,
        created_by=row.created_by,
    )


class TemplateRepository:
    """Repository for managing versioned saga templates."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get(self, name: str, version: int | None = None) -> TemplateRecord:
        """Retrieve a template by name and optional version (latest if None)."""
        if not NAME_RE.fullmatch(name):
            raise InvalidNameError("invalid template name")

        with self._session_factory() as session:
            stmt = select(TemplateModel).where(TemplateModel.name == name)
            if version is not None:
                stmt = stmt.where(TemplateModel.version == version)
            else:
                stmt = stmt.order_by(TemplateModel.version.desc()).limit(1)

            row = session.execute(stmt).scalar_one_or_none()
            if row is None:
                if version is not None:
                    raise TemplateNotFound(
                        f"template '{name}' version {version} not found"
                    )
                raise TemplateNotFound(f"template '{name}' not found")
            return _row_to_record(row)

    def list_latest(self) -> list[TemplateRecord]:
        """List the latest version of every template, ordered by name."""
        stmt = (
            select(TemplateModel)
            .ext(distinct_on(TemplateModel.name))
            .order_by(TemplateModel.name.asc(), TemplateModel.version.desc())
        )
        with self._session_factory() as session:
            rows = session.execute(stmt).scalars().all()
            return [_row_to_record(r) for r in rows]

    def list_versions(self, name: str) -> list[TemplateRecord]:
        """List all versions of a given template in ascending order."""
        if not NAME_RE.fullmatch(name):
            raise InvalidNameError("invalid template name")

        stmt = (
            select(TemplateModel)
            .where(TemplateModel.name == name)
            .order_by(TemplateModel.version.asc())
        )
        with self._session_factory() as session:
            rows = session.execute(stmt).scalars().all()
            return [_row_to_record(r) for r in rows]


    def publish(
        self,
        definition: SagaDefinition,
        *,
        base_version: int | None = None,
        created_by: str | None = None,
    ) -> tuple[TemplateRecord, bool]:
        """Publish a new version of a saga template using an advisory lock.

        Returns (stored_record, created).
        - If name unknown: base_version must be None or 0, else VersionConflict. Inserts v1.
        - If base_version != latest.version: if latest matches new content and version == base_version + 1,
          returns (latest, False) as a safe idempotent retry; otherwise raises VersionConflict.
        - If identical content_hash to latest: returns (latest, False).
        - Otherwise inserts version latest.version + 1, created=True.
        - Any unique violation raises VersionConflict without retrying.
        """
        name = definition.name
        if not NAME_RE.fullmatch(name):
            raise InvalidNameError("invalid template name")

        content_hash, dump = compute_content_hash(definition)

        with self._session_factory() as session:
            with session.begin():
                # Advisory lock serialized per template name
                session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:name, 0));"),
                    {"name": name},
                )

                stmt = (
                    select(TemplateModel)
                    .where(TemplateModel.name == name)
                    .order_by(TemplateModel.version.desc())
                    .limit(1)
                )
                latest = session.execute(stmt).scalar_one_or_none()

                if latest is None:
                    # Case a: unknown name
                    if base_version not in (None, 0):
                        raise VersionConflict(
                            f"template '{name}' does not exist; base_version must be None or 0, got {base_version}"
                        )
                    target_version = 1
                elif base_version is not None and base_version != latest.version:
                    # Case b: base_version mismatch
                    if (
                        latest.content_hash == content_hash
                        and latest.version == base_version + 1
                    ):
                        return _row_to_record(latest), False
                    raise VersionConflict(
                        f"template '{name}' version mismatch: expected {base_version}, found {latest.version}"
                    )
                elif latest.content_hash == content_hash:
                    # Case c: identical content hash to latest
                    return _row_to_record(latest), False
                else:
                    # Case d: publish new version
                    target_version = latest.version + 1

                try:
                    new_row = TemplateModel(
                        name=name,
                        version=target_version,
                        definition=dump,
                        format_version=1,
                        content_hash=content_hash,
                        created_by=created_by,
                    )
                    session.add(new_row)
                    session.flush()
                    return _row_to_record(new_row), True
                except IntegrityError as exc:
                    # Case e: backstop unique violation
                    raise VersionConflict(
                        f"template '{name}' version {target_version} conflict"
                    ) from exc

    def names_referencing_service(self, service_name: str) -> list[str]:
        """Return names of workflows whose latest version references service_name."""
        if not NAME_RE.fullmatch(service_name):
            raise InvalidNameError("invalid service name")

        target = json.dumps([{"service": service_name}])
        stmt = text(
            """
            WITH latest_templates AS (
                SELECT DISTINCT ON (name) name, definition
                FROM templates
                ORDER BY name ASC, version DESC
            )
            SELECT name
            FROM latest_templates
            WHERE definition->'steps' @> CAST(:target AS jsonb)
            ORDER BY name ASC;
            """
        )
        with self._session_factory() as session:
            rows = session.execute(stmt, {"target": target}).scalars().all()
            return list(rows)

