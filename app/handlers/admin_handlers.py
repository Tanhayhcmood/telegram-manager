from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from datetime import datetime, timezone
from html import escape as _esc
from app.config import settings
from app.handlers.callback_utils import safe_callback_answer
from app.services.runtime_config_service import RuntimeConfigService
from app.utils.logger import get_logger

logger = get_logger(__name__)
router = Router(name="admin")


class JoinDelayStates(StatesGroup):
    waiting_custom = State()


def _current_delay_min() -> int:
    """Live (admin-adjustable) join delay midpoint in whole minutes, for display."""
    lo, hi = RuntimeConfigService.get_instance().get_join_delay()
    return round(((lo + hi) / 2) / 60)


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
        [InlineKeyboardButton(text="🆕 ۲ گروه اخیر", callback_data="recent_groups")],
        [InlineKeyboardButton(text="🔄 همگام‌سازی گروه‌ها", callback_data="sync_dialogs"),
            InlineKeyboardButton(text="👥 همگام‌سازی مخاطبین", callback_data="sync_users")],
    ])


async def _safe_edit(callback: CallbackQuery, text: str, **kwargs) -> None:
    """Edit message text, silently ignoring 'message is not modified' errors."""
    try:
        await callback.message.edit_text(text, **kwargs)  # type: ignore[union-attr]
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "🤖 <b>ربات مدیریت گروه‌های تلگرام</b>\n\nانتخاب کنید:",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
    )


@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    await message.answer("منوی اصلی:", reply_markup=main_menu_keyboard())


@router.message(Command("cancel_broadcast"))
async def cmd_cancel_broadcast(message: Message) -> None:
    """Cancel the currently running broadcast immediately."""
    from app.services.broadcast_queue_service import BroadcastQueueService
    bqs = BroadcastQueueService.get_instance()
    if not bqs.is_active():
        await message.answer("⚠️ هیچ broadcast فعالی در حال اجرا نیست.")
        return
    cancelled = bqs.cancel_active()
    if cancelled:
        await message.answer(
            "🛑 <b>Broadcast لغو شد.</b>\n\n"
            "گزارش نهایی با آمار تا الان برایتان ارسال می‌شود.",
            parse_mode="HTML",
        )
    else:
        await message.answer("⚠️ تسک اجرایی پیدا نشد — broadcast flag reset شد.")


@router.message(Command("broadcast_status"))
async def cmd_broadcast_status(message: Message) -> None:
    """Show progress of the currently running broadcast."""
    from app.services.broadcast_queue_service import BroadcastQueueService
    bqs = BroadcastQueueService.get_instance()
    job = bqs.get_active_job()
    if not job:
        await message.answer("⚠️ هیچ broadcast فعالی در حال اجرا نیست.")
        return
    done = job.success + job.failed
    pct = int(done / job.total * 100) if job.total else 0
    target_fa = "گروه‌ها" if job.target == "groups" else "کاربران"
    elapsed = (datetime.now(timezone.utc) - job.started_at).seconds // 60
    await message.answer(
        f"📢 <b>وضعیت broadcast</b>\n\n"
        f"مقصد: {target_fa}\n"
        f"پیشرفت: <code>{done}</code> از <code>{job.total}</code> ({pct}%)\n"
        f"✅ موفق: <code>{job.success}</code>\n"
        f"❌ ناموفق: <code>{job.failed}</code>\n"
        f"⏱ مدت: <code>{elapsed}</code> دقیقه\n\n"
        f"برای لغو: /cancel_broadcast",
        parse_mode="HTML",
    )




