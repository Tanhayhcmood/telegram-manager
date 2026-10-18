"""Upgrade the database before starting the Telegram bot."""

import asyncio
import os
import subprocess
import sys

from sqlalchemy import text

from app.database.connection import engine

_LEGACY_BASELINE_REVISION = "0007"


async def _read_schema_state() -> tuple[bool, bool]:
    """Return whether app tables and an Alembic version table already exist."""
    query = text(
        """
        SELECT
            EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name IN (
                      'groups',
                      'discovered_links',
                      'logs',
                      'contacted_users',
                      'join_attempts',
                      'runtime_settings'
                  )
            ) AS has_app_tables,
            EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = 'alembic_version'
            ) AS has_alembic_version
        """
    )
    try:
        async with engine.connect() as connection:
            row = (await connection.execute(query)).one()
            return bool(row.has_app_tables), bool(row.has_alembic_version)
    finally:
        await engine.dispose()


def _run_alembic(*arguments: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        check=True,
    )


def main() -> None:
    has_app_tables, has_alembic_version = asyncio.run(_read_schema_state())

    # The application historically created its schema with SQLAlchemy before
    # Alembic was run. Mark that existing schema at the last legacy revision
    # so upgrade applies only new migrations instead of recreating its tables.
    if has_app_tables and not has_alembic_version:
        _run_alembic("stamp", _LEGACY_BASELINE_REVISION)

    _run_alembic("upgrade", "head")
    os.execv(sys.executable, [sys.executable, "-m", "app.main"])


if __name__ == "__main__":
    main()
