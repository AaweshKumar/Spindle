from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .constants import NAME_PATTERN
from .database import Base


class SagaModel(Base):
    __tablename__ = "sagas"

    saga_id: Mapped[str] = mapped_column(String, primary_key=True)
    state: Mapped[dict] = mapped_column(JSONB, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    template_name: Mapped[str | None] = mapped_column(String, nullable=True)
    template_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(
        Text, nullable=False, default="template", server_default="template"
    )
    definition_snapshot: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    input: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)

    __table_args__ = (
        Index("ix_sagas_created_at", "created_at", "saga_id"),
        Index("ix_sagas_status_created_at", "status", "created_at", "saga_id"),
        CheckConstraint(
            "source IN ('template', 'workflow')", name="ck_sagas_source_values"
        ),
        CheckConstraint(
            "(source = 'workflow') = (definition_snapshot IS NOT NULL)",
            name="ck_sagas_source_snapshot",
        ),
    )


class StepResultModel(Base):
    __tablename__ = "saga_step_results"

    saga_id: Mapped[str] = mapped_column(
        Text, ForeignKey("sagas.saga_id", ondelete="CASCADE"), primary_key=True
    )
    step_id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, primary_key=True)
    response: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('execute', 'compensate')",
            name="ck_saga_step_results_kind_values",
        ),
    )


class OutboxModel(Base):
    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    saga_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_outbox_unpublished", "id", postgresql_where=text("published = false")),
    )


class ServiceModel(Base):
    __tablename__ = "services"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    definition: Mapped[dict] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(f"name ~ '{NAME_PATTERN}'", name="ck_services_name_format"),
    )


class TemplateModel(Base):
    __tablename__ = "templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    definition: Mapped[dict] = mapped_column(JSONB, nullable=False)
    format_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_by: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(f"name ~ '{NAME_PATTERN}'", name="ck_templates_name_format"),
        CheckConstraint("version >= 1", name="ck_templates_version_min"),
        UniqueConstraint("name", "version", name="uq_templates_name_version"),
        Index("ix_templates_name_content_hash", "name", "content_hash"),
    )