@router.message(Command("queue_status"))
async def cmd_queue_status(message: Message) -> None:
    """نمایش وضعیت صف عضویت گروه‌ها."""
    from app.services.join_queue_service import JoinQueueService
    from app.database.connection import AsyncSessionLocal
    from app.repositories.join_attempt_repository import JoinAttemptRepository

    jq = JoinQueueService.get_instance()
    queue_size = jq.queue_size()

    async with AsyncSessionLocal() as session:
        attempt_repo = JoinAttemptRepository(session)
        today_count = await attempt_repo.count_today()

    from datetime import datetime, timezone
    delay_min = _current_delay_min()
    eta_minutes = queue_size * delay_min
    now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    if eta_minutes == 0:
        eta_str = "صف خالی است ✅"
    elif eta_minutes >= 60:
        eta_str = f"{eta_minutes // 60} ساعت و {eta_minutes % 60} دقیقه"
    else:
        eta_str = f"{eta_minutes} دقیقه"

    status_icon = "⏳" if queue_size > 0 else "✅"

    await message.answer(
        f"📋 <b>وضعیت صف عضویت</b>\n"
        f"⏱ بروزرسانی: <code>{now_str}</code>\n\n"
        f"{status_icon} در صف (حافظه): <code>{queue_size}</code> گروه\n"
        f"✅ عضو شده امروز: <code>{today_count}</code> گروه\n"
        f"⏱ زمان تخمینی: <b>{eta_str}</b>\n"
        f"⚙️ فاصله بین عضویت‌ها: <code>{delay_min} دقیقه</code>",
        parse_mode="HTML",
    )




@router.callback_query(F.data == "queue_status")
async def cb_queue_status(callback: CallbackQuery) -> None:
    """وضعیت صف عضویت — همه اعداد از StatsService (منبع واحد)."""
    await safe_callback_answer(callback)
    from app.services.stats_service import StatsService
    from app.services.join_queue_service import JoinQueueService
    import asyncio as _asyncio
    s = await StatsService().get_stats()
    jq = JoinQueueService.get_instance()

    from datetime import datetime, timezone
    delay_min = _current_delay_min()
    queue_db = s.pending_queue_size
    queue_mem = s.join_queue_size
    eta_minutes = queue_db * delay_min
    now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    if eta_minutes == 0:
        eta_str = "صف خالی است ✅"
    elif eta_minutes >= 60:
        eta_str = f"{eta_minutes // 60} ساعت و {eta_minutes % 60} دقیقه"
    else:
        eta_str = f"{eta_minutes} دقیقه"

    status_icon = "⏳" if queue_db > 0 else "✅"

    # ── نمایش وضعیت توقف صف ──────────────────────────────────────────────────
    paused = jq.is_paused()
    if paused:
        remaining_secs = max(0, int(jq._pause_until - _asyncio.get_event_loop().time()))
        remaining_min = remaining_secs // 60
        pause_line = f"\n🔴 <b>صف متوقف است (PeerFlood)</b> — {remaining_min} دقیقه باقیمانده"
    else:
        pause_line = "\n🟢 صف در حال اجرا است"

    kb_rows = []
    if paused:
        kb_rows.append([InlineKeyboardButton(text="▶️ از سرگیری صف", callback_data="resume_queue")])
    kb_rows.append([InlineKeyboardButton(text="🔄 بروزرسانی", callback_data="queue_status")])
    kb_rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu")])
    back_kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    await _safe_edit(
        callback,
        f"📋 <b>وضعیت صف عضویت</b>\n"
        f"⏱ بروزرسانی: <code>{now_str}</code>"
        f"{pause_line}\n\n"
        f"{status_icon} در صف (دیتابیس): <code>{queue_db}</code> گروه\n"
        f"{status_icon} در صف (حافظه): <code>{queue_mem}</code> گروه\n"
        f"✅ عضو شده امروز: <code>{s.today_joins}</code> گروه\n"
        f"🟢 کل عضویت‌های موفق: <code>{s.joined_groups}</code> گروه\n"
        f"⏱ زمان تخمینی: <b>{eta_str}</b>\n"
        f"⚙️ فاصله بین عضویت‌ها: <code>{delay_min} دقیقه</code>",
        parse_mode="HTML",
        reply_markup=back_kb,
    )


@router.callback_query(F.data == "resume_queue")
async def cb_resume_queue(callback: CallbackQuery) -> None:
    """از سرگیری دستی صف عضویت متوقف‌شده."""
    await safe_callback_answer(callback)
    from app.services.join_queue_service import JoinQueueService
    jq = JoinQueueService.get_instance()
    if jq.is_paused():
        jq.resume()
        await _safe_edit(callback, "▶️ صف عضویت از سرگیری شد. عضویت گروه‌ها ادامه می‌یابد.", parse_mode="HTML")
    else:
        await _safe_edit(callback, "ℹ️ صف در حال حاضر متوقف نیست.", parse_mode="HTML")

