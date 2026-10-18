"""
Non-blocking background broadcast service.
Broadcasts run in a dedicated asyncio task — the bot stays responsive.
Progress and results are reported to the requesting admin.
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.safety import BULK_BROADCASTS_ENABLED
from app.database.connection import AsyncSessionLocal
from app.repositories import GroupRepository, ContactedUserRepository, LogRepository
from app.models.group import GroupStatus
from app.utils.logger import get_logger

logger = get_logger(__name__)

DM_DELAY = 5        # seconds between DMs
GROUP_DELAY = 2     # seconds between group sends
MAX_STORED_JOBS = 50   # keep only last N completed jobs to prevent memory leak


@dataclass
class BroadcastJob:
    job_id: str
    target: str          # "groups" | "users"
    from_chat_id: int
    message_id: int
    actor_id: int
    bot: Any
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    total: int = 0
    success: int = 0
    failed: int = 0
    blocked: int = 0
    deactivated: int = 0
    done: bool = False
    error: str | None = None


class BroadcastQueueService:
    _instance: "BroadcastQueueService | None" = None

    def __init__(self) -> None:
        self._jobs: dict[str, BroadcastJob] = {}
        self._active: bool = False

    @classmethod
    def get_instance(cls) -> "BroadcastQueueService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def get_job(self, job_id: str) -> BroadcastJob | None:
        return self._jobs.get(job_id)

    def is_active(self) -> bool:
        return self._active

    async def start_broadcast(
        self,
        target: str,
        from_chat_id: int,
        message_id: int,
        actor_id: int,
        bot: Any,
    ) -> str:
        if not BULK_BROADCASTS_ENABLED:
            raise RuntimeError("Bulk broadcasts are disabled in this deployment.")
        if self._active:
            raise RuntimeError("یک broadcast در حال اجرا است. لطفاً صبر کنید.")

        import uuid
        job_id = str(uuid.uuid4())[:8]
        job = BroadcastJob(
            job_id=job_id,
            target=target,
            from_chat_id=from_chat_id,
            message_id=message_id,
            actor_id=actor_id,
            bot=bot,
        )
        self._jobs[job_id] = job
        asyncio.create_task(self._run(job), name=f"broadcast-{job_id}")
        return job_id

    def _prune_old_jobs(self) -> None:
        """Remove oldest completed jobs to prevent unbounded memory growth."""
        done_jobs = [(jid, j) for jid, j in self._jobs.items() if j.done]
        if len(done_jobs) > MAX_STORED_JOBS:
            # Sort by started_at ascending, remove oldest excess entries
            done_jobs.sort(key=lambda x: x[1].started_at)
            for jid, _ in done_jobs[:len(done_jobs) - MAX_STORED_JOBS]:
                del self._jobs[jid]

    async def _run(self, job: BroadcastJob) -> None:
        self._active = True
        try:
            if job.target == "groups":
                await self._send_to_groups(job)
            else:
                await self._send_to_users(job)
        except Exception as exc:
            job.error = str(exc)
            logger.error("Broadcast job %s failed: %s", job.job_id, exc, exc_info=True)
        finally:
            job.done = True
            self._active = False
            self._prune_old_jobs()
            await self._report(job)

    async def _send_to_groups(self, job: BroadcastJob) -> None:
        async with AsyncSessionLocal() as session:
            repo = GroupRepository(session)
            groups = await repo.get_joined()

        job.total = len(groups)
        actor = str(job.actor_id)

        for group in groups:
            try:
                await job.bot.forward_message(
                    chat_id=group.group_id,
                    from_chat_id=job.from_chat_id,
                    message_id=job.message_id,
                )
                job.success += 1
                await self._log("broadcast_group_sent", "success", actor, str(group.group_id))
            except Exception as exc:
                job.failed += 1
                await self._log("broadcast_group_failed", "error", actor, str(group.group_id), str(exc))
            await asyncio.sleep(GROUP_DELAY)

    async def _send_to_users(self, job: BroadcastJob) -> None:
        from app.services.telegram_service import TelegramUserService
        tg = TelegramUserService.get_instance()

        async with AsyncSessionLocal() as session:
            repo = ContactedUserRepository(session)
            users = await repo.get_active(limit=10000)

        job.total = len(users)
        actor = str(job.actor_id)

        for user in users:
            ok, reason = await tg.forward_message_to_user(
                user_id=user.user_id,
                from_chat_id=job.from_chat_id,
                message_id=job.message_id,
            )
            if ok:
                job.success += 1
                await self._log("broadcast_user_sent", "success", actor, str(user.user_id))
            else:
                job.failed += 1
                if reason == "blocked":
                    job.blocked += 1
                    await self._mark_user_blocked(user.user_id, is_deactivated=False)
                elif reason == "deactivated":
                    job.deactivated += 1
                    # Mark deactivated users as blocked so we stop DMing them.
                    # is_blocked=True is intentional: deactivated accounts can never
                    # receive messages, so they must be excluded from future sends.
                    await self._mark_user_blocked(user.user_id, is_deactivated=True)
                await self._log("broadcast_user_failed", "error", actor, str(user.user_id), reason)
            await asyncio.sleep(DM_DELAY)

    async def _mark_user_blocked(self, user_id: int, is_deactivated: bool) -> None:
        """Mark a user as blocked to exclude them from future broadcasts."""
        try:
            async with AsyncSessionLocal() as s:
                from app.repositories import ContactedUserRepository as CUR
                r = CUR(s)
                u = await r.get_by_user_id(user_id)
                if u:
                    u.is_blocked = True
                await s.commit()
        except Exception as exc:
            logger.warning("Failed to mark user %d as blocked (deactivated=%s): %s", user_id, is_deactivated, exc)

    async def _log(
        self, action: str, result: str, actor: str,
        target: str, error: str | None = None
    ) -> None:
        try:
            async with AsyncSessionLocal() as session:
                log_repo = LogRepository(session)
                await log_repo.add(action=action, result=result, actor=actor, target=target, error_message=error)
                await session.commit()
        except Exception:
            pass

    async def _report(self, job: BroadcastJob) -> None:
        try:
            target_fa = "گروه‌ها" if job.target == "groups" else "کاربران"
            status = "✅ کامل شد" if not job.error else "❌ با خطا متوقف شد"
            text = (
                f"📢 <b>ارسال همگانی {status}</b>\n\n"
                f"مقصد: {target_fa}\n"
                f"✅ موفق: <code>{job.success}</code>\n"
                f"❌ ناموفق: <code>{job.failed}</code>\n"
                f"🚫 بلاک: <code>{job.blocked}</code>\n"
                f"👻 غیرفعال: <code>{job.deactivated}</code>\n"
                f"📦 کل: <code>{job.total}</code>"
            )
            if job.error:
                text += f"\n\n⚠️ خطا: <code>{job.error[:200]}</code>"

            await job.bot.send_message(job.actor_id, text, parse_mode="HTML")

            for admin_id in settings.get_admin_id_list():
                if admin_id != job.actor_id:
                    try:
                        await job.bot.send_message(admin_id, text, parse_mode="HTML")
                    except Exception:
                        pass
        except Exception as exc:
            logger.error("Failed to send broadcast report: %s", exc)
