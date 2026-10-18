import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from telethon.errors import FloodWaitError
from telethon.tl.types import User

from app.config import settings
from app.database.connection import AsyncSessionLocal
from app.repositories import (
    GroupRepository, DiscoveredLinkRepository, LogRepository, ContactedUserRepository,
    UserBioScanRepository,
)
from app.models.group import GroupStatus
from app.models.discovered_link import LinkStatus
from app.utils.logger import get_logger
from app.utils.validators import LinkValidator

logger = get_logger(__name__)


def _placeholder_group_id(link: str) -> int:
    """Stable negative placeholder ID for private invite links before joining."""
    h = int(hashlib.md5(link.encode()).hexdigest()[:10], 16) % (10 ** 9)
    return -h


class DiscoveryService:
    def __init__(self, tg_service: Any) -> None:
        self._tg = tg_service

    async def process_message(self, event: Any) -> None:
        try:
            text = event.message.text or ""
            sender_id = event.sender_id
            keywords = settings.get_discovery_keywords()

            # Keep the existing keyword filter for message text, but do not let it
            # suppress bio checks for a sender whose message lacks a keyword.
            await self._process_links(
                text,
                f"message:{event.chat_id}:{event.message.id}",
                keywords=keywords,
            )

            if sender_id and sender_id > 0:
                try:
                    sender = await event.get_sender()
                    if sender is not None:
                        await self._track_user(sender)
                    if isinstance(sender, User) and not sender.bot:
                        await self._maybe_process_user_bio(
                            sender,
                            f"message-bio:{event.chat_id}:{event.message.id}:{sender_id}",
                        )
                except FloodWaitError as exc:
                    logger.warning(
                        "FloodWait while checking message sender bio in chat %s: %ds",
                        event.chat_id, exc.seconds,
                    )
                except Exception as exc:
                    logger.debug("Could not check message sender bio: %s", exc)

        except Exception as exc:
            logger.error("Error processing message: %s", exc, exc_info=True)

    async def _process_links(
        self, text: str, source: str, keywords: list[str] | None = None
    ) -> None:
        if not text:
            return
        if keywords and not any(keyword in text.lower() for keyword in keywords):
            return

        for raw_link in set(LinkValidator.extract_links(text)):
            normalized = LinkValidator.normalize(raw_link)
            if normalized:
                await self._register_link(normalized, source)

    async def _maybe_process_user_bio(self, user: User, source: str) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=max(1, settings.BIO_SCAN_RECHECK_DAYS)
        )
        async with AsyncSessionLocal() as session:
            scan_repo = UserBioScanRepository(session)
            if await scan_repo.is_message_checked_since(user.id, cutoff):
                return

        try:
            bio = await self._tg.fetch_user_bio(user)
        except FloodWaitError:
            raise
        except Exception as exc:
            logger.debug("Could not fetch bio for user %d: %s", user.id, exc)
            return

        # Bio links are checked directly; DISCOVERY_KEYWORDS only filter message text.
        await self._process_links(bio, source)
        await self._mark_user_message_bio_checked(user.id)

    async def _mark_user_message_bio_checked(self, user_id: int) -> None:
        async with AsyncSessionLocal() as session:
            scan_repo = UserBioScanRepository(session)
            await scan_repo.mark_message_checked(user_id, datetime.now(timezone.utc))
            await session.commit()

    async def scan_joined_group_member_bios(self) -> None:
        """Gradually check bios of users in groups the account has joined."""
        if not self._tg.is_running():
            logger.debug("Skipping member-bio scan: Telegram user client is offline")
            return

        group_cutoff = datetime.now(timezone.utc) - timedelta(
            days=max(1, settings.BIO_SCAN_GROUP_RECHECK_DAYS)
        )
        async with AsyncSessionLocal() as session:
            group_repo = GroupRepository(session)
            groups = await group_repo.get_bio_scan_candidates(
                cutoff=group_cutoff, limit=5000
            )
            scan_repo = UserBioScanRepository(session)
            checked_users = await scan_repo.get_passively_scanned_user_ids()
        if not groups:
            logger.debug("Member-bio scan: no joined groups to scan")
            return

        try:
            me = await self._tg.client.get_me()
            own_user_id = getattr(me, "id", None)
        except FloodWaitError as exc:
            logger.warning("FloodWait before member-bio scan: %ds", exc.seconds)
            return
        except Exception as exc:
            logger.debug("Could not resolve current Telegram user for bio scan: %s", exc)
            own_user_id = None

        max_total = max(1, settings.BIO_SCAN_MAX_USERS_PER_RUN)
        max_per_group = max(1, settings.BIO_SCAN_MAX_USERS_PER_GROUP)
        delay = max(0.0, settings.BIO_SCAN_DELAY_SECONDS)
        attempted_total = 0
        attempted_user_ids: set[int] = set()
        stop_for_flood_wait = False

        for group in groups:
            if attempted_total >= max_total:
                break
            group_id = group.group_id
            group_attempts = 0
            group_completed = True
            try:
                async for user in self._tg.iter_group_participants(group_id):
                    if not isinstance(user, User) or user.bot or user.id == own_user_id:
                        continue
                    if user.id in checked_users or user.id in attempted_user_ids:
                        continue
                    if attempted_total >= max_total or group_attempts >= max_per_group:
                        group_completed = False
                        break

                    attempted_total += 1
                    group_attempts += 1
                    attempted_user_ids.add(user.id)
                    try:
                        bio = await self._tg.fetch_user_bio(user)
                    except FloodWaitError as exc:
                        logger.warning(
                            "FloodWait scanning member bios in group %d: %ds; pausing this run",
                            group_id, exc.seconds,
                        )
                        stop_for_flood_wait = True
                        break
                    except Exception as exc:
                        logger.debug(
                            "Could not fetch bio for user %d in group %d: %s",
                            user.id, group_id, exc,
                        )
                        checked_users.add(user.id)
                        await self._mark_user_passively_scanned(user.id)
                    else:
                        await self._process_links(
                            bio, f"member-bio:{group_id}:{user.id}"
                        )
                        checked_users.add(user.id)
                        await self._mark_user_passively_scanned(user.id)
                    finally:
                        if delay and not stop_for_flood_wait:
                            await asyncio.sleep(delay)

                    if stop_for_flood_wait:
                        break
            except FloodWaitError as exc:
                logger.warning(
                    "FloodWait listing members of group %d: %ds; pausing this run",
                    group_id, exc.seconds,
                )
                stop_for_flood_wait = True
                group_completed = False
            except Exception as exc:
                logger.warning(
                    "Could not list members of group %d (%r) for bio scan: %s",
                    group_id, group.title, exc,
                )
                group_completed = False

            # Rotate groups even when one is inaccessible or hits a rate limit.
            try:
                async with AsyncSessionLocal() as session:
                    repo = GroupRepository(session)
                    await repo.mark_bio_scan(
                        group_id,
                        datetime.now(timezone.utc),
                        complete=group_completed,
                    )
                    await session.commit()
            except Exception as exc:
                logger.warning("Could not save bio-scan time for group %d: %s", group_id, exc)

            if group_attempts:
                logger.info(
                    "Member-bio scan checked %d users in group %d (%r); run total=%d/%d",
                    group_attempts, group_id, group.title, attempted_total, max_total,
                )
            if stop_for_flood_wait or attempted_total >= max_total:
                break

    async def _mark_user_passively_scanned(self, user_id: int) -> None:
        async with AsyncSessionLocal() as session:
            scan_repo = UserBioScanRepository(session)
            await scan_repo.mark_passively_scanned(user_id, datetime.now(timezone.utc))
            await session.commit()

    async def _track_user(self, sender: Any) -> None:
        try:
            if sender is None:
                return
            if not isinstance(sender, User) or sender.bot:
                return
            async with AsyncSessionLocal() as session:
                repo = ContactedUserRepository(session)
                _, created = await repo.register_or_update(
                    user_id=sender.id,
                    username=getattr(sender, "username", None),
                    first_name=getattr(sender, "first_name", None),
                    last_name=getattr(sender, "last_name", None),
                )
                await session.commit()
            if created:
                logger.debug("New contacted user: %d", sender.id)
        except Exception as exc:
            logger.debug("Could not track user: %s", exc)

    async def _register_link(self, link: str, source: str) -> None:
        async with AsyncSessionLocal() as session:
            link_repo = DiscoveredLinkRepository(session)
            log_repo = LogRepository(session)
            record, created = await link_repo.register(link, source)
            if not created:
                return
            logger.info("Discovered new link: %s from %s", link, source)
            await log_repo.add(action="link_discovered", result="success", target=link, details=f"source={source}")
            await session.commit()

        await self._validate_and_enqueue(link)

    async def _validate_and_enqueue(self, link: str) -> None:
        entity = await self._tg.resolve_entity(link)

        async with AsyncSessionLocal() as session:
            link_repo = DiscoveredLinkRepository(session)
            group_repo = GroupRepository(session)
            log_repo = LogRepository(session)

            record = await link_repo.get_by_link(link)
            if not record:
                return

            if entity is None:
                record.status = LinkStatus.REJECTED
                record.notes = "Could not resolve entity"
                await log_repo.add(
                    action="link_validation_failed", result="error",
                    target=link, details="entity not found",
                )
                await session.commit()
                return

            is_group = await self._tg.is_group(entity)
            if not is_group:
                record.status = LinkStatus.REJECTED
                record.notes = "Not a group (channel or other type)"
                await log_repo.add(action="link_classified_channel", result="skipped", target=link)
                await session.commit()
                logger.info("Link %s is a channel — skipping", link)
                return

            # get_entity_info handles ChatInvite / ChatInviteAlready / regular entities
            group_id, title, username, members_count = await self._tg.get_entity_info(entity)

            # For private invite links not yet joined, group_id is None —
            # use a placeholder so we can track the record.
            if group_id is None:
                group_id = _placeholder_group_id(link)

            # Check for duplicate by real group_id
            existing = await group_repo.get_by_group_id(group_id)
            if existing is not None:
                record.status = LinkStatus.REJECTED
                record.notes = f"Duplicate group_id={group_id}"
                await session.commit()
                logger.debug("Duplicate group %d — skipping", group_id)
                return

            # Also check by invite link to avoid re-queuing the same private link
            existing_by_link = await group_repo.get_by_invite_link(link)
            if existing_by_link is not None:
                record.status = LinkStatus.REJECTED
                record.notes = "Duplicate invite_link"
                await session.commit()
                logger.debug("Duplicate invite_link %s — skipping", link)
                return

            await group_repo.upsert(
                group_id=group_id,
                title=title,
                username=username.lower() if username else None,
                invite_link=link,
                members_count=members_count,
                status=GroupStatus.PENDING,
            )
            record.status = LinkStatus.APPROVED
            await log_repo.add(
                action="group_registered", result="success", target=link,
                details=f"group_id={group_id} title={title!r}",
            )
            await session.commit()
            logger.info("Registered group %d (%s) — enqueueing for join", group_id, title)

        from app.services.join_queue_service import JoinQueueService
        jq = JoinQueueService.get_instance()
        await jq.enqueue(group_id=group_id, link=link, title=title)
