"""
Tests for the Notion -> Google Calendar shapes.

This module had ZERO coverage until 2026-07-31, which was the largest
remaining gap against this project's own stated philosophy (concentrate
tests on pure logic with no I/O). `_event_times` qualifies exactly: it is
pure, it is the source of the event shapes Google either accepts or 400s
on, and it encodes two subtleties that have each cost real time —
Google's exclusive all-day end date, and the timed/all-day split.

upsert_event's delete-on-complete and delete-on-cleared-due-date paths
are covered too. That second one was a real bug: clearing a due date in
Notion used to orphan the Calendar event forever, with nothing left
pointing at it.
"""

import unittest
from unittest import mock

import tests.context  # noqa: F401

from shared import calendar_client


class EventTimes(unittest.TestCase):
    def test_all_day_end_date_is_exclusive(self):
        """
        REGRESSION GUARD. Google's all-day `end.date` is EXCLUSIVE, so a
        one-day event ends on the FOLLOWING day. end == start is accepted
        by the API but is a zero-length span, and clients other than
        Google's own web UI render it inconsistently.
        """
        start, end = calendar_client._event_times("2026-08-26")
        self.assertEqual(start, {"date": "2026-08-26"})
        self.assertEqual(end, {"date": "2026-08-27"})

    def test_all_day_uses_date_not_datetime(self):
        # Google rejects a full datetime in the `date` field outright.
        start, end = calendar_client._event_times("2026-08-26")
        self.assertNotIn("dateTime", start)
        self.assertNotIn("dateTime", end)

    def test_all_day_crossing_a_month_boundary(self):
        _, end = calendar_client._event_times("2026-08-31")
        self.assertEqual(end, {"date": "2026-09-01"})

    def test_all_day_crossing_a_year_boundary(self):
        _, end = calendar_client._event_times("2026-12-31")
        self.assertEqual(end, {"date": "2027-01-01"})

    def test_timed_event_gets_a_visible_block(self):
        """
        A due date is a deadline, not a meeting, but a zero-length timed
        event renders as an easy-to-miss sliver.
        """
        start, end = calendar_client._event_times("2026-07-28T16:00:00.000-06:00")
        self.assertEqual(start["dateTime"], "2026-07-28T16:00:00-06:00")
        self.assertEqual(end["dateTime"], "2026-07-28T16:30:00-06:00")

    def test_timed_event_carries_the_timezone(self):
        start, end = calendar_client._event_times("2026-07-28T16:00:00.000-06:00")
        self.assertIn("timeZone", start)
        self.assertIn("timeZone", end)
        self.assertEqual(start["timeZone"], end["timeZone"])

    def test_timed_block_can_roll_past_midnight(self):
        # 23:50 + 30 min lands on the next calendar day; the offset must
        # survive rather than silently wrapping to the same date.
        start, end = calendar_client._event_times("2026-07-28T23:50:00.000-06:00")
        self.assertTrue(start["dateTime"].startswith("2026-07-28T23:50"))
        self.assertTrue(end["dateTime"].startswith("2026-07-29T00:20"))

    def test_the_offset_in_the_notion_value_is_preserved(self):
        # Notion sends the offset it stored; reinterpreting it in another
        # zone is the exact class of bug shared/timeutil.py exists for.
        start, _ = calendar_client._event_times("2026-07-28T16:00:00.000+00:00")
        self.assertTrue(start["dateTime"].endswith("+00:00"))


class UpsertEvent(unittest.TestCase):
    """The create / update / delete decision, with the API mocked out."""

    def setUp(self):
        self.service = mock.MagicMock()
        patches = [
            mock.patch.object(calendar_client, "_service", return_value=self.service),
            mock.patch.object(calendar_client, "calendar_id", return_value="cal"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def item(self, **over):
        base = {
            "id": "page-1", "name": "Essay", "category": "AP Lang",
            "due_date": "2026-08-26", "is_complete": False,
            "url": "https://notion.so/page-1",
        }
        base.update(over)
        return base

    def _existing(self, event):
        with mock.patch.object(
            calendar_client, "find_event_by_notion_id", return_value=event
        ):
            calendar_client.upsert_event(self.item(**self._over))

    def test_a_completed_item_deletes_its_event(self):
        self._over = {"is_complete": True}
        self._existing({"id": "ev1"})
        self.service.events().delete.assert_called_with(calendarId="cal", eventId="ev1")

    def test_a_cleared_due_date_deletes_its_event(self):
        """
        REGRESSION GUARD: clearing a due date in Notion used to orphan
        the Calendar event forever, with nothing left pointing at it.
        """
        self._over = {"due_date": None}
        self._existing({"id": "ev1"})
        self.service.events().delete.assert_called_with(calendarId="cal", eventId="ev1")

    def test_a_completed_item_with_no_event_does_nothing(self):
        self._over = {"is_complete": True}
        self.service.reset_mock()
        self._existing(None)
        self.service.events().delete.assert_not_called()

    def test_a_new_item_is_inserted_not_updated(self):
        self._over = {}
        self.service.reset_mock()
        self._existing(None)
        self.service.events().insert.assert_called_once()
        self.service.events().update.assert_not_called()

    def test_an_existing_item_is_updated_not_duplicated(self):
        self._over = {}
        self.service.reset_mock()
        self._existing({"id": "ev1"})
        self.service.events().update.assert_called_once()
        self.service.events().insert.assert_not_called()

    def test_the_event_is_tagged_with_the_notion_page_id(self):
        """This tag is the whole idempotency mechanism — it is what stops
        local_sync and the cloud creating two events for one item."""
        self._over = {}
        self.service.reset_mock()
        self._existing(None)
        body = self.service.events().insert.call_args.kwargs["body"]
        self.assertEqual(
            body["extendedProperties"]["private"]["notion_id"], "notion-page-1"
        )

    def test_a_categoryless_item_still_gets_a_summary_prefix(self):
        self._over = {"category": None}
        self.service.reset_mock()
        self._existing(None)
        body = self.service.events().insert.call_args.kwargs["body"]
        self.assertEqual(body["summary"], "School: Essay")


if __name__ == "__main__":
    unittest.main()
