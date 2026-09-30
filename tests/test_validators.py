import importlib.util
import unittest
from pathlib import Path


_VALIDATORS_PATH = Path(__file__).parents[1] / "app" / "utils" / "validators.py"
_SPEC = importlib.util.spec_from_file_location("telegram_manager_validators", _VALIDATORS_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("Could not load the link validator module")
_VALIDATORS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_VALIDATORS)
LinkValidator = _VALIDATORS.LinkValidator


class LinkValidatorTests(unittest.TestCase):
    def test_extracts_public_and_private_links_and_removes_punctuation(self) -> None:
        text = "Join https://t.me/Example_Group?start=abc, or t.me/+InviteHash123!"

        self.assertEqual(
            LinkValidator.extract_links(text),
            [
                "https://t.me/example_group",
                "https://t.me/+InviteHash123",
            ],
        )

    def test_supports_legacy_invites_and_telegram_me(self) -> None:
        text = "telegram.me/joinchat/InviteHash123 and www.telegram.me/Other_Group"

        self.assertEqual(
            LinkValidator.extract_links(text),
            [
                "https://t.me/+InviteHash123",
                "https://t.me/other_group",
            ],
        )

    def test_extracts_hidden_telegram_url_entities(self) -> None:
        hidden_link = type("TextUrlEntity", (), {"url": "https://t.me/Hidden_Group?x=1"})()
        external_link = type("TextUrlEntity", (), {"url": "https://example.com/group"})()

        self.assertEqual(
            LinkValidator.extract_links("Join this group", [hidden_link, external_link]),
            ["https://t.me/hidden_group"],
        )

    def test_ignores_email_mentions_and_non_telegram_urls(self) -> None:
        text = "email me at person@example.com or visit https://example.com/@somegroup"

        self.assertEqual(LinkValidator.extract_links(text), [])

    def test_normalizes_mentions_and_preview_links(self) -> None:
        self.assertEqual(
            LinkValidator.normalize("@Example_Group"),
            "https://t.me/example_group",
        )
        self.assertEqual(
            LinkValidator.normalize("https://t.me/s/Example_Group/123?single"),
            "https://t.me/example_group",
        )

    def test_rejects_malformed_and_non_group_routes(self) -> None:
        self.assertIsNone(LinkValidator.normalize("https://t.me/ab"))
        self.assertIsNone(LinkValidator.normalize("https://t.me/c/12345/99"))
        self.assertIsNone(LinkValidator.normalize("javascript:t.me/example_group"))

    def test_identifies_private_invites_and_public_usernames(self) -> None:
        self.assertTrue(LinkValidator.is_private_invite("https://t.me/joinchat/InviteHash123"))
        self.assertIsNone(LinkValidator.extract_username("https://t.me/+InviteHash123"))
        self.assertEqual(
            LinkValidator.extract_username("https://t.me/Example_Group?start=abc"),
            "example_group",
        )


if __name__ == "__main__":
    unittest.main()