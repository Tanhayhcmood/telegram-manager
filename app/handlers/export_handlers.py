"""Export groups and contacted users as CSV files, sent directly in chat."""
import csv
import io
from aiogram import Router, F
from aiogram.types import CallbackQuery, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton

from app.database.connection import AsyncSessionLocal
from app.handlers.callback_utils import safe_callback_answer
from app.repositories import GroupRepository, ContactedUserRepository
from app.utils.logger import get_logger

logger = get_logger(__name__)
router = Router(name="export")


def _back_btn() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu")]
    ])


def _export_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 خروجی گروه‌ها (CSV)", callback_data="export_groups")],
        [InlineKeyboardButton(text="📥 خروجی کاربران (CSV)", callback_data="export_users")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu")],
    ])


@router.callback_query(F.data == "export_menu")
async def cb_export_menu(callback: CallbackQuery) -> None:
    await safe_callback_answer(callback)
    await callback.message.edit_text(  # type: ignore[union-attr]
        "📥 *خروجی داده*\n\nفرمت انتخاب کنید:",
        parse_mode="Markdown",
        reply_markup=_export_menu(),
    )


@router.callback_query(F.data == "export_groups")
async def cb_export_groups(callback: CallbackQuery) -> None:
    await safe_callback_answer(callback, "⏳ در حال ساخت فایل...")

    async with AsyncSessionLocal() as session:
        repo = GroupRepository(session)
        groups = await repo.get_all(limit=100000)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["group_id", "title", "username", "invite_link", "members_count", "status", "join_date", "created_at"])
    for g in groups:
        writer.writerow([
            g.group_id, g.title or "", g.username or "",
            g.invite_link or "", g.members_count or "",
            g.status.value,
            g.join_date.isoformat() if g.join_date else "",
            g.created_at.isoformat() if g.created_at else "",
        ])

    content = buf.getvalue().encode("utf-8-sig")   # utf-8-sig for Excel compatibility
    file = BufferedInputFile(content, filename="groups_export.csv")
    await callback.message.answer_document(  # type: ignore[union-attr]
        document=file,
        caption=f"📋 خروجی گروه‌ها — {len(groups)} ردیف",
    )
    logger.info("Groups CSV exported (%d rows) by admin %s", len(groups), callback.from_user.id if callback.from_user else "?")


@router.callback_query(F.data == "export_users")
async def cb_export_users(callback: CallbackQuery) -> None:
    await safe_callback_answer(callback, "⏳ در حال ساخت فایل...")

    async with AsyncSessionLocal() as session:
        repo = ContactedUserRepository(session)
        users = await repo.get_active(limit=100000)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["user_id", "username", "first_name", "last_name", "message_count", "first_seen_at", "last_seen_at", "is_blocked"])
    for u in users:
        writer.writerow([
            u.user_id, u.username or "", u.first_name or "", u.last_name or "",
            u.message_count,
            u.first_seen_at.isoformat() if u.first_seen_at else "",
            u.last_seen_at.isoformat() if u.last_seen_at else "",
            u.is_blocked,
        ])

    content = buf.getvalue().encode("utf-8-sig")
    file = BufferedInputFile(content, filename="users_export.csv")
    await callback.message.answer_document(  # type: ignore[union-attr]
        document=file,
        caption=f"👤 خروجی کاربران — {len(users)} ردیف",
    )
    logger.info("Users CSV exported (%d rows) by admin %s", len(users), callback.from_user.id if callback.from_user else "?")
