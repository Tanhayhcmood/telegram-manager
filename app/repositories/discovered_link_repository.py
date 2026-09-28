from sqlalchemy import desc, select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.discovered_link import DiscoveredLink, LinkStatus, LinkType
from .base import BaseRepository


class DiscoveredLinkRepository(BaseRepository[DiscoveredLink]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(DiscoveredLink, session)

    async def get_by_link(self, link: str) -> DiscoveredLink | None:
        result = await self._session.execute(
            select(DiscoveredLink).where(DiscoveredLink.link == link)
        )
        return result.scalar_one_or_none()

    async def get_by_canonical_key(self, canonical_key: str) -> DiscoveredLink | None:
        result = await self._session.execute(
            select(DiscoveredLink).where(DiscoveredLink.canonical_key == canonical_key)
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

    async def count_by_status(self, status: LinkStatus) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(DiscoveredLink)
            .where(DiscoveredLink.status == status)
        )
        return result.scalar_one()

    async def register(
        self,
        link: str,
        source: str,
        canonical_key: str,
        link_type: LinkType,
    ) -> tuple[DiscoveredLink, bool]:
        existing = await self.get_by_canonical_key(canonical_key)
        if existing is not None:
            return existing, False
        record = DiscoveredLink(
            link=link,
            canonical_key=canonical_key,
            type=link_type,
            source=source,
            status=LinkStatus.PENDING,
        )
        self._session.add(record)
        try:
            await self._session.flush()
        except IntegrityError:
            # NewMessage handlers can run concurrently for the same link.
            # The check-then-insert sequence must not let one duplicate abort
            # processing of every other link in that message.
            await self._session.rollback()
            existing = await self.get_by_canonical_key(canonical_key)
            if existing is None:
                raise
            return existing, False
        await self._session.refresh(record)
        return record, True
