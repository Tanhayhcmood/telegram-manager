"""
APScheduler-based task scheduler.
Jobs:
  - Daily PostgreSQL backup
  - Auto-retry failed group joins
  - Daily stats report to admins
"""
from datetime import datetime, timezone, timedelta
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)


class SchedulerService:
    _instance: "SchedulerService | None" = None

    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._bot: Any = None

    @classmethod
    def get_instance(cls) -> "SchedulerService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def set_bot(self, bot: Any) -> None:
        self._bot = bot

    def start(self) -> None:
        # Daily backup
        self._scheduler.add_job(
            self._daily_backup,
            CronTrigger(hour=settings.BACKUP_DAILY_HOUR, minute=0),
            id="daily_backup",
            replace_existing=True,
            misfire_grace_time=3600,
        )

        # Auto-retry failed joins
        if settings.RETRY_FAILED_JOINS:
            self._scheduler.add_job(
                self._retry_failed_joins,
                IntervalTrigger(hours=settings.RETRY_INTERVAL_HOURS),
                id="retry_failed_joins",
                replace_existing=True,
                misfire_grace_time=600,
            )

        # Daily stats at 08:00 UTC
        self._scheduler.add_job(
            self._daily_stats_report,
            CronTrigger(hour=8, minute=0),
            id="daily_stats",
            replace_existing=True,
            misfire_grace_time=3600,
        )

        # Auto-sync live Telethon dialogs into the DB every 10 minutes, so groups
        # the account was kicked/banned from (or newly joined via other means)
        # don't sit stale as JOINED and keep wasting broadcast attempts.
        self._scheduler.add_job(
            self._auto_sync_dialogs,
            IntervalTrigger(minutes=10),
            id="auto_sync_dialogs",
            replace_existing=True,
            misfire_grace_time=600,
            max_instances=1,
        )

        # Reload missed PENDING/APPROVED groups every 5 minutes.
        # The join queue processes one group per ~9 min naturally, but if a group
        # was added to the DB while the queue was draining (e.g. during discovery),
        # or if the bot restarted and missed some groups, this periodic scan
        # picks them up without requiring a manual restart.
        self._scheduler.add_job(
            self._reload_pending_groups,
            IntervalTrigger(minutes=5),
            id="reload_pending_groups",
            replace_existing=True,
            misfire_grace_time=60,
            max_instances=1,
        )

        # Retry links that were discovered while Telegram was reconnecting or
        # temporarily refused entity resolution. They remain PENDING until
        # this succeeds, so a transient outage cannot permanently lose a link.
        self._scheduler.add_job(
            self._retry_pending_links,
            IntervalTrigger(minutes=3),
            id="retry_pending_links",
            replace_existing=True,
            misfire_grace_time=60,
            max_instances=1,
        )

        self._scheduler.start()
        logger.info("Scheduler started — %d jobs registered", len(self._scheduler.get_jobs()))

    def stop(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")

    async def _daily_backup(self) -> None:
        logger.info("Running scheduled daily backup")
        try:
            from app.services.backup_service import BackupService
            svc = BackupService()
            path = await svc.create_backup(actor="scheduler")
            if path:
                from app.services.notification_service import NotificationService
                await NotificationService.get_instance().notify_info(
                    f"💾 بکاپ روزانه ساخته شد:\n`{path}`"
                )
        except Exception as exc:
            logger.error("Scheduled backup failed: %s", exc, exc_info=True)
            from app.services.notification_service import NotificationService
            await NotificationService.get_instance().notify_critical(
                "بکاپ روزانه ناموفق", str(exc)[:300]
            )

    async def _retry_failed_joins(self) -> None:
        logger.info("Running auto-retry for failed joins")
        try:
            from app.database.connection import AsyncSessionLocal
            from app.repositories import GroupRepository
            from app.repositories.join_attempt_repository import JoinAttemptRepository
            from app.models.group import GroupStatus
            from app.services.join_queue_service import JoinQueueService

            async with AsyncSessionLocal() as session:
                group_repo = GroupRepository(session)
                attempt_repo = JoinAttemptRepository(session)
                failed_groups = await group_repo.get_by_status(GroupStatus.FAILED, limit=100)

                queued = 0
                for group in failed_groups:
                    attempt_history = await attempt_repo.get_for_group(group.group_id)
                    if attempt_history and (
                        (attempt_history[0].error or "").startswith(("expired:", "failed:"))
                    ):
                        logger.info(
                            "Skipping permanent join failure retry for group_id=%d error=%s",
                            group.group_id,
                            attempt_history[0].error,
                        )
                        continue
                    attempts = len(attempt_history)
                    if attempts < settings.RETRY_MAX_ATTEMPTS and group.invite_link:
                        jq = JoinQueueService.get_instance()
                        await jq.enqueue(
                            group_id=group.group_id,
                            link=group.invite_link,
                            title=group.title,
                            attempt=attempts + 1,
                        )
                        queued += 1

            if queued:
                logger.info("Queued %d failed joins for retry", queued)
                from app.services.notification_service import NotificationService
                await NotificationService.get_instance().notify_info(
                    f"🔄 {queued} گروه ناموفق برای تلاش مجدد در صف قرار گرفت"
                )
        except Exception as exc:
            logger.error("Auto-retry failed: %s", exc, exc_info=True)

    async def _reload_pending_groups(self) -> None:
        """Every 9 minutes: make sure all PENDING/APPROVED groups are in the join queue.

        Handles cases where a group was added to the DB while the queue was
        draining, or where the bot restarted between discovery and queueing.
        This is a no-op when all groups are already queued (duplicates skipped).
        """
        try:
            from app.services.join_queue_service import JoinQueueService
            jq = JoinQueueService.get_instance()
            await jq._reload_pending_from_db()
            logger.debug(
                "Periodic pending-reload complete — queue_size=%d",
                jq.queue_size(),
            )
        except Exception as exc:
            logger.warning("Periodic pending-reload failed: %s", exc)

    async def _retry_pending_links(self) -> None:
        """Revalidate unresolved links without requiring a new message event."""
        try:
            from app.services.telegram_service import TelegramUserService
            from app.services.discovery_service import DiscoveryService
            from app.database.connection import AsyncSessionLocal
            from app.repositories.discovered_link_repository import DiscoveredLinkRepository

            tg = TelegramUserService.get_instance()
            if not tg.is_running():
                return
            cooldown_remaining = tg.entity_resolve_cooldown_remaining()
            if cooldown_remaining:
                logger.info(
                    "Skipping pending-link retry during Telegram FloodWait (%ds remaining)",
                    cooldown_remaining,
                )
                return

            async with AsyncSessionLocal() as session:
                repo = DiscoveredLinkRepository(session)
                pending = await repo.get_pending(limit=200)
                links = [row.link for row in pending]

            if not links:
                return

            discovery = DiscoveryService(tg)
            # Resolve one link at a time. Four concurrent ResolveUsername RPCs
            # can trigger a long Telegram FloodWait and block the review UI.
            checked = 0
            for link in links[:25]:
                if tg.entity_resolve_cooldown_remaining():
                    break
                try:
                    await discovery.retry_pending_link(link)
                except Exception as exc:
                    logger.warning("Pending link retry failed for %s: %s", link, exc)
                checked += 1
            logger.info(
                "Pending link retry complete: checked=%d available=%d total_pending=%d",
                checked,
                len(links[:25]),
                len(links),
            )
        except Exception as exc:
            logger.warning("Periodic pending-link retry failed: %s", exc)

    async def _auto_sync_dialogs(self) -> None:
        """Periodically re-sync live Telethon dialogs into the DB, marking any
        group the account is no longer a member of as LEFT. Runs alongside the
        per-broadcast LEFT-marking in BroadcastQueueService as a safety net
        (e.g. for accounts kicked while no broadcast is running)."""
        try:
            from app.services.telegram_service import TelegramUserService
            tg = TelegramUserService.get_instance()
            if not tg.is_running():
                return
            new_count, total = await tg.sync_dialogs_to_db()
            logger.info(
                "Auto dialog sync complete: %d total live groups, %d newly added",
                total, new_count,
            )
        except Exception as exc:
            logger.warning("Auto dialog sync failed: %s", exc)

    async def _daily_stats_report(self) -> None:
        try:
            from app.services.stats_service import StatsService
            stats = await StatsService().get_stats()
            from app.services.notification_service import NotificationService
            ns = NotificationService.get_instance()
            await ns.notify(
                "📊 *گزارش روزانه*\n\n"
                f"👥 کل گروه‌ها: `{stats.total_groups}` | عضو شده: `{stats.joined_groups}`\n"
                f"🔗 لینک‌های کشف‌شده: `{stats.total_links}`\n"
                f"👤 کاربران مخاطب: `{stats.total_contacted_users}`\n"
                f"❌ عضویت ناموفق: `{stats.failed_groups}`"
            )
        except Exception as exc:
            logger.error("Daily stats report failed: %s", exc)
