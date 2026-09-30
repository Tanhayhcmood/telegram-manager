import asyncio
import hashlib
import time
from datetime import datetime, timezone
from typing import Any, Coroutine

from app.config import settings
from app.database.connection import AsyncSessionLocal
from app.models.discovered_link import LinkStatus
from app.models.group import GroupStatus
from app.repositories import (
    ContactedUserRepository,
    DiscoveredLinkRepository,
    GroupRepository,
    LogRepository,
)
from app.utils.logger import get_logger
from app.utils.validators import LinkValidator

logger = get_logger(__name__)


def _placeholder_group_id(link: str) -> int:
    """Stable negative placeholder ID for private invite links before joining."""
    digest = int(hashlib.md5(link.encode()).hexdigest()[:10], 16) % (10**9)
    return -digest


class DiscoveryService:
    """Fast, bounded link ingestion followed by precise group validation.

    Telegram update handlers must return quickly. Link discovery therefore only
    extracts and schedules candidates; the slower Telegram entity lookup and DB
    work run in bounded background tasks. Joining remains the responsibility of
    the single-consumer, rate-limited JoinQueueService.
    """

    def __init__(self, tg_service: Any) -> None:
        self._tg = tg_service
        self._candidate_semaphore = asyncio.Semaphore(
            max(1, settings.DISCOVERY_CONCURRENCY)
        )
        self._inflight_links: set[str] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._bio_cache: dict[int, tuple[float, str]] = {}
        self._bio_inflight: dict[int, asyncio.Task[str]] = {}
        self._stopping = False

    async def process_message(self, event: Any) -> None:
        """Schedule processing and return without blocking Telethon updates."""
        if self._stopping:
            return
        self._spawn(self._process_message(event))

    async def _process_message(self, event: Any) -> None:
        try:
            message = event.message
            text = (
                getattr(message, "raw_text", None)
                or getattr(message, "message", None)
                or getattr(message, "text", None)
                or ""
            )
            sender_id = getattr(event, "sender_id", None)

            if sender_id and sender_id > 0:
                self._spawn(self._track_user(event))

            keywords = settings.get_discovery_keywords()
            text_lower = text.casefold()
            message_matches_keywords = any(keyword in text_lower for keyword in keywords)
            links = LinkValidator.extract_links(
                text,
                entities=getattr(message, "entities", None),
            )

            # A direct link is a strong signal and should never be discarded
            # merely because the surrounding message lacks a keyword. Bios are
            # only a fallback and are cached to avoid repeated Telegram calls.
            if not links and sender_id and (not keywords or message_matches_keywords):
                bio = await self._get_cached_bio(sender_id)
                if bio and (
                    not keywords
                    or any(keyword in bio.casefold() for keyword in keywords)
                ):
                    links = LinkValidator.extract_links(bio)

            if not links:
                return

            chat_id = getattr(event, "chat_id", "unknown")
            message_id = getattr(message, "id", "unknown")
            source = f"message:{chat_id}:{message_id}"
            normalized_links = dict.fromkeys(
                normalized
                for raw_link in links
                if (normalized := LinkValidator.normalize(raw_link))
            )

            for normalized in normalized_links:
                # This prevents bursts of duplicate Telegram lookups when the
                # same link is forwarded repeatedly before the first lookup
                # finishes. The DB unique constraint remains the final guard.
                if normalized in self._inflight_links:
                    continue
                self._inflight_links.add(normalized)
                self._spawn(self._process_candidate(normalized, source))

        except Exception as exc:
            logger.error("Error processing message: %s", exc, exc_info=True)

    def _spawn(self, coroutine: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def stop(self) -> None:
        """Cancel discovery work during a graceful Render shutdown."""
        self._stopping = True
        tasks = list(self._tasks)
        tasks.extend(self._bio_inflight.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._bio_inflight.clear()
        self._inflight_links.clear()

    async def _process_candidate(self, link: str, source: str) -> None:
        try:
            async with self._candidate_semaphore:
                await self._register_link(link, source)
                await self._validate_and_enqueue(link)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Error processing discovered link %s: %s", link, exc, exc_info=True)
        finally:
            self._inflight_links.discard(link)

    async def _get_cached_bio(self, sender_id: int) -> str:
        now = time.monotonic()
        cached = self._bio_cache.get(sender_id)
        if cached and now - cached[0] < settings.DISCOVERY_BIO_CACHE_TTL_SECONDS:
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
            if len(self._bio_cache) > settings.DISCOVERY_BIO_CACHE_LIMIT:
                expiry = now - settings.DISCOVERY_BIO_CACHE_TTL_SECONDS
                for user_id, (created_at, _) in list(self._bio_cache.items()):
                    if created_at < expiry:
                        self._bio_cache.pop(user_id, None)
                while len(self._bio_cache) > settings.DISCOVERY_BIO_CACHE_LIMIT:
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
        except asyncio.CancelledError:
            raise
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
            await log_repo.add(
                action="link_discovered",
                result="success",
                target=link,
                details=f"source={source}",
            )
            await session.commit()

    async def _validate_and_enqueue(self, link: str) -> None:
        # Telegram I/O stays outside the DB session so a slow lookup cannot
        # hold a database connection open.
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
                    action="link_validation_failed",
                    result="error",
                    target=link,
                    details="entity not found",
                )
                await session.commit()
                return

            if not await self._tg.is_group(entity):
                record.status = LinkStatus.REJECTED
                record.notes = "Not a group (channel or other type)"
                await log_repo.add(
                    action="link_classified_channel",
                    result="skipped",
                    target=link,
                )
                await session.commit()
                logger.info("Link %s is a channel — skipping", link)
                return

            group_id, title, username, members_count = await self._tg.get_entity_info(entity)
            if group_id is None:
                group_id = _placeholder_group_id(link)

            existing = await group_repo.get_by_group_id(group_id)
            if existing is not None:
                record.status = LinkStatus.REJECTED
                record.notes = f"Duplicate group_id={group_id}"
                await session.commit()
                logger.debug("Duplicate group %d — skipping", group_id)
                return

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
                action="group_registered",
                result="success",
                target=link,
                details=f"group_id={group_id} title={title!r}",
            )
            await session.commit()
            logger.info("Registered group %d (%s) — enqueueing for join", group_id, title)

        # Enqueueing is immediate from the discovery perspective. Actual joins
        # still pass through the queue's Telegram-safe pacing and flood handling.
        from app.services.join_queue_service import JoinQueueService

        await JoinQueueService.get_instance().enqueue(
            group_id=group_id,
            link=link,
            title=title,
        )