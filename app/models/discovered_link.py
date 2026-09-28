import enum
from datetime import datetime
from sqlalchemy import String, Enum, DateTime, func, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import Base


class LinkStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    JOINED = "joined"
    FAILED = "failed"
    EXPIRED = "expired"
    REQUEST_SENT = "request_sent"
    SKIPPED_NOT_GROUP = "skipped_not_group"


class LinkType(str, enum.Enum):
    INVITE = "invite"
    USERNAME = "username"


class DiscoveredLink(Base):
    __tablename__ = "discovered_links"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    link: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True, index=True)
    canonical_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True, index=True)
    type: Mapped[LinkType] = mapped_column(
        "type",
        Enum(
            LinkType,
            name="link_type",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=LinkType.USERNAME,
        nullable=False,
        index=True,
    )
    source: Mapped[str | None] = mapped_column(String(512))
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    status: Mapped[LinkStatus] = mapped_column(
        Enum(
            LinkStatus,
            name="link_status",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=LinkStatus.PENDING,
        nullable=False,
        index=True,
    )
    notes: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<DiscoveredLink id={self.id} link={self.link!r} status={self.status}>"
