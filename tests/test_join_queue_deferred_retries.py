import asyncio
import importlib.util
import logging
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


APP_PATH = Path(__file__).resolve().parents[1] / "app" / "services" / "join_queue_service.py"


def _stub_module(name: str, **attributes: object) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


class RecordingNotificationService:
    messages: list[str] = []

    @classmethod
    def get_instance(cls) -> "RecordingNotificationService":
        return cls()

    async def notify_info(self, text: str) -> None:
        self.messages.append(text)


def _load_join_queue_module() -> types.ModuleType:
    app_package = _stub_module("app")
    app_package.__path__ = [str(APP_PATH.parents[1])]
    database_package = _stub_module("app.database")
    database_package.__path__ = []
    repositories_package = _stub_module("app.repositories")
    repositories_package.__path__ = []
    models_package = _stub_module("app.models")
    models_package.__path__ = []
    utils_package = _stub_module("app.utils")
    utils_package.__path__ = []

    group_status = SimpleNamespace(PENDING="pending", APPROVED="approved")
    modules = {
        "app": app_package,
        "app.config": _stub_module(
            "app.config",
            settings=SimpleNamespace(MAX_JOINS_PER_DAY=50),
        ),
        "app.database": database_package,
        "app.database.connection": _stub_module(
            "app.database.connection",
            AsyncSessionLocal=None,
        ),
        "app.repositories": repositories_package,
        "app.repositories.join_attempt_repository": _stub_module(
            "app.repositories.join_attempt_repository",
            JoinAttemptRepository=object,
        ),
        "app.models": models_package,
        "app.models.group": _stub_module("app.models.group", GroupStatus=group_status),
        "app.utils": utils_package,
        "app.utils.logger": _stub_module(
            "app.utils.logger",
            get_logger=logging.getLogger,
        ),
    }
    modules["app.repositories"].GroupRepository = object
    modules["app.repositories"].LogRepository = object

    module_name = "_join_queue_service_under_test"
    spec = importlib.util.spec_from_file_location(module_name, APP_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {APP_PATH}")
    module = importlib.util.module_from_spec(spec)

    with patch.dict(sys.modules, modules):
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

    return module


class JoinQueueDeferredRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.module = _load_join_queue_module()
        self.queue = self.module.JoinQueueService()
        RecordingNotificationService.messages.clear()

    async def test_only_one_delayed_retry_is_scheduled_for_a_group(self) -> None:
        task = self.module.JoinTask(17, "https://t.me/example", "example")

        self.assertTrue(self.queue._schedule_requeue(task, 3600, "deferred-test-17"))
        self.assertFalse(self.queue._schedule_requeue(task, 3600, "duplicate-test-17"))
        self.assertEqual(self.queue._deferred_ids, {17})

        delayed_tasks = [
            pending
            for pending in asyncio.all_tasks()
            if pending.get_name() in {"deferred-test-17", "duplicate-test-17"}
        ]
        self.assertEqual(len(delayed_tasks), 1)
        for pending in delayed_tasks:
            pending.cancel()
        await asyncio.gather(*delayed_tasks, return_exceptions=True)
        self.assertNotIn(17, self.queue._deferred_ids)

    async def test_enqueue_skips_a_group_with_a_delayed_retry(self) -> None:
        self.queue._deferred_ids.add(23)

        await self.queue.enqueue(23, "https://t.me/example", "example")

        self.assertEqual(self.queue.queue_size(), 0)
        self.assertNotIn(23, self.queue._queued_ids)

    async def test_retry_removes_deferred_marker_before_requeueing(self) -> None:
        task = self.module.JoinTask(31, "https://t.me/example", "example")
        self.queue._deferred_ids.add(31)

        await self.queue._requeue_after_delay(task, 0)

        self.assertNotIn(31, self.queue._deferred_ids)
        self.assertIn(31, self.queue._queued_ids)
        queued = await self.queue._queue.get()
        self.assertEqual(queued.group_id, 31)

    async def test_database_reload_skips_groups_that_are_already_deferred(self) -> None:
        group = SimpleNamespace(
            group_id=42,
            invite_link="https://t.me/example",
            title="example",
            status="pending",
        )

        class FakeSession:
            async def __aenter__(self) -> "FakeSession":
                return self

            async def __aexit__(self, *_: object) -> None:
                return None

        class FakeGroupRepository:
            def __init__(self, _session: object) -> None:
                pass

            async def get_by_status(self, status: str, limit: int) -> list[object]:
                del limit
                return [group] if status == self_module.GroupStatus.PENDING else []

        class FakeAttemptRepository:
            def __init__(self, _session: object) -> None:
                pass

            async def has_pending_approval_attempt(self, _group_id: int) -> bool:
                return False

        self_module = self.module
        self.module.AsyncSessionLocal = FakeSession
        self.module.GroupRepository = FakeGroupRepository
        self.module.JoinAttemptRepository = FakeAttemptRepository
        self.queue._deferred_ids.add(42)

        await self.queue._reload_pending_from_db()

        self.assertEqual(self.queue.queue_size(), 0)
        self.assertNotIn(42, self.queue._queued_ids)

    async def test_active_cooldown_notice_is_sent_once_for_each_deadline(self) -> None:
        app_package = _stub_module("app")
        app_package.__path__ = [str(APP_PATH.parents[1])]
        services_package = _stub_module("app.services")
        services_package.__path__ = []
        notification_module = _stub_module(
            "app.services.notification_service",
            NotificationService=RecordingNotificationService,
        )
        deadline = datetime.now(timezone.utc) + timedelta(hours=2)

        with patch.dict(
            sys.modules,
            {
                "app": app_package,
                "app.services": services_package,
                "app.services.notification_service": notification_module,
            },
        ):
            await self.queue._notify_telegram_flood_wait(
                self.module.JoinTask(17, "https://t.me/example", "example"),
                deadline,
            )
            await self.queue._notify_telegram_flood_wait(
                self.module.JoinTask(18, "https://t.me/example-2", "example-2"),
                deadline,
            )

        self.assertEqual(len(RecordingNotificationService.messages), 1)
        self.assertIn("محدودیت موقت تلگرام", RecordingNotificationService.messages[0])
        self.assertIn("زمان تقریبی ادامهٔ صف", RecordingNotificationService.messages[0])

    async def test_positive_daily_limit_is_enforced(self) -> None:
        self.module.settings.MAX_JOINS_PER_DAY = 2
        self.queue._daily_join_count = 2
        self.assertTrue(self.queue._daily_limit_reached())

    async def test_zero_daily_limit_means_unlimited(self) -> None:
        self.module.settings.MAX_JOINS_PER_DAY = 0
        self.queue._daily_join_count = 999
        self.assertFalse(self.queue._daily_limit_reached())