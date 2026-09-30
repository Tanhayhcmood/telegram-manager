import asyncio
import time
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.database.connection import AsyncSessionLocal
from app.repositories import GroupRepository, DiscoveredLinkRepository, LogRepository, ContactedUserRepository
from app.models.group import GroupStatus
from app.models.discovered_link import LinkStatus
from app.utils.logger import get_logger
from app.utils.validators import LinkValidator

logger = get_logger(__name__)


class DiscoveryService:
    def __init__(self, tg_service: Any) -> None:
        self._tg = tg_service
        self._bio_cache: dict[int, tuple[float, str]] = {}
        self._bio_inflight: dict[int, asyncio.Task[str]] = {}
        self._bio_cache_ttl = 900
        self._bio_cache_limit = 2048

    async def process_message(self, event: Any) -> None:
        try:
            message = event.message
            text = (
                getattr(message, "raw_text", None)
                or getattr(message, "message", None)
                or getattr(message, "text", None)
                or ""
            )
            sender_id = event.sender_id

            # Track sender as contacted user
            if sender_id and sender_id > 0:
                await self._track_user(event)

            keywords = settings.get_discovery_keywords()
            text_lower = text.casefold()
            message_matches_keywords = any(kw in text_lower for kw in keywords)
            links = LinkValidator.extract_links(
                text,
                entities=getattr(message, "entities", None),
            )

            # Direct links should not be discarded just because their message
            # lacks a keyword. Fetch bios only as a fallback, and cache them so
            # repeated messages from the same sender do not trigger API calls.
            if not links and sender_id and (not keywords or message_matches_keywords):
                bio = await self._get_cached_bio(sender_id)
                if bio and (
                    not keywords
                    or any(keyword in bio.casefold() for keyword in keywords)
                ):
                    links = LinkValidator.extract_links(bio)

            if not links:
                return

            source = f"message:{event.chat_id}:{event.message.id}"
            normalized_links = dict.fromkeys(
                normalized
                for raw_link in links
                if (normalized := LinkValidator.normalize(raw_link))
            )
            for normalized in normalized_links:
                await self._register_link(normalized, source)

        except Exception as exc:
            logger.error("Error processing message: %s", exc, exc_info=True)

    async def _get_cached_bio(self, sender_id: int) -> str:
        now = time.monotonic()
        cached = self._bio_cache.get(sender_id)
        if cached and now - cached[0] < self._bio_cache_ttl:
            return cached[1]

        task = self._bio_inflight.get(sender_id)
        if task is None:
            task = asyncio.create_task(self._load_user_bio(sender_id))
            self._bio_inflight[sender_id] = task
        return await asyncio.shield(task)

    async def _load_user_bio(self, sender_id: int) -> str:
        try:
            try:
                bio = await self._tg.get_user_bio(sender_id)
            except Exception as exc:
                logger.debug("Could not fetch bio for user %d: %s", sender_id, exc)
                bio = ""

            now = time.monotonic()
            self._bio_cache[sender_id] = (now, bio)
            if len(self._bio_cache) > self._bio_cache_limit:
                expiry = now - self._bio_cache_ttl
                for user_id, (created_at, _) in list(self._bio_cache.items()):
                    if created_at < expiry:
                        self._bio_cache.pop(user_id, None)
                while len(self._bio_cache) > self._bio_cache_limit:
                    self._bio_cache.pop(next(iter(self._bio_cache)))
            return bio
        finally:
            self._bio_inflight.pop(sender_id, None)

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
                return
            logger.info("Discovered new link: %s from %s", link, source)
            await log_repo.add(action="link_discovered", result="success", target=link, details=f"source={source}")
            await session.commit()

        # Validate and enqueue outside the session so we open a fresh connection
        await self._validate_and_enqueue(link)

    async def _validate_and_enqueue(self, link: str) -> None:
        # Resolve the entity via Telegram API (may be slow — done outside any DB session)
        entity = await self._tg.resolve_entity(link)

        async with AsyncSessionLocal() as session:
            link_repo = DiscoveredLinkRepository(session)
            group_repo = GroupRepository(session)
            log_repo = LogRepository(session)

            # Re-fetch the link record we just inserted
            record = await link_repo.get_by_link(link)
            if not record:
                return

            if entity is None:
                record.status = LinkStatus.REJECTED
                record.notes = "Could not resolve entity"
                await log_repo.add(action="link_validation_failed", result="error", target=link, details="entity not found")
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

            group_id: int = entity.id
            title: str | None = getattr(entity, "title", None)
            username: str | None = getattr(entity, "username", None)
            members_count: int | None = getattr(entity, "participants_count", None)

            existing = await group_repo.get_by_group_id(group_id)
            if existing is not None:
                record.status = LinkStatus.REJECTED
                record.notes = f"Duplicate group_id={group_id}"
                await session.commit()
                logger.debug("Duplicate group %d — skipping", group_id)
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

        # Push to join queue (sequential, jittered)
        from app.services.join_queue_service import JoinQueueService
        jq = JoinQueueService.get_instance()
        await jq.enqueue(group_id=group_id, link=link, title=title)