def _recent_groups_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 بروزرسانی", callback_data="recent_groups")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu")],
    ])


@router.callback_query(F.data == "recent_groups")
async def cb_recent_groups(callback: CallbackQuery) -> None:
    """۲ گروهی که اخیراً عضو شده‌ایم، همراه با آمار دقیق و لحظه‌ای (زنده از تلگرام)."""
    await safe_callback_answer(callback)

    import asyncio

    from app.database.connection import AsyncSessionLocal
    from app.repositories.group_repository import GroupRepository
    from app.services import TelegramUserService

    async with AsyncSessionLocal() as session:
        group_repo = GroupRepository(session)
        recent = await group_repo.get_recently_joined(limit=2)

    if not recent:
        await _safe_edit(
            callback,
            "🆕 <b>۲ گروه اخیر</b>\n\nهنوز هیچ گروهی با موفقیت عضو نشده است.",
            parse_mode="HTML",
            reply_markup=_recent_groups_keyboard(),
        )
        return

    tg = TelegramUserService.get_instance()
    live_ok = tg.is_running()

    lines: list[str] = ["🆕 <b>۲ گروه اخیر که عضو شده‌ایم</b>"]
    for group in recent:
        # Escape group-supplied text (title/username) before embedding in HTML —
        # Telegram titles can legally contain <, >, & which would otherwise
        # break Telegram's HTML entity parser and make this message fail to send.
        title = _esc(group.title) if group.title else str(group.group_id)
        members_count = group.members_count
        live_tag = "⚠️ آمار ذخیره‌شده (اتصال زنده برقرار نیست)"

        if live_ok:
            try:
                # Bound the live Telethon round-trip so a slow/stalled API call
                # can't hang this callback indefinitely — falls back to the
                # last known DB value instead of blocking the admin's tap.
                async def _fetch_live() -> int | None:
                    entity = await tg.client.get_entity(group.group_id)
                    _, _, _, count = await tg.get_entity_info(entity)
                    return count

                fresh_count = await asyncio.wait_for(_fetch_live(), timeout=8)
                if fresh_count is not None:
                    members_count = fresh_count
                    live_tag = "🟢 آمار لحظه‌ای از تلگرام"
                else:
                    live_tag = "⚠️ آمار ذخیره‌شده (تلگرام تعداد اعضا را برنگرداند)"
            except asyncio.TimeoutError:
                logger.debug("Live member-count fetch timed out for group %d", group.group_id)
                live_tag = "⚠️ آمار ذخیره‌شده (دریافت زنده کند بود)"
            except Exception as exc:
                logger.debug("Live member-count fetch failed for group %d: %s", group.group_id, exc)
                live_tag = "⚠️ آمار ذخیره‌شده (خطا در دریافت زنده)"

        join_date_str = (
            group.join_date.strftime("%Y-%m-%d %H:%M UTC") if group.join_date else "نامشخص"
        )
        members_str = f"{members_count:,}" if members_count is not None else "نامشخص"
        lines.append(f"\n📌 <b>{title}</b>")
        lines.append(f"👥 اعضا: <code>{members_str}</code>")
        lines.append(f"🕓 زمان عضویت: <code>{join_date_str}</code>")
        lines.append(live_tag)
        if group.username:
            lines.append(f"🔗 @{_esc(group.username)}")

    if not live_ok:
        lines.append(
            "\n⚠️ برای دریافت آمار کاملاً لحظه‌ای، سیستم باید روشن باشد "
            "(دکمه «▶️ شروع سیستم»)."
        )

    await _safe_edit(
        callback,
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_recent_groups_keyboard(),
    )


