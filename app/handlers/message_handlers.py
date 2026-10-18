from aiogram import Router, F
from aiogram.types import (
    CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from app.database.connection import AsyncSessionLocal
from app.repositories import GroupRepository
from app.models.group import GroupStatus
from app.services.broadcast_queue_service import BroadcastQueueService
from app.safety import BULK_BROADCASTS_ENABLED
from app.utils.logger import get_logger

logger = get_logger(__name__)
router = Router(name="message")


class SendMessageStates(StatesGroup):
    waiting_content = State()
    confirming = State()
    selecting_targets = State()


def _back_btn() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu")]
    ])


@router.callback_query(F.data == "send_message")
async def cb_send_message(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(SendMessageStates.waiting_content)
    await callback.message.edit_text(  # type: ignore[union-attr]
        "📨 *ارسال پیام به گروه‌ها*\n\n"
        "لطفاً محتوای پیام را ارسال کنید.\n"
        "پشتیبانی از: متن، عکس، ویدیو، فایل، فوروارد\n\n"
        "_برای لغو /cancel بنویسید_",
        parse_mode="Markdown",
        reply_markup=_back_btn(),
    )


@router.message(SendMessageStates.waiting_content, F.text == "/cancel")
@router.message(SendMessageStates.confirming, F.text == "/cancel")
@router.message(SendMessageStates.selecting_targets, F.text == "/cancel")
async def cancel_send(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ لغو شد.")


@router.message(SendMessageStates.waiting_content)
async def receive_content(message: Message, state: FSMContext) -> None:
    await state.update_data(
        content_message_id=message.message_id,
        content_chat_id=message.chat.id,
    )
    await state.set_state(SendMessageStates.confirming)
    await message.answer(
        "📋 *پیش‌نمایش پیام بالا* — تایید می‌کنید؟",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ بله، ادامه", callback_data="confirm_content"),
            InlineKeyboardButton(text="❌ لغو", callback_data="cancel_send"),
        ]]),
    )


@router.callback_query(F.data == "cancel_send")
async def cb_cancel_send(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("لغو شد.")
    await callback.message.edit_text("❌ لغو شد.", reply_markup=_back_btn())  # type: ignore[union-attr]


@router.callback_query(F.data == "confirm_content", SendMessageStates.confirming)
async def cb_confirm_content(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(SendMessageStates.selecting_targets)

    async with AsyncSessionLocal() as session:
        repo = GroupRepository(session)
        groups = await repo.get_joined()

    if not groups:
        await state.clear()
        await callback.message.edit_text(  # type: ignore[union-attr]
            "⚠️ هیچ گروه عضو‌شده‌ای وجود ندارد.",
            reply_markup=_back_btn(),
        )
        return

    buttons: list[list[InlineKeyboardButton]] = []
    for g in groups[:20]:
        title = (g.title or str(g.group_id))[:30]
        buttons.append([
            InlineKeyboardButton(text=f"📤 {title}", callback_data=f"send_to:{g.group_id}")
        ])

    buttons.append([InlineKeyboardButton(text="🔙 لغو", callback_data="cancel_send")])

    await callback.message.edit_text(  # type: ignore[union-attr]
        f"📋 انتخاب مقصد ({len(groups)} گروه عضو):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("send_to:"), SendMessageStates.selecting_targets)
async def cb_send_to_group(callback: CallbackQuery, state: FSMContext) -> None:
    group_id = int(callback.data.split(":")[1])  # type: ignore[union-attr]
    data = await state.get_data()
    await state.clear()
    actor = str(callback.from_user.id) if callback.from_user else "admin"
    try:
        await callback.bot.forward_message(  # type: ignore[union-attr]
            chat_id=group_id,
            from_chat_id=data["content_chat_id"],
            message_id=data["content_message_id"],
        )
        await callback.answer(f"✅ ارسال شد به گروه {group_id}.", show_alert=True)
        logger.info("Message forwarded to group %d by admin %s", group_id, actor)
    except Exception as exc:
        await callback.answer(f"❌ خطا: {exc}", show_alert=True)
        logger.error("Failed to forward to %d: %s", group_id, exc)


@router.callback_query(F.data == "send_to_all", SendMessageStates.selecting_targets)
async def cb_send_to_all(callback: CallbackQuery, state: FSMContext) -> None:
    """Bulk messaging is disabled in the safe Render deployment."""
    if not BULK_BROADCASTS_ENABLED:
        await state.clear()
        await callback.answer("ارسال همگانی در این نسخه غیرفعال است.", show_alert=True)
        return
    data = await state.get_data()
    await state.clear()
    bqs = BroadcastQueueService.get_instance()
    actor_id = callback.from_user.id if callback.from_user else 0
    try:
        job_id = await bqs.start_broadcast(
            target="groups",
            from_chat_id=data["content_chat_id"],
            message_id=data["content_message_id"],
            actor_id=actor_id,
            bot=callback.bot,
        )
        await callback.answer()
        await callback.message.edit_text(  # type: ignore[union-attr]
            f"✅ *ارسال به همه گروه‌ها شروع شد* (job: `{job_id}`)\n\n"
            "در پس‌زمینه اجرا می‌شود — ربات همچنان پاسخگو است.\n"
            "نتیجه نهایی پس از اتمام برایتان ارسال می‌شود.",
            parse_mode="Markdown",
            reply_markup=_back_btn(),
        )
    except RuntimeError as exc:
        await callback.answer(str(exc), show_alert=True)
