import asyncio
from typing import Any
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import Channel, Chat, User
from telethon.errors import (
    FloodWaitError,
    UserAlreadyParticipantError,
    UserIsBlockedError,
    InputUserDeactivatedError,
    PeerFloodError,
)

from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)


class TelegramUserService:
    _instance: "TelegramUserService | None" = None

    def __init__(self) -> None:
        session = (
            StringSession(settings.TELEGRAM_SESSION_STRING)
            if settings.TELEGRAM_SESSION_STRING
            else StringSession()
        )
        self.client = TelegramClient(
            session,
            settings.TELEGRAM_API_ID,
            settings.TELEGRAM_API_HASH,
            connection_retries=10,
            retry_delay=5,
            auto_reconnect=True,
        )
        self._running = False
        self._desired_running = False
        self._connection_lock = asyncio.Lock()

    @classmethod
    def get_instance(cls) -> "TelegramUserService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def start(self) -> None:
        self._desired_running = True
        if not await self.reconnect_if_needed():
            raise RuntimeError("Telegram client start was cancelled.")
        logger.info("Telegram user client connected")

    async def reconnect_if_needed(self) -> bool:
        """Reconnect only when the account is expected to be running."""
        if not self._desired_running:
            return False

        async with self._connection_lock:
            if not self._desired_running:
                return False
            if self.is_running():
                return True

            self._running = False
            try:
                if not self.client.is_connected():
                    await self.client.connect()
                if not await self.client.is_user_authorized():
                    await self.client.disconnect()
                    logger.warning(
                        "Telegram session not authorized — interactive login required"
                    )
                    raise RuntimeError(
                        "Telegram session is not authorized. "
                        "Run `python -m app.cli login` locally to generate a session string "
                        "and set TELEGRAM_SESSION_STRING in your environment."
                    )

                await self.client.get_me()
                self._running = True
                logger.info("Telegram user client connection is active")
                return True
            except Exception:
                self._running = False
                raise

    def is_expected_to_run(self) -> bool:
        """Whether an admin or startup requested that this client stay online."""
        return self._desired_running

    async def stop(self) -> None:
        async with self._connection_lock:
            self._desired_running = False
            self._running = False
            if self.client.is_connected():
                await self.client.disconnect()
        logger.info("Telegram user client disconnected")

    def is_running(self) -> bool:
        return self._running and self.client.is_connected()

    async def resolve_entity(self, link: str) -> Any | None:
        try:
            return await self.client.get_entity(link)
        except Exception as exc:
            logger.debug("Cannot resolve entity %s: %s", link, exc)
            return None

    async def is_group(self, entity: Any) -> bool:
        return isinstance(entity, (Chat, Channel)) and not getattr(entity, "broadcast", False)

    async def get_user_bio(self, user_id: int) -> str:
        try:
            full = await self.client.get_entity(user_id)
            if isinstance(full, User):
                from telethon.tl.functions.users import GetFullUserRequest
                info = await self.client(GetFullUserRequest(full))
                return getattr(info.full_user, "about", "") or ""
        except Exception:
            pass
        return ""

    async def join_group(self, link: str) -> bool:
        try:
            from telethon.tl.functions.channels import JoinChannelRequest
            await self.client(JoinChannelRequest(link))
            logger.info("Joined group: %s", link)
            return True
        except UserAlreadyParticipantError:
            logger.info("Already in group: %s", link)
            return True
        except FloodWaitError as exc:
            logger.warning("FloodWait joining %s: wait %d seconds", link, exc.seconds)
            await asyncio.sleep(exc.seconds)
            return False
        except Exception as exc:
            logger.error("Failed to join %s: %s", link, exc)
            return False

    async def send_message_to_group(self, group_id: int, message: Any) -> bool:
        try:
            await self.client.send_message(group_id, message)
            return True
        except FloodWaitError as exc:
            logger.warning("FloodWait sending to %d: wait %d seconds", group_id, exc.seconds)
            await asyncio.sleep(exc.seconds)
            return False
        except Exception as exc:
            logger.error("Failed to send to %d: %s", group_id, exc)
            return False

    async def send_message_to_user(self, user_id: int, message: Any) -> tuple[bool, str | None]:
        """Send a DM to a user. Returns (success, error_reason)."""
        try:
            await self.client.send_message(user_id, message)
            return True, None
        except UserIsBlockedError:
            return False, "blocked"
        except InputUserDeactivatedError:
            return False, "deactivated"
        except PeerFloodError:
            logger.warning("PeerFlood — too many DMs sent, slowing down")
            await asyncio.sleep(60)
            return False, "peer_flood"
        except FloodWaitError as exc:
            logger.warning("FloodWait sending DM to %d: wait %d seconds", user_id, exc.seconds)
            await asyncio.sleep(exc.seconds)
            return False, "flood_wait"
        except Exception as exc:
            logger.error("Failed to send DM to %d: %s", user_id, exc)
            return False, str(exc)

    async def forward_message_to_user(
        self, user_id: int, from_chat_id: int, message_id: int
    ) -> tuple[bool, str | None]:
        """Forward a message to a user. Returns (success, error_reason)."""
        try:
            await self.client.forward_messages(user_id, message_id, from_chat_id)
            return True, None
        except UserIsBlockedError:
            return False, "blocked"
        except InputUserDeactivatedError:
            return False, "deactivated"
        except PeerFloodError:
            logger.warning("PeerFlood — too many DMs sent, slowing down")
            await asyncio.sleep(60)
            return False, "peer_flood"
        except FloodWaitError as exc:
            logger.warning("FloodWait forwarding to %d: wait %d seconds", user_id, exc.seconds)
            await asyncio.sleep(exc.seconds)
            return False, "flood_wait"
        except Exception as exc:
            logger.error("Failed to forward to %d: %s", user_id, exc)
            return False, str(exc)

    def on_new_message(self, handler: Any) -> None:
        self.client.add_event_handler(handler, events.NewMessage())

    async def get_session_string(self) -> str:
        if isinstance(self.client.session, StringSession):
            return self.client.session.save()
        return ""

    async def _get_live_dialogs(self, limit: int = 3000) -> list[Any]:
        if not self.is_running():
            raise RuntimeError("Telegram user client is not connected.")
        try:
            return await asyncio.wait_for(
                self.client.get_dialogs(limit=limit),
                timeout=60.0,
            )
        except asyncio.TimeoutError as exc:
            logger.error("Fetching Telegram dialogs timed out after 60 seconds")
            raise RuntimeError("دریافت گفتگوهای تلگرام بیش از حد طول کشید.") from exc
        except Exception as exc:
            logger.exception("Failed to fetch Telegram dialogs")
            raise RuntimeError("دریافت گفتگوهای تلگرام ناموفق بود.") from exc

    async def get_live_dialogs(self, limit: int = 3000) -> list[Any]:
        """Return live Telegram dialogs for read-only operations."""
        return await self._get_live_dialogs(limit=limit)

    async def get_all_groups_from_dialogs(self, limit: int = 3000) -> list[dict[str, Any]]:
        """Return the active account's group dialogs without broadcast channels."""
        dialogs = await self._get_live_dialogs(limit=limit)
        groups: list[dict[str, Any]] = []
        for dialog in dialogs:
            entity = dialog.entity
            if isinstance(entity, Chat):
                groups.append({
                    "group_id": dialog.id,
                    "title": entity.title,
                    "username": None,
                    "members_count": getattr(entity, "participants_count", None),
                })
            elif isinstance(entity, Channel) and not getattr(entity, "broadcast", False):
                groups.append({
                    "group_id": dialog.id,
                    "title": entity.title,
                    "username": getattr(entity, "username", None),
                    "members_count": getattr(entity, "participants_count", None),
                })
        logger.info("Found %d group dialogs to sync", len(groups))
        return groups

    async def get_all_user_dialogs(self, limit: int = 3000) -> list[dict[str, Any]]:
        """Return non-bot private chats from the active account's dialogs."""
        dialogs = await self._get_live_dialogs(limit=limit)
        users: list[dict[str, Any]] = []
        for dialog in dialogs:
            entity = dialog.entity
            if (
                isinstance(entity, User)
                and not getattr(entity, "bot", False)
                and not getattr(entity, "deleted", False)
            ):
                users.append({
                    "user_id": entity.id,
                    "username": getattr(entity, "username", None),
                    "first_name": getattr(entity, "first_name", None),
                    "last_name": getattr(entity, "last_name", None),
                })
        logger.info("Found %d private-chat users to sync", len(users))
        return users

    async def sync_dialogs_to_db(self) -> tuple[int, int]:
        """Sync live group dialogs into the database as joined groups."""
        from datetime import datetime, timezone

        from app.database.connection import AsyncSessionLocal
        from app.models.group import GroupStatus
        from app.repositories import GroupRepository

        groups = await self.get_all_groups_from_dialogs()
        new_count = 0
        now = datetime.now(timezone.utc)

        async with AsyncSessionLocal() as session:
            repo = GroupRepository(session)
            for group in groups:
                group_id = group["group_id"]
                existing = await repo.get_by_group_id(group_id)
                join_date = (
                    existing.join_date
                    if existing is not None and existing.join_date is not None
                    else now
                )
                _, created = await repo.upsert(
                    group_id=group_id,
                    title=group["title"],
                    username=group.get("username"),
                    members_count=group.get("members_count"),
                    status=GroupStatus.JOINED,
                    join_date=join_date,
                )
                if created:
                    new_count += 1
            await session.commit()

        logger.info("Synced %d group dialogs (%d new)", len(groups), new_count)
        return new_count, len(groups)

    async def sync_user_dialogs_to_db(self) -> tuple[int, int]:
        """Sync private-chat profiles without changing message counts or sending messages."""
        from app.database.connection import AsyncSessionLocal
        from app.repositories import ContactedUserRepository

        users = await self.get_all_user_dialogs()
        new_count = 0

        async with AsyncSessionLocal() as session:
            repo = ContactedUserRepository(session)
            for user in users:
                _, created = await repo.register_from_sync(
                    user_id=user["user_id"],
                    username=user.get("username"),
                    first_name=user.get("first_name"),
                    last_name=user.get("last_name"),
                )
                if created:
                    new_count += 1
            await session.commit()

        logger.info("Synced %d private-chat users (%d new)", len(users), new_count)
        return new_count, len(users)
