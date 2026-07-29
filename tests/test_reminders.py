"""
Tests for the reminder cadence engine.

This is the module worth testing: it's pure logic with no I/O, it
decides whether Peter's phone buzzes, and every bug it has ever had was
found by manually waiting for a notification that did or didn't arrive.

Rewritten 2026-07-29 for the continuous-formula cadence (replacing three
fixed tiers) and the new title/body message shape. Fixture CADENCE uses
round, hand-verifiable numbers rather than the production defaults, and
sets jitter_fraction=0.0 everywhere except the dedicated JitterFactor
class, so every expected interval in this file is an exact number, not
an approximation.
"""

import unittest
from datetime import timedelta

from tests.context import at

from shared import reminders
from shared.reminders import Cadence

CADENCE = Cadence(
    quiet_start=reminders.dtime(0, 0),
    quiet_end=reminders.dtime(5, 0),
    assignment_alpha=4.0,
    assignment_floor=2.0,
    assignment_ceiling=48.0,  # reached at 12 days out
    task_alpha=20.0,
    task_floor=1.0,  # deliberately more aggressive than Assignments' floor
    task_ceiling=60.0,  # reached at 3 days out
    priority_multiplier={"High": 0.5, "Medium": 1.0, "Low": 2.0},
    jitter_fraction=0.0,  # disabled -- makes every expected value exact
    max_per_pass=3,
    event_reminder_hour=reminders.dtime(7, 0),
)


