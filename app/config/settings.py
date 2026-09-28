from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator
from typing import List


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Telegram User Client (Telethon) ────────────────────────────────────────
    # Session-only Render deployments use Telegram's public default API credentials.
    # The authenticated account state still comes from TELEGRAM_SESSION_STRING.
    TELEGRAM_API_ID: int = 2040
    TELEGRAM_API_HASH: str = "b18441a1ff607e10a989891a5462e627"
    TELEGRAM_PHONE: str = ""
    TELEGRAM_SESSION_NAME: str = "tg_session"
    TELEGRAM_SESSION_STRING: str = ""

    # ── Telegram Bot (Aiogram) ─────────────────────────────────────────────────
    BOT_TOKEN: str

    # Admin whitelist (comma-separated Telegram user IDs)
    ADMIN_IDS: str = ""

    # ── Database ───────────────────────────────────────────────────────────────
    DATABASE_URL: str

    # ── Redis (FSM + task queue) ───────────────────────────────────────────────
    REDIS_URL: str = ""

    # ── App ────────────────────────────────────────────────────────────────────
    APP_ENV: str = "production"
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = True
    # Optional bearer token for the read-only web dashboard. Empty keeps the
    # report public; set it in Render when the panel must be private.
    DASHBOARD_TOKEN: str = ""

    # ── Rate limiting ──────────────────────────────────────────────────────────
    RATE_LIMIT_MESSAGES: int = 30
    RATE_LIMIT_WINDOW: int = 60

    # ── Backup ─────────────────────────────────────────────────────────────────
    BACKUP_DIR: str = "/tmp/backups"
    BACKUP_KEEP_DAYS: int = 7
    BACKUP_DAILY_HOUR: int = 3

    # ── S3-compatible backup upload (optional) ─────────────────────────────────
    S3_BUCKET: str = ""
    S3_ENDPOINT: str = ""
    S3_ACCESS_KEY: str = ""
    S3_SECRET_KEY: str = ""
    S3_REGION: str = "auto"

    # ── Discovery ──────────────────────────────────────────────────────────────
    DISCOVERY_KEYWORDS: str = ""

    # ── Daily join limit ───────────────────────────────────────────────────────
    MAX_JOINS_PER_DAY: int = 8

    # ── Join queue anti-detection delays ───────────────────────────────────────
    JOIN_DELAY_MIN: int = 2400
    JOIN_DELAY_MAX: int = 5400
    FLOOD_WAIT_MARGIN_SECONDS: int = 30
    JOIN_GROUPS_ONLY: bool = True
    RETRY_BACKOFF_BASE_SECONDS: int = 60
    RETRY_BACKOFF_MAX_SECONDS: int = 3600

    # ── Auto-retry for failed joins ────────────────────────────────────────────
    RETRY_FAILED_JOINS: bool = True
    RETRY_INTERVAL_HOURS: int = 6
    RETRY_MAX_ATTEMPTS: int = 3

    # ── PeerFlood / DM broadcast protection ───────────────────────────────────
    PEER_FLOOD_PAUSE_SECONDS: int = 1800
    MAX_PEER_FLOOD_PAUSES: int = 3

    # ── TG DM delay (Telethon user-account DMs) ───────────────────────────────
    TG_DM_DELAY_SECONDS: float = 5.0

    # ── Grok AI (xAI) — conversational DM assistant ────────────────────────────
    # API key from https://console.x.ai/
    GROK_API_KEY: str = ""
    # Telegram channel invite link — shared naturally during AI conversation
    CHANNEL_INVITE_LINK: str = "https://t.me/addlist/4xJXMUc98LZhNGM8"
    # Telegram bot link — shared naturally during AI conversation
    BOT_LINK: str = "https://t.me/AmazonGiftCardBot?start=REF6538Q62Z"

    @field_validator("JOIN_DELAY_MAX", mode="after")
    @classmethod
    def validate_delay_range(cls, v: int, info: object) -> int:
        try:
            min_val = getattr(info, "data", {}).get("JOIN_DELAY_MIN", 0)
            if min_val and v < min_val:
                raise ValueError(
                    f"JOIN_DELAY_MAX ({v}) must be >= JOIN_DELAY_MIN ({min_val})."
                )
        except (AttributeError, TypeError):
            pass
        return v

    @field_validator("ADMIN_IDS", mode="before")
    @classmethod
    def parse_admin_ids(cls, v: str) -> str:
        v = v.strip()
        if v:
            for part in v.split(","):
                part = part.strip()
                if part:
                    try:
                        int(part)
                    except ValueError:
                        raise ValueError(
                            f"ADMIN_IDS contains invalid integer: {part!r}."
                        )
        return v

    def get_admin_id_list(self) -> List[int]:
        if not self.ADMIN_IDS:
            return []
        return [int(x.strip()) for x in self.ADMIN_IDS.split(",") if x.strip()]

    def get_discovery_keywords(self) -> List[str]:
        if not self.DISCOVERY_KEYWORDS:
            return []
        return [k.strip().lower() for k in self.DISCOVERY_KEYWORDS.split(",") if k.strip()]

    @property
    def is_development(self) -> bool:
        return self.APP_ENV == "development"

    @property
    def s3_enabled(self) -> bool:
        return bool(self.S3_BUCKET and self.S3_ACCESS_KEY and self.S3_SECRET_KEY)

    @property
    def redis_enabled(self) -> bool:
        return bool(self.REDIS_URL)


settings = Settings()