def _join_delay_keyboard() -> InlineKeyboardMarkup:
    presets = [15, 30, 45, 60, 90, 120]
    rows = []
    for i in range(0, len(presets), 3):
        rows.append([
            InlineKeyboardButton(text=f"{m} دقیقه", callback_data=f"jd_preset:{m}")
            for m in presets[i:i + 3]
        ])
    rows.append([InlineKeyboardButton(text="✏️ مقدار دلخواه", callback_data="jd_custom")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _join_delay_text() -> str:
    lo, hi = RuntimeConfigService.get_instance().get_join_delay()
    lo_min, hi_min = lo / 60, hi / 60
    return (
        "⚙️ <b>تنظیم فاصله عضویت در گروه‌ها</b>\n\n"
        f"فاصله فعلی: <b>{lo_min:.0f} تا {hi_min:.0f} دقیقه</b> (تصادفی بین این دو، برای جلوگیری از شناسایی توسط تلگرام)\n\n"
        "یک مقدار میانگین از گزینه‌های زیر انتخاب کنید (بازه ۲۵٪± حول آن به‌صورت خودکار تنظیم می‌شود)، "
        "یا مقدار دلخواه خود را به دقیقه وارد کنید.\n\n"
        "⚡️ تغییر بلافاصله و بدون نیاز به ری‌استارت اعمال می‌شود."
    )


@router.callback_query(F.data == "join_delay_menu")
async def cb_join_delay_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_callback_answer(callback)
    # Clear any leftover "waiting_custom" FSM state from a prior visit — e.g.
    # if the admin tapped "custom" then navigated back here via the button
    # instead of /cancel. Without this, the next text message they send would
    # be misinterpreted as a delay value.
    await state.clear()
    await _safe_edit(
        callback,
        _join_delay_text(),
        parse_mode="HTML",
        reply_markup=_join_delay_keyboard(),
    )


async def _apply_join_delay_minutes(average_minutes: float) -> tuple[int, int]:
    """Apply a new average delay (in minutes) with ±25% jitter range, return (min_s, max_s)."""
    avg_seconds = average_minutes * 60
    delay_min = max(1, round(avg_seconds * 0.75))
    delay_max = max(delay_min + 1, round(avg_seconds * 1.25))
    await RuntimeConfigService.get_instance().set_join_delay(delay_min, delay_max)
    return delay_min, delay_max


@router.callback_query(F.data.startswith("jd_preset:"))
async def cb_join_delay_preset(callback: CallbackQuery) -> None:
    await safe_callback_answer(callback)
    minutes = int(callback.data.split(":")[1])  # type: ignore[union-attr]
    try:
        await _apply_join_delay_minutes(minutes)
        await safe_callback_answer(callback, f"✅ فاصله عضویت روی {minutes} دقیقه تنظیم شد.", show_alert=True)
    except ValueError as exc:
        await safe_callback_answer(callback, f"❌ {exc}", show_alert=True)
        return
    actor = callback.from_user.id if callback.from_user else "?"
    logger.info("Join delay changed by admin %s → %d min average", actor, minutes)
    await _safe_edit(
        callback,
        _join_delay_text(),
        parse_mode="HTML",
        reply_markup=_join_delay_keyboard(),
    )


@router.callback_query(F.data == "jd_custom")
async def cb_join_delay_custom(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_callback_answer(callback)
    await state.set_state(JoinDelayStates.waiting_custom)
    await _safe_edit(
        callback,
        "✏️ <b>مقدار دلخواه</b>\n\n"
        "عدد فاصله عضویت را به دقیقه ارسال کنید (مثلاً <code>50</code>).\n"
        "برای لغو /cancel",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 بازگشت", callback_data="join_delay_menu")],
        ]),
    )


@router.message(JoinDelayStates.waiting_custom, F.text == "/cancel")
async def cancel_join_delay_custom(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ لغو شد.", reply_markup=main_menu_keyboard())


@router.message(JoinDelayStates.waiting_custom)
async def receive_join_delay_custom(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    try:
        minutes = float(text)
        if minutes <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ لطفاً یک عدد معتبر و بزرگ‌تر از صفر به دقیقه ارسال کنید.")
        return

    await state.clear()
    try:
        delay_min, delay_max = await _apply_join_delay_minutes(minutes)
    except ValueError as exc:
        await message.answer(f"❌ {exc}")
        return

    actor = message.from_user.id if message.from_user else "?"
    logger.info("Join delay changed by admin %s → %.1f min average (custom)", actor, minutes)
    await message.answer(
        f"✅ فاصله عضویت به‌صورت لحظه‌ای تغییر کرد.\n"
        f"میانگین: <b>{minutes:.0f} دقیقه</b> (بازه: {delay_min // 60}–{delay_max // 60} دقیقه)",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
    )


@router.callback_query(F.data == "system_health")
async def cb_system_health(callback: CallbackQuery) -> None:
    await safe_callback_answer(callback)
    from app.services import TelegramUserService, HealthService, JoinQueueService
    from app.services.broadcast_queue_service import BroadcastQueueService
    from app.services.stats_service import StatsService

    tg = TelegramUserService.get_instance()
    health = HealthService.get_instance()
    jq = JoinQueueService.get_instance()
    bqs = BroadcastQueueService.get_instance()
    s = await StatsService().get_stats()

    client_status = "🟢 متصل" if tg.is_running() else "🔴 قطع"
    health_status = "✅ سالم" if health.is_healthy() else "⚠️ مشکل دارد"
    last_ok = health.last_ok_at()
    last_ok_str = last_ok.strftime("%H:%M:%S UTC") if last_ok else "—"

    # Broadcast status
    bc_job = bqs.get_active_job()
    if bc_job:
        done = bc_job.success + bc_job.failed
        pct = int(done / bc_job.total * 100) if bc_job.total else 0
        bc_status = f"📢 در حال ارسال ({done}/{bc_job.total} — {pct}%)"
    else:
        bc_status = "💤 بیکار"

    buttons = [
        [InlineKeyboardButton(text="🔄 بروزرسانی", callback_data="system_health")],
        [InlineKeyboardButton(text="🧹 پاکسازی آنی گروه‌ها", callback_data="instant_cleanup")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu")],
    ]
    if bc_job:
        buttons.insert(1, [
            InlineKeyboardButton(text="🛑 لغو broadcast", callback_data="cancel_broadcast_cb")
        ])

    from datetime import datetime, timezone
    now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    delay_min = _current_delay_min()
    await _safe_edit(
        callback,
        "❤️ <b>وضعیت سیستم</b>\n"
        f"⏱ بروزرسانی: <code>{now_str}</code>\n\n"
        f"📡 User Client: {client_status}\n"
        f"🏥 Health Monitor: {health_status}\n"
        f"⏱ آخرین ping OK: <code>{last_ok_str}</code>\n"
        f"📋 صف (دیتابیس): <code>{s.pending_queue_size}</code> | صف (حافظه): <code>{jq.queue_size()}</code>\n"
        f"✅ عضو شده امروز: <code>{s.today_joins}</code> | 🟢 کل: <code>{s.joined_groups}</code>\n"
        f"🔒 در انتظار خروج (بن ارسال): <code>{s.write_restricted_groups}</code>\n"
        f"⚙️ فاصله عضویت: <code>{delay_min} دقیقه</code>\n"
        f"📢 Broadcast: {bc_status}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data == "cancel_broadcast_cb")
async def cb_cancel_broadcast(callback: CallbackQuery) -> None:
    await safe_callback_answer(callback)
    from app.services.broadcast_queue_service import BroadcastQueueService
    bqs = BroadcastQueueService.get_instance()
    cancelled = bqs.cancel_active()
    await safe_callback_answer(
        callback,
        "🛑 Broadcast لغو شد." if cancelled else "⚠️ تسک پیدا نشد.",
        show_alert=True,
    )
    await cb_system_health(callback)


@router.callback_query(F.data == "system_start")
async def cb_system_start(callback: CallbackQuery) -> None:
    await safe_callback_answer(callback)
    from app.services import TelegramUserService, JoinQueueService, HealthService
    tg = TelegramUserService.get_instance()
    if tg.is_running():
        await safe_callback_answer(callback, "سیستم در حال اجرا است.", show_alert=True)
        return
    try:
        await tg.start()
        await JoinQueueService.get_instance().start()
        await HealthService.get_instance().start()
        actor = callback.from_user.id if callback.from_user else "?"
        await safe_callback_answer(callback, "✅ سیستم شروع شد.", show_alert=True)
        logger.info("System started by admin %s", actor)
    except Exception as exc:
        await safe_callback_answer(callback, f"❌ خطا: {exc}", show_alert=True)


@router.callback_query(F.data == "system_stop")
async def cb_system_stop(callback: CallbackQuery) -> None:
    await safe_callback_answer(callback)
    from app.services import TelegramUserService, JoinQueueService, HealthService
    tg = TelegramUserService.get_instance()
    if not tg.is_running():
        await safe_callback_answer(callback, "سیستم متوقف است.", show_alert=True)
        return
    await HealthService.get_instance().stop()
    await JoinQueueService.get_instance().stop()
    await tg.stop()
    actor = callback.from_user.id if callback.from_user else "?"
    await safe_callback_answer(callback, "⏹ سیستم متوقف شد.", show_alert=True)
    logger.info("System stopped by admin %s", actor)


@router.callback_query(F.data == "error_logs")
async def cb_error_logs(callback: CallbackQuery) -> None:
    """Show recent error entries from the logs table."""
    await safe_callback_answer(callback)
    from app.database.connection import AsyncSessionLocal
    from app.repositories import LogRepository

    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 بروزرسانی", callback_data="error_logs")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu")],
    ])

    async with AsyncSessionLocal() as session:
        repo = LogRepository(session)
        logs = await repo.get_errors(limit=15)

    if not logs:
        await _safe_edit(
            callback,
            "✅ هیچ خطایی در لاگ‌ها ثبت نشده.",
            reply_markup=back_kb,
        )
        return

    lines = [f"🚨 <b>آخرین خطاها ({len(logs)}):</b>\n"]
    for log in logs:
        ts = log.timestamp.strftime("%m/%d %H:%M") if log.timestamp else "?"
        # Escape DB-sourced text before embedding in HTML — error messages can
        # legally contain <, >, & (e.g. from exception text or raw HTML in a
        # scraped title), which would otherwise break Telegram's HTML parser
        # and make this whole message fail to send.
        action = _esc(log.action or "")
        detail = _esc((log.error_message or log.details or "")[:60])
        lines.append(
            f"⏱ <code>{ts}</code>  <b>{action}</b>"
            + (f"\n<i>{detail}</i>" if detail else "")
        )

    await _safe_edit(
        callback,
        "\n\n".join(lines),
        parse_mode="HTML",
        reply_markup=back_kb,
    )


def _back_btn() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu")]
    ])


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_callback_answer(callback)
    # Clear any leftover FSM state (e.g. mid-flow "waiting for custom join
    # delay") so returning to the main menu never leaves a stale input trap.
    await state.clear()
    try:
        await callback.message.edit_text(  # type: ignore[union-attr]
            "🤖 <b>ربات مدیریت گروه‌های تلگرام</b>\n\nانتخاب کنید:",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(),
        )
    except Exception:
        pass


