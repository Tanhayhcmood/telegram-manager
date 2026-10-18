"""Add enum values required by the current application.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-18
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PostgreSQL only permits ALTER TYPE ... ADD VALUE outside a transaction.
    # IF NOT EXISTS also covers databases whose enum was updated manually.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE link_status ADD VALUE IF NOT EXISTS 'skipped_not_group'")
        op.execute("ALTER TYPE group_status ADD VALUE IF NOT EXISTS 'left'")


def downgrade() -> None:
    # PostgreSQL cannot remove enum values without recreating the enum type.
    pass
