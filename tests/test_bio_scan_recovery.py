import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BioScanRecoveryTests(unittest.TestCase):
    def test_passive_user_scan_has_a_recheck_cutoff(self) -> None:
        source = (ROOT / "app/repositories/user_bio_scan_repository.py").read_text()
        module = ast.parse(source)
        repository = next(
            node
            for node in module.body
            if isinstance(node, ast.ClassDef) and node.name == "UserBioScanRepository"
        )
        method = next(
            node
            for node in repository.body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "get_passively_scanned_user_ids"
        )
        self.assertEqual(method.args.args[-1].arg, "cutoff")
        self.assertIn("UserBioScan.passive_scanned_at >= cutoff", source)

    def test_discovery_uses_configured_cutoff_and_emits_run_logs(self) -> None:
        source = (ROOT / "app/services/discovery_service.py").read_text()
        self.assertIn("days=max(1, settings.BIO_SCAN_RECHECK_DAYS)", source)
        self.assertIn("get_passively_scanned_user_ids(user_cutoff)", source)
        self.assertIn("Member-bio scan starting:", source)
        self.assertIn("Member-bio scan finished:", source)
        self.assertIn("no joined groups are due", source)

    def test_scheduler_triggers_scan_when_discovery_becomes_ready(self) -> None:
        source = (ROOT / "app/services/scheduler_service.py").read_text()
        self.assertIn('self._scheduler.get_job("scan_group_member_bios")', source)
        self.assertIn("next_run_time=datetime.now(timezone.utc)", source)
        self.assertIn("Scheduled initial member-bio scan", source)


if __name__ == "__main__":
    unittest.main()
