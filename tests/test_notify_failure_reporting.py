"""
Tests for publishing why a cloud run went red.

GitHub job logs need admin auth — the REST API returns 403 for an
anonymous request even on a public repo. So when `Run cloud sync` fails,
the reason is readable by nobody unless Peter is signed in on a laptop.
Seven runs failed between 2026-08-03 and 2026-08-05 and the cause was
unknowable from outside.

The property that matters most here is the redaction one: this publishes
exception text to an UNAUTHENTICATED ntfy topic, and notion_client
deliberately surfaces Notion's 4xx response bodies in its exception
messages.
"""

import os
import unittest
from unittest import mock

import tests.context  # noqa: F401

from shared import config, notify


class OpsTopicResolution(unittest.TestCase):
    def test_derived_from_the_command_topic(self):
        with mock.patch.dict(
            os.environ, {"NTFY_COMMAND_TOPIC": "cmd-abc", "NTFY_ERROR_TOPIC": ""}
        ):
            self.assertEqual(notify.ops_topic(), "cmd-abc" + notify.OPS_TOPIC_SUFFIX)

    def test_an_explicit_topic_wins(self):
        with mock.patch.dict(
            os.environ, {"NTFY_COMMAND_TOPIC": "cmd-abc", "NTFY_ERROR_TOPIC": "ops-xyz"}
        ):
            self.assertEqual(notify.ops_topic(), "ops-xyz")

    def test_unconfigured_is_none_not_an_error(self):
        """Only when NO topic of any kind is set — see TheFallbackChain."""
        with mock.patch.dict(
            os.environ,
            {"NTFY_COMMAND_TOPIC": "", "NTFY_ERROR_TOPIC": "", "NTFY_TOPIC": ""},
        ):
            self.assertIsNone(notify.ops_topic())

    def test_it_is_never_the_command_topic_itself(self):
        """
        commands.poll_mark_done reads the command topic and prints
        'ignoring malformed mark-done payload' for anything that is not a
        UUID. Publishing error text there would add a line of noise to
        sync-error.log on every pass inside the poll window.
        """
        with mock.patch.dict(
            os.environ, {"NTFY_COMMAND_TOPIC": "cmd-abc", "NTFY_ERROR_TOPIC": ""}
        ):
            self.assertNotEqual(notify.ops_topic(), "cmd-abc")


class Redaction(unittest.TestCase):
    """The topic is unauthenticated. Exception text is not safe by default."""

    def test_a_secret_value_is_replaced_by_its_name(self):
        with mock.patch.dict(os.environ, {"NOTION_TOKEN": "secret_abcdef123456"}):
            out = config.redact("Notion API 400: token secret_abcdef123456 rejected")
        self.assertNotIn("secret_abcdef123456", out)
        self.assertIn("<NOTION_TOKEN>", out)

    def test_every_sensitive_key_is_covered(self):
        env = {key: f"value-of-{key.lower()}" for key in config.SENSITIVE_KEYS}
        text = " ".join(env.values())
        with mock.patch.dict(os.environ, env):
            out = config.redact(text)
        for key, value in env.items():
            self.assertNotIn(value, out, f"{key} leaked")

    def test_school_email_hints_are_redacted_too(self):
        """PII about a minor, not a credential — same treatment."""
        with mock.patch.dict(os.environ, {"SCHOOL_EMAIL_HINTS": "realschool.example"}):
            self.assertNotIn(
                "realschool.example", config.redact("from:realschool.example failed")
            )

    def test_short_values_are_left_alone(self):
        """Redacting an 8-character-or-shorter value would mangle
        ordinary text without protecting anything."""
        with mock.patch.dict(os.environ, {"NOTION_DB_ID": "abc"}):
            self.assertEqual(config.redact("abcdef"), "abcdef")

    def test_ordinary_text_survives(self):
        self.assertEqual(config.redact("Notion API 502"), "Notion API 502")


