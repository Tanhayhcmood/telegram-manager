import re
from dataclasses import dataclass
from typing import Literal, Optional
from urllib.parse import parse_qs, urlsplit


LinkType = Literal["invite", "username"]

_TELEGRAM_HOSTS = {"t.me", "telegram.me", "telegram.dog"}
_TELEGRAM_URL_RE = re.compile(
    r"(?<![\w@])"
    r"(?:(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/"
    r"[^\s<>'\"،؛؟]+)",
    re.IGNORECASE,
)
_TG_URL_RE = re.compile(
    r"(?<![\w@])tg://(?:join\?invite=[^\s<>'\"،؛؟]+|resolve\?domain=[^\s<>'\"،؛؟]+)",
    re.IGNORECASE,
)
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
_PRIVATE_PATH_RE = re.compile(r"^(?:joinchat/|join/|\+)([A-Za-z0-9_-]+)$", re.IGNORECASE)
_IGNORED_PUBLIC_PATHS = {
    "addemoji",
    "addlist",
    "addstickers",
    "boost",
    "c",
    "contact",
    "faq",
    "invoice",
    "iv",
    "joinchat",
    "login",
    "proxy",
    "setlanguage",
    "s",
    "share",
    "socks",
    "stickers",
    "addtheme",
}
_TRAILING_PUNCTUATION = ".,;:!?)]}>»،؛؟"


@dataclass(frozen=True)
class ParsedLink:
    normalized: str
    key: str
    type: LinkType


class LinkValidator:
    """Extract and canonicalize every Telegram join target we support."""

    @staticmethod
    def extract_links(text: str) -> list[str]:
        if not text:
            return []

        found: dict[str, str] = {}
        candidates = [
            *(match.group(0) for match in _TELEGRAM_URL_RE.finditer(text)),
            *(match.group(0) for match in _TG_URL_RE.finditer(text)),
        ]

        # @usernames are common in captions and messages without a full URL.
        # Do not treat email addresses as Telegram usernames.
        candidates.extend(
            f"@{match.group(1)}"
            for match in re.finditer(r"(?<![\w@])@([A-Za-z][A-Za-z0-9_]{4,31})\b", text)
        )

        for candidate in candidates:
            parsed = LinkValidator.parse(candidate)
            if parsed:
                found.setdefault(parsed.key, parsed.normalized)
        return list(found.values())

    @staticmethod
    def parse(link: str) -> Optional[ParsedLink]:
        raw = link.strip().strip(_TRAILING_PUNCTUATION)
        if not raw:
            return None

        if raw.startswith("@"):
            username = raw[1:].strip().lower()
            if not _USERNAME_RE.fullmatch(username):
                return None
            return ParsedLink(
                normalized=f"https://t.me/{username}",
                key=f"username:{username}",
                type="username",
            )

        if raw.lower().startswith("tg://"):
            try:
                parsed = urlsplit(raw)
                query = parse_qs(parsed.query)
            except ValueError:
                return None
            if parsed.netloc.lower() == "join" and query.get("invite"):
                invite_hash = query["invite"][0].strip()
                if re.fullmatch(r"[A-Za-z0-9_-]+", invite_hash):
                    return ParsedLink(
                        normalized=f"https://t.me/+{invite_hash}",
                        key=f"invite:{invite_hash}",
                        type="invite",
                    )
            if parsed.netloc.lower() == "resolve" and query.get("domain"):
                username = query["domain"][0].strip().lower()
                if _USERNAME_RE.fullmatch(username):
                    return ParsedLink(
                        normalized=f"https://t.me/{username}",
                        key=f"username:{username}",
                        type="username",
                    )
            return None

        if not re.match(r"^https?://", raw, re.IGNORECASE):
            raw = f"https://{raw}"

        try:
            parsed = urlsplit(raw)
        except ValueError:
            return None

        hostname = (parsed.hostname or "").lower().removeprefix("www.")
        if hostname not in _TELEGRAM_HOSTS:
            return None

        path = parsed.path.strip().strip("/").strip(_TRAILING_PUNCTUATION)
        if not path:
            return None

        private_match = _PRIVATE_PATH_RE.fullmatch(path)
        if private_match:
            invite_hash = private_match.group(1)
            return ParsedLink(
                normalized=f"https://t.me/+{invite_hash}",
                key=f"invite:{invite_hash}",
                type="invite",
            )

        # Message links such as t.me/name/123 still identify the public
        # username. Route prefixes such as /c/ and /s/ are never usernames.
        first_segment = path.split("/", 1)[0].lower()
        if first_segment in _IGNORED_PUBLIC_PATHS:
            return None
        if not _USERNAME_RE.fullmatch(first_segment):
            return None
        return ParsedLink(
            normalized=f"https://t.me/{first_segment}",
            key=f"username:{first_segment}",
            type="username",
        )

    @staticmethod
    def normalize(link: str) -> Optional[str]:
        parsed = LinkValidator.parse(link)
        return parsed.normalized if parsed else None

    @staticmethod
    def canonical_key(link: str) -> Optional[str]:
        parsed = LinkValidator.parse(link)
        return parsed.key if parsed else None

    @staticmethod
    def link_type(link: str) -> Optional[LinkType]:
        parsed = LinkValidator.parse(link)
        return parsed.type if parsed else None

    @staticmethod
    def is_private_invite(link: str) -> bool:
        return LinkValidator.link_type(link) == "invite"

    @staticmethod
    def extract_username(link: str) -> Optional[str]:
        parsed = LinkValidator.parse(link)
        if parsed and parsed.type == "username":
            return parsed.key.removeprefix("username:")
        return None
