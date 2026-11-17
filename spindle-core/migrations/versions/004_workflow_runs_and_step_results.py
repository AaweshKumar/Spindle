"""add workflow columns to sagas and create saga_step_results table

Revision ID: 004
Revises: 003
Create Date: 2026-11-17
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add columns to sagas first (amendment 2)
    op.add_column(
        "sagas",
        sa.Column("source", sa.Text(), nullable=False, server_default="template"),
    )
    op.add_column(
        "sagas",
        sa.Column("definition_snapshot", JSONB(), nullable=True),
    )
    op.add_column(
        "sagas",
        sa.Column("input", JSONB(), nullable=True),
    )

    # 2. Add check constraints to sagas
    op.create_check_constraint(
        "ck_sagas_source_values",
        "sagas",
        "source IN ('template', 'workflow')",
    )
    op.create_check_constraint(
        "ck_sagas_source_snapshot",
        "sagas",
        "(source = 'workflow') = (definition_snapshot IS NOT NULL)",
    )

    # 3. Create saga_step_results table
    op.create_table(
        "saga_step_results",
        sa.Column(
            "saga_id",
            sa.Text(),
            sa.ForeignKey("sagas.saga_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("step_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("response", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("saga_id", "step_id", "kind", name="pk_saga_step_results"),
        sa.CheckConstraint(
            "kind IN ('execute', 'compensate')",
            name="ck_saga_step_results_kind_values",
        ),
    )


def downgrade() -> None:
    op.drop_table("saga_step_results")
    op.drop_constraint("ck_sagas_source_snapshot", "sagas", type_="check")
    op.drop_constraint("ck_sagas_source_values", "sagas", type_="check")
    op.drop_column("sagas", "input")
    op.drop_column("sagas", "definition_snapshot")
    op.drop_column("sagas", "source")
