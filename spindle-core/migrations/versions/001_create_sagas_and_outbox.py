"""create sagas and outbox tables

Revision ID: 001
Revises:
Create Date: 2026-09-25
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sagas",
        sa.Column("saga_id", sa.String, primary_key=True),
        sa.Column("state", JSONB, nullable=False),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
    )

    op.create_table(
        "outbox",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("saga_id", sa.String, nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("published", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_outbox_saga_id", "outbox", ["saga_id"])
    op.create_index("ix_outbox_unpublished", "outbox", ["id"], postgresql_where=sa.text("published = false"))


def downgrade() -> None:
    op.drop_table("outbox")
    op.drop_table("sagas")
