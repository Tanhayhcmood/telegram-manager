import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.services.discovery_service import DiscoveryService
from app.services.health_service import HealthService
from app.services.link_backfill_service import LinkBackfillService
from app.services.telegram_service import TelegramUserService


class _TelegramClient:
    def __init__(self) -> None:
        self.connected = False
        self.connect = AsyncMock(side_effect=self._connect)
        self.disconnect = AsyncMock(side_effect=self._disconnect)
        self.is_user_authorized = AsyncMock(return_value=True)
        self.get_me = AsyncMock(return_value=object())

    async def _connect(self) -> None:
        self.connected = True

    async def _disconnect(self) -> None:
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected


def _telegram_service(client: _TelegramClient) -> TelegramUserService:
    service = object.__new__(TelegramUserService)
    service.client = client
    service._running = False
    service._desired_running = False
    service._connection_lock = asyncio.Lock()
    return service


class TelegramConnectionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_connects_and_stop_disables_reconnect(self) -> None:
        client = _TelegramClient()
        service = _telegram_service(client)

        await service.start()
        self.assertTrue(service.is_running())
        self.assertTrue(service.is_expected_to_run())
        client.connect.assert_awaited_once()

        await service.stop()
        self.assertFalse(service.is_running())
        self.assertFalse(service.is_expected_to_run())
        client.disconnect.assert_awaited_once()

        self.assertFalse(await service.reconnect_if_needed())
        client.connect.assert_awaited_once()

    async def test_health_check_reconnects_and_restarts_join_queue_once(self) -> None:
        client = SimpleNamespace(get_me=AsyncMock(return_value=object()))

        class RecoverableTelegramService:
            def __init__(self) -> None:
                self.connected = False
                self.client = client
                self.reconnect_if_needed = AsyncMock(side_effect=self._reconnect)

            def is_running(self) -> bool:
                return self.connected

            def is_expected_to_run(self) -> bool:
                return True

            async def _reconnect(self) -> bool:
                self.connected = True
                return True

        tg = RecoverableTelegramService()
        health = HealthService()
        health.set_tg_service(tg)
        restart_queue = AsyncMock()
        health.set_recovery_callback(restart_queue)

        await health._check()

        tg.reconnect_if_needed.assert_awaited_once()
        restart_queue.assert_awaited_once()
        self.assertTrue(health.is_healthy())
        self.assertIsNotNone(health.last_ok_at())

    async def test_health_monitor_does_not_restart_admin_stopped_client(self) -> None:
        tg = SimpleNamespace(
            is_running=Mock(return_value=False),
            is_expected_to_run=Mock(return_value=False),
            reconnect_if_needed=AsyncMock(),
        )
        health = HealthService()
        health.set_tg_service(tg)
        health._consecutive_failures = 2

        await health._check()

        tg.reconnect_if_needed.assert_not_awaited()
        self.assertEqual(health._consecutive_failures, 0)


class HistoricalLinkRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_historical_message_registers_links_without_joining(self) -> None:
        service = DiscoveryService(SimpleNamespace())
        service._register_link = AsyncMock(side_effect=[True, True])
        message = SimpleNamespace(
            raw_text="Join https://t.me/Example_Group and t.me/Another_Group",
            entities=[],
            id=42,
        )

        created = await service.process_historical_message(message, -100123)

        self.assertEqual(created, 2)
        self.assertEqual(
            service._register_link.await_args_list[0].args,
            ("https://t.me/example_group", "history:-100123:42"),
        )
        self.assertFalse(
            service._register_link.await_args_list[0].kwargs["enqueue_join"]
        )
        self.assertFalse(
            service._register_link.await_args_list[1].kwargs["enqueue_join"]
        )

    async def test_backfill_rejects_unsupported_period_before_telegram_io(self) -> None:
        tg = SimpleNamespace(
            is_running=Mock(return_value=True),
            get_live_dialogs=AsyncMock(),
            client=SimpleNamespace(),
        )
        service = LinkBackfillService(tg)

        with self.assertRaises(ValueError):
            await service.run(30)

        tg.get_live_dialogs.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
