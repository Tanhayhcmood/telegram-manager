"""Bounded, read-only-history recovery for links missed during outages."""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from telethon.errors import FloodWaitError
from telethon.tl.types import Channel, Chat

from app.services.discovery_service import DiscoveryService
from app.utils.logger import get_logger

logger = get_logger(__name__)

MAX_BACKFILL_DAYS = 7
MAX_DIALOGS = 3000
MAX_GROUPS_PER_RUN = 100
MAX_MESSAGES_PER_GROUP = 100
MAX_MESSAGES_PER_RUN = 5000
MAX_NEW_LINKS_PER_RUN = 100
INTER_GROUP_DELAY_SECONDS = 0.1


@dataclass(frozen=True)
class LinkBackfillResult:
    days: int
    groups_available: int
    groups_scanned: int
    messages_scanned: int
    new_links: int
    failed_messages: int
    truncated: bool
    rate_limited: bool


class LinkBackfillService:
    """Scan a bounded window of group history without sending or joining."""

    def __init__(self, tg_service: Any) -> None:
        self._tg = tg_service
        self._discovery = DiscoveryService(tg_service)

    async def run(self, days: int) -> LinkBackfillResult:
        if isinstance(days, bool) or not isinstance(days, int):
            raise ValueError("Days must be an integer.")
        if not 1 <= days <= MAX_BACKFILL_DAYS:
            raise ValueError(f"Choose between 1 and {MAX_BACKFILL_DAYS} days.")
        if not self._tg.is_running():
            raise RuntimeError("Telegram user client is not connected.")

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        dialogs = await self._tg.get_live_dialogs(limit=MAX_DIALOGS)
        group_dialogs = [
            dialog
            for dialog in dialogs
            if isinstance(dialog.entity, Chat)
            or (
                isinstance(dialog.entity, Channel)
                and not getattr(dialog.entity, "broadcast", False)
            )
        ]
        scan_dialogs = group_dialogs[:MAX_GROUPS_PER_RUN]
        truncated = len(group_dialogs) > len(scan_dialogs)
        groups_scanned = 0
        messages_scanned = 0
        new_links = 0
        failed_messages = 0
        rate_limited = False

        for dialog in scan_dialogs:
            if (
                messages_scanned >= MAX_MESSAGES_PER_RUN
                or new_links >= MAX_NEW_LINKS_PER_RUN
            ):
                truncated = True
                break

            groups_scanned += 1
            messages_in_group = 0
            try:
                async for message in self._tg.client.iter_messages(
                    dialog.entity,
                    limit=MAX_MESSAGES_PER_GROUP,
                ):
                    message_date = getattr(message, "date", None)
                    if message_date is None:
                        continue
                    if message_date.tzinfo is None:
                        message_date = message_date.replace(tzinfo=timezone.utc)
                    else:
                        message_date = message_date.astimezone(timezone.utc)
                    if message_date < cutoff:
                        break

                    messages_scanned += 1
                    messages_in_group += 1
                    try:
                        new_links += await self._discovery.process_historical_message(
                            message,
                            dialog.id,
                        )
                    except FloodWaitError as exc:
                        failed_messages += 1
                        truncated = True
                        rate_limited = True
                        logger.warning(
                            "Stopping historical recovery after Telegram FloodWait (%ds)",
                            exc.seconds,
                        )
                        break
                    except Exception:
                        failed_messages += 1
                        logger.exception(
                            "Historical link recovery failed for chat=%s message=%s",
                            dialog.id,
                            getattr(message, "id", "?"),
                        )

                    if new_links >= MAX_NEW_LINKS_PER_RUN:
                        truncated = True
                        break
                    if messages_scanned >= MAX_MESSAGES_PER_RUN:
                        truncated = True
                        break
                if messages_in_group >= MAX_MESSAGES_PER_GROUP:
                    truncated = True
            except FloodWaitError as exc:
                failed_messages += 1
                truncated = True
                rate_limited = True
                logger.warning(
                    "Stopping historical recovery after Telegram FloodWait (%ds)",
                    exc.seconds,
                )
            except Exception:
                failed_messages += 1
                logger.exception(
                    "Could not scan Telegram history for chat=%s",
                    getattr(dialog, "id", "?"),
                )

            if rate_limited:
                break
            if groups_scanned < len(scan_dialogs):
                await asyncio.sleep(INTER_GROUP_DELAY_SECONDS)

        return LinkBackfillResult(
            days=days,
            groups_available=len(group_dialogs),
            groups_scanned=groups_scanned,
            messages_scanned=messages_scanned,
            new_links=new_links,
            failed_messages=failed_messages,
            truncated=truncated,
            rate_limited=rate_limited,
        )
