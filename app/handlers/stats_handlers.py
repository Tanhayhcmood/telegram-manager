from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from datetime import datetime, timezone
from html import escape as _esc

from app.services.stats_service import StatsService
from app.utils.logger import get_logger

logger = get_logger(__name__)
router = Router(name="stats")
_stats_service = StatsService()


def _back_btn() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 بروزرسانی", callback_data="stats")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu")],
    ])


@router.callback_query(F.data == "stats")
async def cb_stats(callback: CallbackQuery) -> None:
    await callback.answer()
    s = await _stats_service.get_stats()

    now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    last_act = s.last_activity.strftime("%Y-%m-%d %H:%M UTC") if s.last_activity else "—"
    health_icon = "✅" if s.client_healthy else "⚠️"
    live_refresh = s.live_refreshed_at.strftime("%H:%M:%S UTC")
    live_line = (
        f"🟢 snapshot زنده Telegram: <code>{live_refresh}</code> "
        f"({s.live_group_count} گروه)"
        if s.live_group_count is not None
        else f"⚠️ snapshot زنده در دسترس نیست: <code>{s.live_refresh_error or 'نامشخص'}</code>"
    )

    queue_in_memory = s.join_queue_size
    queue_in_db = s.pending_queue_size
    consistency_line = (
        f"  🔗 هماهنگی Telegram/DB: ✅ برابر ({s.live_group_count})"
        if s.live_group_count is not None and s.live_group_count == s.joined_groups
        else f"  🔗 هماهنگی Telegram/DB: ⚠️ اختلاف ({s.live_group_count} در Telegram / {s.joined_groups} در DB)"
        if s.live_group_count is not None
        else "  🔗 هماهنگی Telegram/DB: ⚠️ snapshot زنده در دسترس نیست"
    )

    text = (
        "📊 <b>آمار لحظه‌ای سیستم</b>\n"
        f"⏱ بروزرسانی: <code>{now_str}</code>\n\n"
        f"{live_line}\n\n"
        "👥 <b>گروه‌ها</b>\n"
        f"  کل ثبت‌شده: <code>{s.total_groups}</code>\n"
        f"  عضو فعلی در Telegram: <code>{s.live_group_count if s.live_group_count is not None else '—'}</code>\n"
        f"  🟢 عضو شده: <code>{s.joined_groups}</code>\n"
        f"{consistency_line}\n"
        f"  ✅ امروز: <code>{s.today_joins}</code> گروه\n"
        f"  ⏳ در صف (دیتابیس): <code>{queue_in_db}</code> | (حافظه): <code>{queue_in_memory}</code>\n"
        f"  🔴 ناموفق: <code>{s.failed_groups}</code>\n\n"
        "🔗 <b>لینک‌ها</b>\n"
        f"  کل: <code>{s.total_links}</code> | در انتظار: <code>{s.pending_links}</code>\n\n"
        "👤 <b>کاربران</b>\n"
        f"  ✅ فعال: <code>{s.total_contacted_users}</code>\n"
        f"  📦 کل: <code>{s.total_contacted_users_all}</code>\n\n"
        "⚙️ <b>سیستم</b>\n"
        f"  {health_icon} User Client: {'سالم' if s.client_healthy else 'مشکل دارد'}\n"
        f"  📝 لاگ‌ها: <code>{s.total_logs}</code>\n\n"
        f"⏱ آخرین فعالیت: <code>{last_act}</code>\n"
        f"📌 آخرین گروه: <code>{_esc(s.last_group_title) if s.last_group_title else '—'}</code>"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_back_btn())  # type: ignore[union-attr]
