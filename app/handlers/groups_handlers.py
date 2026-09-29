"""
Groups management handlers — approve/reject pending groups, view all groups,
list failed groups, and retry failed joins.

Fixes applied:
  1. cb_approve: after setting APPROVED, enqueue the group for join immediately.
  2. _show_pending_page: use DB-level pagination (count_by_status + get_by_status_paged)
     instead of loading 500 rows into memory and slicing.
  3. cb_groups_failed: add full pagination support (was limited to 20 with no next page).
"""
import asyncio
import hashlib
from html import escape as _esc
from aiogram import Router, F
from aiogram.filters import StateFilter
from aiogram.fsm.state import default_state
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, Message

from app.database.connection import AsyncSessionLocal
from app.repositories import GroupRepository
from app.repositories.join_attempt_repository import JoinAttemptRepository
from app.models.group import GroupStatus
from app.utils.logger import get_logger
from app.utils.validators import LinkValidator
from app.services.live_group_state_service import LiveGroupStateService
from app.services.telegram_service import TelegramUserService

logger = get_logger(__name__)
router = Router(name="groups")

PAGE_SIZE = 15


def _back_btn() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu")]
    ])


def _status_emoji(status: GroupStatus) -> str:
    return {
        GroupStatus.PENDING:  "⏳",
        GroupStatus.APPROVED: "✅",
        GroupStatus.REJECTED: "❌",
        GroupStatus.JOINED:   "🟢",
        GroupStatus.FAILED:   "🔴",
        GroupStatus.LEFT:     "🚪",
    }.get(status, "❓")


async def _resolve_pending_title(title: str | None, invite_link: str | None) -> tuple[str, str | None]:
    """Resolve URL-shaped pending titles to the Telegram group name."""
    raw = (title or "").strip()
    if not raw.lower().startswith(("http://", "https://")):
        return raw, None
    link = (invite_link or raw).strip()
    try:
        tg = TelegramUserService.get_instance()
        entity = await asyncio.wait_for(tg.resolve_entity(link), timeout=5)
        if entity is not None:
            _, resolved, username, _ = await tg.get_entity_info(entity)
            resolved = (resolved or "").strip()
            if resolved and not resolved.lower().startswith(("http://", "https://")):
                return resolved, username.lower() if username else None
    except Exception as exc:
        logger.debug("Could not resolve pending group title for %s: %s", link, exc)
    return raw, None

def _list_keyboard(page: int, total: int, prefix: str) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ قبلی", callback_data=f"{prefix}:{page - 1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="بعدی ▶️", callback_data=f"{prefix}:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🔄 بروزرسانی لحظه‌ای", callback_data="groups_list")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ── Groups list with pagination ───────────────────────────────────────────────

@router.callback_query(F.data == "groups_list")
async def cb_groups_list(callback: CallbackQuery) -> None:
    await _show_groups_page(callback, 0)


@router.callback_query(F.data.startswith("groups_page:"))
async def cb_groups_page(callback: CallbackQuery) -> None:
    page = int(callback.data.split(":")[1])  # type: ignore[union-attr]
    await _show_groups_page(callback, page)


async def _show_groups_page(callback: CallbackQuery, page: int) -> None:
    await callback.answer()
    live_snapshot = await LiveGroupStateService.get_instance().refresh()
    async with AsyncSessionLocal() as session:
        repo = GroupRepository(session)
        total = await repo.count()
        groups = await repo.get_latest(limit=PAGE_SIZE, offset=page * PAGE_SIZE)

    if not groups:
        await callback.message.edit_text("📋 هیچ گروهی ثبت نشده.", reply_markup=_back_btn())  # type: ignore[union-attr]
        return

    live_line = (
        f"🟢 snapshot زنده Telegram: <code>{live_snapshot.refreshed_at.strftime('%H:%M:%S UTC')}</code> "
        f"({live_snapshot.live_group_count} گروه)"
        if live_snapshot.live_group_count is not None
        else f"⚠️ snapshot زنده در دسترس نیست: <code>{_esc(live_snapshot.error or 'نامشخص')}</code>"
    )
    lines = [
        f"📋 <b>گروه‌ها</b> — کل ثبت‌شده: <code>{total}</code>",
        live_line,
        f"صفحه {page + 1} از {max(1, -(-total // PAGE_SIZE))}:\n",
    ]
    for g in groups:
        emoji = _status_emoji(g.status)
        title = _esc((g.title or "بدون عنوان")[:35])
        lines.append(f"{emoji} <code>{g.group_id}</code> — {title}")

    await callback.message.edit_text(  # type: ignore[union-attr]
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_list_keyboard(page, total, "groups_page"),
    )


