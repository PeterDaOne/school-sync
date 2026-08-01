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
from dataclasses import replace
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
        "category": None,
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

    def test_freshly_overdue_uses_the_floor(self):
        """The floor still governs the FIRST day overdue -- decay only
        starts biting after a full day has passed."""
        self.assertAlmostEqual(
            CADENCE.interval_hours("Assignments", -0.5, "Medium", "x"), 2.0
        )

    def test_overdue_decays_the_longer_it_sits(self):
        """
        REVERSED 2026-07-30 (Peter's call). This used to assert the
        opposite -- that an item overdue 100 days nagged exactly as hard
        as one overdue 1 day -- which was a deliberate design choice and
        a measurably bad one: simulated against real items it produced
        167 pushes/week from a single stale task. Past about the
        twentieth identical push the pressure has stopped being pressure.
        """
        day1 = CADENCE.interval_hours("Assignments", -0.5, "Medium", "x")
        day2 = CADENCE.interval_hours("Assignments", -1.5, "Medium", "x")
        day3 = CADENCE.interval_hours("Assignments", -2.5, "Medium", "x")
        self.assertAlmostEqual(day2, day1 * 2)
        self.assertAlmostEqual(day3, day1 * 4)

    def test_overdue_decay_is_capped(self):
        deep = CADENCE.interval_hours("Assignments", -100, "Medium", "x")
        self.assertAlmostEqual(deep, CADENCE.overdue_max_interval)

    def test_decay_never_applies_before_the_due_moment(self):
        for days in (0.0, 0.5, 3.0, 40.0):
            self.assertEqual(CADENCE.overdue_decay(days), 1.0, days)

    def test_capped_overdue_items_are_still_desynchronized_by_jitter(self):
        """
        Regression: the cap used to be applied AFTER jitter, which handed
        every deeply-overdue item the identical interval and re-created
        the exact phase-lock jitter exists to prevent -- they would all
        fire together, once a day, forever.
        """
        jittery = Cadence(
            assignment_floor=2.0, assignment_ceiling=48.0, assignment_alpha=4.0,
            overdue_max_interval=24.0, jitter_fraction=0.25,
        )
        a = jittery.interval_hours("Assignments", -100, "Medium", "page-a")
        b = jittery.interval_hours("Assignments", -100, "Medium", "page-b")
        self.assertNotEqual(a, b)
        for v in (a, b):
            self.assertGreaterEqual(v, 24.0 * 0.75)
            self.assertLessEqual(v, 24.0 * 1.25)

    def test_no_discontinuity_crossing_the_due_moment(self):
        just_before = CADENCE.interval_hours("Tasks", 0.001, "Medium", "x")
        just_after = CADENCE.interval_hours("Tasks", -0.001, "Medium", "x")
        self.assertAlmostEqual(just_before, just_after)

    def test_task_alpha_differs_from_assignment(self):
        # UNCAPPED deliberately: this isolates the alpha, and the
        # deadline guarantee would otherwise clamp a 2-days-out item to
        # 1/3 of its remaining time. The cap has its own tests below.
        uncapped = replace(CADENCE, deadline_guarantee=1.0)
        self.assertAlmostEqual(uncapped.interval_hours("Tasks", 2, "Medium", "x"), 40.0)

    def test_min_interval_flattens_the_task_vs_assignment_floor(self):
        """
        SUPERSEDED 2026-07-30. Tasks used to have a 1h overdue floor
        against Assignments' 2h -- Peter's explicit 2026-07-29 call that
        tasks nagged late need harder nagging once missed. The 2h hard
        floor he asked for later overrides it: both now clamp to 2h and
        the distinction is dead AT THE FLOOR.

        It still exists above the floor (see
        test_task_alpha_differs_from_assignment), where the type alphas
        genuinely differ. Recorded rather than deleted so the next person
        to widen TASK_FLOOR knows why it appears to do nothing.
        """
        task = CADENCE.interval_hours("Tasks", -0.5, "Medium", "x")
        assignment = CADENCE.interval_hours("Assignments", -0.5, "Medium", "x")
        self.assertAlmostEqual(task, CADENCE.min_interval)
        self.assertAlmostEqual(assignment, CADENCE.min_interval)

    def test_priority_still_multiplies_above_the_hard_floor(self):
        # Uncapped for the same reason as the alpha test above: at 5 days
        # out the deadline guarantee clamps to 39.6h, which would hide
        # the Low multiplier.
        c = replace(CADENCE, deadline_guarantee=1.0)
        self.assertAlmostEqual(c.interval_hours("Assignments", 5, "High", "x"), 10.0)
        self.assertAlmostEqual(c.interval_hours("Assignments", 5, "Medium", "x"), 20.0)
        self.assertAlmostEqual(c.interval_hours("Assignments", 5, "Low", "x"), 40.0)


