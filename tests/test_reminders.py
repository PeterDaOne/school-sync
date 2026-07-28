"""
Tests for the reminder cadence engine.

This is the module worth testing: it's pure logic with no I/O, it
decides whether Peter's phone buzzes, and every bug it has ever had was
found by manually waiting for a notification that did or didn't arrive.
"""

import unittest
from datetime import timedelta

from tests.context import at

from shared import reminders
from shared.reminders import Cadence

# Fixed cadence so tests never depend on .env or on the env of whatever
# machine is running them.
CADENCE = Cadence(
    quiet_start=reminders.dtime(0, 0),
    quiet_end=reminders.dtime(5, 0),
    interval_hours=24.0,
    soon_interval_hours=4.0,
    urgent_interval_hours=2.0,
)


def item(**overrides) -> dict:
    base = {
        "id": "page-1",
        "name": "Algebra Work",
        "type_name": "Assignments",
        "due_date": "2026-08-26",
        "is_complete": False,
        "last_reminded": None,
        "created_time": "2026-07-01T00:00:00.000Z",
        "external_id": None,
        "url": "https://notion.so/page-1",
    }
    base.update(overrides)
    return base


class QuietHours(unittest.TestCase):
    def test_inside_normal_window(self):
        self.assertTrue(CADENCE.in_quiet_hours(at("2026-07-28T02:00")))

    def test_outside_normal_window(self):
        self.assertFalse(CADENCE.in_quiet_hours(at("2026-07-28T09:00")))

    def test_end_is_exclusive(self):
        # 05:00 is when quiet hours END, so it must not be suppressed —
        # otherwise a reminder held overnight waits an extra pass.
        self.assertFalse(CADENCE.in_quiet_hours(at("2026-07-28T05:00")))

    def test_window_wrapping_midnight(self):
        night = Cadence(quiet_start=reminders.dtime(21, 30), quiet_end=reminders.dtime(7, 0))
        self.assertTrue(night.in_quiet_hours(at("2026-07-28T23:00")))
        self.assertTrue(night.in_quiet_hours(at("2026-07-28T03:00")))
        self.assertFalse(night.in_quiet_hours(at("2026-07-28T12:00")))

    def test_equal_start_and_end_disables_quiet_hours(self):
        always = Cadence(quiet_start=reminders.dtime(0, 0), quiet_end=reminders.dtime(0, 0))
        self.assertFalse(always.in_quiet_hours(at("2026-07-28T02:00")))


class UrgencyTiers(unittest.TestCase):
    def test_far_future_uses_daily_interval(self):
        self.assertEqual(CADENCE.interval_for(10), 24.0)

    def test_boundary_at_three_days_is_soon_not_daily(self):
        # "> 3 days" is daily, so exactly 3.0 falls into the soon tier.
        self.assertEqual(CADENCE.interval_for(3.0), 4.0)
        self.assertEqual(CADENCE.interval_for(3.01), 24.0)

    def test_one_day_is_soon(self):
        self.assertEqual(CADENCE.interval_for(1.0), 4.0)

    def test_under_a_day_is_urgent(self):
        self.assertEqual(CADENCE.interval_for(0.5), 2.0)

    def test_overdue_is_urgent(self):
        self.assertEqual(CADENCE.interval_for(-5), 2.0)


class DueDateResolution(unittest.TestCase):
    def test_date_only_is_end_of_day_local_not_utc(self):
        """
        Regression test for the six-hour bug.

        A date-only Notion due date means "end of that day where Peter
        lives". Treating the naive value as UTC made every such item go
        overdue at 17:59 Mountain instead of 23:59, shifting every
        urgency tier boundary with it.
        """
        due = reminders.due_datetime(item(due_date="2026-07-30"))
        self.assertEqual(due.hour, 23)
        self.assertEqual(due.minute, 59)
        self.assertEqual(due.utcoffset(), timedelta(hours=-6))  # MDT

    def test_offset_aware_datetime_is_preserved(self):
        due = reminders.due_datetime(item(due_date="2026-07-28T16:00:00.000-06:00"))
        self.assertEqual(due.hour, 16)
        self.assertEqual(due.utcoffset(), timedelta(hours=-6))

    def test_missing_due_date(self):
        self.assertIsNone(reminders.due_datetime(item(due_date=None)))


