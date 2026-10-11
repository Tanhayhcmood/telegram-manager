import unittest
from unittest.mock import AsyncMock

from app.handlers.admin_handlers import cmd_menu, cmd_start, main_menu_keyboard


class AdminStartHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_sends_menu_in_one_telegram_request(self) -> None:
        message = type("MessageStub", (), {"answer": AsyncMock()})()

        await cmd_start(message)

        message.answer.assert_awaited_once()
        self.assertIsNotNone(message.answer.await_args.kwargs["reply_markup"])

    async def test_menu_sends_menu_in_one_telegram_request(self) -> None:
        message = type("MessageStub", (), {"answer": AsyncMock()})()

        await cmd_menu(message)

        message.answer.assert_awaited_once()
        self.assertIsNotNone(message.answer.await_args.kwargs["reply_markup"])

    def test_main_menu_includes_discovered_links_view(self) -> None:
        keyboard = main_menu_keyboard()
        callback_data = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
        ]

        self.assertIn("discovered_links", callback_data)