class DeadlineGuarantee(unittest.TestCase):
    """
    An item's interval may never outrun its own deadline.

    Added 2026-08-01 after simulating a real school load through the
    engine: load scaling multiplies every interval by active_items/5, so
    at 55 active items an assignment due in TWO DAYS was reminded every
    73.8 hours -- longer than the 48 it had left. It could come due
    having never been mentioned once.
    """

    def test_interval_cannot_exceed_the_fraction_of_time_remaining(self):
        # 2 days out = 48h remaining; 1/3 of that is 15.84h.
        self.assertAlmostEqual(
            CADENCE.interval_hours("Tasks", 2, "Medium", "x"), 48 * 0.33
        )

    def test_it_survives_heavy_load_scaling(self):
        """The case it exists for: load scaling must not silence an item."""
        loaded = CADENCE.for_load(55)
        self.assertGreater(loaded.load_scale, 10)
        hours = loaded.interval_hours("Assignments", 2, "Medium", "x")
        self.assertLessEqual(hours, 48, "an item due in 2 days must be reminded within 48h")

    def test_it_guarantees_several_reminders_before_the_deadline(self):
        for days in (1, 2, 5, 10):
            hours = CADENCE.for_load(55).interval_hours("Assignments", days, "Medium", "x")
            self.assertGreaterEqual(
                (days * 24) / hours, 2.5, f"too few reminders {days} days out"
            )

    def test_it_never_makes_a_distant_item_louder(self):
        """
        It is a floor on attentiveness, not a new cadence: far from due,
        the normal formula still governs, so this cannot reintroduce the
        volume problem load scaling was added to solve.
        """
        far = CADENCE.interval_hours("Assignments", 30, "Medium", "x")
        uncapped = replace(CADENCE, deadline_guarantee=1.0)
        self.assertAlmostEqual(far, uncapped.interval_hours("Assignments", 30, "Medium", "x"))

    def test_it_does_not_apply_to_overdue_items(self):
        """
        An overdue item has no "time remaining" to take a fraction of,
        and overdue_decay is deliberately backing it OFF -- clamping here
        would fight that.
        """
        capped = CADENCE.interval_hours("Assignments", -3, "Medium", "x")
        uncapped = replace(CADENCE, deadline_guarantee=1.0)
        self.assertAlmostEqual(
            capped, uncapped.interval_hours("Assignments", -3, "Medium", "x")
        )

    def test_the_hard_floor_still_wins(self):
        # Minutes from due, 1/3 of remaining time is tiny -- but nothing
        # may notify twice inside MIN_INTERVAL_HOURS, by any path.
        self.assertGreaterEqual(
            CADENCE.interval_hours("Assignments", 0.01, "High", "x"), CADENCE.min_interval
        )

    def test_hard_floor_collapses_high_and_medium_when_freshly_overdue(self):
        """
        Consequence of the 2h floor, recorded deliberately: High no
        longer nags faster than Medium on a just-overdue item, because
        both land under the floor. Low still differs (4h).
        """
        high = CADENCE.interval_hours("Assignments", -0.5, "High", "x")
        medium = CADENCE.interval_hours("Assignments", -0.5, "Medium", "x")
        low = CADENCE.interval_hours("Assignments", -0.5, "Low", "x")
        self.assertAlmostEqual(high, medium)
        self.assertAlmostEqual(high, CADENCE.min_interval)
        self.assertAlmostEqual(low, 4.0)

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

    def test_hard_floor_survives_pathological_tuning(self):
        # A tiny floor plus an aggressive priority multiplier still
        # cannot produce anything faster than min_interval.
        tiny = Cadence(
            assignment_floor=0.01,
            priority_multiplier={"High": 0.1, "Medium": 1.0, "Low": 2.0},
            jitter_fraction=0.0,
        )
        self.assertEqual(
            tiny.interval_hours("Assignments", -5, "High", "x"), tiny.min_interval
        )

    def test_hard_floor_defaults_to_two_hours(self):
        self.assertEqual(reminders.DEFAULT_MIN_INTERVAL_HOURS, 2.0)
        self.assertEqual(Cadence().min_interval, 2.0)


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
    def test_is_tagged_as_a_capture_not_a_recurring_reminder(self):
        """
        pipeline._allocate rations the two completely differently -- a
        capture is the only time an item is ever announced, a recurring
        reminder repeats by design. The kind must not be inferable only
        by string-matching the title back apart.
        """
        r = reminders.due_for_reminder(
            item(last_reminded=None), at("2026-07-28T09:00"), cadence=CADENCE
        )
        self.assertEqual(r.kind, "capture")

    def test_a_recurring_reminder_is_not_tagged_as_capture(self):
        r = reminders.due_for_reminder(
            item(last_reminded="2026-07-01T09:00:00.000-06:00"),
            at("2026-07-28T09:00"), cadence=CADENCE,
        )
        self.assertEqual(r.kind, "recurring")

    def test_urgency_reflects_the_due_date_rather_than_a_flat_default(self):
        """
        Capture used to sit at the default priority 3 however close the
        due date was, so "you have never heard of this and it is due
        tomorrow" arrived at the same lock-screen weight as one due in
        three weeks.
        """
        overdue = reminders.due_for_reminder(
            item(last_reminded=None, due_date="2026-07-20"),
            at("2026-07-28T09:00"), cadence=CADENCE,
        )
        soon = reminders.due_for_reminder(
            item(last_reminded=None, due_date="2026-07-28T23:00:00.000-06:00"),
            at("2026-07-28T09:00"), cadence=CADENCE,
        )
        far = reminders.due_for_reminder(
            item(last_reminded=None, due_date="2026-08-26"),
            at("2026-07-28T09:00"), cadence=CADENCE,
        )
        self.assertEqual((overdue.priority, soon.priority, far.priority), (5, 4, 3))

    def test_an_undated_capture_stays_at_the_neutral_priority(self):
        r = reminders.due_for_reminder(
            item(last_reminded=None, due_date=None), at("2026-07-28T09:00"), cadence=CADENCE
        )
        self.assertEqual(r.priority, 3)

    def test_fires_once_when_never_reminded(self):
        r = reminders.due_for_reminder(
            item(last_reminded=None), at("2026-07-28T09:00"), cadence=CADENCE
        )
        self.assertEqual(r.title, "📝 New assignment")
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
        self.assertEqual(r.title, "☑️ New task")

    def test_class_adds_an_emoji_to_the_title_and_a_prefix_to_the_body(self):
        r = reminders.due_for_reminder(
            item(last_reminded=None, category="AP Stats"),
            at("2026-07-28T09:00"),
            cadence=CADENCE,
        )
        self.assertEqual(r.title, "📊 New assignment")
        self.assertTrue(r.body.startswith("AP Stats · Algebra Work"))

    def test_no_category_means_type_emoji_and_no_dangling_separator(self):
        r = reminders.due_for_reminder(
            item(last_reminded=None, category=None), at("2026-07-28T09:00"), cadence=CADENCE
        )
        self.assertEqual(r.title, "📝 New assignment")
        self.assertFalse(r.body.startswith("·"))
        self.assertTrue(r.body.startswith("Algebra Work"))

    def test_silenced_during_quiet_hours(self):
        """Returns a Reminder marked silent, not None (changed
        2026-07-30). The caller stamps Last Reminded and sends nothing,
        so the slot is spent rather than queued until 05:00."""
        r = reminders.due_for_reminder(
            item(last_reminded=None), at("2026-07-28T02:00"), cadence=CADENCE
        )
        self.assertIsNotNone(r)
        self.assertTrue(r.silent)


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
        self.assertEqual(r.title, "📝 Assignment reminder")
        self.assertEqual(r.body, "Algebra Work — due today at 9:00 AM")
        self.assertEqual(r.priority, 4)

    def test_overdue_wording(self):
        now = at("2026-07-28T09:00")
        r = reminders.due_for_reminder(
            item(
                due_date="2026-07-25T09:00:00-06:00",  # 3 days ago
                # 3 days overdue -> floor 2h x decay 2^3 = 16h between pushes
                last_reminded=(now - timedelta(hours=17)).isoformat(),
            ),
            now,
            cadence=CADENCE,
        )
        self.assertEqual(r.title, "📝 Assignment overdue")
        self.assertEqual(r.body, "Algebra Work — was due 3 days ago")
        self.assertEqual(r.priority, 5)

    def test_long_overdue_still_reminds_but_only_once_a_day(self):
        """
        REVERSED 2026-07-30. Previously "overdue items stay loud forever"
        -- 3 hours since the last push was enough to fire again even for
        something years overdue. Now the decay has long since hit the
        once-a-day cap, so 3 hours is not enough and 25 is. It stays
        PRESENT (still priority 5, still flagged) without spending
        attention every hour.
        """
        now = at("2026-07-28T09:00")
        stale = item(due_date="2020-01-01T09:00:00-06:00")

        too_soon = reminders.due_for_reminder(
            dict(stale, last_reminded=(now - timedelta(hours=3)).isoformat()),
            now, cadence=CADENCE,
        )
        self.assertIsNone(too_soon)

        r = reminders.due_for_reminder(
            dict(stale, last_reminded=(now - timedelta(hours=25)).isoformat()),
            now, cadence=CADENCE,
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

    def test_quiet_hours_consume_the_slot_rather_than_queueing_it(self):
        """
        REVERSED 2026-07-30. This used to assert None -- meaning the slot
        was preserved and the reminder fired the instant quiet hours
        ended. Observed live, that released three pushes at 05:00:48,
        :49 and :50, one second apart, before Peter was up. Now the slot
        is spent silently and the item waits a full interval.
        """
        r = reminders.due_for_reminder(
            item(due_date="2026-07-28", last_reminded="2026-07-27T12:00:00+00:00"),
            at("2026-07-28T02:00"),
            cadence=CADENCE,
        )
        self.assertIsNotNone(r)
        self.assertTrue(r.silent)

    def test_outside_quiet_hours_is_never_silent(self):
        r = reminders.due_for_reminder(
            item(due_date="2026-07-28", last_reminded="2026-07-27T12:00:00+00:00"),
            at("2026-07-28T09:00"),
            cadence=CADENCE,
        )
        self.assertFalse(r.silent)

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

    def test_events_stay_quiet_further_out_than_three_days(self):
        """
        REGRESSION GUARD against re-widening these tiers.

        They were briefly 14/7/3/1/0 on 2026-08-01, to give a graded
        Event (a final, a recital) study runway. Peter's answer was
        better and reverted it: a presentation is TWO items -- an
        Assignment for preparing and an Event for presenting. Runway is
        the Assignment cadence's job, which already nags early and scales
        with urgency. Widening Event tiers solved the Assignment's
        problem in the wrong type and taxed every ungraded Event (Prom,
        a rehearsal) with two extra notifications to do it.

        If a graded Event ever looks under-reminded, the fix is to check
        that its paired Assignment was captured -- not to add tiers here.
        """
        for days_out, day in ((14, "2026-07-24"), (10, "2026-07-28"), (7, "2026-07-31")):
            r = reminders.due_for_reminder(
                self.event(last_reminded="2026-07-01T00:00:00-06:00"),
                at(f"{day}T07:00"),
                cadence=CADENCE,
            )
            self.assertIsNone(r, f"{days_out} days out should be silent")

    def test_the_default_tiers_are_the_original_three(self):
        self.assertEqual(reminders.Cadence().event_reminder_days, (3, 1, 0))
        self.assertEqual(reminders.DEFAULT_EVENT_REMINDER_DAYS, "3,1,0")

    def test_the_tiers_are_configurable(self):
        narrow = replace(CADENCE, event_reminder_days=(1, 0))
        self.assertIsNone(
            reminders.due_for_reminder(
                self.event(last_reminded="2026-07-01T00:00:00-06:00"),
                at("2026-07-24T07:00"),
                cadence=narrow,
            )
        )

    def test_three_days_before_at_the_marker_hour(self):
        r = reminders.due_for_reminder(
            self.event(last_reminded="2026-07-01T00:00:00-06:00"),
            at("2026-08-04T07:00"),
            cadence=CADENCE,
        )
        self.assertEqual(r.title, "📅 Event reminder")
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
        self.assertEqual(r.tags, "rotating_light")

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
        r = reminders.due_for_reminder(
            item(
                last_reminded=None,
                created_time="2026-07-28T08:00:00.000Z",
                external_id="gmail:abc123",
            ),
            at("2026-07-28T02:00"),
            cadence=CADENCE,
            lag=self.LAG,
        )
        self.assertTrue(r.silent)

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
        self.assertTrue(
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


class NtfyTagsAreNotDecoration(unittest.TestCase):
    """
    ntfy converts a tag matching an emoji short code into an emoji and
    PREPENDS it to the title. The old default tag "school" therefore put
    a 🏫 on the front of every notification (confirmed 2026-07-30 by
    polling the live topic: all 9 cached messages carried it, against
    ntfy's own emoji table where school -> 🏫). It said nothing —
    everything in this system is school — and it pushed the category
    emoji, which does say something, off to the side.

    These pin that no tag is ever emitted for its own sake.
    """

    def _reminder(self, **kw):
        return reminders.due_for_reminder(item(**kw), at("2026-08-20T09:00"), cadence=CADENCE)

    def test_school_tag_is_never_emitted(self):
        for kw in (
            {"last_reminded": None},                                   # capture
            {"last_reminded": "2026-08-01T09:00:00-06:00"},            # recurring
            {"last_reminded": "2026-08-01T09:00:00-06:00",
             "due_date": "2026-08-01"},                                # overdue
        ):
            r = self._reminder(**kw)
            self.assertIsNotNone(r, kw)
            self.assertNotIn("school", r.tags, kw)

    def test_routine_reminder_carries_no_tags_at_all(self):
        r = self._reminder(last_reminded="2026-08-01T09:00:00-06:00")
        self.assertEqual(r.tags, "")

    def test_overdue_still_flags_urgency(self):
        r = self._reminder(
            last_reminded="2026-08-01T09:00:00-06:00", due_date="2026-08-01"
        )
        self.assertEqual(r.tags, "rotating_light")
        self.assertEqual(r.priority, 5)


class CategoryEmojiInTitle(unittest.TestCase):
    def test_class_category_supplies_the_title_emoji(self):
        r = reminders.due_for_reminder(
            item(last_reminded=None, category="AP Lang"), at("2026-08-20T09:00"), cadence=CADENCE
        )
        self.assertTrue(r.title.startswith("✍️"), r.title)

    def test_non_class_category_supplies_one_too(self):
        r = reminders.due_for_reminder(
            item(last_reminded=None, category="Personal", type_name="Tasks"),
            at("2026-08-20T09:00"),
            cadence=CADENCE,
        )
        self.assertTrue(r.title.startswith("👤"), r.title)
        self.assertIn("Personal · ", r.body)

    def test_categoryless_item_falls_back_to_its_type_emoji(self):
        """Regression: a Task with no category used to render a bare
        "Task reminder" title with no glyph, next to assignments that all
        had one."""
        r = reminders.due_for_reminder(
            item(last_reminded=None, category=None, type_name="Tasks"),
            at("2026-08-20T09:00"),
            cadence=CADENCE,
        )
        self.assertTrue(r.title.startswith("☑️"), r.title)
        self.assertNotIn(" · ", r.body)




class HardMinimumInterval(unittest.TestCase):
    """
    Peter's rule (2026-07-30): the same item can never notify twice
    within two hours, by any path. Repeats stack on iOS and can't be
    collapsed, so the only real defense is not generating them.

    due_for_reminder enforces this on top of the cadence math, so it
    holds even for paths that never touch interval_hours -- Events'
    fixed tiers especially.
    """

    def test_recurring_item_is_refused_inside_the_window(self):
        now = at("2026-07-30T13:00")
        self.assertIsNone(
            reminders.due_for_reminder(
                item(due_date="2026-07-25", last_reminded=(now - timedelta(minutes=90)).isoformat()),
                now,
                cadence=CADENCE,
            )
        )

    def test_recurring_item_is_allowed_outside_the_window(self):
        now = at("2026-07-30T13:00")
        self.assertIsNotNone(
            reminders.due_for_reminder(
                item(due_date="2026-07-25", last_reminded=(now - timedelta(hours=30)).isoformat()),
                now,
                cadence=CADENCE,
            )
        )

    def test_event_tiers_cannot_land_within_the_window(self):
        """
        The case the cadence clamp alone would miss. A 9am event fires
        its morning-of tier at 07:00 and its 1-hour-before tier at 08:00
        -- one hour apart. The hard floor is what stops that pair.
        """
        ev = item(
            type_name="Events",
            due_date="2026-08-07T09:00:00-06:00",
            last_reminded="2026-08-07T07:00:00-06:00",  # morning-of just fired
        )
        self.assertIsNone(
            reminders.due_for_reminder(ev, at("2026-08-07T08:00"), cadence=CADENCE)
        )

    def test_event_hour_before_still_fires_when_far_enough_from_the_last(self):
        """The floor must not swallow the hour-before reminder outright
        -- an evening event's morning-of fired long ago."""
        ev = item(
            type_name="Events",
            due_date="2026-08-07T19:00:00-06:00",
            last_reminded="2026-08-07T07:00:00-06:00",
        )
        r = reminders.due_for_reminder(ev, at("2026-08-07T18:00"), cadence=CADENCE)
        self.assertIsNotNone(r)
        self.assertIn("starts in 1 hour", r.body)

    def test_capture_is_unaffected(self):
        """A brand-new item has no Last Reminded, so nothing to compare
        against -- capture must still announce immediately."""
        self.assertIsNotNone(
            reminders.due_for_reminder(
                item(last_reminded=None), at("2026-07-30T13:00"), cadence=CADENCE
            )
        )

    def test_quiet_hours_do_not_consume_a_slot_inside_the_window(self):
        """The floor is checked BEFORE the quiet-hours transform, so a
        blocked reminder isn't silently stamped either."""
        now = at("2026-07-30T02:00")
        self.assertIsNone(
            reminders.due_for_reminder(
                item(due_date="2026-07-25", last_reminded=(now - timedelta(minutes=30)).isoformat()),
                now,
                cadence=CADENCE,
            )
        )

    def test_unparseable_stamp_raises_rather_than_silencing(self):
        """
        Deliberate: a corrupt Last Reminded must fail LOUDLY, not make
        the item quietly unable to ever remind again. pipeline.py catches
        this per item and turns the run red -- the project's error policy.
        """
        with self.assertRaises(ValueError):
            reminders.due_for_reminder(
                item(due_date="2026-07-25", last_reminded="not-a-date"),
                at("2026-07-30T13:00"),
                cadence=CADENCE,
            )
