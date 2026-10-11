import asyncio
import html
import math
from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.config import settings
from app.handlers.admin_handlers import main_menu_keyboard
from app.utils.logger import get_logger

logger = get_logger(__name__)
router = Router(name="admin-tools")

JOIN_DELAY_PRESETS = (15, 30, 45, 60, 90, 120)
MAX_CUSTOM_DELAY_MINUTES = 720
_backfill_task: asyncio.Task[None] | None = None


class JoinDelayStates(StatesGroup):
    waiting_custom = State()


def _back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu")],
    ])


async def _safe_edit(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


def _format_minutes(seconds: int) -> str:
    return f"{seconds / 60:g}"


def _queue_status_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 بروزرسانی", callback_data="queue_status")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu")],
    ])


@router.callback_query(F.data == "queue_status")
async def cb_queue_status(callback: CallbackQuery) -> None:
    await callback.answer()

    from app.services import HealthService, JoinQueueService, TelegramUserService
    from app.services.stats_service import StatsService
    from app.repositories.join_attempt_repository import JoinAttemptRepository
    from app.database.connection import AsyncSessionLocal
    from app.services.runtime_config_service import RuntimeConfigService

    stats = await StatsService().get_stats()
    async with AsyncSessionLocal() as session:
        today_attempts = await JoinAttemptRepository(session).count_today()

    tg = TelegramUserService.get_instance()
    queue_size = JoinQueueService.get_instance().queue_size()
    delay_min, delay_max = RuntimeConfigService.get_instance().get_join_delay()
    health = "✅ سالم" if HealthService.get_instance().is_healthy() else "⚠️ نیاز به بررسی"
    client = "🟢 متصل" if tg.is_running() else "🔴 قطع"
    updated_at = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    await _safe_edit(
        callback,
        "📋 <b>وضعیت صف عضویت</b>\n\n"
        f"⏱ بروزرسانی: <code>{updated_at}</code>\n"
        f"📡 اتصال تلگرام: {client}\n"
        f"🏥 سلامت سیستم: {health}\n"
        f"📥 موارد در صف حافظه: <code>{queue_size:,}</code>\n"
        f"🕓 گروه‌های در وضعیت انتظار: <code>{stats.pending_groups:,}</code>\n"
        f"📝 تلاش‌های عضویت ثبت‌شده امروز: <code>{today_attempts:,}</code> "
        f"از <code>{settings.MAX_JOINS_PER_DAY:,}</code>\n"
        f"✅ کل گروه‌های عضو‌شده: <code>{stats.joined_groups:,}</code>\n"
        "⚙️ فاصله فعلی عضویت: "
        f"<code>{_format_minutes(delay_min)} تا {_format_minutes(delay_max)} دقیقه</code>",
        reply_markup=_queue_status_keyboard(),
    )


def _recent_groups_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 بروزرسانی", callback_data="recent_groups")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu")],
    ])


@router.callback_query(F.data == "recent_groups")
async def cb_recent_groups(callback: CallbackQuery) -> None:
    await callback.answer()

    from app.database.connection import AsyncSessionLocal
    from app.repositories.group_repository import GroupRepository
    from app.services import TelegramUserService

    async with AsyncSessionLocal() as session:
        recent = await GroupRepository(session).get_recently_joined(limit=2)

    if not recent:
        await _safe_edit(
            callback,
            "🆕 <b>۲ گروه اخیر</b>\n\nهنوز گروهی با موفقیت ثبت نشده است.",
            reply_markup=_recent_groups_keyboard(),
        )
        return

    tg = TelegramUserService.get_instance()
    lines = ["🆕 <b>۲ گروه اخیر که عضو شده‌ایم</b>"]
    for group in recent:
        title = html.escape(group.title or str(group.group_id))
        members_count = group.members_count
        live_status = "⚠️ آمار ذخیره‌شده"

        if tg.is_running():
            try:
                async def fetch_live_count() -> int | None:
                    entity = await tg.client.get_entity(group.group_id)
                    count = getattr(entity, "participants_count", None)
                    return count if isinstance(count, int) else None

                fresh_count = await asyncio.wait_for(fetch_live_count(), timeout=8)
                if fresh_count is not None:
                    members_count = fresh_count
                    live_status = "🟢 آمار لحظه‌ای از تلگرام"
                else:
                    live_status = "⚠️ تلگرام تعداد اعضا را برنگرداند"
            except asyncio.TimeoutError:
                live_status = "⚠️ دریافت آمار زنده زمان‌بر بود"
            except Exception as exc:
                logger.debug("Live member count unavailable for %d: %s", group.group_id, exc)

        join_date = (
            group.join_date.strftime("%Y-%m-%d %H:%M UTC")
            if group.join_date
            else "نامشخص"
        )
        members = f"{members_count:,}" if members_count is not None else "نامشخص"
        lines.extend([
            f"\n📌 <b>{title}</b>",
            f"👥 اعضا: <code>{members}</code>",
            f"🕓 زمان عضویت: <code>{join_date}</code>",
            live_status,
        ])
        if group.username:
            lines.append(f"🔗 @{html.escape(group.username)}")

    if not tg.is_running():
        lines.append("\n⚠️ برای آمار لحظه‌ای، سیستم را از منوی اصلی روشن کنید.")

    await _safe_edit(
        callback,
        "\n".join(lines),
        reply_markup=_recent_groups_keyboard(),
    )


