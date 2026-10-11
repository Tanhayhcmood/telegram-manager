import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.services.discovery_service import DiscoveryService


class UserBioDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_message_scans_sender_bio_without_keyword_match(self) -> None:
        tg = SimpleNamespace(
            get_user_bio=AsyncMock(return_value="My group: t.me/Hidden_Group"),
        )
        service = DiscoveryService(tg)
        service._track_user = AsyncMock()
        service._register_link = AsyncMock(return_value=True)
        event = SimpleNamespace(
            message=SimpleNamespace(
                raw_text="hello everyone",
                entities=[],
                id=42,
            ),
            sender_id=77,
            chat_id=-100123,
        )

        await service.process_message(event)

        tg.get_user_bio.assert_awaited_once_with(77)
        service._register_link.assert_awaited_once_with(
            "https://t.me/hidden_group",
            "user_bio:77:chat:-100123",
        )

    async def test_live_message_and_bio_links_are_both_registered(self) -> None:
        tg = SimpleNamespace(
            get_user_bio=AsyncMock(return_value="https://t.me/Bio_Group"),
        )
        service = DiscoveryService(tg)
        service._track_user = AsyncMock()
        service._register_link = AsyncMock(return_value=True)
        event = SimpleNamespace(
            message=SimpleNamespace(
                raw_text="Join https://t.me/Message_Group",
                entities=[],
                id=43,
            ),
            sender_id=78,
            chat_id=-100124,
        )

        await service.process_message(event)

        self.assertEqual(
            service._register_link.await_args_list[0].args,
            ("https://t.me/message_group", "message:-100124:43"),
        )
        self.assertEqual(
            service._register_link.await_args_list[1].args,
            ("https://t.me/bio_group", "user_bio:78:chat:-100124"),
        )

    async def test_historical_backfill_scans_sender_bio_without_joining(self) -> None:
        tg = SimpleNamespace(
            get_user_bio=AsyncMock(return_value="Visit t.me/Old_Bio_Group"),
        )
        service = DiscoveryService(tg)
        service._register_link = AsyncMock(return_value=True)
        message = SimpleNamespace(
            raw_text="no link here",
            entities=[],
            id=44,
            sender_id=79,
        )

        created = await service.process_historical_message(message, -100125)

        self.assertEqual(created, 1)
        service._register_link.assert_awaited_once_with(
            "https://t.me/old_bio_group",
            "user_bio:79:history_chat:-100125",
            enqueue_join=False,
        )
