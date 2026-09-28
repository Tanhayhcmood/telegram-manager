import re
from typing import Optional
from urllib.parse import urlsplit


# Telegram links often arrive with a trailing Persian/English punctuation mark,
# a query string, or as a hidden URL entity. Keep the extractor deliberately
# narrow so message permalinks such as /c/... and /s/... are not mistaken for
# groups.
_TELEGRAM_URL_RE = re.compile(
    r"(?<![\w@])"
    r"(?:(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/"
    r"(?:joinchat/|join/|\+)?[A-Za-z0-9_+-]+)",
    re.IGNORECASE,
)
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
_PRIVATE_PATH_RE = re.compile(r"^(?:joinchat/|join/|\+)([A-Za-z0-9_-]+)$", re.IGNORECASE)
_IGNORED_PUBLIC_PATHS = {
    "addlist",
    "c",
    "faq",
    "iv",
    "joinchat",
    "login",
    "proxy",
    "s",
    "share",
    "setlanguage",
    "stickers",
}
_TRAILING_PUNCTUATION = ".,;:!?)]}>»،؛؟"


class LinkValidator:
    @staticmethod
    def extract_links(text: str) -> list[str]:
        if not text:
            return []

        found: list[str] = []
        for match in _TELEGRAM_URL_RE.finditer(text):
            normalized = LinkValidator.normalize(match.group(0))
            if normalized:
                found.append(normalized)

        # @usernames are common in captions and messages without a full URL.
        # Do not treat email addresses as Telegram usernames.
        for match in re.finditer(r"(?<![\w@])@([A-Za-z][A-Za-z0-9_]{4,31})\b", text):
            normalized = LinkValidator.normalize(f"@{match.group(1)}")
            if normalized:
                found.append(normalized)

        return list(dict.fromkeys(found))

    @staticmethod
    def normalize(link: str) -> Optional[str]:
        link = link.strip().strip(_TRAILING_PUNCTUATION)
        if not link:
            return None

        if link.startswith("@"):
            username = link[1:].strip().lower()
            return f"https://t.me/{username}" if _USERNAME_RE.fullmatch(username) else None

        if not re.match(r"^https?://", link, re.IGNORECASE):
            link = f"https://{link}"

        try:
            parsed = urlsplit(link)
        except ValueError:
            return None

        if parsed.hostname is None or parsed.hostname.lower().removeprefix("www.") not in {
            "t.me",
            "telegram.me",
            "telegram.dog",
        }:
            return None

        path = parsed.path.strip().strip("/").strip(_TRAILING_PUNCTUATION)
        if not path:
            return None

        if _PRIVATE_PATH_RE.fullmatch(path):
            return f"https://t.me/{path}"

        lower_path = path.lower()
        if lower_path.split("/", 1)[0] in _IGNORED_PUBLIC_PATHS:
            return None

        if not _USERNAME_RE.fullmatch(path):
            return None

        # Query strings are tracking/referral data for public usernames, not
        # part of the entity identifier. Removing them also deduplicates links
        # that were copied from different Telegram clients.
        return f"https://t.me/{path.lower()}"

    @staticmethod
    def is_private_invite(link: str) -> bool:
        try:
            path = urlsplit(link).path.strip().strip("/")
        except ValueError:
            return False
        return _PRIVATE_PATH_RE.fullmatch(path) is not None

    @staticmethod
    def extract_username(link: str) -> Optional[str]:
        try:
            path = urlsplit(link).path.strip().strip("/")
        except ValueError:
            return None
        return path.lower() if _USERNAME_RE.fullmatch(path) else None
