from datetime import datetime, timezone, timedelta
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.join_attempt import JoinAttempt
from .base import BaseRepository


class JoinAttemptRepository(BaseRepository[JoinAttempt]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(JoinAttempt, session)

    async def add(
        self,
        group_id: int,
        invite_link: str,
        attempt_number: int = 1,
        success: bool | None = None,
        error: str | None = None,
    ) -> JoinAttempt:
        record = JoinAttempt(
            group_id=group_id,
            invite_link=invite_link,
            attempt_number=attempt_number,
            attempted_at=datetime.now(timezone.utc),
            success=success,
            error=error,
        )
        self._session.add(record)
        await self._session.flush()
        return record

    async def count_today(self) -> int:
        since = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        result = await self._session.execute(
            select(func.count())
            .select_from(JoinAttempt)
            .where(JoinAttempt.attempted_at >= since)
        )
        return result.scalar_one()

    async def count_today_successful(self) -> int:
        """Count successful join attempts today (success=True only)."""
        since = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        result = await self._session.execute(
            select(func.count())
            .select_from(JoinAttempt)
            .where(JoinAttempt.attempted_at >= since)
            .where(JoinAttempt.success == True)  # noqa: E712
        )
        return result.scalar_one()

    async def count_for_group(self, group_id: int) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(JoinAttempt)
            .where(JoinAttempt.group_id == group_id)
        )
        return result.scalar_one()

    async def get_for_group(self, group_id: int) -> list[JoinAttempt]:
        result = await self._session.execute(
            select(JoinAttempt)
            .where(JoinAttempt.group_id == group_id)
            .order_by(JoinAttempt.attempted_at.desc())
        )
        return list(result.scalars().all())

    async def has_pending_approval_attempt(self, group_id: int) -> bool:
        """Return True if the most recent join attempt for this group was a pending
        approval request (InviteRequestSentError). Used to distinguish:
          - APPROVED + no attempts   → bot admin approved but join never happened → re-enqueue
          - APPROVED + pending request → Telegram admin hasn't approved yet → skip (JoinApprovalWatcher handles)
        """
        result = await self._session.execute(
            select(JoinAttempt)
            .where(JoinAttempt.group_id == group_id)
            .order_by(JoinAttempt.attempted_at.desc())
            .limit(1)
        )
        attempt = result.scalar_one_or_none()
        if attempt is None:
            return False
        return attempt.error in {"request_pending_approval", "request_sent"}
