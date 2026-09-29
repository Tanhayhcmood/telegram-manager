import asyncio
import random
from io import BytesIO
from typing import Any
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import Channel, Chat, User, ChatInvite, ChatInviteAlready
from telethon.errors import (
    FloodWaitError,
    UserAlreadyParticipantError,
    InviteRequestSentError,
    UserIsBlockedError,
    InputUserDeactivatedError,
    PeerFloodError,
    ChatWriteForbiddenError,
    ChannelPrivateError,
    ChatAdminRequiredError,
    UserBannedInChannelError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    UsernameNotOccupiedError,
    UsernameInvalidError,
    ChannelsTooMuchError,
)

from app.config import settings
from app.utils.logger import get_logger
from app.utils.validators import LinkValidator

logger = get_logger(__name__)


class EntityResolutionPausedError(RuntimeError):
    """Raised locally while Telegram has an active entity lookup cooldown."""

    def __init__(self, seconds: int) -> None:
        self.seconds = max(1, seconds)
        super().__init__(f"Telegram entity resolution paused for {self.seconds}s")


# Media types that support a caption field in Telethon send_file
_CAPTIONABLE = {"photo", "video", "document", "audio", "animation"}

# Maps media_type → file extension so Telethon knows HOW to send the file.
# Without a proper extension on BytesIO.name, Telethon always sends as document.
_MEDIA_EXT: dict[str, str] = {
    "photo":      ".jpg",
    "video":      ".mp4",
    "animation":  ".mp4",   # GIFs stored as MP4 on Telegram
    "audio":      ".mp3",
    "voice":      ".ogg",
    "document":   ".bin",   # let Telegram keep whatever type it is
    "sticker":    ".webp",
    "video_note": ".mp4",
}


def _name_bio(bio: BytesIO, media_type: str) -> BytesIO:
    """Stamp a .name on a BytesIO so Telethon sends it with the right media type.

    Telethon inspects the file extension to decide between photo/video/document.
    A BytesIO without .name is always sent as a raw document — so stamping
    '.jpg' for photos is the minimal fix that makes Telethon treat it as an image.
    """
    ext = _MEDIA_EXT.get(media_type, ".bin")
    bio.name = f"media{ext}"
    bio.seek(0)
    return bio


def _send_file_kwargs(bio: BytesIO, media_type: str, caption: str = "") -> dict[str, Any]:
    """Build the kwargs dict for client.send_file() with correct flags per type."""
    bio = _name_bio(bio, media_type)
    kwargs: dict[str, Any] = {"file": bio}
    if media_type in _CAPTIONABLE and caption:
        kwargs["caption"] = caption
    # Photos must NOT be forced to document — explicit for clarity
    if media_type == "photo":
        kwargs["force_document"] = False
    elif media_type == "voice":
        kwargs["voice_note"] = True
    elif media_type == "video_note":
        kwargs["video_note"] = True
    return kwargs



def _record_sent_to_group(group_id: int, message_or_list: Any) -> None:
    """Record sent message IDs for forced-subscribe reply-to-us detection."""
    try:
        from app.services.forced_subscribe_service import ForcedSubscribeService
        fs = ForcedSubscribeService.get_instance()
        msgs = message_or_list if isinstance(message_or_list, (list, tuple)) else [message_or_list]
        for m in msgs:
            mid = getattr(m, 'id', None)
            if mid:
                fs.record_our_message(group_id, mid)
    except Exception:
        pass