def _join_delay_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for index in range(0, len(JOIN_DELAY_PRESETS), 3):
        rows.append([
            InlineKeyboardButton(
                text=f"{minutes} دقیقه",
                callback_data=f"jd_preset:{minutes}",
            )
            for minutes in JOIN_DELAY_PRESETS[index:index + 3]
        ])
    rows.extend([
        [InlineKeyboardButton(text="✏️ مقدار دلخواه", callback_data="jd_custom")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _join_delay_text() -> str:
    from app.services.runtime_config_service import RuntimeConfigService

    delay_min, delay_max = RuntimeConfigService.get_instance().get_join_delay()
    return (
        "⚙️ <b>تنظیم فاصله عضویت در گروه‌ها</b>\n\n"
        f"فاصله فعلی: <b>{_format_minutes(delay_min)} تا "
        f"{_format_minutes(delay_max)} دقیقه</b>؛ زمان هر عضویت به‌صورت تصادفی "
        "در این بازه انتخاب می‌شود.\n\n"
        "یک میانگین انتخاب کنید؛ بازهٔ ۲۵٪ کمتر تا ۲۵٪ بیشتر به‌صورت خودکار "
        f"اعمال می‌شود. مقدار دلخواه باید بین ۱ تا "
        f"{MAX_CUSTOM_DELAY_MINUTES} دقیقه باشد.\n\n"
        "تغییر برای نوبت بعدی صف اعمال می‌شود و پس از راه‌اندازی مجدد هم باقی می‌ماند."
    )


@router.callback_query(F.data == "join_delay_menu")
async def cb_join_delay_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await _safe_edit(
        callback,
        _join_delay_text(),
        reply_markup=_join_delay_keyboard(),
    )


async def _apply_join_delay_minutes(average_minutes: float) -> tuple[int, int]:
    if (
        not math.isfinite(average_minutes)
        or average_minutes < 1
        or average_minutes > MAX_CUSTOM_DELAY_MINUTES
    ):
        raise ValueError(
            f"مقدار باید بین ۱ تا {MAX_CUSTOM_DELAY_MINUTES} دقیقه باشد."
        )

    average_seconds = average_minutes * 60
    delay_min = max(1, round(average_seconds * 0.75))
    delay_max = max(delay_min + 1, round(average_seconds * 1.25))

    from app.services.runtime_config_service import RuntimeConfigService

    await RuntimeConfigService.get_instance().set_join_delay(delay_min, delay_max)
    return delay_min, delay_max


@router.callback_query(F.data.startswith("jd_preset:"))
async def cb_join_delay_preset(callback: CallbackQuery) -> None:
    data = callback.data or ""
    try:
        minutes = int(data.partition(":")[2])
        if minutes not in JOIN_DELAY_PRESETS:
            raise ValueError("این مقدار در فهرست انتخاب‌ها نیست.")
        await _apply_join_delay_minutes(float(minutes))
    except (ValueError, TypeError) as exc:
        await callback.answer(f"❌ {exc}", show_alert=True)
        return
    except Exception:
        logger.exception("Failed to update join delay preset")
        await callback.answer("❌ ذخیره فاصله عضویت انجام نشد.", show_alert=True)
        return

    await callback.answer(f"✅ فاصله عضویت روی {minutes} دقیقه تنظیم شد.", show_alert=True)
    await _safe_edit(
        callback,
        _join_delay_text(),
        reply_markup=_join_delay_keyboard(),
    )


@router.callback_query(F.data == "jd_custom")
async def cb_join_delay_custom(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(JoinDelayStates.waiting_custom)
    await _safe_edit(
        callback,
        "✏️ <b>مقدار دلخواه</b>\n\n"
        "عدد فاصله را به دقیقه بفرستید؛ مثلاً <code>50</code>.\n"
        f"محدودهٔ مجاز: ۱ تا {MAX_CUSTOM_DELAY_MINUTES} دقیقه. برای لغو /cancel را بفرستید.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 بازگشت", callback_data="join_delay_menu")],
        ]),
    )


