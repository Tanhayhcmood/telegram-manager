from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from app.handlers.callback_utils import safe_callback_answer
from app.services.backup_service import BackupService
from app.utils.logger import get_logger

logger = get_logger(__name__)
router = Router(name="backup")

_backup_service = BackupService()


def _back_btn() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu")]
    ])


def _backup_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💾 بکاپ دستی", callback_data="backup_create")],
        [InlineKeyboardButton(text="📋 لیست بکاپ‌ها", callback_data="backup_list")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu")],
    ])


@router.callback_query(F.data == "backup_menu")
async def cb_backup_menu(callback: CallbackQuery) -> None:
    await safe_callback_answer(callback)
    await callback.message.edit_text(  # type: ignore[union-attr]
        "💾 <b>مدیریت بکاپ</b>\n\nانتخاب کنید:",
        parse_mode="HTML",
        reply_markup=_backup_menu_keyboard(),
    )


@router.callback_query(F.data == "backup_create")
async def cb_backup_create(callback: CallbackQuery) -> None:
    await safe_callback_answer(callback, "⏳ در حال ساخت بکاپ...", show_alert=False)
    await callback.message.edit_text("⏳ در حال ساخت بکاپ دیتابیس...")  # type: ignore[union-attr]

    actor = str(callback.from_user.id) if callback.from_user else "admin"
    path = await _backup_service.create_backup(actor=actor)

    if path:
        await callback.message.edit_text(  # type: ignore[union-attr]
            f"✅ بکاپ با موفقیت ساخته شد:\n<code>{path}</code>",
            parse_mode="HTML",
            reply_markup=_backup_menu_keyboard(),
        )
    else:
        await callback.message.edit_text(  # type: ignore[union-attr]
            "❌ خطا در ساخت بکاپ. لاگ‌ها را بررسی کنید.",
            reply_markup=_backup_menu_keyboard(),
        )


@router.callback_query(F.data == "backup_list")
async def cb_backup_list(callback: CallbackQuery) -> None:
    await safe_callback_answer(callback)
    backups = _backup_service.list_backups()

    if not backups:
        await callback.message.edit_text(  # type: ignore[union-attr]
            "📋 هیچ بکاپی وجود ندارد.",
            reply_markup=_backup_menu_keyboard(),
        )
        return

    lines = ["📋 <b>بکاپ‌های موجود:</b>\n"]
    for b in backups[:10]:
        lines.append(f"• <code>{b['filename']}</code> — {b['size_kb']} KB\n  <i>{b['created_at']}</i>")

    await callback.message.edit_text(  # type: ignore[union-attr]
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_backup_menu_keyboard(),
    )


# NOTE: "error_logs" is handled by admin_handlers.cb_error_logs (registered
# first in the router chain, so this duplicate was dead code that could never
# run — removed to avoid confusion about which implementation is live).
