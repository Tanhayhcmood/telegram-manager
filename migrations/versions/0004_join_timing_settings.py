"""Add persisted admin-controlled join timing settings.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-29 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "join_timing_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("delay_minutes", sa.Integer(), server_default="30", nullable=False),
        sa.Column("daily_limit", sa.Integer(), server_default="50", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        "INSERT INTO join_timing_settings (id, enabled, delay_minutes, daily_limit) "
        "VALUES (1, true, 30, 50)"
    )


def downgrade() -> None:
    op.drop_table("join_timing_settings")
