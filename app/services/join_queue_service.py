"""
Sequential join queue with random jitter and daily rate-limit.
All discovered group links pass through this queue — never joined in parallel.
"""
import asyncio
import random
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.safety import AUTOMATED_GROUP_JOINS_ENABLED
from app.database.connection import AsyncSessionLocal
from app.repositories import GroupRepository, LogRepository
from app.repositories.join_attempt_repository import JoinAttemptRepository
from app.models.group import GroupStatus
from app.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class JoinTask:
    group_id: int
    link: str
    title: str | None
    attempt_number: int = 1


class JoinQueueService:
    """Single-consumer FIFO queue for joining Telegram groups."""

    _instance: "JoinQueueService | None" = None

    def __init__(self) -> None:
        self._queue: asyncio.Queue[JoinTask] = asyncio.Queue()
        self._running = False
        self._worker_task: asyncio.Task | None = None
        self._tg: Any = None

    @classmethod
    def get_instance(cls) -> "JoinQueueService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def set_tg_service(self, tg: Any) -> None:
        self._tg = tg

    async def enqueue(self, group_id: int, link: str, title: str | None, attempt: int = 1) -> None:
        if not AUTOMATED_GROUP_JOINS_ENABLED:
            raise RuntimeError("Automated group joining is disabled in this deployment.")
        task = JoinTask(group_id=group_id, link=link, title=title, attempt_number=attempt)
        await self._queue.put(task)
        logger.info("Queued join task: group_id=%d title=%r queue_size=%d", group_id, title, self._queue.qsize())

    async def start(self) -> None:
        if not AUTOMATED_GROUP_JOINS_ENABLED:
            raise RuntimeError("Automated group joining is disabled in this deployment.")
        if self._running:
            return
        self._running = True
        self._worker_task = asyncio.create_task(self._worker(), name="join-queue-worker")
        logger.info("Join queue worker started")

    async def stop(self) -> None:
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        logger.info("Join queue worker stopped")

    def queue_size(self) -> int:
        return self._queue.qsize()

    async def _worker(self) -> None:
        while self._running:
            try:
                task = await asyncio.wait_for(self._queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            try:
                await self._process(task)
            except Exception as exc:
                logger.error("Join worker unhandled error: %s", exc, exc_info=True)
            finally:
                self._queue.task_done()

    async def _process(self, task: JoinTask) -> None:
        if not AUTOMATED_GROUP_JOINS_ENABLED:
            logger.warning("Dropping queued join because automated joining is disabled")
            return
        # Fail fast: check TelegramUserService before doing anything
        if self._tg is None:
            logger.error("TelegramUserService not set on JoinQueueService — dropping task for group_id=%d", task.group_id)
            return

        # Check daily rate limit
        async with AsyncSessionLocal() as session:
            attempt_repo = JoinAttemptRepository(session)
            today_count = await attempt_repo.count_today()

        if today_count >= settings.MAX_JOINS_PER_DAY:
            logger.warning(
                "Daily join limit (%d) reached — requeueing group_id=%d for later",
                settings.MAX_JOINS_PER_DAY, task.group_id,
            )
            # Re-enqueue at end of queue and pause the worker for a long interval.
            # Sleeping BEFORE re-enqueue ensures we don't spin through the entire
            # queue only to re-enqueue every task back immediately.
            await asyncio.sleep(300)
            await self._queue.put(task)
            return

        # Random jitter delay (anti-detection)
        from app.services.runtime_config_service import RuntimeConfigService

        delay_min, delay_max = RuntimeConfigService.get_instance().get_join_delay()
        delay = random.uniform(delay_min, delay_max)
        logger.info(
            "Waiting %.0fs before joining group_id=%d (%r) [range: %d-%ds]",
            delay, task.group_id, task.title, delay_min, delay_max,
        )
        await asyncio.sleep(delay)

        success = await self._tg.join_group(task.link)

        async with AsyncSessionLocal() as session:
            group_repo = GroupRepository(session)
            log_repo = LogRepository(session)
            attempt_repo = JoinAttemptRepository(session)

            group = await group_repo.get_by_group_id(task.group_id)

            await attempt_repo.add(
                group_id=task.group_id,
                invite_link=task.link,
                attempt_number=task.attempt_number,
                success=success,
                error=None if success else "join_failed",
            )

            if group:
                if success:
                    group.status = GroupStatus.JOINED
                    group.join_date = datetime.now(timezone.utc)
                    await log_repo.add(
                        action="group_joined",
                        result="success",
                        target=task.link,
                        details=f"group_id={task.group_id} title={task.title!r} attempt={task.attempt_number}",
                    )
                    logger.info("✅ Joined group_id=%d (%r)", task.group_id, task.title)
                else:
                    group.status = GroupStatus.FAILED
                    await log_repo.add(
                        action="group_join_failed",
                        result="error",
                        target=task.link,
                        details=f"group_id={task.group_id} title={task.title!r} attempt={task.attempt_number}",
                    )
                    logger.warning("❌ Failed to join group_id=%d (%r)", task.group_id, task.title)

            await session.commit()

        # Notify admins
        if settings.get_admin_id_list():
            await self._notify_admins(task, success)

    async def _notify_admins(self, task: JoinTask, success: bool) -> None:
        try:
            from app.services.notification_service import NotificationService
            ns = NotificationService.get_instance()
            emoji = "✅" if success else "❌"
            title = task.title or str(task.group_id)
            await ns.notify(
                f"{emoji} <b>عضویت {'موفق' if success else 'ناموفق'}</b>\n"
                f"گروه: <code>{title}</code>\n"
                f"شناسه: <code>{task.group_id}</code>\n"
                f"تلاش: <code>{task.attempt_number}</code>",
                parse_mode="HTML",
            )
        except Exception:
            pass
