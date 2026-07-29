"""
Tests for the mark-done command queue (shared/commands.py).

poll_mark_done reads a public-ish ntfy topic — anyone who knows or
guesses NTFY_COMMAND_TOPIC can post to it — so the page-id validation
here is a real security boundary, not defensive programming for its own
sake. These tests mock the network, since this module's only I/O is a
GET to ntfy and a call into notion_client.mark_done.
"""

import os
import unittest
from unittest import mock

import tests.context  # noqa: F401

from shared import commands


def ntfy_response(*messages: str) -> str:
    """Build the raw newline-delimited JSON body ntfy's /json?poll=1 returns."""
    import json

    lines = [json.dumps({"event": "message", "message": m}) for m in messages]
    # ntfy also emits a trailing "open"/keepalive-style line on some
    # endpoints; a blank line in the body must not crash parsing.
    return "\n".join(lines) + "\n"


class PollMarkDone(unittest.TestCase):
    def setUp(self):
        self._original = os.environ.get("NTFY_COMMAND_TOPIC")
        os.environ["NTFY_COMMAND_TOPIC"] = "test-command-topic"

    def tearDown(self):
        if self._original is None:
            os.environ.pop("NTFY_COMMAND_TOPIC", None)
        else:
            os.environ["NTFY_COMMAND_TOPIC"] = self._original

    def test_unconfigured_topic_returns_empty_without_a_network_call(self):
        os.environ["NTFY_COMMAND_TOPIC"] = ""
        with mock.patch("shared.commands.requests.get") as get:
            result = commands.poll_mark_done("2m")
        get.assert_not_called()
        self.assertEqual(result, [])

    def test_valid_dashed_uuid_is_collected(self):
        page_id = "3abb6829-2d91-80d1-b24c-e32c1b7bd5ee"
        resp = mock.Mock(text=ntfy_response(page_id))
        resp.raise_for_status = mock.Mock()
        with mock.patch("shared.commands.requests.get", return_value=resp):
            self.assertEqual(commands.poll_mark_done("2m"), [page_id])

    def test_valid_undashed_uuid_is_collected(self):
        page_id = "3abb68292d9180d1b24ce32c1b7bd5ee"
        resp = mock.Mock(text=ntfy_response(page_id))
        resp.raise_for_status = mock.Mock()
        with mock.patch("shared.commands.requests.get", return_value=resp):
            self.assertEqual(commands.poll_mark_done("2m"), [page_id])

    def test_malformed_payload_is_dropped_not_raised(self):
        """
        This is the one path an arbitrary internet POST becomes part of
        a Notion API URL -- garbage must be rejected, never passed
        through, and never crash the poll for other valid commands.
        """
        good_id = "3abb6829-2d91-80d1-b24c-e32c1b7bd5ee"
        resp = mock.Mock(text=ntfy_response("DROP TABLE pages;--", good_id, ""))
        resp.raise_for_status = mock.Mock()
        with mock.patch("shared.commands.requests.get", return_value=resp):
            self.assertEqual(commands.poll_mark_done("2m"), [good_id])

    def test_non_message_events_are_ignored(self):
        resp = mock.Mock(text='{"event": "open"}\n{"event": "keepalive"}\n')
        resp.raise_for_status = mock.Mock()
        with mock.patch("shared.commands.requests.get", return_value=resp):
            self.assertEqual(commands.poll_mark_done("2m"), [])

    def test_unparseable_json_line_is_skipped(self):
        good_id = "3abb6829-2d91-80d1-b24c-e32c1b7bd5ee"
        resp = mock.Mock(text="not json at all\n" + ntfy_response(good_id))
        resp.raise_for_status = mock.Mock()
        with mock.patch("shared.commands.requests.get", return_value=resp):
            self.assertEqual(commands.poll_mark_done("2m"), [good_id])

    def test_network_failure_returns_empty_never_raises(self):
        import requests

        with mock.patch(
            "shared.commands.requests.get", side_effect=requests.RequestException("boom")
        ):
            self.assertEqual(commands.poll_mark_done("2m"), [])


class ApplyMarkDone(unittest.TestCase):
    def test_applies_each_id(self):
        with mock.patch.object(commands, "notion_client") as nc:
            applied, errors = commands.apply_mark_done(["a", "b"])
        self.assertEqual(applied, 2)
        self.assertEqual(errors, [])
        self.assertEqual(nc.mark_done.call_count, 2)

    def test_duplicate_ids_collapsed(self):
        """Marking the same page Done twice is wasted work, not wrong
        work -- but there's no reason to do it twice in one batch."""
        with mock.patch.object(commands, "notion_client") as nc:
            applied, _ = commands.apply_mark_done(["a", "a", "a"])
        self.assertEqual(applied, 1)
        self.assertEqual(nc.mark_done.call_count, 1)

    def test_one_bad_id_does_not_stop_the_rest(self):
        with mock.patch.object(commands, "notion_client") as nc:
            nc.mark_done.side_effect = [RuntimeError("Notion 404"), None]
            applied, errors = commands.apply_mark_done(["bad", "good"])
        self.assertEqual(applied, 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("bad", errors[0])

    def test_empty_batch(self):
        with mock.patch.object(commands, "notion_client") as nc:
            applied, errors = commands.apply_mark_done([])
        self.assertEqual((applied, errors), (0, []))
        nc.mark_done.assert_not_called()


if __name__ == "__main__":
    unittest.main()
