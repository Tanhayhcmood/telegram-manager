from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user_bio_scan import UserBioScan
from .base import BaseRepository


class UserBioScanRepository(BaseRepository[UserBioScan]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(UserBioScan, session)

    async def is_message_checked_since(self, user_id: int, cutoff: datetime) -> bool:
        result = await self._session.execute(
            select(UserBioScan.user_id)
            .where(UserBioScan.user_id == user_id)
            .where(UserBioScan.message_checked_at >= cutoff)
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def get_passively_scanned_user_ids(self) -> set[int]:
        result = await self._session.execute(
            select(UserBioScan.user_id).where(UserBioScan.passive_scanned_at.is_not(None))
        )
        return {int(user_id) for user_id in result.scalars().all()}

    async def mark_message_checked(self, user_id: int, checked_at: datetime) -> None:
        statement = pg_insert(UserBioScan).values(
            user_id=user_id,
            message_checked_at=checked_at,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[UserBioScan.user_id],
            set_={"message_checked_at": checked_at},
        )
        await self._session.execute(statement)

    async def mark_passively_scanned(self, user_id: int, scanned_at: datetime) -> None:
        statement = pg_insert(UserBioScan).values(
            user_id=user_id,
            passive_scanned_at=scanned_at,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[UserBioScan.user_id],
            set_={"passive_scanned_at": scanned_at},
        )
        await self._session.execute(statement)
