"""
Watches for ChatAction events where our Telegram account is added/approved
into a group, then updates the group record in the DB to JOINED status.

This fixes the critical bug where:
  1. Admin approves a group via bot → status set to APPROVED (correct)
  2. Bot enqueues group for joining → join request sent to Telegram
  3. Group admin approves the join request → Telethon receives ChatAction event
  4. *** BUG: DB status stays APPROVED forever — never updated to JOINED ***

This service fixes step 4 by listening for the approval event and
immediately marking the group JOINED in the DB.
"""
import asyncio
from typing import Any

from app.database.connection import AsyncSessionLocal
from app.repositories import GroupRepository, LogRepository
from app.models.group import GroupStatus
from app.utils.logger import get_logger

logger = get_logger(__name__)


class JoinApprovalWatcher:
    """Listens for Telethon ChatAction events where our account is approved."""

    _instance: "JoinApprovalWatcher | None" = None

    def __init__(self) -> None:
        self._tg: Any = None
        self._me_id: int | None = None

    @classmethod
    def get_instance(cls) -> "JoinApprovalWatcher":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def set_tg_service(self, tg: Any) -> None:
        self._tg = tg

    async def start(self) -> None:
        """Register the ChatAction handler on the Telethon client."""
        if self._tg is None:
            logger.warning("JoinApprovalWatcher: no TelegramUserService set — skipping")
            return
        try:
            me = await self._tg.client.get_me()
            self._me_id = me.id
            self._tg.on_chat_action(self._handle_chat_action)
            logger.info(
                "JoinApprovalWatcher started — watching for approved requests (my id=%d)",
                self._me_id,
            )
        except Exception as exc:
            logger.error("JoinApprovalWatcher.start failed: %s", exc)

    async def _handle_chat_action(self, event: Any) -> None:
        """Called by Telethon on every ChatAction in any chat the account can see."""
        try:
            # Only care about "user added" or "user joined" events
            user_joined = getattr(event, "user_joined", False)
            user_added  = getattr(event, "user_added",  False)
            if not (user_joined or user_added):
                return

            # Only care if it's OUR account being added
            event_user_id = getattr(event, "user_id", None)
            if event_user_id != self._me_id:
                return

            # `event.chat_id` is Telethon's marked peer ID (for example,
            # -1001234567890 for a channel), while Group.group_id is stored
            # from `entity.id` as the positive Telegram ID. Match using the
            # entity's canonical ID so approvals update the original row.
            marked_chat_id = getattr(event, "chat_id", None)
            chat = await event.get_chat()
            chat_id = getattr(chat, "id", None)
            if not isinstance(chat_id, int) or chat_id <= 0:
                logger.warning(
                    "JoinApprovalWatcher: could not resolve canonical group ID; "
                    "skipping approval event (marked_chat_id=%s)",
                    marked_chat_id,
                )
                return

            logger.info(
                "JoinApprovalWatcher: account was approved/added to group_id=%d", chat_id
            )

            async with AsyncSessionLocal() as session:
                group_repo = GroupRepository(session)
                log_repo   = LogRepository(session)

                group = await group_repo.get_by_group_id(chat_id)
                # Older versions stored event.chat_id directly. If such a
                # legacy row exists, normalize it instead of creating a second
                # row for the same Telegram group.
                if (
                    group is None
                    and isinstance(marked_chat_id, int)
                    and marked_chat_id != chat_id
                ):
                    group = await group_repo.get_by_group_id(marked_chat_id)
                    if group is not None:
                        group.group_id = chat_id
                        await session.flush()
                        logger.info(
                            "JoinApprovalWatcher: normalized legacy group ID %d → %d",
                            marked_chat_id,
                            chat_id,
                        )

                title = (group.title if group else None) or str(chat_id)

                # The event is authoritative: Telegram has added this account,
                # regardless of the row's previous status.
                if group is not None:
                    from datetime import datetime, timezone
                    previous_status = group.status.value
                    group.status = GroupStatus.JOINED
                    group.join_date = datetime.now(timezone.utc)
                    logger.info(
                        "JoinApprovalWatcher: group_id=%d (%r) status updated %s → JOINED",
                        chat_id,
                        title,
                        previous_status,
                    )
                elif group is None:
                    # Unknown group — create a record so it shows up in the DB
                    from app.models.group import Group
                    from datetime import datetime, timezone
                    try:
                        new_group = Group(
                            group_id=chat_id,
                            title=title,
                            status=GroupStatus.JOINED,
                            join_date=datetime.now(timezone.utc),
                        )
                        session.add(new_group)
                        logger.info(
                            "JoinApprovalWatcher: auto-created JOINED record for unknown group_id=%d",
                            chat_id,
                        )
                    except Exception as create_exc:
                        logger.warning(
                            "JoinApprovalWatcher: could not auto-create group record: %s", create_exc
                        )

                await log_repo.add(
                    action="join_request_approved",
                    result="success",
                    target=str(chat_id),
                    details=f"group_id={chat_id} title={title!r} approved_by_admin → status=JOINED",
                )
                await session.commit()

            await self._notify(chat_id, title)

        except Exception as exc:
            logger.error("JoinApprovalWatcher._handle_chat_action error: %s", exc, exc_info=True)

    async def _notify(self, chat_id: int, title: str) -> None:
        try:
            from app.services.notification_service import NotificationService
            ns = NotificationService.get_instance()
            await ns.notify(
                f"🎉 <b>درخواست عضویت تأیید شد!</b>\n\n"
                f"ادمین گروه درخواست عضویت را تأیید کرد.\n"
                f"اکانت اکنون داخل گروه است و وضعیت به <b>JOINED</b> تغییر یافت.\n\n"
                f"📌 گروه: <code>{title}</code>\n"
                f"🆔 شناسه: <code>{chat_id}</code>",
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.warning("JoinApprovalWatcher: notification failed: %s", exc)
