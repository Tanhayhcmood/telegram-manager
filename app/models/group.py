import enum
from datetime import datetime, timezone
from sqlalchemy import BigInteger, String, Integer, Enum, DateTime, Boolean, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import Base


class GroupStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    JOINED = "joined"
    FAILED = "failed"
    LEFT = "left"


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String(512))
    username: Mapped[str | None] = mapped_column(String(255), index=True)
    invite_link: Mapped[str | None] = mapped_column(String(1024))
    members_count: Mapped[int | None] = mapped_column(Integer)
    join_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[GroupStatus] = mapped_column(
        Enum(
            GroupStatus,
            name="group_status",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=GroupStatus.PENDING,
        nullable=False,
        index=True,
    )
    # True unless an admin has restricted/banned the account from posting.
    # Kept separate from `status` because the account can still be a member
    # (status stays JOINED, group still shows up in live dialogs) while
    # being write-restricted — the periodic dialog sync must not reset this.
    can_write: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<Group id={self.group_id} title={self.title!r} status={self.status}>"