class PublishFailure(unittest.TestCase):
    def _publish(self, summary, already=False, post_ok=True, topic="cmd-abc"):
        with (
            mock.patch.dict(
                os.environ,
                {
                    "NTFY_COMMAND_TOPIC": topic,
                    "NTFY_ERROR_TOPIC": "",
                    "NTFY_SERVER": "",
                    # Blanked so `topic=""` really means "nothing
                    # configured" rather than falling back to the main
                    # topic, which is the production default.
                    "NTFY_TOPIC": "",
                },
            ),
            mock.patch.object(notify, "_already_reported", return_value=already),
            mock.patch.object(notify, "requests") as rq,
        ):
            if not post_ok:
                rq.RequestException = Exception
                rq.post.side_effect = Exception("boom")
            result = notify.publish_failure(summary)
            return result, rq

    def test_publishes_when_configured(self):
        result, rq = self._publish("gmail_scan: 500")
        self.assertTrue(result)
        rq.post.assert_called_once()

    def test_the_body_is_redacted_before_it_leaves(self):
        with mock.patch.dict(os.environ, {"NOTION_TOKEN": "secret_abcdef123456"}):
            _, rq = self._publish("token secret_abcdef123456 rejected")
        body = rq.post.call_args.kwargs["data"].decode()
        self.assertNotIn("secret_abcdef123456", body)

    def test_the_body_is_truncated(self):
        _, rq = self._publish("x" * 5000)
        self.assertLessEqual(
            len(rq.post.call_args.kwargs["data"]), notify.MAX_FAILURE_CHARS
        )

    def test_rate_limited_within_the_window(self):
        """A persistent fault at a two-minute cadence must not publish
        thirty times an hour."""
        result, rq = self._publish("gmail_scan: 500", already=True)
        self.assertFalse(result)
        rq.post.assert_not_called()

    def test_unconfigured_is_a_silent_no_op(self):
        result, rq = self._publish("gmail_scan: 500", topic="")
        self.assertFalse(result)
        rq.post.assert_not_called()

    def test_a_publish_failure_is_swallowed_not_raised(self):
        """Reporting a failure must never become a second failure."""
        result, _ = self._publish("gmail_scan: 500", post_ok=False)
        self.assertFalse(result)

    def test_it_does_not_buzz(self):
        _, rq = self._publish("gmail_scan: 500")
        self.assertEqual(rq.post.call_args.kwargs["headers"]["Priority"], "1")



class TheFallbackChain(unittest.TestCase):
    """
    Regression guard for the reason this feature was inert on the day it
    shipped: NTFY_COMMAND_TOPIC is deliberately not a GitHub secret, so
    in the cloud — the only place cloud_sync runs — ops_topic() returned
    None and publish_failure silently did nothing through 36 failed runs.
    """

    def test_falls_back_to_the_main_topic_when_the_command_topic_is_absent(self):
        with mock.patch.dict(
            os.environ,
            {"NTFY_ERROR_TOPIC": "", "NTFY_COMMAND_TOPIC": "", "NTFY_TOPIC": "main-abc"},
        ):
            self.assertEqual(notify.ops_topic(), "main-abc" + notify.OPS_TOPIC_SUFFIX)

    def test_it_is_never_the_main_topic_itself(self):
        """Peter's phone IS subscribed to NTFY_TOPIC. Error text must
        never land there."""
        with mock.patch.dict(
            os.environ,
            {"NTFY_ERROR_TOPIC": "", "NTFY_COMMAND_TOPIC": "", "NTFY_TOPIC": "main-abc"},
        ):
            self.assertNotEqual(notify.ops_topic(), "main-abc")

    def test_the_command_topic_still_wins_over_the_main_topic(self):
        with mock.patch.dict(
            os.environ,
            {"NTFY_ERROR_TOPIC": "", "NTFY_COMMAND_TOPIC": "cmd", "NTFY_TOPIC": "main"},
        ):
            self.assertEqual(notify.ops_topic(), "cmd" + notify.OPS_TOPIC_SUFFIX)

    def test_only_a_total_absence_of_topics_disables_it(self):
        with mock.patch.dict(
            os.environ,
            {"NTFY_ERROR_TOPIC": "", "NTFY_COMMAND_TOPIC": "", "NTFY_TOPIC": ""},
        ):
            self.assertIsNone(notify.ops_topic())


if __name__ == "__main__":
    unittest.main()
