"""
Sequential join queue with random jitter and per-day rate-limit.
All discovered group links pass through this queue — never joined in parallel.

Key guarantees:
  - PENDING groups from DB are reloaded into the queue on every startup,
    so a bot restart never loses groups that were waiting to be joined.
  - The worker loop is self-healing: if an inner error kills the consume
    loop, the outer wrapper restarts it after a short delay.
  - JoinQueueService is a singleton; HealthService can watch its worker task.
  - Daily join limit (MAX_JOINS_PER_DAY) is enforced with an in-memory counter
    that resets at UTC midnight. When the limit is reached, further tasks are
    re-queued and processed the following day.
"""
import asyncio
import random
from datetime import date, datetime, timezone, timedelta
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.database.connection import AsyncSessionLocal
from app.repositories import GroupRepository, LogRepository, DiscoveredLinkRepository
from app.repositories.join_attempt_repository import JoinAttemptRepository
from app.models.group import GroupStatus
from app.models.discovered_link import LinkStatus
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
        # Track IDs already in queue to avoid duplicate requeue on reload
        self._queued_ids: set[int] = set()
        # Daily join counter — reset every UTC midnight
        self._daily_join_date: date = date.today()
        self._daily_join_count: int = 0
        # Pause flag: set by HealthService when a soft-ban / account restriction is detected.
        # While paused, _process re-queues tasks instead of attempting joins,
        # so groups stay PENDING and the daily counter is not burned.
        self._paused: bool = False
        self._pause_until: float = 0.0  # asyncio monotonic time

    @classmethod
    def get_instance(cls) -> "JoinQueueService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def set_tg_service(self, tg: Any) -> None:
        self._tg = tg

    async def enqueue(self, group_id: int, link: str, title: str | None, attempt: int = 1) -> None:
        if group_id in self._queued_ids:
            logger.debug("Group %d already in queue — skipping duplicate enqueue", group_id)
            return
        task = JoinTask(group_id=group_id, link=link, title=title, attempt_number=attempt)
        self._queued_ids.add(group_id)
        await self._queue.put(task)
        logger.info("Queued join task: group_id=%d title=%r queue_size=%d", group_id, title, self._queue.qsize())

    def get_daily_stats(self) -> dict:
        """Return current-day join counter stats."""
        return {
            "date": self._daily_join_date.isoformat(),
            "count": self._daily_join_count,
            "limit": settings.MAX_JOINS_PER_DAY,
            "remaining": max(0, settings.MAX_JOINS_PER_DAY - self._daily_join_count),
        }

    def pause(self, seconds: float = 3600.0) -> None:
        """Pause the join queue for *seconds* seconds.

        Called by HealthService when a soft-ban / consecutive failures are detected.
        While paused, _process re-queues every task without attempting a join,
        so groups stay PENDING and the daily counter is not wasted.
        """
        import asyncio as _asyncio
        loop = _asyncio.get_event_loop()
        self._paused = True
        self._pause_until = loop.time() + seconds
        logger.warning(
            "Join queue PAUSED for %.0f seconds (%.1f hours) due to account restriction",
            seconds, seconds / 3600,
        )

    def resume(self) -> None:
        """Manually resume a paused queue before the timer expires."""
        self._paused = False
        self._pause_until = 0.0
        logger.info("Join queue RESUMED manually")

    def is_paused(self) -> bool:
        import asyncio as _asyncio
        if not self._paused:
            return False
        loop = _asyncio.get_event_loop()
        if loop.time() >= self._pause_until:
            self._paused = False
            self._pause_until = 0.0
            logger.info("Join queue pause expired — resuming automatically")
            return False
        return True

    async def start(self) -> None:
        if self._running:
            return
        self._running = True

        # ── Seed the in-memory daily counter from DB so a restart doesn't
        # silently reset the quota tracker (counter would otherwise start at 0
        # even if attempts already happened earlier today). ───────────────────
        await self._init_daily_counter_from_db()

        # ── Critical: reload any PENDING groups from DB on every startup ──────
        # The asyncio.Queue is in-memory. When the bot restarts (deploy, crash,
        # Render restart), groups with status=PENDING in the DB would otherwise
        # sit idle forever. This reload restores them to the queue automatically.
        await self._reload_pending_from_db()

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

    # ------------------------------------------------------------------
    # Daily limit helpers
    # ------------------------------------------------------------------

    def _reset_daily_counter_if_needed(self) -> None:
        """Reset the daily join counter when UTC date has rolled over."""
        today = date.today()
        if today != self._daily_join_date:
            logger.info(
                "New UTC day — resetting daily join counter (was %d/%d for %s)",
                self._daily_join_count, settings.MAX_JOINS_PER_DAY,
                self._daily_join_date.isoformat(),
            )
            self._daily_join_date = today
            self._daily_join_count = 0

    def _daily_limit_reached(self) -> bool:
        self._reset_daily_counter_if_needed()
        return self._daily_join_count >= settings.MAX_JOINS_PER_DAY

    def _seconds_until_midnight_utc(self) -> float:
        """Seconds remaining until the next UTC midnight."""
        now = datetime.now(timezone.utc)
        tomorrow_midnight = datetime.combine(
            now.date() + timedelta(days=1),
            datetime.min.time(),
        ).replace(tzinfo=timezone.utc)
        return (tomorrow_midnight - now).total_seconds() + 60  # +60s buffer

    # ------------------------------------------------------------------
    # Startup daily-counter seed
    # ------------------------------------------------------------------

    async def _init_daily_counter_from_db(self) -> None:
        """Seed the in-memory daily join counter from today's DB attempts.

        The counter increments on every attempt (success or failure — see
        _process()), so it must be seeded from count_today(), not just
        successful joins, or a restart mid-day would under-count and let the
        bot exceed MAX_JOINS_PER_DAY.
        """
        try:
            async with AsyncSessionLocal() as session:
                attempt_repo = JoinAttemptRepository(session)
                count = await attempt_repo.count_today()
            self._daily_join_date = date.today()
            self._daily_join_count = count
            logger.info(
                "Daily join counter seeded from DB on startup: %d/%d",
                count, settings.MAX_JOINS_PER_DAY,
            )
        except Exception as exc:
            logger.error("Failed to seed daily join counter from DB: %s", exc, exc_info=True)

    # ------------------------------------------------------------------
    # Startup reload
    # ------------------------------------------------------------------

    async def _reload_pending_from_db(self) -> None:
        """Load all PENDING and APPROVED groups from DB into the in-memory queue.

        Safe to call repeatedly — uses _queued_ids to skip duplicates.
        Groups with no invite_link are skipped (cannot join without a link).

        APPROVED groups are reloaded too: they represent groups where the bot admin
        approved the join but the bot may have restarted before the Telegram-side
        join request was sent or approved. Duplicate requests are harmless.
        """
        try:
            async with AsyncSessionLocal() as session:
                repo = GroupRepository(session)
                attempt_repo = JoinAttemptRepository(session)
                pending = await repo.get_by_status(GroupStatus.PENDING, limit=1000)
                approved = await repo.get_by_status(GroupStatus.APPROVED, limit=500)

                # For APPROVED groups, distinguish two cases:
                #   a) Bot-admin approved but join never attempted → MUST re-enqueue
                #   b) Join request sent (InviteRequestSentError) → SKIP; JoinApprovalWatcher handles these
                approved_to_rejoin = []
                approved_skipped_pending = 0
                for g in approved:
                    if await attempt_repo.has_pending_approval_attempt(g.group_id):
                        approved_skipped_pending += 1  # already waiting for Telegram admin
                    else:
                        approved_to_rejoin.append(g)  # bot crashed before first join attempt

                to_reload = pending + approved_to_rejoin

            loaded = 0
            skipped_no_link = 0
            for group in to_reload:
                if not group.invite_link:
                    skipped_no_link += 1
                    logger.debug(
                        "group %d (%s) has no invite_link — cannot auto-join, skipping",
                        group.group_id, group.status.value,
                    )
                    continue
                if group.group_id in self._queued_ids:
                    continue
                task = JoinTask(
                    group_id=group.group_id,
                    link=group.invite_link,
                    title=group.title,
                    attempt_number=1,
                )
                self._queued_ids.add(group.group_id)
                await self._queue.put(task)
                loaded += 1

            if approved_skipped_pending:
                logger.info(
                    "Skipped %d APPROVED group(s) with pending Telegram-admin approval — "
                    "JoinApprovalWatcher will update them when approved",
                    approved_skipped_pending,
                )
            if loaded:
                logger.info(
                    "Reloaded %d group(s) (PENDING + unattempted APPROVED) from DB into join queue",
                    loaded,
                )
            else:
                logger.info("No PENDING/APPROVED groups to reload — queue starts empty")
            if skipped_no_link:
                logger.debug("Skipped %d group(s) with no invite_link", skipped_no_link)
        except Exception as exc:
            logger.error("Failed to reload groups from DB: %s", exc, exc_info=True)

    # ------------------------------------------------------------------
    # Self-healing worker
    # ------------------------------------------------------------------

    async def _worker(self) -> None:
        """Self-healing outer loop. If _consume_loop crashes, restart after delay."""
        while self._running:
            try:
                await self._consume_loop()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(
                    "Join queue consume loop crashed — restarting in 5s: %s",
                    exc, exc_info=True,
                )
                await asyncio.sleep(5)

    async def _consume_loop(self) -> None:
        """Inner FIFO consumer — processes one task at a time."""
        while self._running:
            try:
                task = await asyncio.wait_for(self._queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise  # propagate so _worker breaks cleanly on shutdown

            try:
                await self._process(task)
            except Exception as exc:
                logger.error("Join worker unhandled error for group %d: %s", task.group_id, exc, exc_info=True)
            finally:
                self._queued_ids.discard(task.group_id)
                self._queue.task_done()

    # ------------------------------------------------------------------
    # Core join logic
    # ------------------------------------------------------------------

    async def _process(self, task: JoinTask) -> None:
        if self._tg is None:
            logger.error(
                "TelegramUserService not set on JoinQueueService — dropping task for group_id=%d",
                task.group_id,
            )
            return

        # ── Account restriction / soft-ban guard (pre-sleep check) ──────────────
        # Cache is_paused() once to avoid race around expiry boundary during check.
        # If paused or client offline, re-queue immediately — groups stay PENDING.
        paused_now = self.is_paused()
        client_online = self._tg.is_running()
        if paused_now or not client_online:
            reason = "queue paused (soft-ban)" if paused_now else "user client offline"
            retry_delay = (
                max(self._pause_until - asyncio.get_event_loop().time(), 60.0)
                if paused_now else 300.0
            )
            logger.warning(
                "Cannot join group_id=%d (%r): %s — re-queuing in %.0fs",
                task.group_id, task.title, reason, retry_delay,
            )
            asyncio.create_task(
                self._requeue_after_delay(task, retry_delay),
                name=f"restriction-requeue-{task.group_id}",
            )
            return

        # ── Daily limit check ─────────────────────────────────────────────────
        # Check BEFORE sleeping so we don't waste the delay period on a task
        # that will just be re-queued anyway.
        if self._daily_limit_reached():
            wait_secs = self._seconds_until_midnight_utc()
            logger.warning(
                "Daily join limit reached (%d/%d) for group_id=%d (%r) — "
                "scheduling retry in %.0f seconds (next UTC midnight)",
                self._daily_join_count, settings.MAX_JOINS_PER_DAY,
                task.group_id, task.title, wait_secs,
            )
            # Notify admins once per limit-hit (not once per queued task)
            await self._notify_daily_limit(task, wait_secs)
            # Re-enqueue after midnight
            asyncio.create_task(
                self._requeue_after_delay(task, wait_secs),
                name=f"daily-limit-requeue-{task.group_id}",
            )
            return

        # ── Anti-detection delay: randomised jitter in [MIN, MAX] seconds ─────
        # Read live (admin-adjustable) values — a change made mid-queue takes
        # effect immediately on the next task, no restart needed.
        from app.services.runtime_config_service import RuntimeConfigService
        delay_min, delay_max = RuntimeConfigService.get_instance().get_join_delay()
        delay = random.uniform(delay_min, delay_max)
        logger.info(
            "Waiting %.0fs (%.1f min) before joining group_id=%d (%r)  "
            "[daily: %d/%d]",
            delay, delay / 60, task.group_id, task.title,
            self._daily_join_count, settings.MAX_JOINS_PER_DAY,
        )
        await asyncio.sleep(delay)

        # ── Re-check limit after sleeping (another task may have filled quota) ─
        if self._daily_limit_reached():
            wait_secs = self._seconds_until_midnight_utc()
            logger.warning(
                "Daily join limit reached after delay — re-queueing group_id=%d for midnight",
                task.group_id,
            )
            asyncio.create_task(
                self._requeue_after_delay(task, wait_secs),
                name=f"daily-limit-requeue-post-sleep-{task.group_id}",
            )
            return

        # ── Post-sleep account restriction guard ──────────────────────────────
        # Client may have dropped or been restricted during the jitter sleep.
        # Re-check before making the actual Telegram API call so we don't
        # increment the daily counter or mark groups FAILED on an offline client.
        if self.is_paused() or not self._tg.is_running():
            reason = "queue paused (soft-ban)" if self.is_paused() else "user client offline"
            retry_delay = (
                max(self._pause_until - asyncio.get_event_loop().time(), 60.0)
                if self.is_paused() else 300.0
            )
            logger.warning(
                "Post-sleep restriction for group_id=%d (%r): %s — re-queuing in %.0fs",
                task.group_id, task.title, reason, retry_delay,
            )
            asyncio.create_task(
                self._requeue_after_delay(task, retry_delay),
                name=f"post-sleep-restriction-requeue-{task.group_id}",
            )
            return

        success, real_group_id, join_error = await self._tg.join_group(task.link)

        # ── Handle FloodWait: re-queue with delay instead of marking FAILED ────
        if not success and join_error and join_error.startswith("flood_wait:"):
            try:
                wait_secs = int(join_error.split(":")[1].rstrip("s"))
            except (IndexError, ValueError):
                wait_secs = 3600  # safe fallback: 1 hour

            logger.warning(
                "FloodWait for group_id=%d (%r): scheduling retry in %d seconds (~%.1fh)",
                task.group_id, task.title, wait_secs, wait_secs / 3600,
            )
            asyncio.create_task(
                self._requeue_after_delay(task, wait_secs),
                name=f"flood-requeue-{task.group_id}",
            )
            # Keep DB status as PENDING — group is not failed, just rate-limited
            return

        # ── Handle PeerFlood / soft-ban: pause queue + re-queue without FAILED ─
        # PeerFlood means Telegram temporarily restricted the account.
        # Do NOT mark group FAILED or burn the daily counter —
        # pause the whole queue for 2 hours and re-queue as PENDING.
        if not success and join_error == "peer_flood":
            pause_secs = 7200.0  # 2 hours
            logger.warning(
                "PeerFlood for group_id=%d (%r): pausing queue for %.0fs and re-queuing",
                task.group_id, task.title, pause_secs,
            )
            self.pause(pause_secs)
            asyncio.create_task(
                self._requeue_after_delay(task, pause_secs + 120),
                name=f"peer-flood-requeue-{task.group_id}",
            )
            try:
                from app.services.notification_service import NotificationService
                ns = NotificationService.get_instance()
                await ns.notify_critical(
                    "🚫 PeerFlood — صف متوقف شد",
                    f"حساب کاربری موقتاً توسط تلگرام محدود شد (PeerFlood).\n"
                    f"صف عضویت برای <b>2 ساعت</b> متوقف شد.\n"
                    f"گروه فعلی: <code>{task.title or task.group_id}</code>\n"
                    f"وضعیت: <b>PENDING</b> (تلاش مجدد بعد از رفع محدودیت)",
                )
            except Exception:
                pass
            # Keep DB status as PENDING — not a permanent failure
            return

        # ── Increment daily counter on every actual join attempt ───────────────
        # (regardless of success — each attempt consumes part of the daily quota)
        self._daily_join_count += 1
        logger.info(
            "Daily join counter: %d/%d", self._daily_join_count, settings.MAX_JOINS_PER_DAY
        )

        async with AsyncSessionLocal() as session:
            group_repo = GroupRepository(session)
            log_repo = LogRepository(session)
            attempt_repo = JoinAttemptRepository(session)
            link_repo = DiscoveredLinkRepository(session)

            group = await group_repo.get_by_group_id(task.group_id)
            discovered_link = await link_repo.get_by_link(task.link)

            if success and real_group_id and real_group_id != task.group_id:
                existing_real = await group_repo.get_by_group_id(real_group_id)
                if existing_real is None and group is not None:
                    try:
                        group.group_id = real_group_id
                        await session.flush()
                        logger.info(
                            "Updated placeholder group_id %d → real group_id %d",
                            task.group_id, real_group_id,
                        )
                    except Exception as exc:
                        logger.warning("Could not update group_id %d → %d: %s", task.group_id, real_group_id, exc)

            await attempt_repo.add(
                group_id=real_group_id or task.group_id,
                invite_link=task.link,
                attempt_number=task.attempt_number,
                success=success,
                error=join_error,
            )

            if group:
                # ── BUG FIX: groups requiring Telegram admin approval (صفحه درخواست عضویت)
                # When InviteRequestSentError is raised, join_group() returns success=True
                # with join_error='request_pending_approval'.  This does NOT mean the account
                # is in the group — it means a request was submitted.  JoinApprovalWatcher
                # watches for the ChatAction approval event and sets status → JOINED then.
                # Previously this set status=JOINED immediately, which was wrong.
                if join_error == "request_pending_approval":
                    group.status = GroupStatus.APPROVED  # awaiting Telegram admin approval
                    if discovered_link:
                        discovered_link.status = LinkStatus.APPROVED
                    logger.info(
                        "group_id=%d (%r): join request sent — status=APPROVED (awaiting Telegram admin)",
                        task.group_id, task.title,
                    )
                elif success:
                    from datetime import datetime, timezone as tz
                    group.status = GroupStatus.JOINED
                    group.join_date = datetime.now(tz.utc)
                    if discovered_link:
                        discovered_link.status = LinkStatus.JOINED
                else:
                    group.status = GroupStatus.FAILED
                    if discovered_link:
                        discovered_link.status = LinkStatus.FAILED

                if join_error == "request_pending_approval":
                    log_action = "group_join_requested"
                    log_result = "success"
                elif success:
                    log_action = "group_joined"
                    log_result = "success"
                else:
                    log_action = "group_join_failed"
                    log_result = "error"

                await log_repo.add(
                    action=log_action,
                    result=log_result,
                    target=task.link,
                    details=(
                        f"group_id={real_group_id or task.group_id} "
                        f"title={task.title!r} attempt={task.attempt_number} "
                        f"daily={self._daily_join_count}/{settings.MAX_JOINS_PER_DAY}"
                    ),
                )
                if join_error == "request_pending_approval":
                    logger.info(
                        "📨 Join request sent for group_id=%d (%r) — awaiting Telegram admin approval",
                        real_group_id or task.group_id, task.title
                    )
                elif success:
                    logger.info(
                        "✅ Joined group_id=%d (%r)", real_group_id or task.group_id, task.title
                    )
                else:
                    logger.warning(
                        "❌ Failed to join group_id=%d (%r)", task.group_id, task.title
                    )
            elif discovered_link:
                discovered_link.status = (
                    LinkStatus.JOINED if success
                    else LinkStatus.APPROVED if join_error == "request_pending_approval"
                    else LinkStatus.FAILED
                )

            await session.commit()

        # Notify admins: only on FAILED joins.
        # request_pending_approval is NOT a failure — it's a pending approval.
        is_request_pending = (join_error == "request_pending_approval")
        if settings.get_admin_id_list() and not success and not is_request_pending:
            await self._notify_admins(task, success)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _requeue_after_delay(self, task: JoinTask, delay: float) -> None:
        """Sleep delay seconds then re-enqueue the task."""
        await asyncio.sleep(delay)
        logger.info(
            "Re-enqueuing group_id=%d (%r) after %.0f-second delay",
            task.group_id, task.title, delay,
        )
        await self.enqueue(
            group_id=task.group_id,
            link=task.link,
            title=task.title,
            attempt=task.attempt_number,
        )

    async def _notify_daily_limit(self, task: JoinTask, wait_secs: float) -> None:
        """Notify admins that the daily join limit has been reached."""
        try:
            from app.services.notification_service import NotificationService
            ns = NotificationService.get_instance()
            hours = wait_secs / 3600
            await ns.notify(
                f"⏸ <b>محدودیت روزانه عضویت</b>\n\n"
                f"سهمیه روزانه به پایان رسید: "
                f"<code>{self._daily_join_count}/{settings.MAX_JOINS_PER_DAY}</code>\n"
                f"گروه <code>{task.title or task.group_id}</code> در صف انتظار شب است.\n"
                f"ادامه کار در: <b>{hours:.1f} ساعت دیگر</b> (نیمه‌شب UTC)",
                parse_mode="HTML",
            )
        except Exception:
            pass

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
                f"تلاش: <code>{task.attempt_number}</code>\n"
                f"امروز: <code>{self._daily_join_count}/{settings.MAX_JOINS_PER_DAY}</code>",
                parse_mode="HTML",
            )
        except Exception:
            pass