# ── Pending groups with DB-level pagination ───────────────────────────────────
# FIX: Previously loaded up to 500 rows into memory then sliced in Python.
# Now uses count_by_status + get_by_status_paged for proper DB-level paging.

@router.callback_query(F.data == "groups_pending")
async def cb_groups_pending(callback: CallbackQuery) -> None:
    await _show_pending_page(callback, 0)


@router.callback_query(F.data.startswith("pending_page:"))
async def cb_pending_page(callback: CallbackQuery) -> None:
    page = int(callback.data.split(":")[1])  # type: ignore[union-attr]
    await _show_pending_page(callback, page)


async def _show_pending_page(callback: CallbackQuery, page: int) -> None:
    await callback.answer()
    async with AsyncSessionLocal() as session:
        repo = GroupRepository(session)
        # DB-level count — no full table scan
        total = await repo.count_by_status(GroupStatus.PENDING)
        # DB-level paged fetch — only PAGE_SIZE rows loaded
        groups = await repo.get_by_status_paged(
            GroupStatus.PENDING, limit=PAGE_SIZE, offset=page * PAGE_SIZE
        )
        rows = [(g.group_id, g.title, g.invite_link, g.username) for g in groups]

    if not rows:
        await callback.message.edit_text("✅ هیچ گروهی در انتظار بررسی نیست.", reply_markup=_back_btn())  # type: ignore[union-attr]
        return

    resolved_rows = await asyncio.gather(*[
        _resolve_pending_title(title, invite_link)
        for _, title, invite_link, _ in rows
    ])
    resolved_updates = {}
    for (group_id, original_title, _, _), (resolved_title, username) in zip(rows, resolved_rows):
        if resolved_title and resolved_title != (original_title or "").strip():
            resolved_updates[group_id] = (resolved_title, username)
    if resolved_updates:
        async with AsyncSessionLocal() as session:
            repo = GroupRepository(session)
            for group_id, (resolved_title, username) in resolved_updates.items():
                group = await repo.get_by_group_id(group_id)
                if group is not None:
                    group.title = resolved_title
                    if username:
                        group.username = username
            await session.commit()

    total_pages = max(1, -(-total // PAGE_SIZE))
    lines = [f"⏳ <b>در انتظار بررسی</b> ({total} گروه — صفحه {page + 1} از {total_pages}):" + chr(10)]
    action_btns: list[list[InlineKeyboardButton]] = []

    for (group_id, original_title, _, _), (resolved_title, _) in zip(rows, resolved_rows):
        raw_title = (resolved_title or original_title or str(group_id))[:25]
        safe_title = _esc(raw_title)
        lines.append(f"• <code>{group_id}</code> — {safe_title}")
        action_btns.append([
            InlineKeyboardButton(text=f"✅ {raw_title}", callback_data=f"approve:{group_id}"),
            InlineKeyboardButton(text="❌ رد", callback_data=f"reject:{group_id}"),
        ])
# ── Approve / Reject ──────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("approve:"))
async def cb_approve(callback: CallbackQuery) -> None:
    """
    Approve a pending group and immediately enqueue it for joining.

    FIX: Previously only set status=APPROVED in DB but never triggered the actual
    join. The group would sit in APPROVED state forever with no join attempt made.
    Now we also call JoinQueueService.enqueue() so the join happens automatically.
    """
    group_id = int(callback.data.split(":")[1])  # type: ignore[union-attr]
    actor = str(callback.from_user.id) if callback.from_user else "admin"

    async with AsyncSessionLocal() as session:
        repo = GroupRepository(session)
        from app.repositories import LogRepository
        log_repo = LogRepository(session)
        group = await repo.get_by_group_id(group_id)
        if not group:
            await callback.answer("گروه یافت نشد.", show_alert=True)
            return

        group.status = GroupStatus.APPROVED
        await log_repo.add(
            action="group_approved",
            result="success",
            actor=actor,
            target=str(group_id),
            details=f"title={group.title!r} link={group.invite_link!r}",
        )
        await session.commit()

        # Capture values needed for enqueueing after session closes
        invite_link = group.invite_link
        title = group.title

    # ── CRITICAL FIX: enqueue the group for joining ────────────────────────────
    # Without this, approve only changed the DB status but never triggered a join.
    if invite_link:
        from app.services.join_queue_service import JoinQueueService
        jq = JoinQueueService.get_instance()
        await jq.enqueue(group_id=group_id, link=invite_link, title=title, attempt=1)
        await callback.answer(f"✅ گروه تایید شد و در صف عضویت قرار گرفت.")
        logger.info(
            "Admin %s approved group_id=%d (%r) — enqueued for join",
            actor, group_id, title,
        )
    else:
        await callback.answer(
            f"✅ گروه {group_id} تایید شد (بدون لینک — عضویت دستی لازم است).",
            show_alert=True,
        )
        logger.warning(
            "Admin approved group_id=%d but it has no invite_link — cannot auto-join",
            group_id,
        )


@router.callback_query(F.data.startswith("reject:"))
async def cb_reject(callback: CallbackQuery) -> None:
    group_id = int(callback.data.split(":")[1])  # type: ignore[union-attr]
    actor = str(callback.from_user.id) if callback.from_user else "admin"
    async with AsyncSessionLocal() as session:
        repo = GroupRepository(session)
        from app.repositories import LogRepository
        log_repo = LogRepository(session)
        group = await repo.get_by_group_id(group_id)
        if group:
            group.status = GroupStatus.REJECTED
            await log_repo.add(action="group_rejected", result="success", actor=actor, target=str(group_id))
            await session.commit()
            await callback.answer(f"❌ گروه {group_id} رد شد.")
        else:
            await callback.answer("گروه یافت نشد.", show_alert=True)


# ── Failed groups with full pagination ────────────────────────────────────────
# FIX: Previously loaded only 20 rows with no "next page" button.
# Now uses count_by_status + get_by_status_paged for full pagination.

@router.callback_query(F.data == "groups_failed")
async def cb_groups_failed(callback: CallbackQuery) -> None:
    await _show_failed_page(callback, 0)


@router.callback_query(F.data.startswith("failed_page:"))
async def cb_failed_page(callback: CallbackQuery) -> None:
    page = int(callback.data.split(":")[1])  # type: ignore[union-attr]
    await _show_failed_page(callback, page)


async def _show_failed_page(callback: CallbackQuery, page: int) -> None:
    await callback.answer()
    async with AsyncSessionLocal() as session:
        repo = GroupRepository(session)
        total = await repo.count_by_status(GroupStatus.FAILED)
        groups = await repo.get_by_status_paged(
            GroupStatus.FAILED, limit=PAGE_SIZE, offset=page * PAGE_SIZE
        )

    if not groups:
        await callback.message.edit_text("✅ هیچ گروه ناموفقی وجود ندارد.", reply_markup=_back_btn())  # type: ignore[union-attr]
        return

    total_pages = max(1, -(-total // PAGE_SIZE))
    lines = [f"🔴 <b>گروه‌های ناموفق</b> ({total} گروه — صفحه {page + 1} از {total_pages}):\n"]
    btns: list[list[InlineKeyboardButton]] = []

    for g in groups:
        raw_title = (g.title or str(g.group_id))[:25]
        title = _esc(raw_title)
        lines.append(f"• <code>{g.group_id}</code> — {title}")
        btns.append([
            InlineKeyboardButton(
                text=f"🔄 {raw_title}",
                callback_data=f"retry_join:{g.group_id}",
            )
        ])

    # Navigation
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ قبلی", callback_data=f"failed_page:{page - 1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="بعدی ▶️", callback_data=f"failed_page:{page + 1}"))
    if nav:
        btns.append(nav)

    btns.append([InlineKeyboardButton(text="🔄 تلاش مجدد همه", callback_data="retry_all_failed")])
    btns.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu")])

    await callback.message.edit_text(  # type: ignore[union-attr]
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=btns),
    )


# ── Manual retry for a single failed group ────────────────────────────────────

@router.callback_query(F.data.startswith("retry_join:"))
async def cb_retry_join(callback: CallbackQuery) -> None:
    group_id = int(callback.data.split(":")[1])  # type: ignore[union-attr]
    invite_link: str | None = None
    title: str | None = None
    attempt_count: int = 0

    async with AsyncSessionLocal() as session:
        repo = GroupRepository(session)
        attempt_repo = JoinAttemptRepository(session)
        group = await repo.get_by_group_id(group_id)
        if not group or not group.invite_link:
            await callback.answer("گروه یا لینک یافت نشد.", show_alert=True)
            return

        attempt_count = await attempt_repo.count_for_group(group_id)
        invite_link = group.invite_link
        title = group.title

        # Refuse to retry if Telegram admin approval is still pending —
        # re-sending the request wastes quota and Telegram ignores it.
        if group.status == GroupStatus.APPROVED:
            has_pending = await attempt_repo.has_pending_approval_attempt(group_id)
            if has_pending:
                await callback.answer(
                    "⏳ درخواست عضویت قبلاً ارسال شده و در انتظار تأیید ادمین گروه است.",
                    show_alert=True,
                )
                return

    max_attempts = 3
    if attempt_count >= max_attempts:
        await callback.answer(
            f"⚠️ حداکثر تعداد تلاش ({max_attempts} بار) رسیده.",
            show_alert=True,
        )
        return

    # Reset status to PENDING so the group doesn't show as FAILED during the retry.
    async with AsyncSessionLocal() as session:
        repo = GroupRepository(session)
        from app.repositories import LogRepository
        log_repo = LogRepository(session)
        group = await repo.get_by_group_id(group_id)
        if group and group.status == GroupStatus.FAILED:
            group.status = GroupStatus.PENDING
            await log_repo.add(
                action="group_retry_queued",
                result="success",
                actor=str(callback.from_user.id) if callback.from_user else "admin",
                target=str(group_id),
                details=f"attempt={attempt_count + 1}/{max_attempts}",
            )
            await session.commit()

    from app.services.join_queue_service import JoinQueueService
    jq = JoinQueueService.get_instance()
    await jq.enqueue(
        group_id=group_id,
        link=invite_link,
        title=title,
        attempt=attempt_count + 1,
    )
    await callback.answer(
        f"🔄 گروه {group_id} در صف تلاش مجدد ({attempt_count + 1}/{max_attempts}) قرار گرفت.",
        show_alert=True,
    )
    logger.info(
        "Manual retry queued for group_id=%d attempt=%d by admin %s",
        group_id, attempt_count + 1,
        callback.from_user.id if callback.from_user else "?",
    )


# ── Retry ALL failed groups ───────────────────────────────────────────────────

@router.callback_query(F.data == "retry_all_failed")
async def cb_retry_all_failed(callback: CallbackQuery) -> None:
    await callback.answer("⏳ در حال افزودن به صف...")
    from app.services.join_queue_service import JoinQueueService
    jq = JoinQueueService.get_instance()

    async with AsyncSessionLocal() as session:
        repo = GroupRepository(session)
        attempt_repo = JoinAttemptRepository(session)
        from app.repositories import LogRepository
        log_repo = LogRepository(session)
        groups = await repo.get_by_status(GroupStatus.FAILED, limit=200)
        queued = 0
        skipped_max = 0
        skipped_no_link = 0
        actor = str(callback.from_user.id) if callback.from_user else "admin"
        for g in groups:
            if not g.invite_link:
                skipped_no_link += 1
                continue
            attempts = await attempt_repo.count_for_group(g.group_id)
            if attempts >= 3:
                skipped_max += 1
                continue
            # Reset status FAILED → PENDING so group shows correctly during retry
            g.status = GroupStatus.PENDING
            await jq.enqueue(
                group_id=g.group_id,
                link=g.invite_link,
                title=g.title,
                attempt=attempts + 1,
            )
            queued += 1
        if queued:
            await log_repo.add(
                action="retry_all_failed",
                result="success",
                actor=actor,
                target="all_failed",
                details=f"queued={queued} skipped_max={skipped_max} skipped_no_link={skipped_no_link}",
            )
            await session.commit()

    parts = [f"✅ <b>{queued} گروه</b> برای تلاش مجدد در صف قرار گرفتند."]
    if skipped_max:
        parts.append(f"⏭ {skipped_max} گروه (حداکثر تلاش رسیده) نادیده گرفته شد.")
    if skipped_no_link:
        parts.append(f"⚠️ {skipped_no_link} گروه (بدون لینک) نادیده گرفته شد.")

    await callback.message.answer(  # type: ignore[union-attr]
        "\n".join(parts),
        parse_mode="HTML",
        reply_markup=_back_btn(),
    )


# ── Admin sends link directly to the bot ──────────────────────────────────────

def _placeholder_group_id(link: str) -> int:
    """Generate a stable negative placeholder ID for private invite links before joining."""
    h = int(hashlib.md5(link.encode()).hexdigest()[:10], 16) % (10 ** 9)
    return -h  # Negative to distinguish from real Telegram IDs


@router.message(StateFilter(default_state), F.text)
async def handle_admin_link(message: Message) -> None:
    """Process Telegram group links sent directly by admin in private chat."""
    text = message.text or ""
    links = LinkValidator.extract_links(text)

    if not links:
        return  # Plain text with no links — ignore

    from app.services import TelegramUserService, JoinQueueService

    tg = TelegramUserService.get_instance()
    results: list[str] = []

    for raw_link in dict.fromkeys(links):  # deduplicate, preserve order
        normalized = LinkValidator.normalize(raw_link)
        if not normalized:
            results.append(f"❌ لینک نامعتبر: <code>{raw_link}</code>")
            continue

        if not tg.is_running():
            results.append(
                f"⚠️ <b>User Client متصل نیست.</b>\n"
                f"لینک <code>{normalized}</code> قابل پردازش نیست.\n"
                f"ابتدا از دکمه «▶️ شروع سیستم» در منو استفاده کنید."
            )
            continue

        try:
            entity = await tg.resolve_entity(normalized)
            if entity is None:
                results.append(f"❌ لینک قابل حل نیست: <code>{normalized}</code>")
                continue

            is_group = await tg.is_group(entity)
            if not is_group:
                results.append(f"⚠️ این یک کانال است نه گروه: <code>{normalized}</code>")
                continue

            group_id, title, username, members_count = await tg.get_entity_info(entity)

            # For private invite links not yet joined, group_id is None —
            # use a stable placeholder so we can store the record and track it.
            if group_id is None:
                group_id = _placeholder_group_id(normalized)

            async with AsyncSessionLocal() as session:
                group_repo = GroupRepository(session)
                existing = await group_repo.get_by_group_id(group_id)
                if existing:
                    results.append(
                        f"ℹ️ قبلاً ثبت شده: <b>{_esc(str(title or group_id))}</b> — وضعیت: {existing.status.value}"
                    )
                    continue

                existing_by_link = await group_repo.get_by_invite_link(normalized)
                if existing_by_link:
                    results.append(
                        f"ℹ️ قبلاً با این لینک ثبت شده: "
                        f"<b>{_esc(str(existing_by_link.title or existing_by_link.group_id))}</b>"
                        f" — وضعیت: {existing_by_link.status.value}"
                    )
                    continue

                await group_repo.upsert(
                    group_id=group_id,
                    title=title,
                    username=username.lower() if username else None,
                    invite_link=normalized,
                    members_count=members_count,
                    status=GroupStatus.PENDING,
                )
                await session.commit()

            jq = JoinQueueService.get_instance()
            await jq.enqueue(group_id=group_id, link=normalized, title=title)
            results.append(
                f"✅ در صف عضویت: <b>{_esc(str(title or group_id))}</b>"
                + (f" ({members_count:,} عضو)" if members_count else "")
            )
            logger.info(
                "Admin manually queued group %d (%r) via direct link",
                group_id, title,
            )

        except Exception as exc:
            results.append(f"❌ خطا برای <code>{normalized}</code>: {exc}")

    if results:
        await message.answer(
            "🔗 <b>نتیجه پردازش لینک:</b>\n\n" + "\n\n".join(results),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 منوی اصلی", callback_data="main_menu")]
            ]),
        )