@router.message(JoinDelayStates.waiting_custom)
async def receive_join_delay_custom(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        await state.clear()
        await message.answer("تنظیم لغو شد.", reply_markup=main_menu_keyboard())
        return

    normalized = text.translate(
        str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    ).replace("٫", ".").replace(",", ".")
    try:
        minutes = float(normalized)
        if (
            not math.isfinite(minutes)
            or minutes < 1
            or minutes > MAX_CUSTOM_DELAY_MINUTES
        ):
            raise ValueError
    except ValueError:
        await message.answer(
            f"❌ عدد معتبری بین ۱ تا {MAX_CUSTOM_DELAY_MINUTES} دقیقه بفرستید."
        )
        return

    try:
        delay_min, delay_max = await _apply_join_delay_minutes(minutes)
    except ValueError as exc:
        await message.answer(f"❌ {html.escape(str(exc))}")
        return
    except Exception:
        logger.exception("Failed to save custom join delay")
        await message.answer("❌ ذخیره فاصله عضویت انجام نشد؛ دوباره تلاش کنید.")
        return

    await state.clear()
    actor = message.from_user.id if message.from_user else "?"
    logger.info("Join delay changed by admin %s to %.2f minutes average", actor, minutes)
    await message.answer(
        "✅ فاصله عضویت ذخیره شد و از نوبت بعدی اعمال می‌شود.\n"
        f"میانگین: <b>{minutes:g} دقیقه</b>؛ بازه: "
        f"<b>{_format_minutes(delay_min)} تا {_format_minutes(delay_max)} دقیقه</b>.",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
    )


async def _run_sync(callback: CallbackQuery, *, users: bool) -> None:
    label = "مخاطبین" if users else "گروه‌ها"
    await callback.answer()
    await _safe_edit(
        callback,
        f"⏳ <b>در حال همگام‌سازی {label}...</b>\n\n"
        "این عملیات فقط پایگاه داده را به‌روزرسانی می‌کند و پیامی ارسال نمی‌کند.",
        reply_markup=_back_keyboard(),
    )

    try:
        from app.services import TelegramUserService

        tg = TelegramUserService.get_instance()
        if users:
            new_count, total = await tg.sync_user_dialogs_to_db()
            result = (
                f"✅ <b>همگام‌سازی مخاطبین کامل شد</b>\n\n"
                f"📦 کل گفتگوهای خصوصی: <code>{total:,}</code>\n"
                f"🆕 مخاطب جدید ثبت‌شده: <code>{new_count:,}</code>\n"
                f"♻️ از قبل موجود: <code>{total - new_count:,}</code>\n\n"
                "این کار هیچ پیامی ارسال نکرد."
            )
        else:
            new_count, total = await tg.sync_dialogs_to_db()
            result = (
                f"✅ <b>همگام‌سازی گروه‌ها کامل شد</b>\n\n"
                f"📦 کل گروه‌های فعال: <code>{total:,}</code>\n"
                f"🆕 گروه جدید ثبت‌شده: <code>{new_count:,}</code>\n"
                f"♻️ از قبل موجود: <code>{total - new_count:,}</code>"
            )
    except Exception as exc:
        logger.exception("Telegram %s sync failed", label)
        result = f"❌ <b>همگام‌سازی انجام نشد:</b>\n<code>{html.escape(str(exc)[:300])}</code>"

    await _safe_edit(callback, result, reply_markup=_back_keyboard())


@router.callback_query(F.data == "sync_dialogs")
async def cb_sync_dialogs(callback: CallbackQuery) -> None:
    await _run_sync(callback, users=False)


@router.callback_query(F.data == "sync_users")
async def cb_sync_users(callback: CallbackQuery) -> None:
    await _run_sync(callback, users=True)


def _backfill_links_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="۲۴ ساعت", callback_data="backfill_links:1"),
            InlineKeyboardButton(text="۳ روز", callback_data="backfill_links:3"),
            InlineKeyboardButton(text="۷ روز", callback_data="backfill_links:7"),
        ],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu")],
    ])


