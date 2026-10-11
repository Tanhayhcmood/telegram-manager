"""
Periodic health monitoring for the Telegram user client.
Detects soft-bans, disconnects, and session issues — alerts admins immediately.
"""
import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from app.config import settings
from app.database.connection import AsyncSessionLocal
from app.repositories import LogRepository
from app.utils.logger import get_logger

logger = get_logger(__name__)

CHECK_INTERVAL = 60          # seconds between checks
SOFT_BAN_THRESHOLD = 3       # consecutive failures before alert
MESSAGE_TIMEOUT = 15         # seconds to wait for Telegram API response


class HealthService:
    _instance: "HealthService | None" = None

    def __init__(self) -> None:
        self._tg: Any = None
        self._running = False
        self._task: asyncio.Task | None = None
        self._consecutive_failures = 0
        self._last_ok: datetime | None = None
        self._alerted = False
        self._recovery_callback: Callable[[], Awaitable[None]] | None = None

    @classmethod
    def get_instance(cls) -> "HealthService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def set_tg_service(self, tg: Any) -> None:
        self._tg = tg

    def set_recovery_callback(self, callback: Callable[[], Awaitable[None]]) -> None:
        self._recovery_callback = callback

    async def start(self) -> None:
        if self._running:
            return
        # Reset alert state on (re)start so a newly-restarted service
        # will re-alert if it continues to fail.
        self._alerted = False
        self._consecutive_failures = 0
        self._last_ok = None
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="health-monitor")
        logger.info("Health monitor started (interval=%ds)", CHECK_INTERVAL)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def is_healthy(self) -> bool:
        if self._tg is None or not self._tg.is_running():
            return False
        is_expected_to_run = getattr(self._tg, "is_expected_to_run", None)
        if callable(is_expected_to_run) and not is_expected_to_run():
            return False
        return self._consecutive_failures < SOFT_BAN_THRESHOLD

    def is_ready(self) -> bool:
        """Strict status for external monitors: require a recent successful Telegram probe."""
        if not self.is_healthy() or self._consecutive_failures:
            return False
        if self._last_ok is None:
            return False
        age = (datetime.now(timezone.utc) - self._last_ok).total_seconds()
        return age <= CHECK_INTERVAL * 2

    def last_ok_at(self) -> datetime | None:
        return self._last_ok

    async def _loop(self) -> None:
        while self._running:
            await self._check()
            await asyncio.sleep(CHECK_INTERVAL)

    async def _check(self) -> None:
        if self._tg is None:
            return

        is_expected_to_run = getattr(self._tg, "is_expected_to_run", None)
        if callable(is_expected_to_run) and not is_expected_to_run():
            # An admin deliberately stopped the account client; do not restart it.
            self._consecutive_failures = 0
            self._last_ok = None
            self._alerted = False
            return

        try:
            if not self._tg.is_running():
                reconnect = getattr(self._tg, "reconnect_if_needed", None)
                if reconnect is None or not await reconnect():
                    raise RuntimeError("User client is not running")
                if self._recovery_callback is not None:
                    await self._recovery_callback()

            # Ping Telegram — get_me() is the lightest possible API call
            me = await asyncio.wait_for(
                self._tg.client.get_me(),
                timeout=MESSAGE_TIMEOUT,
            )
            if me is None:
                raise RuntimeError("get_me() returned None")

            self._consecutive_failures = 0
            self._last_ok = datetime.now(timezone.utc)
            self._alerted = False

        except Exception as exc:
            self._consecutive_failures += 1
            logger.warning(
                "Health check failed (consecutive=%d): %s",
                self._consecutive_failures, exc,
            )

            await self._log_failure(str(exc))

            if self._consecutive_failures >= SOFT_BAN_THRESHOLD and not self._alerted:
                self._alerted = True
                await self._alert(str(exc))

    async def _log_failure(self, error: str) -> None:
        try:
            async with AsyncSessionLocal() as session:
                log_repo = LogRepository(session)
                await log_repo.add(
                    action="health_check_failed",
                    result="error",
                    error_message=error,
                    details=f"consecutive_failures={self._consecutive_failures}",
                )
                await session.commit()
        except Exception:
            pass

    async def _alert(self, error: str) -> None:
        try:
            from app.services.notification_service import NotificationService
            ns = NotificationService.get_instance()
            await ns.notify_critical(
                "⚠️ User Client ناسالم",
                f"تعداد خطاهای متوالی: <code>{self._consecutive_failures}</code>\n"
                f"خطا: <code>{error[:200]}</code>\n\n"
                "ربات ممکن است soft-ban شده باشد یا session منقضی شده.",
            )
        except Exception:
            pass
