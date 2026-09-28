"""Store canonical Telegram link keys and explicit join classifications.

Revision ID: 0007
Revises: 0006
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    link_type = sa.Enum("invite", "username", name="link_type")
    link_type.create(op.get_bind(), checkfirst=True)

    bind = op.get_bind()
    for value in ("expired", "request_sent", "skipped_not_group"):
        bind.execute(sa.text(f"ALTER TYPE link_status ADD VALUE IF NOT EXISTS '{value}'"))

    op.add_column(
        "discovered_links",
        sa.Column("canonical_key", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "discovered_links",
        sa.Column(
            "type",
            link_type,
            nullable=False,
            server_default="username",
        ),
    )
    op.create_index(
        "ix_discovered_links_canonical_key",
        "discovered_links",
        ["canonical_key"],
    )


def downgrade() -> None:
    op.drop_index("ix_discovered_links_canonical_key", table_name="discovered_links")
    op.drop_column("discovered_links", "type")
    op.drop_column("discovered_links", "canonical_key")
    sa.Enum(name="link_type").drop(op.get_bind(), checkfirst=True)