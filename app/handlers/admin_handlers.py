from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext

from app.utils.logger import get_logger

logger = get_logger(__name__)
router = Router(name="admin")


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 آمار", callback_data="stats"),
            InlineKeyboardButton(text="📋 لیست گروه‌ها", callback_data="groups_list"),
        ],
        [
            InlineKeyboardButton(text="⏳ در انتظار بررسی", callback_data="groups_pending"),
            InlineKeyboardButton(text="🔴 ناموفق‌ها", callback_data="groups_failed"),
        ],
        [
            InlineKeyboardButton(
                text="🔗 لینک‌های کشف‌شده",
                callback_data="discovered_links",
            ),
        ],
        [
            InlineKeyboardButton(text="📨 ارسال به گروه", callback_data="send_message"),
            InlineKeyboardButton(text="📢 ارسال همگانی", callback_data="broadcast"),
        ],
        [
            InlineKeyboardButton(text="📥 خروجی داده", callback_data="export_menu"),
            InlineKeyboardButton(text="🚨 گزارش خطاها", callback_data="error_logs"),
        ],
        [
            InlineKeyboardButton(text="💾 بکاپ دیتابیس", callback_data="backup_menu"),
            InlineKeyboardButton(text="❤️ وضعیت سیستم", callback_data="system_health"),
        ],
        [
            InlineKeyboardButton(text="▶️ شروع سیستم", callback_data="system_start"),
            InlineKeyboardButton(text="⏹ توقف سیستم", callback_data="system_stop"),
        ],
        [
            InlineKeyboardButton(text="📋 وضعیت صف عضویت", callback_data="queue_status"),
            InlineKeyboardButton(text="⚙️ فاصله عضویت", callback_data="join_delay_menu"),
        ],
        [
            InlineKeyboardButton(text="🆕 ۲ گروه اخیر", callback_data="recent_groups"),
        ],
        [
            InlineKeyboardButton(text="🔄 همگام‌سازی گروه‌ها", callback_data="sync_dialogs"),
            InlineKeyboardButton(text="👥 همگام‌سازی مخاطبین", callback_data="sync_users"),
        ],
        [
            InlineKeyboardButton(
                text="♻️ بازخوانی لینک‌های جاافتاده",
                callback_data="backfill_links_menu",
            ),
        ],
    ])


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    # AdminAuthMiddleware already blocks non-admins before this handler runs.
    await state.clear()
    await message.answer(
        "🤖 <b>ربات مدیریت گروه‌های تلگرام</b>\n\nانتخاب کنید:",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
    )


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext) -> None:
    # AdminAuthMiddleware already blocks non-admins before this handler runs.
    await state.clear()
    await message.answer("منوی اصلی:", reply_markup=main_menu_keyboard())


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("منوی اصلی:", reply_markup=main_menu_keyboard())  # type: ignore[union-attr]
    await callback.answer()


@router.callback_query(F.data == "system_health")
async def cb_system_health(callback: CallbackQuery) -> None:
    await callback.answer()
    from app.services import TelegramUserService, HealthService, JoinQueueService

    tg = TelegramUserService.get_instance()
    health = HealthService.get_instance()
    jq = JoinQueueService.get_instance()

    client_status = "🟢 متصل" if tg.is_running() else "🔴 قطع"
    health_status = "✅ سالم" if health.is_healthy() else "⚠️ مشکل دارد"
    last_ok = health.last_ok_at()
    last_ok_str = last_ok.strftime("%H:%M:%S UTC") if last_ok else "—"

    await callback.message.edit_text(  # type: ignore[union-attr]
        "❤️ <b>وضعیت سیستم</b>\n\n"
        f"📡 User Client: {client_status}\n"
        f"🏥 Health Monitor: {health_status}\n"
        f"⏱ آخرین بررسی OK: <code>{last_ok_str}</code>\n"
        f"📋 صف عضویت: <code>{jq.queue_size()}</code> مورد در انتظار",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 بروزرسانی", callback_data="system_health")],
            [InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu")],
        ]),
    )


@router.callback_query(F.data == "system_start")
async def cb_system_start(callback: CallbackQuery) -> None:
    from app.services import TelegramUserService, JoinQueueService, HealthService
    tg = TelegramUserService.get_instance()
    if tg.is_running():
        await callback.answer("سیستم در حال اجرا است.", show_alert=True)
        return
    try:
        await tg.start()
        await JoinQueueService.get_instance().start()
        await HealthService.get_instance().start()
        actor = callback.from_user.id if callback.from_user else "?"
        await callback.answer("✅ سیستم شروع شد.", show_alert=True)
        logger.info("System started by admin %s", actor)
    except Exception as exc:
        await callback.answer(f"❌ خطا: {exc}", show_alert=True)


@router.callback_query(F.data == "system_stop")
async def cb_system_stop(callback: CallbackQuery) -> None:
    from app.services import TelegramUserService, JoinQueueService, HealthService
    tg = TelegramUserService.get_instance()
    if not tg.is_running() and not tg.is_expected_to_run():
        await callback.answer("سیستم متوقف است.", show_alert=True)
        return
    await HealthService.get_instance().stop()
    await JoinQueueService.get_instance().stop()
    await tg.stop()
    actor = callback.from_user.id if callback.from_user else "?"
    await callback.answer("⏹ سیستم متوقف شد.", show_alert=True)
    logger.info("System stopped by admin %s", actor)
