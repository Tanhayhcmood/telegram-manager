import asyncio
import hashlib
from datetime import datetime, timezone
from typing import Any

from app.database.connection import AsyncSessionLocal
from app.repositories import GroupRepository, DiscoveredLinkRepository, LogRepository, ContactedUserRepository
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
            message = event.message
            text = message.text or ""
            sender_id = event.sender_id

            if sender_id and sender_id > 0:
                await self._track_user(event)

            links = LinkValidator.extract_links(text)
            # Text-url entities contain links hidden behind labels/buttons and
            # are not necessarily present in message.text.
            try:
                from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl

                for entity, entity_text in message.get_entities_text():
                    if isinstance(entity, MessageEntityTextUrl):
                        links.extend(LinkValidator.extract_links(entity.url or ""))
                    elif isinstance(entity, MessageEntityUrl):
                        links.extend(LinkValidator.extract_links(str(entity_text or "")))
            except Exception as exc:
                logger.debug("Could not inspect message URL entities: %s", exc)

            if sender_id:
                try:
                    bio = await self._tg.get_user_bio(sender_id)
                    if bio:
                        links += LinkValidator.extract_links(bio)
                except Exception:
                    pass

            if not links:
                # Keywords remain useful for future extensions, but they must
                # never suppress a message that already contains a Telegram
                # link. Direct links are always collected.
                return

            source = f"message:{event.chat_id}:{event.message.id}"
            for raw_link in dict.fromkeys(links):
                normalized = LinkValidator.normalize(raw_link)
                if normalized:
                    try:
                        await self._register_link(normalized, source)
                    except Exception as exc:
                        # One malformed/contended link must not drop the rest
                        # of the links found in the same message.
                        logger.error(
                            "Could not process discovered link %s: %s",
                            normalized,
                            exc,
                            exc_info=True,
                        )

        except Exception as exc:
            logger.error("Error processing message: %s", exc, exc_info=True)

    async def _track_user(self, event: Any) -> None:
        try:
            sender = await event.get_sender()
            if sender is None:
                return
            from telethon.tl.types import User
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
                # A transient resolve/database/client failure must not make a
                # link permanently invisible. Permanent classifications can
                # safely remain terminal; everything else is eligible for a
                # fresh validation and queue attempt.
                if record.status in (LinkStatus.APPROVED, LinkStatus.JOINED):
                    return
                if record.status == LinkStatus.REJECTED and (
                    (record.notes or "").startswith("Not a group")
                    or (record.notes or "").startswith("Duplicate")
                ):
                    return
                record.status = LinkStatus.PENDING
                record.notes = None
                await session.commit()
            else:
                logger.info("Discovered new link: %s from %s", link, source)
                await log_repo.add(
                    action="link_discovered",
                    result="success",
                    target=link,
                    details=f"source={source}",
                )
                await session.commit()

        await self._validate_and_enqueue(link)

    async def retry_pending_link(self, link: str) -> None:
        """Revalidate a link that could not be resolved during a transient outage."""
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
                # Keep it pending so a reconnect or a later Telegram API
                # response can recover it. The scheduler retries pending links.
                record.status = LinkStatus.PENDING
                record.notes = "Could not resolve entity yet; retry scheduled"
                await log_repo.add(
                    action="link_validation_pending", result="retryable",
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

            # Check both identifiers. A public group may already be known by
            # its numeric ID, while a private invite may first be represented
            # by a stable negative placeholder.
            existing_by_id = await group_repo.get_by_group_id(group_id)
            existing_by_link = await group_repo.get_by_invite_link(link)
            existing_group = existing_by_id or existing_by_link

            if existing_group is not None:
                if existing_group.status not in (GroupStatus.LEFT, GroupStatus.FAILED):
                    if existing_group.status == GroupStatus.JOINED:
                        record.status = LinkStatus.JOINED
                        record.notes = "Already joined"
                    elif existing_group.status == GroupStatus.APPROVED:
                        record.status = LinkStatus.APPROVED
                        record.notes = "Join request already pending"
                    else:
                        record.status = LinkStatus.REJECTED
                        record.notes = (
                            f"Duplicate group_id={existing_group.group_id}"
                            if existing_by_id is not None
                            else "Duplicate invite_link"
                        )
                    await session.commit()
                    logger.debug("Duplicate group/link %s — skipping", link)
                    return

                group_id = existing_group.group_id
                existing_group.status = GroupStatus.PENDING
                existing_group.invite_link = link
                existing_group.title = title or existing_group.title
                existing_group.username = username.lower() if username else existing_group.username
                existing_group.members_count = members_count or existing_group.members_count
                record.status = LinkStatus.APPROVED
                record.notes = None
                await log_repo.add(
                    action="group_reactivated",
                    result="success",
                    target=link,
                    details=f"group_id={group_id} title={title!r}",
                )
                await session.commit()
                logger.info("Reactivated group %d (%s) — enqueueing for join", group_id, title)
            else:
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
