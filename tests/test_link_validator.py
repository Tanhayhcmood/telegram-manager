import importlib.util
import unittest
from pathlib import Path


VALIDATOR_PATH = Path(__file__).resolve().parents[1] / "app" / "utils" / "validators.py"


def load_validator():
    spec = importlib.util.spec_from_file_location("_link_validator_under_test", VALIDATOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {VALIDATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.LinkValidator


class HiddenUrl:
    def __init__(self, url: str) -> None:
        self.url = url


class LinkValidatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.validator = load_validator()

    def test_extracts_and_deduplicates_all_supported_link_forms(self) -> None:
        links = self.validator.extract_links(
            "https://t.me/My_Group, @MY_GROUP and https://t.me/s/My_Group",
            entities=[HiddenUrl("https://t.me/My_Group?single")],
        )
        self.assertEqual(links, ["https://t.me/my_group"])

    def test_canonicalizes_private_invites(self) -> None:
        self.assertEqual(
            self.validator.normalize("https://t.me/joinchat/AbCde-12345"),
            "https://t.me/+AbCde-12345",
        )
        self.assertTrue(self.validator.is_private_invite("t.me/+AbCde-12345"))

    def test_rejects_unsafe_hosts_ports_and_malformed_usernames(self) -> None:
        self.assertIsNone(self.validator.normalize("https://evil.example/group"))
        self.assertIsNone(self.validator.normalize("https://t.me:443/groupname"))
        self.assertIsNone(self.validator.normalize("https://t.me/abc"))


if __name__ == "__main__":
    unittest.main()