"""create services and templates tables

Revision ID: 003
Revises: 002
Create Date: 2026-11-16
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"


def upgrade() -> None:
    op.create_table(
        "services",
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("definition", JSONB(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(f"name ~ '{NAME_PATTERN}'", name="ck_services_name_format"),
    )

    op.create_table(
        "templates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("definition", JSONB(), nullable=False),
        sa.Column("format_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.CheckConstraint(f"name ~ '{NAME_PATTERN}'", name="ck_templates_name_format"),
        sa.CheckConstraint("version >= 1", name="ck_templates_version_min"),
        sa.UniqueConstraint("name", "version", name="uq_templates_name_version"),
    )

    op.create_index(
        "ix_templates_name_content_hash",
        "templates",
        ["name", "content_hash"],
        unique=False,
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION trg_templates_immutable()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION 'templates rows are immutable and cannot be updated or deleted';
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_templates_no_update_or_delete
        BEFORE UPDATE OR DELETE ON templates
        FOR EACH ROW
        EXECUTE FUNCTION trg_templates_immutable();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_templates_no_update_or_delete ON templates;")
    op.execute("DROP FUNCTION IF EXISTS trg_templates_immutable();")
    op.drop_index("ix_templates_name_content_hash", table_name="templates")
    op.drop_table("templates")
    op.drop_table("services")
