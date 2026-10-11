import unittest
from unittest.mock import AsyncMock

from app.handlers.admin_handlers import cmd_menu, cmd_start, main_menu_keyboard
from app.handlers.groups_handlers import _format_discovered_links
from app.models.discovered_link import DiscoveredLink, LinkStatus


class AdminStartHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_sends_menu_in_one_telegram_request(self) -> None:
        message = type("MessageStub", (), {"answer": AsyncMock()})()
        state = AsyncMock()

        await cmd_start(message, state)

        message.answer.assert_awaited_once()
        self.assertIsNotNone(message.answer.await_args.kwargs["reply_markup"])
        state.clear.assert_awaited_once()

    async def test_menu_sends_menu_in_one_telegram_request(self) -> None:
        message = type("MessageStub", (), {"answer": AsyncMock()})()
        state = AsyncMock()

        await cmd_menu(message, state)

        message.answer.assert_awaited_once()
        self.assertIsNotNone(message.answer.await_args.kwargs["reply_markup"])
        state.clear.assert_awaited_once()

    def test_main_menu_includes_discovered_links_view(self) -> None:
        keyboard = main_menu_keyboard()
        callback_data = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
        ]

        self.assertIn("discovered_links", callback_data)

    def test_discovered_link_text_escapes_content_and_limits_line_length(self) -> None:
        item = DiscoveredLink(
            link="https://t.me/" + ("a" * 200),
            status=LinkStatus.REJECTED,
            notes="<b>not trusted</b>",
        )

        text = _format_discovered_links([item], total=1, page=0)

        self.assertIn("&lt;b&gt;not trusted&lt;/b&gt;", text)
        self.assertNotIn("<b>not trusted</b>", text)
        self.assertLess(len(text), 4096)

    def test_discovered_link_uses_only_normalized_telegram_targets(self) -> None:
        item = DiscoveredLink(
            link="https://t.me/telegram",
            status=LinkStatus.PENDING,
        )

        text = _format_discovered_links([item], total=1, page=0)

        self.assertIn('<a href="https://t.me/telegram">', text)
