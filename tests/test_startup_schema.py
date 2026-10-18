import ast
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class StartupSchemaTests(unittest.TestCase):
    def test_link_status_contains_skipped_not_group_value(self) -> None:
        source = (ROOT / "app/models/discovered_link.py").read_text()
        module = ast.parse(source)
        link_status = next(
            node
            for node in module.body
            if isinstance(node, ast.ClassDef) and node.name == "LinkStatus"
        )
        values = {
            target.id: ast.literal_eval(statement.value)
            for statement in link_status.body
            if isinstance(statement, ast.Assign)
            for target in statement.targets
            if isinstance(target, ast.Name)
        }
        self.assertEqual(values["SKIPPED_NOT_GROUP"], "skipped_not_group")

    def test_enum_migration_is_idempotent_and_autocommitted(self) -> None:
        source = (ROOT / "migrations/versions/0008_enum_compatibility.py").read_text()
        self.assertIn("with op.get_context().autocommit_block():", source)
        self.assertRegex(
            source,
            re.compile(
                r"ALTER TYPE link_status ADD VALUE IF NOT EXISTS "
                r"'skipped_not_group'"
            ),
        )
        self.assertRegex(
            source,
            re.compile(
                r"ALTER TYPE group_status ADD VALUE IF NOT EXISTS 'left'"
            ),
        )
        self.assertIn('revision: str = "0008"', source)
        self.assertIn('down_revision: Union[str, None] = "0007"', source)

    def test_0006_adds_can_write_only_when_missing(self) -> None:
        source = (ROOT / "migrations/versions/0006_group_can_write.py").read_text()
        self.assertIn(
            "ADD COLUMN IF NOT EXISTS can_write BOOLEAN NOT NULL DEFAULT TRUE",
            source,
        )
        self.assertNotIn("op.add_column(", source)

    def test_0007_schema_changes_are_idempotent(self) -> None:
        source = (ROOT / "migrations/versions/0007_user_bio_scan.py").read_text()
        self.assertIn(
            "ADD COLUMN IF NOT EXISTS last_bio_scan_at TIMESTAMPTZ",
            source,
        )
        self.assertIn(
            "ADD COLUMN IF NOT EXISTS bio_scan_complete_at TIMESTAMPTZ",
            source,
        )
        self.assertIn("CREATE TABLE IF NOT EXISTS user_bio_scans", source)
        self.assertIn("passive_scanned_at TIMESTAMPTZ NULL", source)
        self.assertIn("message_checked_at TIMESTAMPTZ NULL", source)
        self.assertIn('down_revision: Union[str, None] = "0006"', source)

    def test_existing_schema_is_stamped_before_upgrade(self) -> None:
        source = (ROOT / "start.py").read_text()
        self.assertIn("if has_app_tables and not has_alembic_version:", source)
        self.assertRegex(
            source,
            re.compile(
                r'_run_alembic\("stamp", _LEGACY_BASELINE_REVISION\)'
                r".*_run_alembic\(\"upgrade\", \"head\"\)",
                re.DOTALL,
            ),
        )
        self.assertIn("'alembic_version'", source)
        self.assertIn('_run_alembic("upgrade", "head")', source)

    def test_alembic_output_is_forwarded_and_failure_stops_startup(self) -> None:
        source = (ROOT / "start.py").read_text()
        self.assertIn("stdout=sys.stdout", source)
        self.assertIn("stderr=sys.stderr", source)
        self.assertIn("full output is above.", source)
        self.assertIn("raise SystemExit(result.returncode)", source)

    def test_docker_uses_migration_startup_script(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn('CMD ["python", "start.py"]', dockerfile)


if __name__ == "__main__":
    unittest.main()
