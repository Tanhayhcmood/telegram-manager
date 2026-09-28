"""Shared live Telegram snapshot used by the stats and groups-list views."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

from app.services.telegram_service import TelegramUserService
from app.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class LiveGroupRefresh:
    refreshed_at: datetime
    live: bool
    live_group_count: int | None
    error: str | None = None


class LiveGroupStateService:
    """Refresh the DB from Telegram once for concurrent view requests.

    The one-second cache prevents two buttons pressed almost simultaneously
    from starting duplicate get_dialogs requests. It is deliberately short:
    every normal view request still gets a fresh Telegram snapshot.
    """

    _instance: "LiveGroupStateService | None" = None

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._last_refresh_monotonic: float = 0.0
        self._last_result: LiveGroupRefresh | None = None

    @classmethod
    def get_instance(cls) -> "LiveGroupStateService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def refresh(self) -> LiveGroupRefresh:
        loop = asyncio.get_running_loop()
        now = loop.time()
        if (
            self._last_result is not None
            and now - self._last_refresh_monotonic < 1.0
        ):
            return self._last_result

        async with self._lock:
            now = loop.time()
            if (
                self._last_result is not None
                and now - self._last_refresh_monotonic < 1.0
            ):
                return self._last_result

            refreshed_at = datetime.now(timezone.utc)
            tg = TelegramUserService.get_instance()
            if not tg.is_running():
                result = LiveGroupRefresh(
                    refreshed_at=refreshed_at,
                    live=False,
                    live_group_count=None,
                    error="User Client متصل نیست",
                )
                self._remember(result, loop.time())
                return result

            try:
                _, total = await asyncio.wait_for(
                    tg.sync_dialogs_to_db(strict=True),
                    timeout=75.0,
                )
                result = LiveGroupRefresh(
                    refreshed_at=datetime.now(timezone.utc),
                    live=True,
                    live_group_count=total,
                )
            except asyncio.TimeoutError:
                result = LiveGroupRefresh(
                    refreshed_at=datetime.now(timezone.utc),
                    live=False,
                    live_group_count=None,
                    error="دریافت snapshot از Telegram بیشتر از ۷۵ ثانیه طول کشید",
                )
                logger.warning("Live group snapshot timed out")
            except Exception as exc:
                result = LiveGroupRefresh(
                    refreshed_at=datetime.now(timezone.utc),
                    live=False,
                    live_group_count=None,
                    error=f"خطای دریافت از Telegram: {str(exc)[:160]}",
                )
                logger.warning("Live group snapshot failed: %s", exc)

            self._remember(result, loop.time())
            return result

    def _remember(self, result: LiveGroupRefresh, monotonic: float) -> None:
        self._last_result = result
        self._last_refresh_monotonic = monotonic