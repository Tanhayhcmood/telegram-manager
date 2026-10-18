from datetime import datetime

from sqlalchemy import BigInteger, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import Base


class UserBioScan(Base):
    """Stores scan timestamps for Telegram users, never the bio text."""

    __tablename__ = "user_bio_scans"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    passive_scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
