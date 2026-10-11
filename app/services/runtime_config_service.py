import asyncio

from app.config import settings
from app.database.connection import AsyncSessionLocal
from app.repositories.runtime_setting_repository import RuntimeSettingRepository
from app.utils.logger import get_logger

logger = get_logger(__name__)


class RuntimeConfigService:
    """Persist and serve join-delay overrides without restarting the bot."""

    _instance: "RuntimeConfigService | None" = None

    def __init__(self) -> None:
        self._join_delay_min = settings.JOIN_DELAY_MIN
        self._join_delay_max = settings.JOIN_DELAY_MAX
        self._lock = asyncio.Lock()

    @classmethod
    def get_instance(cls) -> "RuntimeConfigService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def load(self) -> None:
        try:
            async with AsyncSessionLocal() as session:
                row = await RuntimeSettingRepository(session).get_or_create()
            self._join_delay_min = row.join_delay_min
            self._join_delay_max = row.join_delay_max
            logger.info(
                "Runtime join delay loaded: %d-%d seconds",
                self._join_delay_min,
                self._join_delay_max,
            )
        except Exception:
            logger.exception("Could not load runtime join-delay settings; using environment defaults")

    def get_join_delay(self) -> tuple[int, int]:
        return self._join_delay_min, self._join_delay_max

    async def set_join_delay(self, delay_min: int, delay_max: int) -> None:
        if delay_min <= 0 or delay_max < delay_min:
            raise ValueError("بازه فاصله عضویت معتبر نیست.")

        async with self._lock:
            async with AsyncSessionLocal() as session:
                row = await RuntimeSettingRepository(session).update_join_delay(
                    delay_min,
                    delay_max,
                )
            self._join_delay_min = row.join_delay_min
            self._join_delay_max = row.join_delay_max
            logger.info(
                "Runtime join delay updated to %d-%d seconds",
                self._join_delay_min,
                self._join_delay_max,
            )
