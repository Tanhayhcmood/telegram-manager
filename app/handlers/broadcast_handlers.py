from aiogram import Router, F
from aiogram.types import (
    CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton,
    MessageOriginChannel, MessageOriginChat,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from app.database.connection import AsyncSessionLocal
from app.handlers.callback_utils import safe_callback_answer
from app.repositories import GroupRepository
from app.models.group import GroupStatus
from app.services.broadcast_queue_service import BroadcastQueueService
from app.utils.logger import get_logger

logger = get_logger(__name__)
router = Router(name="broadcast")


class BroadcastStates(StatesGroup):
    waiting_target = State()
    waiting_content = State()
    confirming = State()


def _back_btn() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu")]
    ])


def _target_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 تمام گروه‌ها (عضو شده)", callback_data="bc_target:groups")],
        [InlineKeyboardButton(text="👤 تمام کاربران مخاطب", callback_data="bc_target:users")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu")],
    ])


@router.callback_query(F.data == "broadcast")
async def cb_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_callback_answer(callback)
    bqs = BroadcastQueueService.get_instance()
    if bqs.is_active():
        await callback.message.edit_text(  # type: ignore[union-attr]
            "⏳ <b>یک ارسال همگانی در جریان است.</b>\nلطفاً صبر کنید تا تمام شود.",
            parse_mode="HTML", reply_markup=_back_btn(),
        )
        return
    await state.set_state(BroadcastStates.waiting_target)
    await callback.message.edit_text(  # type: ignore[union-attr]
        "📢 <b>ارسال پیام همگانی</b>\n\nمقصد را انتخاب کنید:",
        parse_mode="HTML", reply_markup=_target_keyboard(),
    )


@router.callback_query(F.data.startswith("bc_target:"), BroadcastStates.waiting_target)
async def cb_target(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_callback_answer(callback)
    target = callback.data.split(":")[1]  # type: ignore[union-attr]
    await state.update_data(target=target)
    await state.set_state(BroadcastStates.waiting_content)
    label = "گروه‌ها" if target == "groups" else "کاربران"
    await safe_callback_answer(callback)
    await callback.message.edit_text(  # type: ignore[union-attr]
        f"📨 ارسال به <b>{label}</b>\n\nپیام خود را ارسال کنید.\n"
        "پشتیبانی: متن، عکس، ویدیو، فایل، فوروارد\n\n<i>برای لغو /cancel</i>",
        parse_mode="HTML", reply_markup=_back_btn(),
    )


@router.message(BroadcastStates.waiting_content, F.text == "/cancel")
@router.message(BroadcastStates.confirming, F.text == "/cancel")
async def cancel_bc(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ لغو شد.")


@router.message(BroadcastStates.waiting_content)
async def receive_content(message: Message, state: FSMContext) -> None:
    # --- Detect forwarded messages and capture original source ---
    # When a message is forwarded from a channel/group, we store the ORIGINAL
    # chat_id and message_id so Telethon can forward from the real source
    # (preserving the "Forwarded from …" header) rather than re-sending as text.
    is_forward: bool = False
    forward_from_chat_id: int | None = None
    forward_from_message_id: int | None = None

    if message.forward_origin is not None:
        is_forward = True
        origin = message.forward_origin
        # Channel or linked supergroup — has both chat.id and message_id
        if isinstance(origin, MessageOriginChannel):
            forward_from_chat_id = origin.chat.id
            forward_from_message_id = origin.message_id
        # Chat (group/supergroup) forwarded as a message — may have sender_chat
        elif isinstance(origin, MessageOriginChat):
            forward_from_chat_id = origin.sender_chat.id
            # MessageOriginChat does NOT expose the original message_id
            # so we fall back to downloading media or sending text.

    # --- Extract media file_id from direct sends (non-forward or forward with media) ---
    media_file_id: str | None = None
    media_type: str | None = None
    if message.photo:
        media_file_id = message.photo[-1].file_id
        media_type = "photo"
    elif message.video:
        media_file_id = message.video.file_id
        media_type = "video"
    elif message.document:
        media_file_id = message.document.file_id
        media_type = "document"
    elif message.audio:
        media_file_id = message.audio.file_id
        media_type = "audio"
    elif message.voice:
        media_file_id = message.voice.file_id
        media_type = "voice"
    elif message.sticker:
        media_file_id = message.sticker.file_id
        media_type = "sticker"
    elif message.video_note:
        media_file_id = message.video_note.file_id
        media_type = "video_note"
    elif message.animation:
        media_file_id = message.animation.file_id
        media_type = "animation"

    await state.update_data(
        from_chat_id=message.chat.id,
        message_id=message.message_id,
        message_text=message.text or message.caption or "",
        media_file_id=media_file_id,
        media_type=media_type,
        is_forward=is_forward,
        forward_from_chat_id=forward_from_chat_id,
        forward_from_message_id=forward_from_message_id,
    )
    data = await state.get_data()
    target = data.get("target", "groups")
    label = "گروه‌ها" if target == "groups" else "کاربران"

    if target == "groups":
        # Live count, not a DB snapshot: mirrors exactly what _send_to_groups
        # will target (live Telethon dialogs, plus any DB-only joined groups
        # not currently visible as dialogs). Using count_by_status(JOINED)
        # here showed stale numbers whenever the DB still held groups the
        # account had already left/been removed from since the last sync.
        from app.services.telegram_service import TelegramUserService
        tg = TelegramUserService.get_instance()
        dialog_groups = await tg.get_all_groups_from_dialogs()
        dialog_ids = {g["group_id"] for g in dialog_groups}
        async with AsyncSessionLocal() as session:
            db_groups = await GroupRepository(session).get_joined()
        db_only = sum(1 for g in db_groups if g.group_id not in dialog_ids)
        count = len(dialog_groups) + db_only
    else:
        # Use live PV dialogs as the count — identical to what _send_to_users will
        # actually send to.  DB count is intentionally NOT used here because the DB
        # may contain stale records from previous sessions that inflate the number.
        from app.services.telegram_service import TelegramUserService
        tg = TelegramUserService.get_instance()
        live_users = await tg.get_all_user_dialogs()
        count = len(live_users)

    # Build a human-readable description of what will be sent
    if is_forward and forward_from_chat_id and forward_from_message_id:
        content_desc = "📨 <b>فوروارد</b> از منبع اصلی"
    elif is_forward:
        content_desc = "📨 <b>فوروارد</b> (با محتوای پیام)"
    elif media_type:
        content_desc = f"🖼 <b>{media_type}</b>"
        if message.caption:
            content_desc += f" + کپشن"
    else:
        content_desc = "✍️ <b>متن</b>"

    await state.set_state(BroadcastStates.confirming)
    await message.answer(
        f"📋 <b>پیش‌نمایش پیام بالا</b>\n\n"
        f"نوع محتوا: {content_desc}\n"
        f"مقصد: <b>{label}</b>\n"
        f"تعداد: <b>{count}</b>\n\n"
        "⚠️ ارسال در پس‌زمینه اجرا می‌شود — ربات همچنان پاسخگو می‌ماند.\n"
        "نتیجه نهایی پس از اتمام ارسال می‌رسد.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ شروع ارسال", callback_data="bc_confirm"),
            InlineKeyboardButton(text="❌ لغو", callback_data="bc_cancel"),
        ]]),
    )