@router.callback_query(F.data == "backfill_links_menu")
async def cb_backfill_links_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    await _safe_edit(
        callback,
        "♻️ <b>بازخوانی لینک‌های جاافتاده</b>\n\n"
        "پیام‌های گروه‌هایی که اکانت فعلی عضو آن‌هاست در بازهٔ انتخابی بررسی می‌شوند. "
        "پیام خصوصی اسکن نمی‌شود. لینک‌های جدید ثبت و اعتبارسنجی می‌شوند؛ "
        "این عملیات گروهی را join نمی‌کند و پیامی هم ارسال نمی‌کند.\n\n"
        "برای محدودکردن درخواست‌ها، هر اجرا حداکثر ۱۰۰ گروه و ۵۰۰۰ پیام را بررسی می‌کند. "
        "تکرارها دوباره ثبت نمی‌شوند.",
        reply_markup=_backfill_links_keyboard(),
    )


async def _execute_link_backfill(bot: Bot, chat_id: int, days: int) -> None:
    global _backfill_task
    try:
        from app.services.link_backfill_service import LinkBackfillService
        from app.services import TelegramUserService

        result = await LinkBackfillService(
            TelegramUserService.get_instance()
        ).run(days)
        truncated_note = (
            "\n⚠️ به سقف این اجرا رسیدیم؛ احتمال دارد همهٔ گروه‌ها یا پیام‌ها بررسی نشده باشند."
            if result.truncated
            else ""
        )
        failure_note = (
            f"\n⚠️ موارد ناموفق: <code>{result.failed_messages:,}</code>"
            if result.failed_messages
            else ""
        )
        flood_wait_note = (
            "\n⏸ تلگرام محدودیت موقت درخواست اعمال کرد؛ ادامهٔ بازخوانی متوقف شد."
            if result.rate_limited
            else ""
        )
        await bot.send_message(
            chat_id,
            "✅ <b>بازخوانی پایان یافت</b>\n\n"
            f"🗓 بازه: <code>{result.days}</code> روز اخیر\n"
            f"👥 گروه‌ها: <code>{result.groups_scanned:,}</code> از "
            f"<code>{result.groups_available:,}</code>\n"
            f"💬 پیام‌های بررسی‌شده: <code>{result.messages_scanned:,}</code>\n"
            f"🔗 لینک‌های جدید: <code>{result.new_links:,}</code>"
            f"{failure_note}{flood_wait_note}{truncated_note}",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(),
        )
    except Exception as exc:
        logger.exception("Historical link recovery failed")
        await bot.send_message(
            chat_id,
            f"❌ <b>بازخوانی انجام نشد:</b>\n<code>{html.escape(str(exc)[:300])}</code>",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(),
        )
    finally:
        _backfill_task = None


@router.callback_query(F.data.startswith("backfill_links:"))
async def cb_start_link_backfill(callback: CallbackQuery) -> None:
    global _backfill_task
    data = callback.data or ""
    try:
        days = int(data.partition(":")[2])
        if days not in (1, 3, 7):
            raise ValueError("Invalid recovery period.")
    except ValueError:
        await callback.answer("بازهٔ زمانی معتبر نیست.", show_alert=True)
        return

    if _backfill_task is not None and not _backfill_task.done():
        await callback.answer("یک بازخوانی دیگر در حال اجراست.", show_alert=True)
        return
    if callback.message is None:
        await callback.answer("پیام برای شروع عملیات در دسترس نیست.", show_alert=True)
        return

    await callback.answer()
    await _safe_edit(
        callback,
        f"⏳ بازخوانی لینک‌های {days} روز اخیر شروع شد. "
        "پس از پایان، نتیجه برایتان ارسال می‌شود.",
        reply_markup=_backfill_links_keyboard(),
    )
    _backfill_task = asyncio.create_task(
        _execute_link_backfill(callback.bot, callback.message.chat.id, days),
        name="telegram-link-history-backfill",
    )
