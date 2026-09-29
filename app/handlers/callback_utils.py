"""Reliable callback-query acknowledgements.

Telegram expects every inline-button callback to be acknowledged quickly. Some
of the manager actions legitimately perform database or Telethon work, so the
first acknowledgement must not wait for that work to finish.
"""

import time

from aiogram.types import CallbackQuery


_ACK_TTL_SECONDS = 120.0
_acked_callbacks: dict[str, float] = {}


def _remember_callback(callback_id: str, now: float) -> None:
    _acked_callbacks[callback_id] = now
    if len(_acked_callbacks) > 4096:
        cutoff = now - _ACK_TTL_SECONDS
        stale = [key for key, timestamp in _acked_callbacks.items() if timestamp < cutoff]
        for key in stale:
            _acked_callbacks.pop(key, None)


async def safe_callback_answer(
    callback: CallbackQuery,
    text: str | None = None,
    *,
    show_alert: bool = False,
) -> None:
    """Acknowledge a callback once, without turning Telegram races into errors.

    A callback query can only be answered once. Existing handlers sometimes
    acknowledge at the end to report a result, while the UI needs an immediate
    acknowledgement at the start. The first call answers the Telegram query;
    later calls become a normal chat message so the result is still visible.
    """

    callback_id = callback.id
    now = time.monotonic()
    already_answered = callback_id in _acked_callbacks

    if not already_answered:
        _remember_callback(callback_id, now)
        try:
            await callback.answer(text=text, show_alert=show_alert)
        except Exception:
            # The query may have expired while Render was restarting. There is
            # nothing useful to retry, and the handler can still finish safely.
            return
        return

    if not text:
        return

    # Telegram cannot show a second callback alert for the same query. Preserve
    # the feedback as a regular message instead of silently dropping it.
    if callback.message is None:
        return
    try:
        prefix = "ℹ️ " if show_alert else ""
        await callback.message.answer(f"{prefix}{text}")
    except Exception:
        return