"""Tests for .env parsing and required-value handling."""

import tempfile
import unittest
from pathlib import Path

import tests.context  # noqa: F401

from shared import config


class ParseEnvFile(unittest.TestCase):
    def parse(self, text: str) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(text)
            return config.parse_env_file(path)

    def test_basic_pairs(self):
        self.assertEqual(self.parse("A=1\nB=two\n"), {"A": "1", "B": "two"})

    def test_ignores_comments_and_blanks(self):
        self.assertEqual(self.parse("# note\n\nA=1\n"), {"A": "1"})

    def test_strips_surrounding_quotes(self):
        self.assertEqual(self.parse('A="quoted"\nB=\'single\'\n'), {"A": "quoted", "B": "single"})

    def test_preserves_equals_inside_values(self):
        # Base64-ish secrets and tokens routinely contain "=".
        self.assertEqual(self.parse("TOKEN=abc==\n"), {"TOKEN": "abc=="})

    def test_missing_file_is_empty_not_an_error(self):
        self.assertEqual(config.parse_env_file(Path("/nonexistent/.env")), {})

    def test_strips_trailing_comments(self):
        """
        REGRESSION GUARD (2026-07-31). .env.example documents five
        tunables with an inline note, and the README says
        `cp .env.example .env`. Without stripping, the value was the
        whole string including the comment: numeric settings printed a
        "bad value" warning on every pass and silently used the default,
        and a commented NTFY_TOPIC would have published to a topic that
        does not exist.
        """
        self.assertEqual(
            self.parse("ALPHA=3.4   # -> ceiling at ~14 days\n"), {"ALPHA": "3.4"}
        )

    def test_a_hash_inside_a_value_is_not_a_comment(self):
        # Only whitespace-then-hash starts a comment, so a topic or
        # password containing a bare "#" survives.
        self.assertEqual(self.parse("TOPIC=school#1\n"), {"TOPIC": "school#1"})

    def test_a_quoted_value_keeps_everything_including_hashes(self):
        # The escape hatch for a secret that really does contain " #".
        self.assertEqual(
            self.parse('SECRET="a b #c"\n'), {"SECRET": "a b #c"}
        )

    def test_every_value_in_the_shipped_example_parses_cleanly(self):
        """
        .env.example is what setup copies, so anything unparseable in it
        is a trap for the next person to follow the README.
        """
        example = Path(__file__).resolve().parent.parent / ".env.example"
        for key, value in config.parse_env_file(example).items():
            self.assertNotIn("#", value, f"{key} still carries its comment")


class Accessors(unittest.TestCase):
    def test_require_raises_with_actionable_message(self):
        with self.assertRaises(RuntimeError) as ctx:
            config.require("DEFINITELY_NOT_SET_ANYWHERE")
        message = str(ctx.exception)
        self.assertIn("DEFINITELY_NOT_SET_ANYWHERE", message)
        self.assertIn("generate_plist.py", message)  # tells Peter the fix

    def test_optional_falls_back_to_default(self):
        self.assertEqual(config.optional("ALSO_NOT_SET", "fallback"), "fallback")

    def test_flag_parsing(self):
        import os

        for raw, expected in [("true", True), ("1", True), ("yes", True), ("on", True),
                              ("false", False), ("0", False), ("no", False)]:
            os.environ["SCHOOLSYNC_TEST_FLAG"] = raw
            self.assertIs(config.flag("SCHOOLSYNC_TEST_FLAG"), expected, raw)
        del os.environ["SCHOOLSYNC_TEST_FLAG"]

    def test_flag_default_when_unset(self):
        self.assertIs(config.flag("SCHOOLSYNC_UNSET_FLAG", default=True), True)
        self.assertIs(config.flag("SCHOOLSYNC_UNSET_FLAG", default=False), False)


class PlaceholderDetection(unittest.TestCase):
    def test_detects_unfilled_values(self):
        for value in ["", "sk-ant-xxxx", "REPLACE_WITH_A_RANDOM_PRIVATE_TOPIC_NAME", "changeme"]:
            self.assertTrue(config.is_placeholder(value), value)

    def test_accepts_real_looking_values(self):
        for value in ["sk-ant-api03-realkey", "ntfy-9f3a2b81c4"]:
            self.assertFalse(config.is_placeholder(value), value)


if __name__ == "__main__":
    unittest.main()