def item(**overrides) -> dict:
    base = {
        "id": "page-1",
        "name": "Algebra Work",
        "type_name": "Assignments",
        "class_name": None,
        "priority": None,
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


class CadenceFormula(unittest.TestCase):
    """
    Cadence.interval_hours() replaced the three fixed tiers 2026-07-29.
    One formula, no branch between "not yet due" and "overdue" -- see
    the module docstring for the derivation. These pin the formula
    itself; due_for_reminder's end-to-end wiring is covered separately.
    """

    def test_assignment_scales_linearly_with_days_until(self):
        self.assertAlmostEqual(
            CADENCE.interval_hours("Assignments", 5, "Medium", "x"), 20.0
        )

    def test_assignment_clamped_to_ceiling_far_out(self):
        self.assertAlmostEqual(
            CADENCE.interval_hours("Assignments", 30, "Medium", "x"), 48.0
        )

    def test_assignment_clamped_to_floor_near_due(self):
        self.assertAlmostEqual(
            CADENCE.interval_hours("Assignments", 0.1, "Medium", "x"), 2.0
        )

    def test_assignment_overdue_uses_the_floor(self):
        self.assertAlmostEqual(
            CADENCE.interval_hours("Assignments", -5, "Medium", "x"), 2.0
        )

    def test_assignment_overdue_does_not_reaccelerate_the_longer_it_sits(self):
        shallow = CADENCE.interval_hours("Assignments", -1, "Medium", "x")
        deep = CADENCE.interval_hours("Assignments", -100, "Medium", "x")
        self.assertAlmostEqual(shallow, deep)
        self.assertAlmostEqual(deep, 2.0)

    def test_no_discontinuity_crossing_the_due_moment(self):
        just_before = CADENCE.interval_hours("Tasks", 0.001, "Medium", "x")
        just_after = CADENCE.interval_hours("Tasks", -0.001, "Medium", "x")
        self.assertAlmostEqual(just_before, just_after)

    def test_task_alpha_differs_from_assignment(self):
        self.assertAlmostEqual(CADENCE.interval_hours("Tasks", 2, "Medium", "x"), 40.0)

    def test_task_overdue_floor_is_more_aggressive_than_assignment(self):
        """
        Peter's explicit call: Assignments get nagged early (high
        ceiling reached far out), so their overdue floor doesn't need to
        be aggressive. Tasks stay quiet until close, so DO need a harder
        push once missed.
        """
        task = CADENCE.interval_hours("Tasks", -5, "Medium", "x")
        assignment = CADENCE.interval_hours("Assignments", -5, "Medium", "x")
        self.assertLess(task, assignment)
        self.assertAlmostEqual(task, 1.0)
        self.assertAlmostEqual(assignment, 2.0)

    def test_priority_multiplies_the_whole_thing_including_the_floor(self):
        self.assertAlmostEqual(CADENCE.interval_hours("Assignments", -5, "High", "x"), 1.0)
        self.assertAlmostEqual(CADENCE.interval_hours("Assignments", -5, "Medium", "x"), 2.0)
        self.assertAlmostEqual(CADENCE.interval_hours("Assignments", -5, "Low", "x"), 4.0)

    def test_missing_priority_defaults_to_medium(self):
        self.assertAlmostEqual(
            CADENCE.interval_hours("Assignments", -5, None, "x"),
            CADENCE.interval_hours("Assignments", -5, "Medium", "x"),
        )

    def test_unrecognized_priority_defaults_to_medium(self):
        self.assertAlmostEqual(
            CADENCE.interval_hours("Assignments", -5, "Whenever", "x"),
            CADENCE.interval_hours("Assignments", -5, "Medium", "x"),
        )

    def test_absolute_minimum_safety_rail(self):
        # A pathological combination of a tiny floor and an aggressive
        # priority multiplier still can't go below the hard minimum.
        tiny = Cadence(
            assignment_floor=0.01,
            priority_multiplier={"High": 0.1, "Medium": 1.0, "Low": 2.0},
            jitter_fraction=0.0,
        )
        self.assertEqual(
            tiny.interval_hours("Assignments", -5, "High", "x"),
            reminders.ABSOLUTE_MIN_INTERVAL_HOURS,
        )


class JitterFactor(unittest.TestCase):
    def test_deterministic_for_the_same_id(self):
        a = reminders._jitter_factor("page-abc", 0.25)
        b = reminders._jitter_factor("page-abc", 0.25)
        self.assertEqual(a, b)

    def test_different_ids_produce_different_factors(self):
        a = reminders._jitter_factor("page-abc", 0.25)
        b = reminders._jitter_factor("page-xyz", 0.25)
        self.assertNotEqual(a, b)

    def test_bounded_by_the_fraction(self):
        for pid in ["a", "b", "page-1", "3abb6829-2d91-80d1-b24c-e32c1b7bd5ee"]:
            factor = reminders._jitter_factor(pid, 0.25)
            self.assertGreaterEqual(factor, 0.75)
            self.assertLessEqual(factor, 1.25)

    def test_zero_fraction_disables_jitter(self):
        self.assertEqual(reminders._jitter_factor("anything", 0.0), 1.0)

    def test_breaks_up_a_shared_tier(self):
        """
        The actual bug this exists to fix: two items landing on the same
        interval at the same last_reminded moment must diverge, not
        stay phase-locked together forever.
        """
        c = Cadence(assignment_floor=2.0, jitter_fraction=0.25)
        a = c.interval_hours("Assignments", -5, "Medium", "page-a")
        b = c.interval_hours("Assignments", -5, "Medium", "page-b")
        self.assertNotEqual(a, b)


class DueDateResolution(unittest.TestCase):
    def test_date_only_is_end_of_day_local_not_utc(self):
        """
        Regression test for the six-hour bug.

        A date-only Notion due date means "end of that day where Peter
        lives". Treating the naive value as UTC made every such item go
        overdue at 17:59 Mountain instead of 23:59, shifting every
        urgency boundary with it.
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


class RelativeDue(unittest.TestCase):
    def test_today_with_time(self):
        due, now = at("2026-07-28T16:00"), at("2026-07-28T09:00")
        self.assertEqual(reminders.relative_due(due, True, now), "today at 4:00 PM")

    def test_today_without_time(self):
        due, now = at("2026-07-28T23:59"), at("2026-07-28T09:00")
        self.assertEqual(reminders.relative_due(due, False, now), "today")

    def test_tomorrow_with_time(self):
        due, now = at("2026-07-29T16:00"), at("2026-07-28T09:00")
        self.assertEqual(reminders.relative_due(due, True, now), "tomorrow at 4:00 PM")

    def test_yesterday_with_time(self):
        due, now = at("2026-07-27T16:00"), at("2026-07-28T09:00")
        self.assertEqual(reminders.relative_due(due, True, now), "yesterday at 4:00 PM")

    def test_multiple_days_out_has_no_time_suffix(self):
        due, now = at("2026-08-02T16:00"), at("2026-07-28T09:00")
        self.assertEqual(reminders.relative_due(due, True, now), "in 5 days")

    def test_multiple_days_ago_has_no_time_suffix(self):
        due, now = at("2026-07-20T16:00"), at("2026-07-28T09:00")
        self.assertEqual(reminders.relative_due(due, True, now), "8 days ago")

    def test_calendar_day_boundary_not_a_24_hour_bucket(self):
        """
        The same class of bug this codebase has shipped twice: an
        evening `now` against an early-morning `due` the very next
        calendar day is only two hours apart, but must say "tomorrow",
        not "today".
        """
        due, now = at("2026-07-29T01:00"), at("2026-07-28T23:00")
        self.assertEqual(reminders.relative_due(due, True, now), "tomorrow at 1:00 AM")


class CaptureNotification(unittest.TestCase):
    def test_fires_once_when_never_reminded(self):
        r = reminders.due_for_reminder(
            item(last_reminded=None), at("2026-07-28T09:00"), cadence=CADENCE
        )
        self.assertEqual(r.title, "New assignment")
        self.assertEqual(r.body, "Algebra Work — due Aug 26")

    def test_includes_time_when_due_date_has_one(self):
        r = reminders.due_for_reminder(
            item(last_reminded=None, due_date="2026-07-28T16:00:00.000-06:00"),
            at("2026-07-28T09:00"),
            cadence=CADENCE,
        )
        self.assertEqual(r.body, "Algebra Work — due Jul 28, 4:00 PM")

    def test_omits_due_clause_when_there_is_no_due_date(self):
        r = reminders.due_for_reminder(
            item(last_reminded=None, due_date=None), at("2026-07-28T09:00"), cadence=CADENCE
        )
        self.assertEqual(r.body, "Algebra Work")

    def test_uses_the_type_category(self):
        r = reminders.due_for_reminder(
            item(last_reminded=None, type_name="Tasks"), at("2026-07-28T09:00"), cadence=CADENCE
        )
        self.assertEqual(r.title, "New task")

    def test_class_adds_an_emoji_to_the_title_and_a_prefix_to_the_body(self):
        r = reminders.due_for_reminder(
            item(last_reminded=None, class_name="AP Stats"),
            at("2026-07-28T09:00"),
            cadence=CADENCE,
        )
        self.assertEqual(r.title, "📊 New assignment")
        self.assertTrue(r.body.startswith("AP Stats · Algebra Work"))

    def test_no_class_means_no_emoji_and_no_dangling_separator(self):
        r = reminders.due_for_reminder(
            item(last_reminded=None, class_name=None), at("2026-07-28T09:00"), cadence=CADENCE
        )
        self.assertEqual(r.title, "New assignment")
        self.assertFalse(r.body.startswith("·"))
        self.assertTrue(r.body.startswith("Algebra Work"))

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
                item(is_complete=True, last_reminded=None),
                at("2026-07-28T09:00"),
                cadence=CADENCE,
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
        now = at("2026-07-28T09:00")
        self.assertIsNone(
            reminders.due_for_reminder(
                item(
                    due_date="2026-08-15T09:00:00-06:00",  # far out -> long interval
                    last_reminded=(now - timedelta(hours=1)).isoformat(),
                ),
                now,
                cadence=CADENCE,
            )
        )

    def test_fires_once_the_interval_elapses(self):
        now = at("2026-07-28T09:00")
        r = reminders.due_for_reminder(
            item(
                due_date="2026-07-28T09:00:00-06:00",  # due exactly now -> floor interval
                last_reminded=(now - timedelta(hours=3)).isoformat(),
            ),
            now,
            cadence=CADENCE,
        )
        self.assertEqual(r.title, "Assignment reminder")
        self.assertEqual(r.body, "Algebra Work — due today at 9:00 AM")
        self.assertEqual(r.priority, 4)

    def test_overdue_wording(self):
        now = at("2026-07-28T09:00")
        r = reminders.due_for_reminder(
            item(
                due_date="2026-07-25T09:00:00-06:00",  # 3 days ago
                last_reminded=(now - timedelta(hours=5)).isoformat(),
            ),
            now,
            cadence=CADENCE,
        )
        self.assertEqual(r.title, "Assignment overdue")
        self.assertEqual(r.body, "Algebra Work — was due 3 days ago")
        self.assertEqual(r.priority, 5)

    def test_overdue_stays_loud_no_matter_how_long(self):
        """Confirmed with Peter 2026-07-28: overdue items stay loud forever."""
        now = at("2026-07-28T09:00")
        r = reminders.due_for_reminder(
            item(
                due_date="2020-01-01T09:00:00-06:00",
                last_reminded=(now - timedelta(hours=3)).isoformat(),
            ),
            now,
            cadence=CADENCE,
        )
        self.assertIsNotNone(r)
        self.assertEqual(r.priority, 5)

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

    def test_priority_changes_whether_it_fires_yet(self):
        now = at("2026-07-28T09:00")
        due = "2026-08-02T09:00:00-06:00"  # 5 days out -> raw interval 20h
        last_reminded = (now - timedelta(hours=15)).isoformat()
        high = reminders.due_for_reminder(
            item(due_date=due, last_reminded=last_reminded, priority="High"),
            now,
            cadence=CADENCE,
        )
        low = reminders.due_for_reminder(
            item(due_date=due, last_reminded=last_reminded, priority="Low"),
            now,
            cadence=CADENCE,
        )
        self.assertIsNotNone(high)  # 20h * 0.5 = 10h <= 15h elapsed
        self.assertIsNone(low)  # 20h * 2.0 = 40h > 15h elapsed


class EventReminders(unittest.TestCase):
    def event(self, **kw):
        kw.setdefault("due_date", "2026-08-07T19:00:00.000-06:00")
        return item(name="Prom", type_name="Events", **kw)

    def test_three_days_before_at_the_marker_hour(self):
        r = reminders.due_for_reminder(
            self.event(last_reminded="2026-07-01T00:00:00-06:00"),
            at("2026-08-04T07:00"),
            cadence=CADENCE,
        )
        self.assertEqual(r.title, "Event reminder")
        self.assertEqual(r.body, "Prom — in 3 days")
        self.assertEqual(r.priority, 3)

    def test_one_day_before_at_the_marker_hour(self):
        r = reminders.due_for_reminder(
            self.event(last_reminded="2026-07-01T00:00:00-06:00"),
            at("2026-08-06T07:00"),
            cadence=CADENCE,
        )
        self.assertEqual(r.body, "Prom — tomorrow at 7:00 PM")
        self.assertEqual(r.priority, 3)

    def test_morning_of_at_the_marker_hour(self):
        r = reminders.due_for_reminder(
            self.event(last_reminded="2026-07-01T00:00:00-06:00"),
            at("2026-08-07T07:00"),
            cadence=CADENCE,
        )
        self.assertEqual(r.body, "Prom — today at 7:00 PM")
        self.assertEqual(r.priority, 4)

    def test_one_hour_before_takes_precedence(self):
        r = reminders.due_for_reminder(
            self.event(last_reminded="2026-08-07T07:01:00-06:00"),  # today-tier already fired
            at("2026-08-07T18:30"),
            cadence=CADENCE,
        )
        self.assertEqual(r.body, "Prom — starts in 1 hour (Aug 7, 7:00 PM)")
        self.assertEqual(r.priority, 4)
        self.assertEqual(r.tags, "school,rotating_light")

    def test_hour_before_skipped_for_a_date_only_event(self):
        """A date-only event has no "1 hour before" to compute -- the
        hour-before tier must not fire for it, has_time or not."""
        self.assertIsNone(
            reminders.due_for_reminder(
                self.event(due_date="2026-08-07", last_reminded="2026-08-07T07:01:00-06:00"),
                at("2026-08-07T22:59"),
                cadence=CADENCE,
            )
        )

    def test_each_tier_fires_only_once(self):
        # Reminded right at the 3-day marker; an hour later, still
        # silent -- the next tier (1-day-before) hasn't arrived yet.
        self.assertIsNone(
            reminders.due_for_reminder(
                self.event(last_reminded="2026-08-04T07:00:00-06:00"),
                at("2026-08-04T08:00"),
                cadence=CADENCE,
            )
        )

    def test_silent_well_before_the_event(self):
        self.assertIsNone(
            reminders.due_for_reminder(
                self.event(last_reminded="2026-07-01T00:00:00-06:00"),
                at("2026-08-01T12:00"),
                cadence=CADENCE,
            )
        )

    def test_event_without_due_date(self):
        self.assertIsNone(
            reminders.due_for_reminder(
                self.event(due_date=None, last_reminded="2026-07-01T00:00:00-06:00"),
                at("2026-08-07T18:30"),
                cadence=CADENCE,
            )
        )

    def test_silent_once_the_event_has_already_started(self):
        """
        Regression guard: an early event (6am) must not have its
        hour-before tier fire hours after it already happened. Without
        an upper bound on the hour-before window, `last_reminded <
        hour_before` stays true forever once that tier never got a
        chance to fire, and it would wrongly say "starts in 1 hour" at
        8am for an event that started at 6am.
        """
        early = self.event(
            due_date="2026-08-07T06:00:00-06:00", last_reminded="2026-07-01T00:00:00-06:00"
        )
        self.assertIsNone(
            reminders.due_for_reminder(early, at("2026-08-07T08:00"), cadence=CADENCE)
        )

    def test_priority_and_jitter_do_not_apply_to_events(self):
        # Same due date, same last_reminded, only priority differs --
        # events don't read Priority at all, so both fire identically.
        now = at("2026-08-07T07:00")
        a = reminders.due_for_reminder(
            self.event(priority="High", last_reminded="2026-07-01T00:00:00-06:00"),
            now,
            cadence=CADENCE,
        )
        b = reminders.due_for_reminder(
            self.event(priority="Low", last_reminded="2026-07-01T00:00:00-06:00"),
            now,
            cadence=CADENCE,
        )
        self.assertEqual(a.body, b.body)
        self.assertEqual(a.priority, b.priority)


class CloudTakeoverLag(unittest.TestCase):
    """
    The lag is what keeps local_sync and cloud_sync from both sending
    the same reminder. cloud_sync only reports a reminder that came due
    at least LAG ago — which only happens when the Mac was asleep.
    """

    LAG = timedelta(minutes=10)

    def test_local_path_fires_immediately(self):
        now = at("2026-07-28T09:01")
        r = reminders.due_for_reminder(
            item(
                due_date="2026-07-28T09:00:00-06:00",
                last_reminded=(now - timedelta(hours=3)).isoformat(),
            ),
            now,
            cadence=CADENCE,
        )
        self.assertIsNotNone(r)

    def test_cloud_path_defers_on_a_freshly_due_reminder(self):
        now = at("2026-07-28T09:01")
        self.assertIsNone(
            reminders.due_for_reminder(
                item(
                    due_date="2026-07-28T09:00:00-06:00",
                    last_reminded=(now - timedelta(hours=2, minutes=5)).isoformat(),
                ),
                now,
                cadence=CADENCE,
                lag=self.LAG,
            )
        )

    def test_cloud_path_takes_over_once_the_lag_passes(self):
        now = at("2026-07-28T09:20")
        r = reminders.due_for_reminder(
            item(
                due_date="2026-07-28T09:00:00-06:00",
                last_reminded=(now - timedelta(hours=3)).isoformat(),
            ),
            now,
            cadence=CADENCE,
            lag=self.LAG,
        )
        self.assertIsNotNone(r)

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
        r = reminders.due_for_reminder(
            item(
                last_reminded=None,
                created_time="2026-07-28T15:09:30.000Z",  # 30 seconds old
                external_id="classroom:123:456",
            ),
            at("2026-07-28T09:10"),
            cadence=CADENCE,
            lag=self.LAG,
        )
        self.assertEqual(r.body, "Algebra Work — due Aug 26")

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
        r = reminders.due_for_reminder(
            item(last_reminded=None, created_time="2026-07-28T14:00:00.000Z"),
            at("2026-07-28T09:10"),
            cadence=CADENCE,
            lag=self.LAG,
        )
        self.assertIsNotNone(r)

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
    def test_zero_value_is_rejected(self):
        """A zero interval would re-fire every 60-second pass, forever."""
        self.assertEqual(reminders._parse_float("0", 24.0, "TEST"), 24.0)

    def test_negative_value_is_rejected(self):
        self.assertEqual(reminders._parse_float("-3", 24.0, "TEST"), 24.0)

    def test_garbage_value_falls_back(self):
        self.assertEqual(reminders._parse_float("soon", 24.0, "TEST"), 24.0)

    def test_valid_value_is_used(self):
        self.assertEqual(reminders._parse_float("6", 24.0, "TEST"), 6.0)

    def test_garbage_quiet_hours_falls_back(self):
        self.assertEqual(reminders._parse_hhmm("nope", "00:00"), reminders.dtime(0, 0))

    def test_valid_quiet_hours_parsed(self):
        self.assertEqual(reminders._parse_hhmm("21:30", "00:00"), reminders.dtime(21, 30))

    def test_garbage_int_falls_back(self):
        self.assertEqual(reminders._parse_int("many", 3, "TEST"), 3)

    def test_zero_int_is_rejected(self):
        self.assertEqual(reminders._parse_int("0", 3, "TEST"), 3)

    def test_valid_int_is_used(self):
        self.assertEqual(reminders._parse_int("5", 3, "TEST"), 5)

    def test_cadence_from_env_does_not_warn_when_simply_unset(self):
        """
        Regression guard: config.optional() must be called WITH each
        constant's own default so an unset (not merely bad) env var
        parses silently. Passing "" through to _parse_float would print
        a "bad value" warning on every single run for every tuning knob
        Peter hasn't customized -- which is nearly all of them, always.
        """
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            Cadence.from_env()
        self.assertNotIn("bad", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
