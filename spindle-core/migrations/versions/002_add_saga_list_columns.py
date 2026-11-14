"""add list columns to sagas

Revision ID: 002
Revises: 001
Create Date: 2026-11-14
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sagas", sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.add_column("sagas", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.add_column("sagas", sa.Column("template_name", sa.String(), nullable=True))
    op.add_column("sagas", sa.Column("template_version", sa.Integer(), nullable=True))
    op.add_column("sagas", sa.Column("status", sa.String(), nullable=True))
    op.execute("UPDATE sagas SET status = state->>'status'")
    op.alter_column("sagas", "status", nullable=False)
    op.create_index("ix_sagas_created_at", "sagas", ["created_at", "saga_id"])
    op.create_index("ix_sagas_status_created_at", "sagas", ["status", "created_at", "saga_id"])


def downgrade() -> None:
    op.drop_index("ix_sagas_status_created_at", table_name="sagas")
    op.drop_index("ix_sagas_created_at", table_name="sagas")
    for column in ("status", "template_version", "template_name", "updated_at", "created_at"):
        op.drop_column("sagas", column)

