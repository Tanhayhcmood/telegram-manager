"""
Live-adjustable runtime configuration.

Values here start from env defaults (app.config.settings) but can be
overridden by an admin at any time through the bot UI. Overrides are
persisted to the `runtime_settings` DB row (survives restarts) and also
kept in an in-memory cache on this singleton so every reader in the same
process sees the new value instantly — no restart, no polling delay.
"""
import asyncio
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.database.connection import AsyncSessionLocal
from app.repositories.runtime_setting_repository import RuntimeSettingRepository
from app.utils.logger import get_logger

logger = get_logger(__name__)


class RuntimeConfigService:
    _instance: "RuntimeConfigService | None" = None

    def __init__(self) -> None:
        # In-memory cache — starts from env defaults until load() runs.
        self._join_delay_min: int = settings.JOIN_DELAY_MIN
        self._join_delay_max: int = settings.JOIN_DELAY_MAX
        self._loaded = False
        # Serializes concurrent set_join_delay() calls so a DB write followed
        # by the in-memory cache update always happens as one atomic step —
        # otherwise two interleaved admin edits could leave the cache holding
        # an older value than what's actually persisted in the DB.
        self._lock = asyncio.Lock()

    @classmethod
    def get_instance(cls) -> "RuntimeConfigService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def load(self) -> None:
        """Load persisted overrides from DB into the in-memory cache. Call once at startup."""
        try:
            async with AsyncSessionLocal() as session:
                repo = RuntimeSettingRepository(session)
                row = await repo.get_or_create()
            self._join_delay_min = row.join_delay_min
            self._join_delay_max = row.join_delay_max
            self._loaded = True
            logger.info(
                "Runtime config loaded: join_delay=[%d, %d]s",
                self._join_delay_min, self._join_delay_max,
            )
        except Exception as exc:
            logger.error(
                "Failed to load runtime_settings from DB — falling back to env defaults: %s",
                exc, exc_info=True,
            )

    def get_join_delay(self) -> tuple[int, int]:
        """Return (min_seconds, max_seconds) for the join-queue anti-detection jitter."""
        return self._join_delay_min, self._join_delay_max

    async def set_join_delay(self, delay_min: int, delay_max: int) -> None:
        """Persist a new join-delay range and update the in-memory cache immediately.

        Every subsequent read (queue status, health screen, the join worker's
        next iteration) sees the new value right away — no restart needed.
        """
        if delay_min <= 0 or delay_max <= 0:
            raise ValueError("مقادیر باید بزرگ‌تر از صفر باشند.")
        if delay_max < delay_min:
            raise ValueError("حداکثر باید بزرگ‌تر یا مساوی حداقل باشد.")

        async with self._lock:
            async with AsyncSessionLocal() as session:
                repo = RuntimeSettingRepository(session)
                row = await repo.update_join_delay(delay_min, delay_max)

            # Update the cache from the row the DB actually persisted (not the
            # raw input) so cache and DB can never diverge even under overlap.
            self._join_delay_min = row.join_delay_min
            self._join_delay_max = row.join_delay_max
            logger.info(
                "Join delay updated by admin: [%d, %d]s (live — takes effect on the next queued join)",
                self._join_delay_min, self._join_delay_max,
            )

    async def get_join_not_before_at(self) -> datetime | None:
        async with AsyncSessionLocal() as session:
            repo = RuntimeSettingRepository(session)
            return await repo.get_join_not_before_at()

    async def extend_join_not_before_at(self, seconds: float) -> datetime:
        """Persist a global join cooldown, only extending any existing deadline."""
        if seconds <= 0:
            raise ValueError("Cooldown must be greater than zero.")

        deadline = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        async with self._lock:
            async with AsyncSessionLocal() as session:
                repo = RuntimeSettingRepository(session)
                row = await repo.extend_join_not_before_at(deadline)
                return row.join_not_before_at or deadline