@router.callback_query(F.data == "instant_cleanup")
async def cb_instant_cleanup(callback: CallbackQuery) -> None:
    """پاکسازی آنی: گروه‌هایی که اکانت دیگر عضو آن‌ها نیست (بن/اخراج/ترک) را
    فوراً از وضعیت «عضو» خارج می‌کند، بدون نیاز به منتظر ماندن برای
    همگام‌سازی خودکار هر ۳ ساعت یکبار."""
    await safe_callback_answer(callback, "در حال پاکسازی...")
    try:
        await callback.message.edit_text(  # type: ignore[union-attr]
            "🧹 <b>در حال پاکسازی آنی گروه‌ها...</b>\n\nلطفاً چند ثانیه صبر کنید.",
            parse_mode="HTML",
        )
    except Exception:
        pass
    try:
        from app.services.telegram_service import TelegramUserService
        tg = TelegramUserService.get_instance()
        if not tg.is_running():
            text = "⚠️ اکانت شخصی متصل نیست — ابتدا سیستم را روشن کنید."
        else:
            new_count, total = await tg.sync_dialogs_to_db()
            text = (
                f"✅ <b>پاکسازی آنی کامل شد</b>\n\n"
                f"📦 گروه‌های زنده فعلی: <code>{total}</code>\n"
                f"🆕 جدید ثبت‌شده: <code>{new_count}</code>\n"
                f"🧹 گروه‌هایی که دیگر عضوشان نبودیم، به وضعیت «ترک‌شده» منتقل شدند.\n\n"
                f"ℹ️ گروه‌هایی که ارسال پیام در آن‌ها بسته/بن شده باشد، به‌صورت "
                f"خودکار حین ارسال همگانی شناسایی و از آن‌ها خارج می‌شویم — "
                f"نیازی به عملیات جدا نیست."
            )
    except Exception as exc:
        text = f"❌ <b>خطا در پاکسازی:</b>\n<code>{str(exc)[:300]}</code>"

    buttons = [
        [InlineKeyboardButton(text="🧹 پاکسازی مجدد", callback_data="instant_cleanup")],
        [InlineKeyboardButton(text="❤️ وضعیت سیستم", callback_data="system_health")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu")],
    ]
    try:
        await callback.message.edit_text(  # type: ignore[union-attr]
            text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
    except Exception:
        pass


@router.callback_query(F.data == "sync_dialogs")
async def cb_sync_dialogs(callback: CallbackQuery) -> None:
    """همگام‌سازی گروه‌های تلگرام با دیتابیس."""
    await safe_callback_answer(callback)
    try:
        await callback.message.edit_text(  # type: ignore[union-attr]
            "⏳ <b>در حال همگام‌سازی گروه‌ها...</b>\n\nلطفاً چند ثانیه صبر کنید.",
            parse_mode="HTML",
            reply_markup=_back_btn(),
        )
    except Exception:
        pass
    try:
        from app.services.telegram_service import TelegramUserService
        tg = TelegramUserService.get_instance()
        new_count, total = await tg.sync_dialogs_to_db()
        text = (
            f"✅ <b>همگام‌سازی گروه‌ها کامل شد</b>\n\n"
            f"📦 کل گروه‌ها: <code>{total}</code>\n"
            f"🆕 جدید: <code>{new_count}</code>\n"
            f"♻️ موجود: <code>{total - new_count}</code>"
        )
    except Exception as exc:
        text = f"❌ <b>خطا:</b>\n<code>{str(exc)[:300]}</code>"
    try:
        await callback.message.edit_text(  # type: ignore[union-attr]
            text, parse_mode="HTML", reply_markup=_back_btn(),
        )
    except Exception:
        pass


@router.callback_query(F.data == "sync_users")
async def cb_sync_users(callback: CallbackQuery) -> None:
    """همگام‌سازی PVهای شخصی اکانت با دیتابیس."""
    await safe_callback_answer(callback)
    try:
        await callback.message.edit_text(  # type: ignore[union-attr]
            "⏳ <b>در حال همگام‌سازی مخاطبین...</b>\n\nلطفاً چند ثانیه صبر کنید.",
            parse_mode="HTML",
            reply_markup=_back_btn(),
        )
    except Exception:
        pass
    try:
        from app.services.telegram_service import TelegramUserService
        tg = TelegramUserService.get_instance()
        new_count, total = await tg.sync_user_dialogs_to_db()
        text = (
            f"✅ <b>همگام‌سازی مخاطبین کامل شد</b>\n\n"
            f"📦 کل PVهای اکانت: <code>{total}</code>\n"
            f"🆕 جدید اضافه شده: <code>{new_count}</code>\n"
            f"♻️ موجود در دیتابیس: <code>{total - new_count}</code>"
        )
    except Exception as exc:
        text = f"❌ <b>خطا:</b>\n<code>{str(exc)[:300]}</code>"
    try:
        await callback.message.edit_text(  # type: ignore[union-attr]
            text, parse_mode="HTML", reply_markup=_back_btn(),
        )
    except Exception:
        pass