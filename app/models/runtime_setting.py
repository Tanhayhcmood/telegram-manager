from datetime import datetime
from sqlalchemy import Integer, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import Base


class RuntimeSetting(Base):
    """Singleton row (id=1) holding admin-adjustable runtime settings.

    Currently the exact join-queue delay is exposed to admins via the bot UI,
    but this table is the place to add future live-configurable knobs without
    needing a redeploy.
    """
    __tablename__ = "runtime_settings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    join_delay_min: Mapped[int] = mapped_column(Integer, nullable=False)
    join_delay_max: Mapped[int] = mapped_column(Integer, nullable=False)
    # Persistent global cooldown. It survives a process restart so a Telegram
    # FloodWait cannot be bypassed by restarting the worker.
    join_not_before_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<RuntimeSetting join_delay=[{self.join_delay_min},{self.join_delay_max}]>"
