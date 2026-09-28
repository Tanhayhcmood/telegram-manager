"""
Periodic health monitoring + auto-recovery for the Telegram user client.

Responsibilities:
  1. Keep the Telegram account visibly online (UpdateStatusRequest every cycle).
  2. Auto-reconnect if the Telethon client drops its connection.
  3. Alert admins if consecutive failures reach the soft-ban threshold.
  4. Watch the JoinQueueService worker task and restart it if it crashes.
"""
import asyncio
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.database.connection import AsyncSessionLocal
from app.repositories import LogRepository
from app.utils.logger import get_logger

logger = get_logger(__name__)

CHECK_INTERVAL = 60          # seconds between health checks
SOFT_BAN_THRESHOLD = 3       # consecutive failures before alert
MESSAGE_TIMEOUT = 30         # seconds to wait for Telegram API response
RECONNECT_COOLDOWN = 30      # seconds to wait before retrying a failed reconnect


class HealthService:
    _instance: "HealthService | None" = None

    def __init__(self) -> None:
        self._tg: Any = None
        self._jq: Any = None          # JoinQueueService (for worker watchdog)
        self._running = False
        self._task: asyncio.Task | None = None
        self._consecutive_failures = 0
        self._last_ok: datetime | None = None
        self._alerted = False
        self._last_reconnect_attempt: float = 0.0

    @classmethod
    def get_instance(cls) -> "HealthService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def set_tg_service(self, tg: Any) -> None:
        self._tg = tg

    def set_join_queue(self, jq: Any) -> None:
        """Provide a reference to JoinQueueService for worker watchdog."""
        self._jq = jq

    async def start(self) -> None:
        if self._running:
            return
        self._alerted = False
        self._consecutive_failures = 0
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
        return self._consecutive_failures < SOFT_BAN_THRESHOLD

    def last_ok_at(self) -> datetime | None:
        return self._last_ok

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        while self._running:
            # Verify immediately after startup instead of leaving the
            # dashboard with the optimistic default state for one full
            # interval. Later checks continue on the normal cadence.
            await self._check()
            await self._watch_join_worker()
            await asyncio.sleep(CHECK_INTERVAL)

    async def _check(self) -> None:
        if self._tg is None:
            return

        try:
            # ── Step 1: auto-reconnect if client dropped ─────────────────────
            if not self._tg.is_running():
                now = asyncio.get_event_loop().time()
                if now - self._last_reconnect_attempt < RECONNECT_COOLDOWN:
                    raise RuntimeError("User client offline — reconnect on cooldown")
                self._last_reconnect_attempt = now
                logger.warning("User client is not running — triggering reconnect")
                await self._tg.reconnect()
                if not self._tg.is_running():
                    raise RuntimeError("User client still offline after reconnect attempt")

            # ── Step 2: keep account online (prevent Telegram offline timeout) ─
            await self._tg.keep_online()

            # ── Step 3: ping Telegram — lightest possible API call ───────────
            me = await asyncio.wait_for(
                self._tg.client.get_me(),
                timeout=MESSAGE_TIMEOUT,
            )
            if me is None:
                raise RuntimeError("get_me() returned None")

            # All good
            self._consecutive_failures = 0
            self._last_ok = datetime.now(timezone.utc)
            self._alerted = False

        except Exception as exc:
            # ── FloodWait: account is healthy, just rate-limited by Telegram ──
            # Do NOT count as failure — client is connected, Telegram throttling.
            exc_str = str(exc)
            is_flood = (
                "FloodWait" in type(exc).__name__
                or "A wait of" in exc_str
                or "flood" in exc_str.lower()
            )
            if is_flood:
                logger.info(
                    "Health check: Telegram rate-limit (FloodWait) — "
                    "client is connected, skipping failure count. detail: %s",
                    exc_str,
                )
                # Still mark last_ok so dashboard shows client is alive
                self._last_ok = datetime.now(timezone.utc)
                return

            self._consecutive_failures += 1
            logger.warning(
                "Health check failed (consecutive=%d): %s",
                self._consecutive_failures, exc,
            )
            await self._log_failure(str(exc))

            if self._consecutive_failures >= SOFT_BAN_THRESHOLD and not self._alerted:
                self._alerted = True
                await self._alert(str(exc))

    async def _watch_join_worker(self) -> None:
        """Restart the JoinQueueService worker if it has silently died."""
        if self._jq is None:
            return
        try:
            worker_task: asyncio.Task | None = getattr(self._jq, "_worker_task", None)
            if worker_task is not None and worker_task.done() and self._jq._running:
                exc = worker_task.exception() if not worker_task.cancelled() else None
                logger.error(
                    "Join queue worker task died unexpectedly (exc=%s) — restarting",
                    exc,
                )
                self._jq._worker_task = asyncio.create_task(
                    self._jq._worker(), name="join-queue-worker-restarted"
                )
                logger.info("Join queue worker restarted by health watchdog")
        except Exception as exc:
            logger.warning("Join worker watchdog error: %s", exc)

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
        # ── Pause join queue to protect daily quota while account is restricted ─
        try:
            if self._jq is not None:
                pause_secs = 3600.0  # 1 hour — give Telegram time to lift restriction
                self._jq.pause(pause_secs)
                logger.warning(
                    "Soft-ban detected — join queue paused for %.0fs to protect daily quota",
                    pause_secs,
                )
        except Exception as exc:
            logger.warning("Could not pause join queue on soft-ban alert: %s", exc)

        try:
            from app.services.notification_service import NotificationService
            ns = NotificationService.get_instance()
            await ns.notify_critical(
                "⚠️ User Client ناسالم",
                f"تعداد خطاهای متوالی: <code>{self._consecutive_failures}</code>\n"
                f"خطا: <code>{error[:200]}</code>\n\n"
                "ربات ممکن است soft-ban شده باشد یا session منقضی شده.\n"
                "در حال تلاش برای reconnect خودکار …\n\n"
                "⏸ <b>صف عضویت 1 ساعت متوقف شد</b> تا quota روزانه حفظ شود.",
            )
        except Exception:
            pass
