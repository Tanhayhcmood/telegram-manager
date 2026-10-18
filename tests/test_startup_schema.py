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

    def test_docker_uses_migration_startup_script(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn('CMD ["python", "start.py"]', dockerfile)


if __name__ == "__main__":
    unittest.main()
