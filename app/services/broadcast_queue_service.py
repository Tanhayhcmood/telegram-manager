"""
Non-blocking background broadcast service.
Broadcasts run in a dedicated asyncio task — the bot stays responsive.
Progress and results are reported to the requesting admin.

ARCHITECTURE NOTE
-----------------
User DMs  → Telethon user client : personal account sends DMs to contacts directly.
                                    Requires shared-group history for entity resolution.
Group msgs → Telethon user client: the Telethon account joined those groups.
                                   The bot may not be a member of every group.
Bot API (aiogram) is used ONLY for admin notifications and progress messages.
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramBadRequest,
    TelegramRetryAfter,
    TelegramMigrateToChat,
)

from app.config import settings
from app.database.connection import AsyncSessionLocal
from app.repositories import GroupRepository, ContactedUserRepository, LogRepository
from app.models.group import GroupStatus
from app.utils.logger import get_logger

logger = get_logger(__name__)

DM_DELAY = 0.5        # seconds between DMs — 0.5s is safe under Bot API 30/s global limit
TG_DM_DELAY = 3.0    # seconds between Telethon user-account DMs (avoids FloodWait)
GROUP_DELAY = 2       # seconds between group sends
MAX_STORED_JOBS = 50
PER_CALL_TIMEOUT = 60 # max seconds for a single Telethon call (prevents FloodWait hang)
PROGRESS_EVERY = 25   # edit progress message every N users

_MAX_BROADCAST_SECONDS: int = 3600

# Failure reasons that mean the account can never post there again: either
# an admin permanently banned it from sending (UserBannedInChannelError), or
# writing is globally forbidden for it in that chat (ChatWriteForbiddenError /
# ChatAdminRequiredError). Matched against the canonical reason prefixes set
# by TelegramUserService.forward_message_to_group's typed exception handlers
# — not a loose substring guess — so detection doesn't depend on Telegram's
# (locale-dependent) client-facing error text.
_GROUP_WRITE_RESTRICTED_MARKERS = (
    "user_banned_in_channel",
    "chat_write_forbidden",
    # Telegram can reject a post even when the account is still a member:
    # paid-message chats and globally restricted chats are not broadcastable.
    "allow_payment_required",
    "chat is restricted",
    "chat_restricted",
)

# Failure reasons that mean the group/chat itself is gone or the account was
# fully removed (kicked, chat deleted, entity can't be resolved at all).
# Safe to mark LEFT immediately instead of waiting for the next manual/auto sync.
_GROUP_UNREACHABLE_MARKERS = (
    "chat not found",
    "entity_not_found",
    "channel_private",
    "not a participant",
    "usernotparticipant",
    "chat_id_invalid",
    "peer_id_invalid",
    "invalid peer",
    "kicked",
)


def _matches_any(reason: str | None, markers: tuple[str, ...]) -> bool:
    if not reason:
        return False
    r = reason.lower()
    return any(marker in r for marker in markers)


def _is_write_restricted(reason: str | None) -> bool:
    return _matches_any(reason, _GROUP_WRITE_RESTRICTED_MARKERS)


def _is_group_unreachable(reason: str | None) -> bool:
    return _matches_any(reason, _GROUP_UNREACHABLE_MARKERS)


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
    first_user_error: str | None = None
    first_group_error: str | None = None
    message_text: str = ""
    media_file_id: str | None = None
    media_type: str | None = None
    is_forward: bool = False
    forward_from_chat_id: int | None = None
    forward_from_message_id: int | None = None
    progress_message_id: int | None = None  # ID of the single live-edited progress message


class BroadcastQueueService:
    _instance: "BroadcastQueueService | None" = None

    def __init__(self) -> None:
        self._jobs: dict[str, BroadcastJob] = {}
        self._active: bool = False
        self._task: asyncio.Task | None = None
        self._manually_cancelled: bool = False

    @classmethod
    def get_instance(cls) -> "BroadcastQueueService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def get_job(self, job_id: str) -> BroadcastJob | None:
        return self._jobs.get(job_id)

    def is_active(self) -> bool:
        return self._active

    def get_active_job(self) -> BroadcastJob | None:
        for job in self._jobs.values():
            if not job.done:
                return job
        return None

    def cancel_active(self) -> bool:
        if self._task and not self._task.done():
            self._manually_cancelled = True
            self._task.cancel()
            logger.warning("BroadcastQueueService: active task cancelled by admin")
            return True
        self._active = False
        return False

    async def start_broadcast(
        self,
        target: str,
        from_chat_id: int,
        message_id: int,
        actor_id: int,
        bot: Any,
        message_text: str = "",
        media_file_id: str | None = None,
        media_type: str | None = None,
        is_forward: bool = False,
        forward_from_chat_id: int | None = None,
        forward_from_message_id: int | None = None,
    ) -> str:
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
            message_text=message_text,
            media_file_id=media_file_id,
            media_type=media_type,
            is_forward=is_forward,
            forward_from_chat_id=forward_from_chat_id,
            forward_from_message_id=forward_from_message_id,
        )
        self._jobs[job_id] = job
        self._task = asyncio.create_task(self._run(job), name=f"broadcast-{job_id}")
        return job_id

    def _prune_old_jobs(self) -> None:
        done_jobs = [(jid, j) for jid, j in self._jobs.items() if j.done]
        if len(done_jobs) > MAX_STORED_JOBS:
            done_jobs.sort(key=lambda x: x[1].started_at)
            for jid, _ in done_jobs[:len(done_jobs) - MAX_STORED_JOBS]:
                del self._jobs[jid]

    async def _run(self, job: BroadcastJob) -> None:
        self._active = True
        try:
            coro = (
                self._send_to_groups(job)
                if job.target == "groups"
                else self._send_to_users(job)
            )
            await asyncio.wait_for(coro, timeout=_MAX_BROADCAST_SECONDS)
        except asyncio.CancelledError:
            if self._manually_cancelled:
                job.error = "cancelled by admin"
                logger.warning("Broadcast job %s cancelled by admin", job.job_id)
            else:
                job.error = "service restarted"
                logger.warning("Broadcast job %s interrupted by service restart", job.job_id)
        except asyncio.TimeoutError:
            job.error = f"timed out after {_MAX_BROADCAST_SECONDS}s"
            logger.error("Broadcast job %s timed out", job.job_id)
        except Exception as exc:
            job.error = str(exc)
            logger.error("Broadcast job %s crashed: %s", job.job_id, exc, exc_info=True)
        finally:
            job.done = True
            self._active = False
            self._task = None
            self._manually_cancelled = False
            self._prune_old_jobs()
            await self._report(job)

    # ══════════════════════════════════════════════════════════════════
    # USER BROADCAST — uses Telethon user client (personal account)
    # ══════════════════════════════════════════════════════════════════
    # Messages are sent from the personal Telethon account, not the bot.
    # Telethon resolves users via shared-group entity cache.
    # Users with no shared history → 'peer_not_found' (counted as deactivated).
    # ══════════════════════════════════════════════════════════════════

    async def _bot_send_to_user(self, job: BroadcastJob, user_id: int) -> tuple[bool, str | None]:
        """Send one message to a user via Bot API. Returns (ok, reason)."""
        bot = job.bot
        try:
            # ── Forward (preserves "Forwarded from …" header) ─────────────────────
            if job.is_forward and job.forward_from_chat_id and job.forward_from_message_id:
                await bot.forward_message(
                    chat_id=user_id,
                    from_chat_id=job.forward_from_chat_id,
                    message_id=job.forward_from_message_id,
                )

            # ── Media via Bot API file_id (no download/re-upload needed) ──────────
            elif job.media_file_id and job.media_type:
                await self._bot_send_media(bot, user_id, job.media_file_id, job.media_type, job.message_text)

            # ── Plain text ────────────────────────────────────────────────────────
            elif job.message_text:
                await bot.send_message(chat_id=user_id, text=job.message_text)

            # ── Last resort: forward original message from admin's chat with bot ──
            else:
                await bot.forward_message(
                    chat_id=user_id,
                    from_chat_id=job.from_chat_id,
                    message_id=job.message_id,
                )

            return True, None

        except TelegramRetryAfter as exc:
            # Bot API rate limit — respect it but cap at 120s
            wait = min(exc.retry_after, 120)
            logger.warning("Bot API RetryAfter for user %d: wait %ds", user_id, wait)
            await asyncio.sleep(wait)
            return False, f"retry_after_{exc.retry_after}s"

        except TelegramForbiddenError:
            # User blocked the bot
            return False, "blocked"

        except TelegramBadRequest as exc:
            msg = str(exc).lower()
            if "chat not found" in msg or "user not found" in msg or "deactivated" in msg:
                return False, "deactivated"
            if "bot was blocked" in msg:
                return False, "blocked"
            logger.warning("TelegramBadRequest for user %d: %s", user_id, exc)
            return False, str(exc)[:120]

        except TelegramMigrateToChat:
            return False, "migrated"

        except Exception as exc:
            logger.error("Unexpected error sending to user %d: %s", user_id, exc)
            return False, str(exc)[:120]

    @staticmethod
    async def _bot_send_media(bot: Any, chat_id: int, file_id: str, media_type: str, caption: str) -> None:
        """Dispatch the right Bot API send method based on media_type."""
        kw: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            kw["caption"] = caption
        if media_type == "photo":
            await bot.send_photo(**kw, photo=file_id)
        elif media_type == "video":
            await bot.send_video(**kw, video=file_id)
        elif media_type == "document":
            await bot.send_document(**kw, document=file_id)
        elif media_type == "audio":
            await bot.send_audio(**kw, audio=file_id)
        elif media_type == "voice":
            await bot.send_voice(**kw, voice=file_id)
        elif media_type == "animation":
            await bot.send_animation(**kw, animation=file_id)
        elif media_type == "sticker":
            kw.pop("caption", None)  # stickers don't support captions
            await bot.send_sticker(**kw, sticker=file_id)
        elif media_type == "video_note":
            kw.pop("caption", None)
            await bot.send_video_note(**kw, video_note=file_id)
        else:
            # Fallback: treat as document
            await bot.send_document(**kw, document=file_id)

    async def _send_to_users(self, job: BroadcastJob) -> None:
        from app.services.telegram_service import TelegramUserService
        tg = TelegramUserService.get_instance()

        # Single source of truth: live PV dialogs from the personal Telethon account.
        # Only people whose dialog currently exists in the account are messaged —
        # no stale DB fallback that inflates the list with old/deleted contacts.
        dialog_users = await tg.get_all_user_dialogs(limit=3000)

        target_users: list[dict] = [
            {
                "user_id": du["user_id"],
                "username": du.get("username"),
                "first_name": du.get("first_name"),
            }
            for du in dialog_users
        ]

        job.total = len(target_users)
        actor = str(job.actor_id)
        logger.info(
            "Broadcast job %s: %d users (live PV dialogs only)",
            job.job_id, job.total,
        )

        if not target_users:
            logger.info("Broadcast job %s: no users to send to", job.job_id)
            return

        try:
            prog_msg = await job.bot.send_message(
                job.actor_id,
                f"⏳ <b>ارسال همگانی به مخاطبین شروع شد</b>\n\n"
                f"📦 کاربران: <code>{job.total}</code>\n"
                f"در حال ارسال از اکانت شخصی...",
                parse_mode="HTML",
            )
            job.progress_message_id = prog_msg.message_id
        except Exception as exc:
            logger.warning("Could not send initial progress message: %s", exc)

        peer_flood_count = 0
        for idx, user in enumerate(target_users):
            user_id = user["user_id"]
            ok, reason = await tg.send_dm_to_user(
                user_id=user_id,
                message_text=job.message_text,
                media_file_id=job.media_file_id,
                media_type=job.media_type,
                is_forward=job.is_forward,
                forward_from_chat_id=job.forward_from_chat_id,
                forward_from_message_id=job.forward_from_message_id,
                bot=job.bot,
            )

            if ok:
                job.success += 1
                await self._log("broadcast_user_sent", "success", actor, str(user_id))
            else:
                job.failed += 1
                if job.first_user_error is None:
                    job.first_user_error = f"uid={user_id}: {reason}"
                if reason == "blocked":
                    job.blocked += 1
                    await self._mark_user_blocked(user_id)
                elif reason in ("deactivated", "peer_not_found"):
                    job.deactivated += 1
                    await self._mark_user_blocked(user_id)
                elif reason == "peer_flood":
                    peer_flood_count += 1
                    logger.warning(
                        "PeerFlood hit #%d for user %d in broadcast job %s — pausing %ds",
                        peer_flood_count, user_id, job.job_id, settings.PEER_FLOOD_PAUSE_SECONDS,
                    )
                    await self._log("broadcast_user_failed", "error", actor, str(user_id), reason)
                    if peer_flood_count >= settings.MAX_PEER_FLOOD_PAUSES:
                        job.error = (
                            f"PeerFlood {peer_flood_count}x: اکانت محدود شد. "
                            "broadcast متوقف شد. چند ساعت صبر کنید."
                        )
                        logger.error("Broadcast job %s aborted after %d PeerFlood errors", job.job_id, peer_flood_count)
                        return
                    try:
                        await job.bot.send_message(
                            job.actor_id,
                            f"⚠️ <b>PeerFlood #{peer_flood_count}</b>\n"
                            f"{settings.PEER_FLOOD_PAUSE_SECONDS // 60} دقیقه صبر می‌کنم...\n"
                            f"(حداکثر {settings.MAX_PEER_FLOOD_PAUSES} بار)",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(settings.PEER_FLOOD_PAUSE_SECONDS)
                    await self._log("broadcast_user_failed", "error", actor, str(user_id), reason)
                elif reason and reason.startswith("flood_wait:"):
                    try:
                        wait_secs = int(reason.split(":")[1].rstrip("s"))
                    except Exception:
                        wait_secs = 60
                    logger.warning("FloodWait %ds -- sleeping before next user", wait_secs)
                    await asyncio.sleep(wait_secs)
                await self._log("broadcast_user_failed", "error", actor, str(user_id), reason)

            done_count = idx + 1
            if done_count % PROGRESS_EVERY == 0 and done_count < job.total:
                await self._send_progress(job, done_count)
            await asyncio.sleep(settings.TG_DM_DELAY_SECONDS)

    async def _send_to_groups(self, job: BroadcastJob) -> None:
        from app.services.telegram_service import TelegramUserService
        tg = TelegramUserService.get_instance()

        # ── Step 1: Live Telethon dialogs — ALL groups the account is in ──────────
        # Ground truth. Entities carry access_hash → no invalid peer errors.
        dialog_groups = await tg.get_all_groups_from_dialogs(limit=3000)
        dialog_ids = {g["group_id"] for g in dialog_groups}

        # ── Step 2: DB-only joined+writable groups not in live dialogs (edge cases) ─
        async with AsyncSessionLocal() as session:
            repo = GroupRepository(session)
            db_groups = await repo.get_broadcastable()
            # Need can_write for *all* joined groups (not just writable ones)
            # so we can also skip live dialogs the account is restricted in.
            all_joined = await repo.get_joined()
        db_by_id = {g.group_id: g for g in all_joined}

        target_groups: list[dict] = []
        skipped_restricted = 0
        for dg in dialog_groups:
            db_g = db_by_id.get(dg["group_id"])
            if db_g is not None and not db_g.can_write:
                # Still a member (shows up live), but banned/restricted from
                # posting — don't waste an attempt, it will just fail again.
                skipped_restricted += 1
                continue
            target_groups.append({
                "group_id": dg["group_id"],
                "title": dg["title"],
                "invite_link": db_g.invite_link if db_g else None,
                "username": dg.get("username"),
            })
        for db_g in db_groups:
            if db_g.group_id not in dialog_ids:
                target_groups.append({
                    "group_id": db_g.group_id,
                    "title": db_g.title,
                    "invite_link": db_g.invite_link,
                    "username": db_g.username,
                })
        if skipped_restricted:
            logger.info(
                "Broadcast job: skipped %d write-restricted groups (still joined, can't post)",
                skipped_restricted,
            )

        job.total = len(target_groups)
        actor = str(job.actor_id)
        # Count only destinations that will actually be attempted.  The live
        # dialog list also contains write-restricted groups that were skipped
        # above; counting all dialogs here made the progress message report an
        # impossible negative DB-only count.
        live_target_ids = {
            group["group_id"]
            for group in target_groups
            if group["group_id"] in dialog_ids
        }
        live_cnt = len(live_target_ids)
        db_only_cnt = job.total - live_cnt
        logger.info(
            "Broadcast job %s: %d groups (live=%d db_only=%d)",
            job.job_id, job.total, live_cnt, db_only_cnt,
        )

        try:
            prog_msg = await job.bot.send_message(
                job.actor_id,
                f"⏳ <b>ارسال همگانی به گروه‌ها شروع شد</b>\n\n"
                f"📦 کل: <code>{job.total}</code> (لایو: {live_cnt} | فقط DB: {db_only_cnt})\nدر حال ارسال...",
                parse_mode="HTML",
            )
            job.progress_message_id = prog_msg.message_id
        except Exception as exc:
            logger.warning("Could not send initial progress message: %s", exc)

        peer_flood_count_g = 0
        for idx, group in enumerate(target_groups):
            group_id = group["group_id"]
            group_link = group.get("invite_link") or (
                f"@{group['username']}" if group.get("username") else None
            )
            ok, reason = await tg.forward_message_to_group(
                group_id=group_id,
                group_link=group_link,
                message_text=job.message_text,
                media_file_id=job.media_file_id,
                media_type=job.media_type,
                is_forward=job.is_forward,
                forward_from_chat_id=job.forward_from_chat_id,
                forward_from_message_id=job.forward_from_message_id,
                bot=job.bot,
            )
            if ok:
                job.success += 1
                await self._log("broadcast_group_sent", "success", actor, str(group_id))
            else:
                job.failed += 1
                if job.first_group_error is None:
                    job.first_group_error = f"gid={group_id}: {reason}"
                if _is_write_restricted(reason):
                    await self._leave_write_restricted_group(group_id, group.get("invite_link") or (
                        f"@{group['username']}" if group.get("username") else None
                    ), reason)
                elif _is_group_unreachable(reason):
                    await self._mark_group_left(group_id, reason)
                if reason == "peer_flood":
                    peer_flood_count_g += 1
                    logger.warning(
                        "PeerFlood #%d on group %d in broadcast %s — pausing %ds",
                        peer_flood_count_g, group_id, job.job_id, settings.PEER_FLOOD_PAUSE_SECONDS,
                    )
                    await self._log("broadcast_group_failed", "error", actor, str(group_id), reason)
                    if peer_flood_count_g >= settings.MAX_PEER_FLOOD_PAUSES:
                        job.error = f"PeerFlood {peer_flood_count_g}x گروهی: broadcast متوقف شد."
                        logger.error(
                            "Broadcast %s aborted after %d group PeerFlood errors",
                            job.job_id, peer_flood_count_g,
                        )
                        return
                    try:
                        await job.bot.send_message(
                            job.actor_id,
                            f"⚠️ <b>PeerFlood گروهی #{peer_flood_count_g}</b>\n"
                            f"{settings.PEER_FLOOD_PAUSE_SECONDS // 60} دقیقه صبر می‌کنم...",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(settings.PEER_FLOOD_PAUSE_SECONDS)
                elif reason and reason.startswith("flood_wait:"):
                    try:
                        wait_secs = int(reason.split(":")[1].rstrip("s"))
                    except Exception:
                        wait_secs = 60
                    logger.warning("FloodWait %ds — sleeping before next group", wait_secs)
                    await asyncio.sleep(wait_secs)
                await self._log("broadcast_group_failed", "error", actor, str(group_id), reason)

            done_count = idx + 1
            if done_count % PROGRESS_EVERY == 0 and done_count < job.total:
                await self._send_progress(job, done_count)
            await asyncio.sleep(GROUP_DELAY)

    # ══════════════════════════════════════════════════════════════════
    # HELPERS
    # ══════════════════════════════════════════════════════════════════

    async def _send_progress(self, job: BroadcastJob, done: int) -> None:
        """Edit the single live-progress message instead of flooding new ones."""
        try:
            pct = int(done / job.total * 100) if job.total else 0
            text = (
                f"⏳ <b>ارسال همگانی در حال اجرا</b> ({pct}%)\n\n"
                f"✅ موفق: <code>{job.success}</code>\n"
                f"❌ ناموفق: <code>{job.failed}</code>\n"
                f"📦 {done} از {job.total}"
            )
            if job.progress_message_id:
                await job.bot.edit_message_text(
                    chat_id=job.actor_id,
                    message_id=job.progress_message_id,
                    text=text,
                    parse_mode="HTML",
                )
            else:
                msg = await job.bot.send_message(job.actor_id, text, parse_mode="HTML")
                job.progress_message_id = msg.message_id
        except Exception:
            pass
    async def _mark_group_left(self, group_id: int, reason: str | None) -> None:
        """Mark a group LEFT the moment a broadcast proves the account can no
        longer reach it (banned/kicked/left/deleted). Without this, the group
        stays JOINED in the DB and every future broadcast wastes an attempt
        on it until someone manually re-syncs dialogs."""
        try:
            async with AsyncSessionLocal() as s:
                repo = GroupRepository(s)
                group = await repo.get_by_group_id(group_id)
                if group and group.status != GroupStatus.LEFT:
                    group.status = GroupStatus.LEFT
                    await s.commit()
                    logger.info(
                        "Marked group %d as LEFT after broadcast failure (%s)",
                        group_id, reason,
                    )
        except Exception as exc:
            logger.warning("Failed to mark group %d as left: %s", group_id, exc)

    async def _mark_group_write_restricted(self, group_id: int, reason: str | None) -> None:
        """Flip can_write off for a group that is still joined but where the
        account is banned/restricted from posting. Kept separate from status
        so the periodic dialog sync (which re-marks every live dialog as
        JOINED) doesn't silently undo the exclusion."""
        try:
            async with AsyncSessionLocal() as s:
                repo = GroupRepository(s)
                updated = await repo.mark_write_restricted(group_id)
                await s.commit()
                if updated:
                    logger.info(
                        "Marked group %d as write-restricted after broadcast failure (%s)",
                        group_id, reason,
                    )
        except Exception as exc:
            logger.warning("Failed to mark group %d write-restricted: %s", group_id, exc)

    async def _leave_write_restricted_group(self, group_id: int, group_link: str | None, reason: str | None) -> None:
        """A group where the account is permanently banned/forbidden from
        sending is dead weight — there's no reason to stay a silent member.

        Actually leaves via Telethon (real Telegram-level exit, not just a
        DB flag), then marks the group LEFT in the DB either way so it never
        gets targeted again — even if the live leave call fails (e.g.
        transient network error), the DB record already reflects the
        decision and the periodic dialog sync will reconcile the rest.
        """
        from app.services.telegram_service import TelegramUserService
        tg = TelegramUserService.get_instance()
        left_live = False
        try:
            left_live = await tg.leave_group_by_id(group_id, group_link)
        except Exception as exc:
            logger.warning("Error while trying to leave group %d live: %s", group_id, exc)

        await self._mark_group_left(group_id, reason)
        # can_write=False is also recorded for audit/history even though the
        # group moves to LEFT — harmless if the leave call is later retried.
        try:
            async with AsyncSessionLocal() as s:
                repo = GroupRepository(s)
                await repo.mark_write_restricted(group_id)
                await s.commit()
        except Exception:
            pass

        if left_live:
            logger.info("Left write-restricted group %d live via Telethon (%s)", group_id, reason)
        else:
            logger.warning(
                "Could not confirm live leave for write-restricted group %d — marked LEFT in DB only (%s)",
                group_id, reason,
            )

    async def _mark_user_blocked(self, user_id: int) -> None:
        try:
            async with AsyncSessionLocal() as s:
                from app.repositories import ContactedUserRepository as CUR
                r = CUR(s)
                u = await r.get_by_user_id(user_id)
                if u:
                    u.is_blocked = True
                await s.commit()
        except Exception as exc:
            logger.warning("Failed to mark user %d blocked: %s", user_id, exc)

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
            if job.error == "cancelled by admin":
                status = "🛑 لغو شد توسط ادمین"
            elif job.error == "service restarted":
                status = "⚠️ قطع شد (ری‌استارت سرویس)"
            elif job.error:
                status = "❌ با خطا متوقف شد"
            else:
                status = "✅ کامل شد"

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
            if job.first_user_error:
                text += f"\n\n🔍 اولین خطای کاربر: <code>{job.first_user_error[:200]}</code>"
            if job.first_group_error:
                text += f"\n\n🔍 اولین خطای گروه: <code>{job.first_group_error[:200]}</code>"

            # Edit the live-progress message with the final result
            if job.progress_message_id:
                try:
                    await job.bot.edit_message_text(
                        chat_id=job.actor_id,
                        message_id=job.progress_message_id,
                        text=text,
                        parse_mode="HTML",
                    )
                except Exception:
                    await job.bot.send_message(job.actor_id, text, parse_mode="HTML")
            else:
                await job.bot.send_message(job.actor_id, text, parse_mode="HTML")
            for admin_id in settings.get_admin_id_list():
                if admin_id != job.actor_id:
                    try:
                        await job.bot.send_message(admin_id, text, parse_mode="HTML")
                    except Exception:
                        pass
        except Exception as exc:
            logger.error("Failed to send broadcast report: %s", exc)