class TelegramUserService:
    _instance: "TelegramUserService | None" = None

    # Entity lookup is part of message discovery. A Telegram RPC that hangs
    # here must not block the discovery handler forever or leave newer links
    # waiting behind it.
    _ENTITY_RESOLVE_TIMEOUT = 20.0

    # ── Global join throttle ──────────────────────────────────────────────
    # Applies to EVERY join_group() call regardless of caller (main discovery
    # queue OR forced-subscribe auto-join). Without this, forced-subscribe
    # could fire several joins seconds apart -- independent of the main
    # queue's pacing -- creating a burst pattern that looks like spam to
    # Telegram and triggers temporary restrictions / soft-bans.
    _GLOBAL_JOIN_MIN_GAP = 2400.0   # 40 minutes
    _GLOBAL_JOIN_MAX_GAP = 5400.0   # 90 minutes

    def __init__(self) -> None:
        session = (
            StringSession(settings.TELEGRAM_SESSION_STRING)
            if settings.TELEGRAM_SESSION_STRING
            else StringSession()
        )
        # Use placeholder values when credentials are not configured.
        # The client will not be started (start() checks credentials first).
        api_id = settings.TELEGRAM_API_ID or 1
        api_hash = settings.TELEGRAM_API_HASH or "0" * 32
        self.client = TelegramClient(
            session,
            api_id,
            api_hash,
            connection_retries=10,
            retry_delay=5,
            auto_reconnect=True,
        )
        self._running = False
        self._join_lock = asyncio.Lock()
        self._last_join_at: float = 0.0
        # Telegram can rate-limit entity lookups for many hours. Keep the
        # cooldown in memory so discovery, the review screen, and schedulers
        # stop issuing duplicate RPCs until Telegram allows them again.
        self._entity_resolve_cooldown_until: float = 0.0

    async def _throttle_join(self) -> None:
        """
        Enforce a randomized minimum gap between ANY two join_group() calls,
        no matter which service triggered them. This is the account-wide
        safety net against join bursts.
        """
        async with self._join_lock:
            loop = asyncio.get_event_loop()
            now = loop.time()
            if self._last_join_at > 0:
                min_gap = random.uniform(self._GLOBAL_JOIN_MIN_GAP, self._GLOBAL_JOIN_MAX_GAP)
                elapsed = now - self._last_join_at
                if elapsed < min_gap:
                    wait = min_gap - elapsed
                    logger.info(
                        "Global join throttle: waiting %.0fs before next join (any source)",
                        wait,
                    )
                    await asyncio.sleep(wait)
            self._last_join_at = loop.time()

    @classmethod
    def get_instance(cls) -> "TelegramUserService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _has_valid_credentials(self) -> bool:
        """Return True only when real (non-placeholder) API credentials are set."""
        return bool(settings.TELEGRAM_API_ID and settings.TELEGRAM_API_HASH)

    async def start(self) -> None:
        if not self._has_valid_credentials():
            logger.error(
                "TELEGRAM_API_ID / TELEGRAM_API_HASH are not configured. "
                "Register your own app at https://my.telegram.org/apps and set "
                "these environment variables in Render. "
                "The Telethon user client will NOT start until they are provided."
            )
            # Do NOT raise — let the bot (aiogram) keep running normally.
            # Only Telethon-backed features (join, broadcast via user account) are disabled.
            return
        await self.client.connect()

        # Do not reset Telethon's update state here. Resetting pts/date makes
        # every Render restart silently discard messages received while the
        # container was sleeping or reconnecting — exactly the links the
        # discovery queue must not lose. Telethon will catch up from the
        # persisted session state and the discovery handler is idempotent.

        if not await self.client.is_user_authorized():
            logger.warning("Telegram session not authorized — interactive login required")
            raise RuntimeError(
                "Telegram session is not authorized. "
                "Run `python -m app.cli login` locally to generate a session string "
                "and set TELEGRAM_SESSION_STRING in your environment."
            )
        me = await self.client.get_me()
        logger.info("Telegram user client connected", extra={"user": str(me)})
        self._running = True

    async def stop(self) -> None:
        self._running = False
        if self.client.is_connected():
            await self.client.disconnect()
        logger.info("Telegram user client disconnected")

    def is_running(self) -> bool:
        return self._running and self.client.is_connected()

    def entity_resolve_cooldown_remaining(self) -> int:
        """Return the current entity-lookup cooldown in whole seconds."""
        remaining = self._entity_resolve_cooldown_until - asyncio.get_event_loop().time()
        return max(0, int(remaining))

    def _set_entity_resolve_cooldown(self, seconds: int) -> None:
        """Record a Telegram FloodWait without extending it on every retry."""
        if seconds <= 0:
            return
        loop = asyncio.get_event_loop()
        requested_until = loop.time() + seconds
        self._entity_resolve_cooldown_until = max(
            self._entity_resolve_cooldown_until,
            requested_until,
        )
        logger.warning(
            "Telegram entity lookups paused for %ds after FloodWait",
            self.entity_resolve_cooldown_remaining(),
        )

    async def resolve_entity(self, link: str) -> Any | None:
        """Resolve a Telegram link to an entity.
        Supports both public username links and private invite links.
        """
        parsed = LinkValidator.parse(link)
        if parsed is None:
            raise ValueError(f"Unsupported Telegram link: {link}")
        remaining = self.entity_resolve_cooldown_remaining()
        if remaining:
            raise EntityResolutionPausedError(remaining)

        try:
            if parsed.type == "invite":
                invite_hash = parsed.key.removeprefix("invite:")
                from telethon.tl.functions.messages import CheckChatInviteRequest
                result = await asyncio.wait_for(
                    self.client(CheckChatInviteRequest(invite_hash)),
                    timeout=self._ENTITY_RESOLVE_TIMEOUT,
                )
                return result  # ChatInvite or ChatInviteAlready

            username = parsed.key.removeprefix("username:")
            return await asyncio.wait_for(
                self.client.get_entity(username),
                timeout=self._ENTITY_RESOLVE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Telegram entity lookup timed out after %.0fs: %s",
                self._ENTITY_RESOLVE_TIMEOUT,
                link,
            )
            return None
        except FloodWaitError as exc:
            self._set_entity_resolve_cooldown(int(getattr(exc, "seconds", 0) or 0))
            logger.warning(
                "telegram_entity_resolve_rate_limited link=%s type=%s wait_seconds=%s",
                parsed.normalized,
                parsed.type,
                getattr(exc, "seconds", "unknown"),
            )
            raise
        except Exception as exc:
            logger.error(
                "telegram_entity_resolve_error link=%s type=%s error_type=%s error=%s",
                parsed.normalized,
                parsed.type,
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            raise

    async def is_allowed_target(self, entity: Any) -> bool:
        """Return True only for Telegram groups and supergroups.

        This is intentionally stricter than a generic "not a channel" check:
        broadcast channels, private chats, users, bots, and unresolved invite
        previews are never valid discovery or join targets.
        """
        if isinstance(entity, ChatInviteAlready):
            return self._is_allowed_chat(getattr(entity, "chat", None))
        if isinstance(entity, ChatInvite):
            # ChatInvite exposes `broadcast` for channels and `megagroup` for
            # supergroups. Basic groups have broadcast=False too, so both
            # group kinds are accepted while broadcast channels are rejected.
            return getattr(entity, "broadcast", None) is False
        return self._is_allowed_chat(entity)

    @staticmethod
    def _is_allowed_chat(entity: Any) -> bool:
        if isinstance(entity, Chat):
            return True
        if isinstance(entity, Channel):
            # Telegram represents supergroups as Channel(megagroup=True).
            # A broadcast channel is always megagroup=False and is rejected.
            return bool(getattr(entity, "megagroup", False))
        # Users and bots are never join targets.
        return False

    async def is_group(self, entity: Any) -> bool:
        """Backward-compatible alias for callers outside discovery."""
        return await self.is_allowed_target(entity)

    async def get_entity_info(
        self, entity: Any
    ) -> tuple[int | None, str | None, str | None, int | None]:
        """Extract (group_id, title, username, members_count) from any resolved entity type."""
        if isinstance(entity, ChatInviteAlready):
            chat = getattr(entity, "chat", None)
            if chat:
                return (
                    chat.id,
                    getattr(chat, "title", None),
                    getattr(chat, "username", None),
                    getattr(chat, "participants_count", None),
                )
            return None, None, None, None
        if isinstance(entity, ChatInvite):
            return (
                None,
                getattr(entity, "title", None),
                None,
                getattr(entity, "participants_count", None),
            )
        return (
            getattr(entity, "id", None),
            getattr(entity, "title", None),
            getattr(entity, "username", None),
            getattr(entity, "participants_count", None),
        )

    async def get_user_bio(self, user_id: int) -> str:
        try:
            full = await self.client.get_entity(user_id)
            if isinstance(full, User):
                from telethon.tl.functions.users import GetFullUserRequest
                info = await self.client(GetFullUserRequest(full))
                return getattr(info.full_user, "about", "") or ""
        except Exception as exc:
            logger.warning(
                "telegram_user_bio_error user_id=%d error_type=%s error=%s",
                user_id,
                type(exc).__name__,
                exc,
                exc_info=True,
            )
        return ""

    async def join_group(
        self,
        link: str,
        *,
        enforce_throttle: bool = True,
    ) -> tuple[bool, int | None, str | None]:
        """
        Join a group by invite link or public username.

        Returns (success, real_group_id, error_message).
        error_message is the exact Telethon exception type + text so callers
        can store it in the DB for later diagnosis.
        """
        if enforce_throttle:
            await self._throttle_join()
        try:
            parsed = LinkValidator.parse(link)
            if parsed is None:
                raise ValueError(f"Unsupported Telegram link: {link}")

            if parsed.type == "invite":
                # Validate the invite preview before ImportChatInviteRequest.
                # Without this guard, a channel invite could be joined before
                # the later database classification had a chance to reject it.
                invite_entity = await self.resolve_entity(parsed.normalized)
                if invite_entity is None or not await self.is_allowed_target(invite_entity):
                    logger.info(
                        "join_skipped_not_group link=%s invite_type=%s",
                        parsed.normalized,
                        type(invite_entity).__name__ if invite_entity is not None else "unresolved",
                    )
                    return False, getattr(invite_entity, "id", None), "skipped_not_group"

                invite_hash = parsed.key.removeprefix("invite:")
                from telethon.tl.functions.messages import ImportChatInviteRequest
                updates = await self.client(ImportChatInviteRequest(invite_hash))
                real_id: int | None = None
                if hasattr(updates, "chats") and updates.chats:
                    real_id = updates.chats[0].id
                logger.info("Joined private group via invite: %s (group_id=%s)", link, real_id)
                return True, real_id, None
            else:
                from telethon.tl.functions.channels import JoinChannelRequest
                username = parsed.key.removeprefix("username:")
                entity = await self.client.get_entity(username)
                if not isinstance(entity, Channel):
                    logger.warning(
                        "join_skipped_not_group link=%s entity_type=%s",
                        parsed.normalized,
                        type(entity).__name__,
                    )
                    return False, getattr(entity, "id", None), "skipped_not_group"
                if not await self.is_allowed_target(entity):
                    logger.info(
                        "join_skipped_not_group link=%s entity_type=%s megagroup=%s",
                        parsed.normalized,
                        type(entity).__name__,
                        getattr(entity, "megagroup", False),
                    )
                    return False, getattr(entity, "id", None), "skipped_not_group"
                await self.client(JoinChannelRequest(entity))
                real_id = getattr(entity, "id", None)
                logger.info("Joined public target: %s (group_id=%s)", parsed.normalized, real_id)
                return True, real_id, None
        except UserAlreadyParticipantError:
            logger.info("Already in group: %s", link)
            return True, None, None
        except InviteRequestSentError:
            # Group requires admin approval before entry.
            # The join REQUEST was successfully submitted — this is NOT a failure.
            # Status will be set to JOINED in DB so we don't retry endlessly;
            # the account will be admitted once the admin approves.
            logger.info("Join request sent (pending admin approval): %s", link)
            return True, None, "request_sent"
        except FloodWaitError as exc:
            logger.warning(
                "FloodWait joining %s: Telegram requires waiting %d seconds (~%.1fh). "
                "Queue must stop before retrying.",
                link, exc.seconds, exc.seconds / 3600,
            )
            return False, None, f"flood_wait:{exc.seconds}s"
        except PeerFloodError:
            # Account is temporarily restricted by Telegram (soft-ban / anti-spam).
            # Return a dedicated sentinel so callers re-queue WITHOUT marking FAILED
            # and WITHOUT burning the daily join counter.
            logger.warning(
                "PeerFlood joining %s — account temporarily restricted. "
                "Returning peer_flood so caller re-queues without FAILED status.",
                link,
            )
            return False, None, "peer_flood"
        except (InviteHashExpiredError, InviteHashInvalidError) as exc:
            err = f"{type(exc).__name__}: {exc}"
            logger.error("join_expired link=%s error_type=%s error=%s", link, type(exc).__name__, exc)
            return False, None, f"expired:{err}"
        except (UsernameNotOccupiedError, UsernameInvalidError, ChannelPrivateError, ChannelsTooMuchError) as exc:
            err = f"{type(exc).__name__}: {exc}"
            logger.error("join_failed_permanent link=%s error_type=%s error=%s", link, type(exc).__name__, exc)
            return False, None, f"failed:{err}"
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            logger.error(
                "join_retryable_error link=%s error_type=%s error=%s",
                link,
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            return False, None, f"retryable:{err}"

    async def send_message_to_group(self, group_id: int, message: Any) -> bool:
        try:
            await self.client.send_message(group_id, message)
            return True
        except FloodWaitError as exc:
            logger.warning(
                "FloodWait sending to group %d: wait %d seconds — returning immediately",
                group_id, exc.seconds,
            )
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
            logger.warning("PeerFlood sending DM to %d — returning immediately", user_id)
            return False, "peer_flood"
        except FloodWaitError as exc:
            logger.warning(
                "FloodWait sending DM to %d: wait %d seconds — returning immediately",
                user_id, exc.seconds,
            )
            return False, f"flood_wait:{exc.seconds}s"
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
            logger.warning("PeerFlood forwarding to %d — returning immediately", user_id)
            return False, "peer_flood"
        except FloodWaitError as exc:
            logger.warning(
                "FloodWait forwarding to %d: wait %d seconds — returning immediately",
                user_id, exc.seconds,
            )
            return False, f"flood_wait:{exc.seconds}s"
        except Exception as exc:
            logger.error("Failed to forward to %d: %s", user_id, exc)
            return False, str(exc)

    async def send_media_to_user(
        self,
        user_id: int,
        media_file_id: str,
        media_type: str,
        caption: str = "",
        bot: Any = None,
    ) -> tuple[bool, str | None]:
        """Download a Bot API file and re-upload it to a user via Telethon."""
        bio = None
        try:
            bio = await self._download_bot_file(media_file_id, bot)
            if bio is None:
                return False, "download_failed"
            await self.client.send_file(user_id, **_send_file_kwargs(bio, media_type, caption))
            return True, None
        except UserIsBlockedError:
            return False, "blocked"
        except InputUserDeactivatedError:
            return False, "deactivated"
        except FloodWaitError as exc:
            logger.warning(
                "FloodWait sending media to user %d: wait %d seconds — returning immediately",
                user_id, exc.seconds,
            )
            return False, f"flood_wait:{exc.seconds}s"
        except Exception as exc:
            logger.error("Failed to send media to user %d: %s", user_id, exc)
            return False, str(exc)
        finally:
            # Release the in-memory download buffer as soon as we're done with
            # it — otherwise it lingers until GC, wasting memory on busy
            # broadcast runs with many large media files in flight.
            if bio is not None:
                bio.close()

    # ------------------------------------------------------------------
    # Core broadcast method for groups
    # ------------------------------------------------------------------

    async def forward_message_to_group(
        self,
        group_id: int,
        group_link: str | None = None,
        message_text: str = "",
        media_file_id: str | None = None,
        media_type: str | None = None,
        is_forward: bool = False,
        forward_from_chat_id: int | None = None,
        forward_from_message_id: int | None = None,
        bot: Any = None,
        # Legacy params kept for compatibility — no longer used for primary logic
        fallback_from_peer: str | None = None,
        fallback_message_id: int | None = None,
    ) -> tuple[bool, str | None]:
        """Send a broadcast message to a group via the Telethon user client.

        Priority order:
        1. If is_forward=True and we have the original chat+message IDs →
           use Telethon forward_messages (preserves "Forwarded from …" header).
        2. If media_file_id is set → download via Bot API and re-upload.
        3. If message_text is set → send as plain text.
        """
        try:
            # --- Resolve destination group entity ---
            # Always resolve to a proper entity (carries access_hash).
            # Raw integer peer IDs cause "invalid peer" for supergroups not in cache.
            dest_entity: Any = None
            if group_link:
                try:
                    dest_entity = await self.client.get_entity(group_link)
                except Exception as link_exc:
                    logger.warning(
                        "Cannot resolve group via link %s: %s — falling back to ID",
                        group_link, link_exc,
                    )
            if dest_entity is None:
                try:
                    dest_entity = await self.client.get_entity(group_id)
                except Exception as id_exc:
                    logger.warning(
                        "Cannot resolve entity for group_id %d: %s", group_id, id_exc
                    )
                    return False, f"entity_not_found: {id_exc}"

            # ----------------------------------------------------------------
            # Path 1: TRUE FORWARD — use Telethon forward_messages so the
            # "Forwarded from <source>" label is preserved in the destination.
            # ----------------------------------------------------------------
            if is_forward and forward_from_chat_id and forward_from_message_id:
                try:
                    _fwd = await self.client.forward_messages(
                        dest_entity,
                        messages=forward_from_message_id,
                        from_peer=forward_from_chat_id,
                    )
                    _record_sent_to_group(group_id, _fwd)
                    return True, None
                except (ChannelPrivateError, Exception) as fwd_exc:
                    # Source may not be accessible from the user account.
                    # Fall through to media/text fallback.
                    logger.warning(
                        "forward_messages failed for group %d (source %d msg %d): %s — trying fallback",
                        group_id, forward_from_chat_id, forward_from_message_id, fwd_exc,
                    )
                    # If we have media or text captured at receive time, use those.
                    if not media_file_id and not message_text:
                        return False, f"forward_failed: {fwd_exc}"

            # ----------------------------------------------------------------
            # Path 2: MEDIA — download from Bot API and re-upload via Telethon.
            # This is the correct fix for photos/videos/docs: Bot API file_ids
            # cannot be used by Telethon directly. We download the bytes first.
            # ----------------------------------------------------------------
            if media_file_id and media_type:
                bio = await self._download_bot_file(media_file_id, bot)
                if bio is not None:
                    try:
                        _sent = await self.client.send_file(
                            dest_entity,
                            **_send_file_kwargs(bio, media_type, message_text),
                        )
                        _record_sent_to_group(group_id, _sent)
                        return True, None
                    except Exception as media_exc:
                        logger.warning(
                            "send_file to group %d failed: %s — trying text fallback",
                            group_id, media_exc,
                        )
                        # Fall through to text if we have it
                        if not message_text:
                            return False, str(media_exc)
                    finally:
                        bio.close()
                else:
                    logger.warning(
                        "Could not download file_id for group %d — trying text fallback",
                        group_id,
                    )
                    if not message_text:
                        return False, "media_download_failed"

            # ----------------------------------------------------------------
            # Path 3: PLAIN TEXT
            # ----------------------------------------------------------------
            if message_text:
                _sent_txt = await self.client.send_message(dest_entity, message_text)
                _record_sent_to_group(group_id, _sent_txt)
                return True, None

            return False, "no_sendable_content"

        except UserBannedInChannelError as exc:
            # Admin-level ban from sending in this specific group/channel.
            # The account is still a member — just permanently muted here.
            logger.warning("UserBannedInChannelError in group %d — account is banned from posting", group_id)
            return False, f"user_banned_in_channel: {exc}"
        except (ChatWriteForbiddenError, ChatAdminRequiredError) as exc:
            # Writing is forbidden in this chat (either this account's own
            # write rights were revoked, or the group disabled messaging for
            # non-admins entirely). Either way, we can never post here.
            logger.warning("ChatWriteForbiddenError in group %d — skipping (forced-subscribe disabled)", group_id)
            return False, f"chat_write_forbidden: {exc}"
        except FloodWaitError as exc:
            logger.warning(
                "FloodWait sending to group %d: wait %d seconds (~%.1fh)",
                group_id, exc.seconds, exc.seconds / 3600,
            )
            return False, f"flood_wait:{exc.seconds}s"
        except Exception as exc:
            logger.error("Failed to send to group %d: %s", group_id, exc, exc_info=True)
            return False, str(exc)

    async def _handle_forced_subscribe(self, group_id: int) -> None:
        """Triggered after ChatWriteForbiddenError — detects and resolves forced-subscribe."""
        try:
            from app.services.forced_subscribe_service import ForcedSubscribeService
            fs = ForcedSubscribeService.get_instance()
            auto_joined = await fs.handle_write_forbidden(group_id)
            if auto_joined:
                logger.info(
                    "ForcedSubscribe[write-forbidden]: auto-joined %d target(s) for group %d: %s",
                    len(auto_joined), group_id, auto_joined,
                )
        except Exception as exc:
            logger.error(
                "ForcedSubscribe._handle_forced_subscribe error for group %d: %s",
                group_id, exc,
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _download_bot_file(self, file_id: str, bot: Any) -> BytesIO | None:
        """Download a file from Telegram using the Bot API and return as BytesIO.

        Bot API file_ids cannot be used by Telethon directly. This bridge
        downloads the bytes via the aiogram Bot object so Telethon can
        re-upload them as native media.
        """
        if bot is None:
            logger.warning("_download_bot_file: bot is None, cannot download file_id %s", file_id)
            return None
        try:
            bio = await bot.download(file_id)
            if bio is None:
                return None
            # aiogram returns BytesIO; ensure position is at start
            bio.seek(0)
            return bio  # type: ignore[return-value]
        except Exception as exc:
            logger.warning("Failed to download file_id %s via Bot API: %s", file_id, exc)
            return None

    async def keep_online(self) -> None:
        """Set account presence to online in Telegram.

        Telegram marks accounts offline after ~5 minutes of API inactivity.
        Call this periodically (every 60s is fine) to stay visible as online.
        Failures are non-fatal — a debug log is sufficient.
        """
        try:
            from telethon.tl.functions.account import UpdateStatusRequest
            await asyncio.wait_for(
                self.client(UpdateStatusRequest(offline=False)),
                timeout=10.0,
            )
        except Exception as exc:
            logger.debug("keep_online: non-critical failure: %s", exc)

    async def reconnect(self) -> None:
        """Re-establish connection after an unexpected disconnect.

        Telethon's auto_reconnect handles TCP-level drops, but if the
        asyncio connection object itself is gone (e.g., Render network blip
        lasting longer than the keepalive window), we need to call connect()
        again manually.
        """
        try:
            if not self.client.is_connected():
                logger.info("User client disconnected — reconnecting …")
                await self.client.connect()

            if not await self.client.is_user_authorized():
                logger.error("Session not authorized after reconnect — cannot recover automatically")
                self._running = False
                return

            self._running = True
            logger.info("User client reconnected successfully")
        except Exception as exc:
            logger.error("Reconnect attempt failed: %s", exc)
            self._running = False

    async def refresh_dialogs(self, limit: int = 50, timeout: float = 12.0) -> None:
        """Refresh Telethon's entity cache with a strict timeout."""
        try:
            await asyncio.wait_for(
                self.client.get_dialogs(limit=limit),
                timeout=timeout,
            )
            logger.info("Entity cache refreshed via get_dialogs(limit=%d)", limit)
        except asyncio.TimeoutError:
            logger.warning(
                "refresh_dialogs timed out after %.0fs — proceeding without full cache",
                timeout,
            )
        except Exception as exc:
            logger.warning("refresh_dialogs failed: %s — proceeding anyway", exc)

    def on_new_message(self, handler: Any) -> None:
        self.client.add_event_handler(handler, events.NewMessage())

    def on_chat_action(self, handler: Any) -> None:
        """Register a handler for ChatAction events (user joined/added/left/etc.)."""
        self.client.add_event_handler(handler, events.ChatAction())


    async def send_dm_to_user(
        self,
        user_id: int,
        message_text: str = "",
        media_file_id: str | None = None,
        media_type: str | None = None,
        is_forward: bool = False,
        forward_from_chat_id: int | None = None,
        forward_from_message_id: int | None = None,
        bot: Any = None,
    ) -> tuple[bool, str | None]:
        """Send a direct message to a user via the Telethon user client (personal account).

        Returns (ok, error_reason).
        Possible error_reason values:
          'blocked'         — user blocked the account
          'deactivated'     — user account is deactivated
          'peer_not_found'  — Telethon cannot resolve this user_id (no shared history)
          'flood_wait:<N>s' — API flood wait; caller will sleep accordingly
          'peer_flood'      — too many DMs, account temporarily restricted
          or raw exception string for unexpected errors.
        """
        from telethon.errors.rpcerrorlist import (
            PeerIdInvalidError,
            UsernameInvalidError,
            UsernameNotOccupiedError,
        )

        try:
            # Resolve the user entity — Telethon caches access_hash after any
            # shared-group interaction, so this succeeds for contacts seen before.
            try:
                entity = await self.client.get_input_entity(user_id)
            except (PeerIdInvalidError, UsernameInvalidError,
                    UsernameNotOccupiedError, ValueError, KeyError):
                return False, "peer_not_found"

            # ── Path 1: FORWARD (preserves "Forwarded from …" header) ──────────
            if is_forward and forward_from_chat_id and forward_from_message_id:
                try:
                    await self.client.forward_messages(
                        entity=entity,
                        messages=[forward_from_message_id],
                        from_peer=forward_from_chat_id,
                    )
                    return True, None
                except Exception as fwd_exc:
                    logger.warning(
                        "forward_messages to user %d failed: %s — trying fallback",
                        user_id, fwd_exc,
                    )
                    if not media_file_id and not message_text:
                        return False, f"forward_failed: {fwd_exc}"

            # ── Path 2: MEDIA — download via Bot API, re-upload via Telethon ───
            if media_file_id and media_type:
                bio = await self._download_bot_file(media_file_id, bot)
                if bio is not None:
                    try:
                        await self.client.send_file(
                            entity,
                            **_send_file_kwargs(bio, media_type, message_text),
                        )
                        return True, None
                    except Exception as media_exc:
                        logger.warning(
                            "send_file to user %d failed: %s — trying text fallback",
                            user_id, media_exc,
                        )
                        if not message_text:
                            return False, str(media_exc)
                    finally:
                        bio.close()
                else:
                    logger.warning("Could not download file_id for user %d — text fallback", user_id)
                    if not message_text:
                        return False, "media_download_failed"

            # ── Path 3: PLAIN TEXT ─────────────────────────────────────────────
            if message_text:
                await self.client.send_message(entity, message_text)
                return True, None

            return False, "no_sendable_content"

        except UserIsBlockedError:
            return False, "blocked"
        except InputUserDeactivatedError:
            return False, "deactivated"
        except PeerFloodError:
            return False, "peer_flood"
        except FloodWaitError as exc:
            logger.warning("FloodWait sending DM to user %d: wait %ds", user_id, exc.seconds)
            return False, f"flood_wait:{exc.seconds}s"
        except Exception as exc:
            logger.error("Failed to send DM to user %d: %s", user_id, exc, exc_info=True)
            return False, str(exc)[:120]

    async def get_all_groups_from_dialogs(
        self,
        limit: int = 3000,
        *,
        strict: bool = False,
    ) -> list[dict]:
        """Return all groups/supergroups the account is in, from live Telethon dialogs.

        Ground truth for broadcast — every group the account is currently a member of,
        not just those stored in the DB. Entities carry access_hash so no
        'invalid peer' errors occur during sending.
        """
        try:
            dialogs = await asyncio.wait_for(
                self.client.get_dialogs(limit=limit),
                timeout=60.0,
            )
        except asyncio.TimeoutError:
            logger.warning("get_all_groups_from_dialogs timed out after 60s")
            if strict:
                raise
            dialogs = []
        except Exception as exc:
            logger.warning("get_all_groups_from_dialogs failed: %s", exc)
            if strict:
                raise
            dialogs = []

        groups: list[dict] = []
        for d in dialogs:
            entity = d.entity
            if isinstance(entity, Chat):
                groups.append({
                    "group_id": d.id,
                    "title": entity.title,
                    "username": None,
                    "members_count": getattr(entity, "participants_count", None),
                })
            elif isinstance(entity, Channel) and getattr(entity, "megagroup", False):
                groups.append({
                    "group_id": d.id,
                    "title": entity.title,
                    "username": getattr(entity, "username", None),
                    "members_count": getattr(entity, "participants_count", None),
                })
        logger.info("get_all_groups_from_dialogs: %d groups found", len(groups))
        return groups

    async def sync_dialogs_to_db(self, *, strict: bool = False) -> tuple[int, int]:
        """Sync all live Telethon group dialogs into the DB as JOINED groups.

        Returns (new_count, total_count).
        """
        from datetime import datetime, timezone
        from app.database.connection import AsyncSessionLocal
        from app.repositories import GroupRepository
        from app.models.group import GroupStatus

        all_groups = await self.get_all_groups_from_dialogs(strict=strict)
        new_count = 0
        active_ids: set[int] = {g["group_id"] for g in all_groups}

        async with AsyncSessionLocal() as session:
            repo = GroupRepository(session)
            for g in all_groups:
                existing = await repo.get_by_group_id(g["group_id"])
                _, created = await repo.upsert(
                    group_id=g["group_id"],
                    title=g["title"],
                    username=g.get("username"),
                    members_count=g.get("members_count"),
                    verified_target=True,
                    status=GroupStatus.JOINED,
                    # A periodic live refresh must not rewrite the original
                    # join time; otherwise "recent groups" changes every
                    # time the stats/list view is opened.
                    join_date=(
                        existing.join_date
                        if existing and existing.join_date
                        else datetime.now(timezone.utc)
                    ),
                )
                if created:
                    new_count += 1
            # Prune stale records: any group previously marked JOINED that is
            # no longer among the live dialogs (left/removed/kicked) is moved
            # to LEFT so it stops inflating "joined" counts used everywhere
            # else (stats, broadcast target count).
            left_count = await repo.mark_left_not_in(active_ids)
            await session.commit()

        logger.info(
            "sync_dialogs_to_db: %d total, %d new, %d marked left",
            len(all_groups), new_count, left_count,
        )
        return new_count, len(all_groups)

    async def leave_group_by_id(self, group_id: int, group_link: str | None = None) -> bool:
        """Actually leave a group/channel via Telethon — not just a DB flag.

        Used for groups where the account has been banned/restricted from
        sending (UserBannedInChannelError / ChatWriteForbiddenError): there
        is no value in staying a silent, permanently-muted member, so we
        exit for real instead of just excluding it from future broadcasts.
        `delete_dialog` picks the correct RPC (leave channel vs. delete
        small-group membership) based on the entity type.
        """
        try:
            entity = None
            if group_link:
                try:
                    entity = await self.client.get_entity(group_link)
                except Exception:
                    entity = None
            if entity is None:
                entity = await self.client.get_entity(group_id)
            await asyncio.wait_for(self.client.delete_dialog(entity), timeout=30.0)
            logger.info("Left group %d via Telethon (write-restricted cleanup)", group_id)
            return True
        except Exception as exc:
            logger.warning("leave_group_by_id: could not leave group %d live: %s", group_id, exc)
            return False

    async def get_all_user_dialogs(self, limit: int = 3000) -> list[dict]:
        """Return all private (one-on-one) chat users from the personal Telethon account.

        Ground truth for user broadcast — the account's actual PV contacts,
        NOT the bot's contacted_users DB. Entities carry access_hash.
        """
        from telethon.tl.types import User as TLUser
        try:
            dialogs = await asyncio.wait_for(
                self.client.get_dialogs(limit=limit),
                timeout=60.0,
            )
        except asyncio.TimeoutError:
            logger.warning("get_all_user_dialogs timed out after 60s")
            dialogs = []
        except Exception as exc:
            logger.warning("get_all_user_dialogs failed: %s", exc)
            dialogs = []

        users: list[dict] = []
        for d in dialogs:
            entity = d.entity
            if (isinstance(entity, TLUser)
                    and not getattr(entity, "bot", False)
                    and not getattr(entity, "deleted", False)):
                users.append({
                    "user_id": entity.id,
                    "username": getattr(entity, "username", None),
                    "first_name": getattr(entity, "first_name", None),
                    "last_name": getattr(entity, "last_name", None),
                })
        logger.info("get_all_user_dialogs: %d private-chat users found", len(users))
        return users

    async def sync_user_dialogs_to_db(self) -> tuple[int, int]:
        """Sync all private-chat Telethon contacts into the contacted_users DB.

        Returns (new_count, total_count).
        """
        from datetime import datetime, timezone
        from app.database.connection import AsyncSessionLocal
        from app.repositories import ContactedUserRepository

        all_users = await self.get_all_user_dialogs()
        new_count = 0
        active_ids: set[int] = {u["user_id"] for u in all_users}

        async with AsyncSessionLocal() as session:
            repo = ContactedUserRepository(session)
            for u in all_users:
                _, created = await repo.register_or_update(
                    user_id=u["user_id"],
                    username=u.get("username"),
                    first_name=u.get("first_name"),
                    last_name=u.get("last_name"),
                )
                if created:
                    new_count += 1
            # Prune stale records: anyone no longer in live PV dialogs gets
            # marked is_blocked=True so they are excluded from future broadcasts.
            pruned = await repo.prune_not_in(active_ids)
            await session.commit()

        logger.info(
            "sync_user_dialogs_to_db: %d total, %d new, %d pruned",
            len(all_users), new_count, pruned,
        )
        return new_count, len(all_users)

    async def get_session_string(self) -> str:
        if isinstance(self.client.session, StringSession):
            return self.client.session.save()
        return ""