class CaptureNotification(unittest.TestCase):
    def test_fires_once_when_never_reminded(self):
        msg = reminders.due_for_reminder(
            item(last_reminded=None), at("2026-07-28T09:00"), cadence=CADENCE
        )
        self.assertEqual(msg, "New assignment added: Algebra Work, due Aug 26.")

    def test_includes_time_when_due_date_has_one(self):
        msg = reminders.due_for_reminder(
            item(last_reminded=None, due_date="2026-07-28T16:00:00.000-06:00"),
            at("2026-07-28T09:00"),
            cadence=CADENCE,
        )
        self.assertEqual(msg, "New assignment added: Algebra Work, due Jul 28, 4:00 PM.")

    def test_omits_due_clause_when_there_is_no_due_date(self):
        msg = reminders.due_for_reminder(
            item(last_reminded=None, due_date=None), at("2026-07-28T09:00"), cadence=CADENCE
        )
        self.assertEqual(msg, "New assignment added: Algebra Work.")

    def test_uses_type_label(self):
        msg = reminders.due_for_reminder(
            item(last_reminded=None, type_name="Tasks"), at("2026-07-28T09:00"), cadence=CADENCE
        )
        self.assertTrue(msg.startswith("New task added:"))

    def test_suppressed_during_quiet_hours(self):
        self.assertIsNone(
            reminders.due_for_reminder(
                item(last_reminded=None), at("2026-07-28T02:00"), cadence=CADENCE
            )
        )


class CompletedItems(unittest.TestCase):
    def test_completed_assignment_never_reminds(self):
        self.assertIsNone(
            reminders.due_for_reminder(
                item(is_complete=True, last_reminded=None), at("2026-07-28T09:00"), cadence=CADENCE
            )
        )

    def test_completed_event_never_reminds(self):
        """Pins the documented behavior: Status gates every type, Events included."""
        self.assertIsNone(
            reminders.due_for_reminder(
                item(
                    type_name="Events",
                    is_complete=True,
                    due_date="2026-07-28T18:00:00.000-06:00",
                    last_reminded="2026-07-20T00:00:00+00:00",
                ),
                at("2026-07-28T17:30"),
                cadence=CADENCE,
            )
        )


class RecurringReminders(unittest.TestCase):
    def test_silent_before_the_interval_elapses(self):
        self.assertIsNone(
            reminders.due_for_reminder(
                item(due_date="2026-07-29", last_reminded="2026-07-28T15:00:00+00:00"),
                at("2026-07-28T10:00"),  # 16:00 UTC — only one hour later
                cadence=CADENCE,
            )
        )

    def test_fires_once_the_urgent_interval_elapses(self):
        msg = reminders.due_for_reminder(
            item(due_date="2026-07-28", last_reminded="2026-07-28T12:00:00+00:00"),
            at("2026-07-28T09:00"),  # 15:00 UTC — three hours later
            cadence=CADENCE,
        )
        self.assertEqual(msg, "Reminder: Algebra Work due Jul 28.")

    def test_overdue_wording(self):
        msg = reminders.due_for_reminder(
            item(due_date="2026-07-25", last_reminded="2026-07-28T00:00:00+00:00"),
            at("2026-07-28T09:00"),
            cadence=CADENCE,
        )
        self.assertEqual(msg, "Overdue: Algebra Work was due Jul 25.")

    def test_overdue_is_uncapped(self):
        """Confirmed with Peter 2026-07-28: overdue items stay loud forever."""
        msg = reminders.due_for_reminder(
            item(due_date="2026-01-01", last_reminded="2026-07-28T00:00:00+00:00"),
            at("2026-07-28T09:00"),
            cadence=CADENCE,
        )
        self.assertIsNotNone(msg)

    def test_no_due_date_means_no_recurring_reminder(self):
        self.assertIsNone(
            reminders.due_for_reminder(
                item(due_date=None, last_reminded="2026-01-01T00:00:00+00:00"),
                at("2026-07-28T09:00"),
                cadence=CADENCE,
            )
        )

    def test_suppressed_during_quiet_hours_without_consuming_the_slot(self):
        # The caller only stamps Last Reminded when a message comes back,
        # so returning None here is what preserves the slot for later.
        self.assertIsNone(
            reminders.due_for_reminder(
                item(due_date="2026-07-28", last_reminded="2026-07-27T12:00:00+00:00"),
                at("2026-07-28T02:00"),
                cadence=CADENCE,
            )
        )


