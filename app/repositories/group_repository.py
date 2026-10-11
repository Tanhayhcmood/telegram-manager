from datetime import datetime
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.group import Group, GroupStatus
from .base import BaseRepository


class GroupRepository(BaseRepository[Group]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(Group, session)

    async def get_by_group_id(self, group_id: int) -> Group | None:
        result = await self._session.execute(
            select(Group).where(Group.group_id == group_id)
        )
        return result.scalar_one_or_none()

    async def get_by_username(self, username: str) -> Group | None:
        result = await self._session.execute(
            select(Group).where(Group.username == username.lower())
        )
        return result.scalar_one_or_none()

    async def get_by_status(self, status: GroupStatus, limit: int = 50) -> list[Group]:
        result = await self._session.execute(
            select(Group)
            .where(Group.status == status)
            .order_by(Group.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def count_by_status(self, status: GroupStatus) -> int:
        result = await self._session.execute(
            select(func.count()).select_from(Group).where(Group.status == status)
        )
        return result.scalar_one()

    async def upsert(self, group_id: int, **kwargs: object) -> tuple[Group, bool]:
        existing = await self.get_by_group_id(group_id)
        created = False
        if existing is None:
            existing = Group(group_id=group_id, **kwargs)
            self._session.add(existing)
            created = True
        else:
            for key, value in kwargs.items():
                setattr(existing, key, value)
        await self._session.flush()
        await self._session.refresh(existing)
        return existing, created

    async def get_latest(self, limit: int = 10, offset: int = 0) -> list[Group]:
        result = await self._session.execute(
            select(Group)
            .order_by(Group.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def get_recently_joined(self, limit: int = 2) -> list[Group]:
        """Return joined groups ordered by join time, with legacy fallback."""
        order_col = func.coalesce(Group.join_date, Group.created_at)
        result = await self._session.execute(
            select(Group)
            .where(Group.status == GroupStatus.JOINED)
            .order_by(order_col.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_joined(self) -> list[Group]:
        return await self.get_by_status(GroupStatus.JOINED, limit=5000)

    async def get_all(self, limit: int = 100000, offset: int = 0) -> list[Group]:
        result = await self._session.execute(
            select(Group).order_by(Group.created_at.desc()).limit(limit).offset(offset)
        )
        return list(result.scalars().all())
