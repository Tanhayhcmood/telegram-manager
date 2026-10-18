"""Add can_write flag to groups

Some groups keep the account as a member (status stays JOINED / still shows
up in live Telethon dialogs) but an admin has restricted/banned the account
from posting there. That is different from actually leaving the group, so
it needs its own flag instead of reusing status — otherwise the periodic
dialog sync (which re-marks every live dialog as JOINED) would keep
reviving these groups back into the broadcast target list.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-08 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The application may have added this column before Alembic ran.
    op.execute(
        "ALTER TABLE groups "
        "ADD COLUMN IF NOT EXISTS can_write BOOLEAN NOT NULL DEFAULT TRUE"
    )


def downgrade() -> None:
    op.drop_column("groups", "can_write")