class EventReminders(unittest.TestCase):
    def event(self, **kw):
        kw.setdefault("due_date", "2026-08-07T19:00:00.000-06:00")
        return item(name="Prom", type_name="Events", **kw)

    def test_one_day_before(self):
        msg = reminders.due_for_reminder(
            self.event(last_reminded="2026-08-01T00:00:00+00:00"),
            at("2026-08-06T19:30"),
            cadence=CADENCE,
        )
        self.assertEqual(msg, "Prom is tomorrow (Aug 7, 7:00 PM).")

    def test_one_hour_before_takes_precedence(self):
        msg = reminders.due_for_reminder(
            self.event(last_reminded="2026-08-01T00:00:00+00:00"),
            at("2026-08-07T18:30"),
            cadence=CADENCE,
        )
        self.assertEqual(msg, "Prom starts in 1 hour (Aug 7, 7:00 PM).")

    def test_each_event_reminder_fires_only_once(self):
        # Already reminded after the day-before mark: nothing more until
        # the hour-before mark arrives.
        self.assertIsNone(
            reminders.due_for_reminder(
                self.event(last_reminded="2026-08-06T20:00:00-06:00"),
                at("2026-08-07T09:00"),
                cadence=CADENCE,
            )
        )

    def test_silent_well_before_the_event(self):
        self.assertIsNone(
            reminders.due_for_reminder(
                self.event(last_reminded="2026-08-01T00:00:00+00:00"),
                at("2026-08-03T12:00"),
                cadence=CADENCE,
            )
        )

    def test_event_without_due_date(self):
        self.assertIsNone(
            reminders.due_for_reminder(
                self.event(due_date=None, last_reminded="2026-08-01T00:00:00+00:00"),
                at("2026-08-07T18:30"),
                cadence=CADENCE,
            )
        )