@router.callback_query(F.data == "bc_cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_callback_answer(callback)
    await state.clear()
    await safe_callback_answer(callback, "لغو شد.")
    await callback.message.edit_text("❌ لغو شد.", reply_markup=_back_btn())  # type: ignore[union-attr]


@router.callback_query(F.data == "bc_confirm", BroadcastStates.confirming)
async def cb_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_callback_answer(callback)
    data = await state.get_data()
    await state.clear()
    await safe_callback_answer(callback)

    bqs = BroadcastQueueService.get_instance()
    actor_id = callback.from_user.id if callback.from_user else 0

    try:
        job_id = await bqs.start_broadcast(
            target=data.get("target", "groups"),
            from_chat_id=data["from_chat_id"],
            message_id=data["message_id"],
            actor_id=actor_id,
            bot=callback.bot,
            message_text=data.get("message_text", ""),
            media_file_id=data.get("media_file_id"),
            media_type=data.get("media_type"),
            is_forward=data.get("is_forward", False),
            forward_from_chat_id=data.get("forward_from_chat_id"),
            forward_from_message_id=data.get("forward_from_message_id"),
        )
        await callback.message.edit_text(  # type: ignore[union-attr]
            f"✅ <b>ارسال همگانی شروع شد</b> (job: <code>{job_id}</code>)\n\n"
            "در پس‌زمینه اجرا می‌شود.\n"
            "نتیجه نهایی پس از اتمام برایتان ارسال می‌شود.",
            parse_mode="HTML", reply_markup=_back_btn(),
        )
        logger.info("Broadcast job %s started by admin %d", job_id, actor_id)
    except RuntimeError as exc:
        await callback.message.edit_text(  # type: ignore[union-attr]
            f"⚠️ {exc}", reply_markup=_back_btn()
        )
