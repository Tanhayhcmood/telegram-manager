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

    # Telegram User Client (Telethon)
    TELEGRAM_API_ID: int = 2040
    TELEGRAM_API_HASH: str = "b18441a1ff607e10a989891a5462e627"
    TELEGRAM_PHONE: str = ""
    TELEGRAM_SESSION_NAME: str = "tg_session"
    TELEGRAM_SESSION_STRING: str = ""

    # Telegram Bot (Aiogram)
    BOT_TOKEN: str

    # Admin whitelist (comma-separated Telegram user IDs)
    ADMIN_IDS: str = ""

    # Database
    DATABASE_URL: str

    # Redis (for FSM + task queue — optional but strongly recommended for production)
    REDIS_URL: str = ""

    # App
    APP_ENV: str = "production"
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = True

    # Rate limiting
    RATE_LIMIT_MESSAGES: int = 30
    RATE_LIMIT_WINDOW: int = 60

    # Backup
    BACKUP_DIR: str = "/tmp/backups"
    BACKUP_KEEP_DAYS: int = 7
    BACKUP_DAILY_HOUR: int = 3        # UTC hour for daily backup

    # S3-compatible backup upload (optional)
    S3_BUCKET: str = ""
    S3_ENDPOINT: str = ""            # e.g. https://r2.example.com (for Cloudflare R2)
    S3_ACCESS_KEY: str = ""
    S3_SECRET_KEY: str = ""
    S3_REGION: str = "auto"

    # Discovery
    # Comma-separated keywords; if set, only messages containing at least one are scanned
    DISCOVERY_KEYWORDS: str = ""
    MAX_JOINS_PER_DAY: int = 50       # Anti-detection: max joins in 24h

    # Join queue (anti-detection)
    JOIN_DELAY_MIN: int = 240         # seconds (4 min)
    JOIN_DELAY_MAX: int = 480         # seconds (8 min)

    # Auto-retry for failed joins
    RETRY_FAILED_JOINS: bool = True
    RETRY_INTERVAL_HOURS: int = 6
    RETRY_MAX_ATTEMPTS: int = 3

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
                            f"ADMIN_IDS contains invalid integer: {part!r}. "
                            "Expected comma-separated Telegram user IDs (integers)."
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
