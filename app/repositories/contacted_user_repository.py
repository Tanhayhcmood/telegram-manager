from datetime import datetime, timezone
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contacted_user import ContactedUser
from .base import BaseRepository


class ContactedUserRepository(BaseRepository[ContactedUser]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(ContactedUser, session)

    async def get_by_user_id(self, user_id: int) -> ContactedUser | None:
        result = await self._session.execute(
            select(ContactedUser).where(ContactedUser.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def register_or_update(
        self,
        user_id: int,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> tuple[ContactedUser, bool]:
        existing = await self.get_by_user_id(user_id)
        if existing is not None:
            existing.message_count += 1
            existing.last_seen_at = datetime.now(timezone.utc)
            if username:
                existing.username = username
            if first_name:
                existing.first_name = first_name
            if last_name:
                existing.last_name = last_name
            await self._session.flush()
            return existing, False

        user = ContactedUser(
            user_id=user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            message_count=1,
        )
        self._session.add(user)
        await self._session.flush()
        await self._session.refresh(user)
        return user, True

    async def register_from_sync(
        self,
        user_id: int,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> tuple[ContactedUser, bool]:
        """Add/update a private-chat profile without recording a message sent."""
        existing = await self.get_by_user_id(user_id)
        if existing is not None:
            if username:
                existing.username = username
            if first_name:
                existing.first_name = first_name
            if last_name:
                existing.last_name = last_name
            await self._session.flush()
            return existing, False

        user = ContactedUser(
            user_id=user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            message_count=0,
        )
        self._session.add(user)
        await self._session.flush()
        await self._session.refresh(user)
        return user, True

    async def get_active(self, limit: int = 500) -> list[ContactedUser]:
        result = await self._session.execute(
            select(ContactedUser)
            .where(ContactedUser.is_blocked.is_(False))
            .order_by(ContactedUser.last_seen_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def count(self) -> int:
        result = await self._session.execute(
            select(func.count()).select_from(ContactedUser)
        )
        return result.scalar_one()
