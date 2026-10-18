import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class JoinNotificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.queue_source = (ROOT / "app/services/join_queue_service.py").read_text()
        cls.queue_module = ast.parse(cls.queue_source)
        cls.queue_class = next(
            node
            for node in cls.queue_module.body
            if isinstance(node, ast.ClassDef) and node.name == "JoinQueueService"
        )
        cls.process_method = next(
            node
            for node in cls.queue_class.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_process"
        )

    def test_success_failure_and_floodwait_dispatch_notifications(self) -> None:
        events = {
            keyword.value.value
            for node in ast.walk(self.process_method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_notify_admins"
            for keyword in node.keywords
            if keyword.arg == "event"
            and isinstance(keyword.value, ast.Constant)
            and isinstance(keyword.value.value, str)
        }
        self.assertTrue({"join_success", "join_failed", "flood_wait", "peer_flood"} <= events)
        self.assertIn("امروز:", self.queue_source)
        self.assertIn("settings.MAX_JOINS_PER_DAY", self.queue_source)

    def test_notification_failure_is_logged_and_swallowed(self) -> None:
        helper = next(
            node
            for node in self.queue_class.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_notify_admins"
        )
        helper_source = ast.get_source_segment(self.queue_source, helper)
        self.assertIsNotNone(helper_source)
        self.assertIn("except Exception as exc:", helper_source)
        self.assertIn("logger.warning(", helper_source)
        self.assertIn("await ns.notify(message, parse_mode=\"HTML\")", helper_source)

    def test_notification_service_uses_configured_admin_ids_and_aiogram(self) -> None:
        source = (ROOT / "app/services/notification_service.py").read_text()
        self.assertIn("settings.get_admin_id_list()", source)
        self.assertIn("await self._bot.send_message(admin_id, text, parse_mode=parse_mode)", source)
        self.assertIn("logger.warning(", source)


if __name__ == "__main__":
    unittest.main()
