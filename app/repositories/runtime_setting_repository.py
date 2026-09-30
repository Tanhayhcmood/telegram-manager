from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.runtime_setting import RuntimeSetting
from .base import BaseRepository

# Fixed singleton row id — every read/write targets this exact row so
# concurrent startups/admin edits can never create a second "settings" row.
SINGLETON_ID = 1


class RuntimeSettingRepository(BaseRepository[RuntimeSetting]):
    """Singleton-row repository (fixed id=1) for admin-adjustable runtime settings."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(RuntimeSetting, session)

    async def get_or_create(self) -> RuntimeSetting:
        """Return the singleton row, creating it from env defaults if missing.

        Uses INSERT ... ON CONFLICT DO NOTHING on the fixed id=1 primary key so
        concurrent callers (e.g. two processes starting up at once) can never
        race into creating two rows — the loser's insert is a no-op and it
        just re-selects the row the winner created.
        """
        stmt = pg_insert(RuntimeSetting).values(
            id=SINGLETON_ID,
            join_delay_min=settings.JOIN_DELAY_MIN,
            join_delay_max=settings.JOIN_DELAY_MAX,
        ).on_conflict_do_nothing(index_elements=["id"])
        await self._session.execute(stmt)
        await self._session.commit()

        result = await self._session.execute(
            select(RuntimeSetting).where(RuntimeSetting.id == SINGLETON_ID)
        )
        return result.scalar_one()

    async def update_join_delay(self, delay_min: int, delay_max: int) -> RuntimeSetting:
        """Atomically upsert the join-delay range on the fixed singleton row."""
        stmt = pg_insert(RuntimeSetting).values(
            id=SINGLETON_ID,
            join_delay_min=delay_min,
            join_delay_max=delay_max,
        ).on_conflict_do_update(
            index_elements=["id"],
            set_={"join_delay_min": delay_min, "join_delay_max": delay_max},
        )
        await self._session.execute(stmt)
        await self._session.commit()

        result = await self._session.execute(
            select(RuntimeSetting).where(RuntimeSetting.id == SINGLETON_ID)
        )
        return result.scalar_one()

    async def get_join_not_before_at(self) -> datetime | None:
        row = await self.get_or_create()
        return row.join_not_before_at

    async def extend_join_not_before_at(self, deadline: datetime) -> RuntimeSetting:
        """Atomically extend the global join cooldown without shortening it."""
        await self.get_or_create()
        stmt = (
            update(RuntimeSetting)
            .where(RuntimeSetting.id == SINGLETON_ID)
            .values(
                join_not_before_at=func.greatest(
                    func.coalesce(RuntimeSetting.join_not_before_at, deadline),
                    deadline,
                )
            )
        )
        await self._session.execute(stmt)
        await self._session.commit()

        result = await self._session.execute(
            select(RuntimeSetting).where(RuntimeSetting.id == SINGLETON_ID)
        )
        return result.scalar_one()
