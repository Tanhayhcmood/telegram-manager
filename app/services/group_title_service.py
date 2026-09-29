import re
from collections.abc import Sequence
from typing import Any

from app.services.telegram_service import TelegramUserService


_TELEGRAM_LINK_TITLE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/",
    re.IGNORECASE,
)


def is_link_title(value: str | None) -> bool:
    """Return whether a stored title is actually a Telegram link or username."""
    normalized = (value or "").strip()
    return bool(
        normalized
        and (
            _TELEGRAM_LINK_TITLE.match(normalized)
            or normalized.startswith("@")
        )
    )


def fallback_group_title(group: Any) -> str:
    """Return a useful label that never exposes a raw Telegram URL."""
    title = (group.title or "").strip()
    if title and not is_link_title(title):
        return title

    username = (group.username or "").strip()
    return f"@{username.lstrip('@')}" if username else str(group.group_id)


async def resolve_group_titles(
    groups: Sequence[Any],
    max_length: int | None = None,
) -> dict[int, str]:
    """Resolve legacy link titles and return labels safe for Telegram UI."""
    telegram = TelegramUserService.get_instance()
    display_titles: dict[int, str] = {}

    for group in groups:
        group_id = int(group.group_id)
        title = (group.title or "").strip()

        if (not title or is_link_title(title)) and telegram.is_running():
            invite_link = (group.invite_link or title).strip()
            entity = await telegram.resolve_entity(invite_link) if invite_link else None
            if entity is not None:
                _, resolved_title, username, _ = await telegram.get_entity_info(entity)
                if (
                    isinstance(resolved_title, str)
                    and resolved_title.strip()
                    and not is_link_title(resolved_title)
                ):
                    title = resolved_title.strip()
                    group.title = title
                    if username:
                        group.username = username.lower()

        if not title or is_link_title(title):
            title = fallback_group_title(group)

        display_titles[group_id] = title[:max_length] if max_length else title

    return display_titles