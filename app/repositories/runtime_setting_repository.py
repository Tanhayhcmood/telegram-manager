from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.runtime_setting import RuntimeSetting
from .base import BaseRepository

SINGLETON_ID = 1


class RuntimeSettingRepository(BaseRepository[RuntimeSetting]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(RuntimeSetting, session)

    async def get_or_create(self) -> RuntimeSetting:
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
        stmt = pg_insert(RuntimeSetting).values(
            id=SINGLETON_ID,
            join_delay_min=delay_min,
            join_delay_max=delay_max,
        ).on_conflict_do_update(
            index_elements=["id"],
            set_={
                "join_delay_min": delay_min,
                "join_delay_max": delay_max,
                "updated_at": func.now(),
            },
        )
        await self._session.execute(stmt)
        await self._session.commit()
        result = await self._session.execute(
            select(RuntimeSetting).where(RuntimeSetting.id == SINGLETON_ID)
        )
        return result.scalar_one()
