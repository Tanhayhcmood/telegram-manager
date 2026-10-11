from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from app.database.connection import AsyncSessionLocal
from app.repositories import GroupRepository
from app.repositories.join_attempt_repository import JoinAttemptRepository
from app.models.group import GroupStatus
from app.utils.logger import get_logger

logger = get_logger(__name__)
router = Router(name="groups")

PAGE_SIZE = 15


def _back_btn() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu")]
    ])


def _status_emoji(status: GroupStatus) -> str:
    return {
        GroupStatus.PENDING: "⏳", GroupStatus.APPROVED: "✅",
        GroupStatus.REJECTED: "❌", GroupStatus.JOINED: "🟢",
        GroupStatus.FAILED: "🔴",
    }.get(status, "❓")


def _list_keyboard(page: int, total: int, prefix: str) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ قبلی", callback_data=f"{prefix}:{page - 1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="بعدی ▶️", callback_data=f"{prefix}:{page + 1}"))
    if nav:
        buttons.append(nav)
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
    async with AsyncSessionLocal() as session:
        repo = GroupRepository(session)
        total = await repo.count()
        groups = await repo.get_latest(limit=PAGE_SIZE, offset=page * PAGE_SIZE)

    if not groups:
        await callback.message.edit_text("📋 هیچ گروهی ثبت نشده.", reply_markup=_back_btn())  # type: ignore[union-attr]
        return

    lines = [f"📋 <b>گروه‌ها</b> (صفحه {page + 1} از {max(1, -(-total // PAGE_SIZE))}):\n"]
    for g in groups:
        emoji = _status_emoji(g.status)
        title = (g.title or "بدون عنوان")[:35]
        lines.append(f"{emoji} <code>{g.group_id}</code> — {title}")

    await callback.message.edit_text(  # type: ignore[union-attr]
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_list_keyboard(page, total, "groups_page"),
    )


# ── Pending groups with pagination ────────────────────────────────────────────

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
        all_pending = await repo.get_by_status(GroupStatus.PENDING, limit=500)

    total = len(all_pending)
    groups = all_pending[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    if not groups:
        await callback.message.edit_text("✅ هیچ گروهی در انتظار بررسی نیست.", reply_markup=_back_btn())  # type: ignore[union-attr]
        return

    lines = [f"⏳ <b>در انتظار بررسی</b> ({total} گروه — صفحه {page + 1}):\n"]
    action_btns: list[list[InlineKeyboardButton]] = []

    for g in groups:
        title = (g.title or str(g.group_id))[:25]
        lines.append(f"• <code>{g.group_id}</code> — {title}")
        action_btns.append([
            InlineKeyboardButton(text=f"✅ {title}", callback_data=f"approve:{g.group_id}"),
            InlineKeyboardButton(text="❌ رد", callback_data=f"reject:{g.group_id}"),
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ قبلی", callback_data=f"pending_page:{page - 1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="بعدی ▶️", callback_data=f"pending_page:{page + 1}"))
    if nav:
        action_btns.append(nav)
    action_btns.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu")])

    await callback.message.edit_text(  # type: ignore[union-attr]
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=action_btns),
    )


@router.callback_query(F.data.startswith("approve:"))
async def cb_approve(callback: CallbackQuery) -> None:
    group_id = int(callback.data.split(":")[1])  # type: ignore[union-attr]
    actor = str(callback.from_user.id) if callback.from_user else "admin"
    async with AsyncSessionLocal() as session:
        repo = GroupRepository(session)
        from app.repositories import LogRepository
        log_repo = LogRepository(session)
        group = await repo.get_by_group_id(group_id)
        if group:
            group.status = GroupStatus.APPROVED
            await log_repo.add(action="group_approved", result="success", actor=actor, target=str(group_id))
            await session.commit()
            await callback.answer(f"✅ گروه {group_id} تایید شد.")
        else:
            await callback.answer("گروه یافت نشد.", show_alert=True)


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


# ── Failed groups + manual retry ─────────────────────────────────────────────

@router.callback_query(F.data == "groups_failed")
async def cb_groups_failed(callback: CallbackQuery) -> None:
    await callback.answer()
    async with AsyncSessionLocal() as session:
        repo = GroupRepository(session)
        groups = await repo.get_by_status(GroupStatus.FAILED, limit=20)

    if not groups:
        await callback.message.edit_text("✅ هیچ گروه ناموفقی وجود ندارد.", reply_markup=_back_btn())  # type: ignore[union-attr]
        return

    lines = [f"🔴 <b>گروه‌های ناموفق ({len(groups)}):</b>\n"]
    btns: list[list[InlineKeyboardButton]] = []
    for g in groups:
        title = (g.title or str(g.group_id))[:25]
        lines.append(f"• <code>{g.group_id}</code> — {title}")
        btns.append([
            InlineKeyboardButton(text=f"🔄 تلاش مجدد: {title}", callback_data=f"retry_join:{g.group_id}")
        ])

    btns.append([InlineKeyboardButton(text="🔄 تلاش مجدد همه", callback_data="retry_all_failed")])
    btns.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu")])

    await callback.message.edit_text(  # type: ignore[union-attr]
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=btns),
    )


@router.callback_query(F.data.startswith("retry_join:"))
async def cb_retry_join(callback: CallbackQuery) -> None:
    group_id = int(callback.data.split(":")[1])  # type: ignore[union-attr]
    async with AsyncSessionLocal() as session:
        repo = GroupRepository(session)
        attempt_repo = JoinAttemptRepository(session)
        group = await repo.get_by_group_id(group_id)
        if not group or not group.invite_link:
            await callback.answer("گروه یا لینک یافت نشد.", show_alert=True)
            return
        attempts = await attempt_repo.count_for_group(group_id)

    if attempts >= 3:
        await callback.answer("⚠️ حداکثر تعداد تلاش (۳ بار) رسیده.", show_alert=True)
        return

    from app.services.join_queue_service import JoinQueueService
    jq = JoinQueueService.get_instance()
    await jq.enqueue(group_id=group.group_id, link=group.invite_link, title=group.title, attempt=attempts + 1)
    await callback.answer(f"🔄 گروه {group_id} در صف تلاش مجدد قرار گرفت.", show_alert=True)
    logger.info("Manual retry queued for group_id=%d by admin %s", group_id, callback.from_user.id if callback.from_user else "?")


@router.callback_query(F.data == "retry_all_failed")
async def cb_retry_all_failed(callback: CallbackQuery) -> None:
    await callback.answer("⏳ در حال افزودن به صف...")
    async with AsyncSessionLocal() as session:
        repo = GroupRepository(session)
        attempt_repo = JoinAttemptRepository(session)
        groups = await repo.get_by_status(GroupStatus.FAILED, limit=100)
        queued = 0
        for g in groups:
            if g.invite_link:
                attempts = await attempt_repo.count_for_group(g.group_id)
                if attempts < 3:
                    from app.services.join_queue_service import JoinQueueService
                    await JoinQueueService.get_instance().enqueue(
                        group_id=g.group_id, link=g.invite_link, title=g.title, attempt=attempts + 1
                    )
                    queued += 1

    await callback.message.answer(  # type: ignore[union-attr]
        f"✅ <b>{queued} گروه</b> برای تلاش مجدد در صف قرار گرفتند.",
        parse_mode="HTML",
        reply_markup=_back_btn(),
    )
