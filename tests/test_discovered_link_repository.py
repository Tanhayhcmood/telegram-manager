import unittest
from typing import Any, cast

from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.discovered_link import DiscoveredLink, LinkStatus
from app.repositories.discovered_link_repository import DiscoveredLinkRepository


class _Result:
    def __init__(self, value: Any | None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any | None:
        return self._value


class _Session:
    def __init__(self, *results: Any | None) -> None:
        self._results = list(results)
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return _Result(self._results.pop(0))


class DiscoveredLinkRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_register_uses_atomic_conflict_handling_for_racing_duplicate(self) -> None:
        existing = DiscoveredLink(
            link="https://t.me/example",
            source="test",
            status=LinkStatus.PENDING,
        )
        session = _Session(None, None, existing)

        record, created = await DiscoveredLinkRepository(cast(AsyncSession, session)).register(
            "https://t.me/example",
            "test",
        )

        self.assertIs(record, existing)
        self.assertFalse(created)
        insert_sql = str(session.statements[1].compile(dialect=postgresql.dialect()))
        self.assertIn("ON CONFLICT (link) DO NOTHING", insert_sql)

    async def test_register_returns_new_record_when_insert_succeeds(self) -> None:
        inserted = DiscoveredLink(
            link="https://t.me/new_example",
            source="test",
            status=LinkStatus.PENDING,
        )
        session = _Session(None, inserted)

        record, created = await DiscoveredLinkRepository(cast(AsyncSession, session)).register(
            "https://t.me/new_example",
            "test",
        )

        self.assertIs(record, inserted)
        self.assertTrue(created)
