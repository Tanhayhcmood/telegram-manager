from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.discovered_link import DiscoveredLink, LinkStatus
from .base import BaseRepository


class DiscoveredLinkRepository(BaseRepository[DiscoveredLink]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(DiscoveredLink, session)

    async def get_by_link(self, link: str) -> DiscoveredLink | None:
        result = await self._session.execute(
            select(DiscoveredLink).where(DiscoveredLink.link == link)
        )
        return result.scalar_one_or_none()

    async def exists(self, link: str) -> bool:
        return await self.get_by_link(link) is not None

    async def get_pending(self, limit: int = 50) -> list[DiscoveredLink]:
        result = await self._session.execute(
            select(DiscoveredLink)
            .where(DiscoveredLink.status == LinkStatus.PENDING)
            .order_by(DiscoveredLink.discovered_at.asc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_latest(self, limit: int = 50, offset: int = 0) -> list[DiscoveredLink]:
        result = await self._session.execute(
            select(DiscoveredLink)
            .order_by(DiscoveredLink.discovered_at.desc(), DiscoveredLink.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def count_by_status(self, status: LinkStatus) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(DiscoveredLink)
            .where(DiscoveredLink.status == status)
        )
        return result.scalar_one()

    async def register(self, link: str, source: str) -> tuple[DiscoveredLink, bool]:
        existing = await self.get_by_link(link)
        if existing is not None:
            return existing, False

        statement = (
            pg_insert(DiscoveredLink)
            .values(link=link, source=source, status=LinkStatus.PENDING)
            .on_conflict_do_nothing(index_elements=[DiscoveredLink.link])
            .returning(DiscoveredLink)
        )
        result = await self._session.execute(statement)
        record = result.scalar_one_or_none()
        if record is not None:
            return record, True

        # Another worker may have inserted this link after the initial read.
        existing = await self.get_by_link(link)
        if existing is None:
            raise RuntimeError(
                f"Link {link!r} conflicted during registration but could not be loaded"
            )
        return existing, False
