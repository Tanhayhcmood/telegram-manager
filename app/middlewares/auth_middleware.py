import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery

from app.handlers.callback_utils import safe_callback_answer
from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

_DUPLICATE_CALLBACK_WINDOW = 0.75
_recent_callbacks: dict[tuple[int, str], float] = {}


class AdminAuthMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        admin_ids = settings.get_admin_id_list()

        user = None
        if isinstance(event, Message):
            user = event.from_user
        elif isinstance(event, CallbackQuery):
            user = event.from_user

        if user is None or user.id not in admin_ids:
            uid = user.id if user else "unknown"
            logger.warning("Unauthorized access attempt by user_id=%s", uid)
            if isinstance(event, Message):
                await event.answer("⛔ دسترسی غیرمجاز.")
            elif isinstance(event, CallbackQuery):
                await safe_callback_answer(event, "⛔ دسترسی غیرمجاز.", show_alert=True)
            return

        if isinstance(event, CallbackQuery):
            now = time.monotonic()
            key = (user.id, event.data or "")
            previous = _recent_callbacks.get(key)
            _recent_callbacks[key] = now
            if len(_recent_callbacks) > 2048:
                cutoff = now - 60.0
                _recent_callbacks.clear()
                _recent_callbacks.update(
                    {k: timestamp for k, timestamp in [(key, now)] if timestamp >= cutoff}
                )
            if previous is not None and now - previous < _DUPLICATE_CALLBACK_WINDOW:
                await safe_callback_answer(
                    event,
                    "⏳ درخواست قبلی در حال پردازش است.",
                )
                return

        data["is_admin"] = True
        return await handler(event, data)
