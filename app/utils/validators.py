import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit


_USERNAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{4,31}$")
_INVITE_HASH_RE = re.compile(r"^[a-zA-Z0-9_-]{5,}$")
_TELEGRAM_URL_RE = re.compile(
    r"(?<![\w.@])(?:(?:https?://)?(?:www\.)?)?"
    r"(?:t\.me|telegram\.me)/"
    r"(?:joinchat/[a-zA-Z0-9_-]{5,}|\+[a-zA-Z0-9_-]{5,}|"
    r"s/[a-zA-Z][a-zA-Z0-9_]{4,31}|"
    r"[a-zA-Z][a-zA-Z0-9_]{4,31})"
    r"(?:[?#][^\s<>\"']*)?",
    re.IGNORECASE,
)
_MENTION_RE = re.compile(r"(?<![\w.@/?=&%#])@([a-zA-Z][a-zA-Z0-9_]{4,31})(?!\w)")
_TRAILING_PUNCTUATION = ".,!?;:)]}»"
_TELEGRAM_HOSTS = {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}


class LinkValidator:
    @staticmethod
    def extract_links(
        text: str,
        entities: Iterable[Any] | None = None,
    ) -> list[str]:
        """Extract canonical Telegram entity links from visible and hidden URLs.

        Telegram text links can be stored in message entities and omitted from
        the visible message text. All returned values are normalized and
        de-duplicated while preserving first-seen order.
        """
        found: dict[str, None] = {}

        for match in _TELEGRAM_URL_RE.finditer(text or ""):
            normalized = LinkValidator.normalize(match.group(0))
            if normalized:
                found.setdefault(normalized, None)

        for match in _MENTION_RE.finditer(text or ""):
            normalized = LinkValidator.normalize(f"@{match.group(1)}")
            if normalized:
                found.setdefault(normalized, None)

        for entity in entities or ():
            target = getattr(entity, "url", None)
            if isinstance(target, str):
                normalized = LinkValidator.normalize(target)
                if normalized:
                    found.setdefault(normalized, None)

        return list(found)

    @staticmethod
    def normalize(link: str) -> str | None:
        """Return one canonical URL or None for malformed/non-Telegram input."""
        value = link.strip().strip("<>'\"").rstrip(_TRAILING_PUNCTUATION)
        if not value:
            return None

        if value.startswith("@"):
            username = value[1:]
            if _USERNAME_RE.fullmatch(username):
                return f"https://t.me/{username.lower()}"
            return None

        if "://" not in value:
            value = f"https://{value}"

        try:
            parsed = urlsplit(value)
            host = (parsed.hostname or "").lower()
            port = parsed.port
        except ValueError:
            return None

        if parsed.scheme.lower() not in {"http", "https"}:
            return None
        if host not in _TELEGRAM_HOSTS or port is not None:
            return None
        if parsed.username or parsed.password:
            return None

        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            return None

        first = parts[0]
        if first.lower() == "joinchat":
            if len(parts) < 2 or not _INVITE_HASH_RE.fullmatch(parts[1]):
                return None
            return f"https://t.me/+{parts[1]}"

        if first.startswith("+"):
            invite_hash = first[1:]
            if _INVITE_HASH_RE.fullmatch(invite_hash):
                return f"https://t.me/+{invite_hash}"
            return None

        # Telegram's /s/<username> preview links still identify a public entity.
        username = parts[1] if first.lower() == "s" and len(parts) > 1 else first
        if _USERNAME_RE.fullmatch(username):
            return f"https://t.me/{username.lower()}"
        return None

    @staticmethod
    def is_private_invite(link: str) -> bool:
        normalized = LinkValidator.normalize(link)
        return bool(normalized and urlsplit(normalized).path.startswith("/+"))

    @staticmethod
    def extract_username(link: str) -> str | None:
        normalized = LinkValidator.normalize(link)
        if not normalized or LinkValidator.is_private_invite(normalized):
            return None
        username = urlsplit(normalized).path.removeprefix("/")
        return username or None