"""Track member bio checks and rotate passive group scans.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-18 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The application also uses Base.metadata.create_all() plus idempotent
    # ALTERs at startup, so keep this migration safe after an app cold start.
    op.execute(
        "ALTER TABLE groups "
        "ADD COLUMN IF NOT EXISTS last_bio_scan_at TIMESTAMPTZ"
    )
    op.execute(
        "ALTER TABLE groups "
        "ADD COLUMN IF NOT EXISTS bio_scan_complete_at TIMESTAMPTZ"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS user_bio_scans ("
        "user_id BIGINT PRIMARY KEY, "
        "passive_scanned_at TIMESTAMPTZ NULL, "
        "message_checked_at TIMESTAMPTZ NULL)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS user_bio_scans")
    op.execute("ALTER TABLE groups DROP COLUMN IF EXISTS bio_scan_complete_at")
    op.execute("ALTER TABLE groups DROP COLUMN IF EXISTS last_bio_scan_at")