class CloudTakeoverLag(unittest.TestCase):
    """
    The lag is what keeps local_sync and cloud_sync from both sending
    the same reminder. cloud_sync only reports a reminder that came due
    at least LAG ago — which only happens when the Mac was asleep.
    """

    LAG = timedelta(minutes=10)

    def test_local_path_fires_immediately(self):
        msg = reminders.due_for_reminder(
            item(due_date="2026-07-28", last_reminded="2026-07-28T13:00:00+00:00"),
            at("2026-07-28T09:01"),  # 15:01 UTC — just past the 2h urgent interval
            cadence=CADENCE,
        )
        self.assertIsNotNone(msg)

    def test_cloud_path_defers_on_a_freshly_due_reminder(self):
        self.assertIsNone(
            reminders.due_for_reminder(
                item(due_date="2026-07-28", last_reminded="2026-07-28T13:00:00+00:00"),
                at("2026-07-28T09:01"),
                cadence=CADENCE,
                lag=self.LAG,
            )
        )

    def test_cloud_path_takes_over_once_the_lag_passes(self):
        msg = reminders.due_for_reminder(
            item(due_date="2026-07-28", last_reminded="2026-07-28T13:00:00+00:00"),
            at("2026-07-28T09:15"),  # 15:15 UTC — due 15 minutes ago
            cadence=CADENCE,
            lag=self.LAG,
        )
        self.assertIsNotNone(msg)

    def test_cloud_capture_waits_for_a_newly_created_page(self):
        # Typed by hand a moment ago (no External ID) — local_sync may
        # already be about to announce it, so the cloud holds off.
        self.assertIsNone(
            reminders.due_for_reminder(
                item(last_reminded=None, created_time="2026-07-28T15:05:00.000Z"),
                at("2026-07-28T09:10"),  # 15:10 UTC — page is 5 minutes old
                cadence=CADENCE,
                lag=self.LAG,
            )
        )

    def test_script_captured_items_announce_immediately(self):
        """
        An item the Gmail/Classroom sweep just created carries an
        External ID and must notify on the very same run. It didn't
        exist on local_sync's last pass, so there is no race to lose —
        and waiting a whole extra cron cycle to say "new assignment
        posted" defeats the point of the capture sweeps.
        """
        msg = reminders.due_for_reminder(
            item(
                last_reminded=None,
                created_time="2026-07-28T15:09:30.000Z",  # 30 seconds old
                external_id="classroom:123:456",
            ),
            at("2026-07-28T09:10"),
            cadence=CADENCE,
            lag=self.LAG,
        )
        self.assertEqual(msg, "New assignment added: Algebra Work, due Aug 26.")

    def test_script_captured_items_still_respect_quiet_hours(self):
        self.assertIsNone(
            reminders.due_for_reminder(
                item(
                    last_reminded=None,
                    created_time="2026-07-28T08:00:00.000Z",
                    external_id="gmail:abc123",
                ),
                at("2026-07-28T02:00"),
                cadence=CADENCE,
                lag=self.LAG,
            )
        )

    def test_script_captured_items_still_respect_completion(self):
        self.assertIsNone(
            reminders.due_for_reminder(
                item(last_reminded=None, external_id="gmail:abc123", is_complete=True),
                at("2026-07-28T09:10"),
                cadence=CADENCE,
                lag=self.LAG,
            )
        )

    def test_cloud_capture_fires_for_an_older_page(self):
        msg = reminders.due_for_reminder(
            item(last_reminded=None, created_time="2026-07-28T14:00:00.000Z"),
            at("2026-07-28T09:10"),
            cadence=CADENCE,
            lag=self.LAG,
        )
        self.assertIsNotNone(msg)

    def test_quiet_hours_use_real_now_not_the_lagged_time(self):
        """
        The lag shifts cadence math, never the delivery gate. At 00:05
        the lagged time is 23:55 — outside quiet hours — but it is
        genuinely the middle of the night and must stay silent.
        """
        self.assertIsNone(
            reminders.due_for_reminder(
                item(due_date="2026-07-28", last_reminded="2026-07-27T12:00:00+00:00"),
                at("2026-07-28T00:05"),
                cadence=CADENCE,
                lag=self.LAG,
            )
        )


class ConfigParsing(unittest.TestCase):
    def test_zero_interval_is_rejected(self):
        """A zero interval would re-fire every 60-second pass, forever."""
        self.assertEqual(reminders._parse_float("0", 24.0, "TEST"), 24.0)

    def test_negative_interval_is_rejected(self):
        self.assertEqual(reminders._parse_float("-3", 24.0, "TEST"), 24.0)

    def test_garbage_interval_falls_back(self):
        self.assertEqual(reminders._parse_float("soon", 24.0, "TEST"), 24.0)

    def test_valid_interval_is_used(self):
        self.assertEqual(reminders._parse_float("6", 24.0, "TEST"), 6.0)

    def test_garbage_quiet_hours_falls_back(self):
        self.assertEqual(reminders._parse_hhmm("nope", "00:00"), reminders.dtime(0, 0))

    def test_valid_quiet_hours_parsed(self):
        self.assertEqual(reminders._parse_hhmm("21:30", "00:00"), reminders.dtime(21, 30))


if __name__ == "__main__":
    unittest.main()
