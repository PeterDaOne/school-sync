"""
Tests for the sync pass's reporting and exit-code policy.

The important ones here are the undelivered-reminder tests. On
2026-07-29 a misspelled NTFY_TOPIC secret caused GitHub Actions to drop
every reminder for a full day while every run stayed green: notify()
returned False, the Last Reminded stamp was correctly skipped, and
nothing else noticed. These pin the two halves of the fix —
notify_failures is incremented at the call site, and it reaches the
exit code — plus the pre-existing behavior that must NOT regress: a
failed push must never stamp Last Reminded, or the reminder is consumed
and lost rather than retried.
"""

import unittest
from dataclasses import replace
from datetime import timedelta
from unittest import mock

import tests.context  # noqa: F401  (path + timezone setup)

from shared import pipeline, timeutil, reminders


class ReportSummary(unittest.TestCase):
    def test_empty_report_is_ok(self):
        report = pipeline.Report()
        self.assertTrue(report.ok)
        self.assertEqual(pipeline.finish(report, "test"), 0)

    def test_undelivered_reminder_is_not_ok(self):
        report = pipeline.Report(reminded=0, notify_failures=1)
        self.assertFalse(report.ok)

    def test_undelivered_reminder_exits_non_zero(self):
        """The whole point of the fix: a dropped push turns the run red."""
        report = pipeline.Report(synced=3, notify_failures=2)
        self.assertEqual(pipeline.finish(report, "test"), 1)

    def test_undelivered_reminders_named_in_summary(self):
        report = pipeline.Report(notify_failures=2)
        self.assertIn("2 reminder(s) UNDELIVERED", report.summary("cloud_sync"))

    def test_item_failures_still_exit_non_zero(self):
        report = pipeline.Report(failures=["calendar sync failed for 'x'"])
        self.assertFalse(report.ok)
        self.assertEqual(pipeline.finish(report, "test"), 1)

    def test_successful_work_is_ok(self):
        report = pipeline.Report(synced=2, reminded=1)
        self.assertTrue(report.ok)
        self.assertEqual(pipeline.finish(report, "test"), 0)
        self.assertIn("sent 1 reminder(s)", report.summary("local_sync"))


ITEM = {
    "id": "page-1",
    "url": "https://notion.so/page-1",
    "name": "Essay",
    "type_name": "Assignments",
    "is_complete": False,
    "due_date": "2026-07-29T12:00:00.000-06:00",
    "last_reminded": "2026-07-29T00:00:00.000+00:00",
    "external_id": None,
}


REMINDER = reminders.Reminder(title="Assignment overdue", body="Essay — was due yesterday")


class RunSyncPassNotifyWiring(unittest.TestCase):
    """
    Drives run_sync_pass with every collaborator stubbed, so the only
    thing under test is what the pass does with notify()'s return value.

    commands.poll_mark_done/apply_mark_done are deliberately NOT mocked
    here — they run for real, and since NTFY_COMMAND_TOPIC is blanked in
    tests/context.py, poll_mark_done no-ops immediately with no network
    call. That's worth relying on rather than mocking: it's the same
    "unconfigured is a no-op, not an error" path production hits until
    Peter sets the topic up.
    """

    def _run(self, notify_result: bool, reminder=None, max_per_pass: int = 3):
        page = {"id": "page-1"}
        with (
            mock.patch.object(pipeline, "notion_client") as nc,
            mock.patch.object(pipeline, "calendar_client"),
            mock.patch.object(pipeline, "state") as st,
            mock.patch.object(pipeline, "reminders") as rm,
            mock.patch.object(pipeline, "notify", return_value=notify_result) as nf,
        ):
            nc.get_all_items.return_value = [page]
            nc.extract_fields.return_value = dict(ITEM)
            st.needs_sync.return_value = False
            cadence = mock.Mock(
                max_per_pass=max_per_pass,
                daily_budget=8,
                load_scale=1.0,
                capture_digest_threshold=99,
            )
            cadence.for_load.return_value = cadence
            cadence.in_class_hours.return_value = False
            rm.Cadence.from_env.return_value = cadence
            rm.due_for_reminder.return_value = reminder or REMINDER
            report = pipeline.run_sync_pass(
                "test", send_reminders=True, lag=timedelta(0)
            )
            return report, nc, nf

    def test_failed_push_is_counted_not_swallowed(self):
        report, _, notify_fn = self._run(notify_result=False)
        notify_fn.assert_called_once()
        self.assertEqual(report.notify_failures, 1)
        self.assertEqual(report.reminded, 0)
        self.assertFalse(report.ok)

    def test_failed_push_does_not_stamp_last_reminded(self):
        """
        Regression guard. Stamping after a failed send consumes the
        reminder slot and the notification is lost rather than retried —
        the worst failure mode this system has.
        """
        _, nc, _ = self._run(notify_result=False)
        nc.mark_reminded.assert_not_called()

    def test_successful_push_stamps_and_counts(self):
        report, nc, _ = self._run(notify_result=True)
        nc.mark_reminded.assert_called_once()
        self.assertEqual(report.reminded, 1)
        self.assertEqual(report.notify_failures, 0)
        self.assertTrue(report.ok)

    def test_notify_receives_reminder_fields_not_a_raw_string(self):
        """
        due_for_reminder returns a Reminder object now, not a str --
        pipeline must unpack .title/.body/.priority/.tags into notify(),
        not pass the object straight through.
        """
        report, _, notify_fn = self._run(notify_result=True)
        _, kwargs = notify_fn.call_args
        args = notify_fn.call_args.args
        self.assertEqual(args[0], REMINDER.title)
        self.assertEqual(args[1], REMINDER.body)
        self.assertEqual(kwargs.get("priority"), REMINDER.priority)
        self.assertEqual(kwargs.get("tags"), REMINDER.tags)

    def test_per_pass_cap_defers_without_touching_last_reminded(self):
        """
        With max_per_pass=0, every due reminder is deferred, not sent --
        notify() is never even called, and nothing is lost (no stamp).
        """
        report, nc, notify_fn = self._run(notify_result=True, max_per_pass=0)
        notify_fn.assert_not_called()
        nc.mark_reminded.assert_not_called()
        self.assertEqual(report.deferred, 1)
        self.assertEqual(report.reminded, 0)
        # Deferred is routine, not a failure -- must not affect exit code.
        self.assertTrue(report.ok)


if __name__ == "__main__":
    unittest.main()


class QuietHoursConsumeTheSlot(unittest.TestCase):
    """
    A silent Reminder must be STAMPED and NOT SENT. Getting this backwards
    in either direction is a real bug: not stamping re-creates the 05:00
    burst this change exists to kill, and sending it buzzes Peter's phone
    at 2am.
    """

    def _run(self, silent: bool):
        page = {"id": "page-1"}
        with (
            mock.patch.object(pipeline, "notion_client") as nc,
            mock.patch.object(pipeline, "calendar_client"),
            mock.patch.object(pipeline, "state") as st,
            mock.patch.object(pipeline, "reminders") as rm,
            mock.patch.object(pipeline, "notify", return_value=True) as nf,
        ):
            nc.get_all_items.return_value = [page]
            nc.extract_fields.return_value = dict(ITEM)
            st.needs_sync.return_value = False
            cadence = mock.Mock(
                max_per_pass=3,
                daily_budget=8,
                load_scale=1.0,
                capture_digest_threshold=99,
            )
            cadence.for_load.return_value = cadence
            cadence.in_class_hours.return_value = False
            rm.Cadence.from_env.return_value = cadence
            rm.due_for_reminder.return_value = replace(REMINDER, silent=silent)
            report = pipeline.run_sync_pass("test", send_reminders=True)
            return report, nc, nf

    def test_silent_reminder_is_not_pushed(self):
        _, _, notify_fn = self._run(silent=True)
        notify_fn.assert_not_called()

    def test_silent_reminder_still_stamps_last_reminded(self):
        """This is the whole point: spending the slot is what stops it
        from detonating the moment quiet hours end."""
        _, nc, _ = self._run(silent=True)
        nc.mark_reminded.assert_called_once()

    def test_silent_reminder_is_counted_as_suppressed_not_failed(self):
        report, _, _ = self._run(silent=True)
        self.assertEqual(report.suppressed, 1)
        self.assertEqual(report.reminded, 0)
        self.assertEqual(report.deferred, 0)
        self.assertTrue(report.ok)

    def test_a_normal_reminder_is_still_sent(self):
        report, _, notify_fn = self._run(silent=False)
        notify_fn.assert_called_once()
        self.assertEqual(report.suppressed, 0)
        self.assertEqual(report.reminded, 1)


class Allocation(unittest.TestCase):
    """
    Peter's rule (2026-07-30): guarantee every item one notification
    before any item gets a second, then spend what's left on the most
    urgent.
    """

    def _cadence(self, max_per_pass=10, daily_budget=8, capture_digest_threshold=99):
        # The digest threshold defaults absurdly high here so these cases
        # keep exercising the one-push-per-item path they were written
        # for. Digest behaviour has its own class below.
        c = mock.Mock(
            max_per_pass=max_per_pass,
            daily_budget=daily_budget,
            load_scale=1.0,
            capture_digest_threshold=capture_digest_threshold,
        )
        c.for_load.return_value = c
        c.in_class_hours.return_value = False
        return c

    def _cand(self, item_id, priority, due=None, kind="recurring"):
        return (
            dict(ITEM, id=item_id, due_date=due, priority=None),
            replace(REMINDER, priority=priority, kind=kind),
        )

    def _capture(self, item_id, priority=3):
        return self._cand(item_id, priority, kind="capture")

    def _allocate(self, candidates, reminded_today, sent_today=0, **kw):
        report = pipeline.Report()
        with mock.patch.object(pipeline.reminders, "due_datetime", return_value=None):
            allocation = pipeline._allocate(
                candidates, reminded_today, self._cadence(**kw), report, sent_today
            )
        return [c[0]["id"] for c in allocation.sends], report

    def test_first_timers_come_before_repeats(self):
        """A loud item that already had one today must not outrank a
        quiet item that has had none."""
        loud_repeat = self._cand("loud", 5)
        quiet_new = self._cand("quiet", 3)
        ids, _ = self._allocate([loud_repeat, quiet_new], reminded_today={"loud"})
        self.assertEqual(ids[0], "quiet")

    def test_within_first_timers_the_most_urgent_wins(self):
        ids, _ = self._allocate(
            [self._cand("low", 3), self._cand("high", 5)], reminded_today=set()
        )
        self.assertEqual(ids, ["high", "low"])

    def test_repeats_are_capped_by_the_daily_budget(self):
        repeats = [self._cand(f"r{i}", 4) for i in range(6)]
        ids, report = self._allocate(
            repeats, reminded_today={f"r{i}" for i in range(6)}, daily_budget=2
        )
        self.assertEqual(len(ids), 2)
        self.assertEqual(report.deferred, 4)

    def test_first_timers_are_capped_too_because_a_cap_must_be_a_cap(self):
        """
        REVERSED 2026-07-30. The budget used to govern repeats only, so a
        first-time reminder could never be blocked. That made it not a
        cap: at a realistic 24-item load the system produced 333
        pushes/week with a peak of 69 in a day, almost all of them each
        item's first of the day. A ceiling that exempts the common case
        is not a ceiling.

        Load scaling is what keeps this from binding often -- it stretches
        every interval as the workload grows, so fewer candidates arrive
        here in the first place.
        """
        news = [self._cand(f"n{i}", 4) for i in range(6)]
        ids, report = self._allocate(news, reminded_today=set(), daily_budget=2)
        self.assertEqual(len(ids), 2)
        self.assertEqual(report.deferred, 4)

    def test_budget_counts_what_already_went_out_today(self):
        news = [self._cand(f"n{i}", 4) for i in range(6)]
        ids, _ = self._allocate(news, reminded_today=set(), daily_budget=6, sent_today=4)
        self.assertEqual(len(ids), 2)

    def test_exhausted_budget_sends_nothing(self):
        news = [self._cand(f"n{i}", 4) for i in range(3)]
        ids, report = self._allocate(news, reminded_today=set(), daily_budget=6, sent_today=6)
        self.assertEqual(ids, [])
        self.assertEqual(report.deferred, 3)

    def test_per_pass_cap_still_applies_on_top(self):
        news = [self._cand(f"n{i}", 4) for i in range(6)]
        ids, report = self._allocate(news, reminded_today=set(), max_per_pass=2)
        self.assertEqual(len(ids), 2)
        self.assertEqual(report.deferred, 4)

    def test_nothing_due_allocates_nothing_and_defers_nothing(self):
        ids, report = self._allocate([], reminded_today=set())
        self.assertEqual(ids, [])
        self.assertEqual(report.deferred, 0)


class CaptureAnnouncementsAreNotNags(unittest.TestCase):
    """
    REGRESSION GUARD for a real, user-visible failure on 2026-07-31.

    A Gmail-captured assignment sat in Notion, due the next day, and was
    never announced for 3h20m — 137 consecutive passes — while the daily
    budget was spent re-nagging two stale junk Tasks Peter had been told
    about days earlier. He reasonably concluded Gmail capture was broken.
    It wasn't; the notification was.

    A capture is the ONLY time an item is ever announced. A nag repeats
    by construction. Rationing them identically loses information that is
    never re-sent.
    """

    def _cadence(self, max_per_pass=10, daily_budget=6, capture_digest_threshold=99):
        c = mock.Mock(
            max_per_pass=max_per_pass,
            daily_budget=daily_budget,
            load_scale=1.0,
            capture_digest_threshold=capture_digest_threshold,
        )
        c.for_load.return_value = c
        c.in_class_hours.return_value = False
        return c

    def _cand(self, item_id, priority, kind="recurring"):
        return (
            dict(ITEM, id=item_id, due_date=None, priority=None),
            replace(REMINDER, priority=priority, kind=kind),
        )

    def _allocate(self, candidates, reminded_today=frozenset(), sent_today=0, **kw):
        report = pipeline.Report()
        with mock.patch.object(pipeline.reminders, "due_datetime", return_value=None):
            allocation = pipeline._allocate(
                candidates, set(reminded_today), self._cadence(**kw), report, sent_today
            )
        return [c[0]["id"] for c in allocation.sends], report

    def test_an_announcement_survives_a_fully_spent_budget(self):
        """The exact 2026-07-31 failure, in one assertion."""
        ids, _ = self._allocate(
            [self._cand("new-assignment", 3, kind="capture")],
            sent_today=6, daily_budget=6,
        )
        self.assertEqual(ids, ["new-assignment"])

    def test_an_announcement_outranks_a_louder_overdue_nag(self):
        """
        ntfy priority alone put capture (3) behind overdue (5), so the one
        message carrying new information sorted last.
        """
        ids, _ = self._allocate(
            [self._cand("overdue-junk", 5), self._cand("brand-new", 3, kind="capture")],
            max_per_pass=1,
        )
        self.assertEqual(ids, ["brand-new"])

    def test_nags_are_still_capped_by_the_budget(self):
        # The exemption must not leak into the nag path.
        ids, report = self._allocate(
            [self._cand(f"n{i}", 4) for i in range(3)], sent_today=6, daily_budget=6
        )
        self.assertEqual(ids, [])
        self.assertEqual(report.deferred, 3)

    def test_announcements_consume_the_nag_budget(self):
        """
        Deliberate: on a day full of real news, stacking the usual nagging
        on top is exactly the volume problem the budget exists to stop.
        """
        ids, _ = self._allocate(
            [self._cand("cap1", 3, kind="capture"), self._cand("nag1", 5)],
            sent_today=5, daily_budget=6,
        )
        self.assertEqual(ids, ["cap1"])

    def test_announcements_are_still_bounded_per_pass(self):
        """
        Exempt from the DAILY budget, not from throttling — otherwise a
        bulk import would fire every notification at once.
        """
        caps = [self._cand(f"c{i}", 3, kind="capture") for i in range(10)]
        ids, report = self._allocate(caps, max_per_pass=3)
        self.assertEqual(len(ids), 3)
        self.assertEqual(report.deferred, 7)

    def test_a_deferred_announcement_is_reported_as_budget_blocked_only_when_it_is(self):
        # Held by the per-pass cap, not the budget: it really does go out
        # on the next pass, and saying "held until tomorrow" would lie.
        _, report = self._allocate(
            [self._cand(f"c{i}", 3, kind="capture") for i in range(5)],
            max_per_pass=2, daily_budget=6,
        )
        self.assertTrue(report.deferred)
        self.assertFalse(report.budget_blocked)

    def test_budget_blocked_is_set_when_the_daily_cap_is_the_reason(self):
        _, report = self._allocate(
            [self._cand("n1", 4)], sent_today=6, daily_budget=6
        )
        self.assertTrue(report.budget_blocked)


class DailyCounterSelfResets(unittest.TestCase):
    """
    The daily cap is backed by a per-item counter dated by that item's own
    Last Reminded stamp. That is what lets it reset at midnight with no
    cleanup job and no global counter row -- but only if the pipeline
    RESETS to 1 on a new day instead of incrementing forever.
    """

    def _run(self, last_reminded, reminders_today):
        page = {"id": "page-1"}
        item = dict(ITEM, last_reminded=last_reminded, reminders_today=reminders_today)
        with (
            mock.patch.object(pipeline, "notion_client") as nc,
            mock.patch.object(pipeline, "calendar_client"),
            mock.patch.object(pipeline, "state") as st,
            mock.patch.object(pipeline, "reminders") as rm,
            mock.patch.object(pipeline, "notify", return_value=True),
        ):
            nc.get_all_items.return_value = [page]
            nc.extract_fields.return_value = item
            st.needs_sync.return_value = False
            cadence = mock.Mock(
                max_per_pass=3,
                daily_budget=6,
                load_scale=1.0,
                capture_digest_threshold=99,
            )
            cadence.for_load.return_value = cadence
            cadence.in_class_hours.return_value = False
            rm.Cadence.from_env.return_value = cadence
            rm.due_for_reminder.return_value = REMINDER
            pipeline.run_sync_pass("test", send_reminders=True)
            return nc.mark_reminded.call_args

    def test_increments_when_the_previous_reminder_was_today(self):
        now = timeutil.now()
        args = self._run(now.isoformat(), 2)
        self.assertEqual(args[0][2], 3)

    def test_resets_to_one_when_the_previous_reminder_was_yesterday(self):
        """Without this the counter grows forever and the cap locks the
        system silent after one busy day."""
        stale = (timeutil.now() - timedelta(days=1)).isoformat()
        args = self._run(stale, 5)
        self.assertEqual(args[0][2], 1)

    def test_resets_when_there_is_no_previous_reminder(self):
        args = self._run(None, 0)
        self.assertEqual(args[0][2], 1)


class LoadScaling(unittest.TestCase):
    def test_cadence_is_scaled_for_the_active_workload(self):
        pages = [{"id": f"p{i}"} for i in range(12)]
        with (
            mock.patch.object(pipeline, "notion_client") as nc,
            mock.patch.object(pipeline, "calendar_client"),
            mock.patch.object(pipeline, "state") as st,
            mock.patch.object(pipeline, "reminders") as rm,
            mock.patch.object(pipeline, "notify", return_value=True),
        ):
            nc.get_all_items.return_value = pages
            nc.extract_fields.side_effect = [
                dict(ITEM, id=f"p{i}", is_complete=False, type_name="Assignments")
                for i in range(12)
            ]
            st.needs_sync.return_value = False
            cadence = mock.Mock(
                max_per_pass=3,
                daily_budget=6,
                load_scale=1.0,
                capture_digest_threshold=99,
            )
            cadence.for_load.return_value = cadence
            cadence.in_class_hours.return_value = False
            rm.Cadence.from_env.return_value = cadence
            rm.due_for_reminder.return_value = None
            pipeline.run_sync_pass("test", send_reminders=True)
            cadence.for_load.assert_called_once_with(12)

    def test_completed_items_do_not_inflate_the_load(self):
        pages = [{"id": f"p{i}"} for i in range(5)]
        with (
            mock.patch.object(pipeline, "notion_client") as nc,
            mock.patch.object(pipeline, "calendar_client"),
            mock.patch.object(pipeline, "state") as st,
            mock.patch.object(pipeline, "reminders") as rm,
            mock.patch.object(pipeline, "notify", return_value=True),
        ):
            nc.get_all_items.return_value = pages
            nc.extract_fields.side_effect = [
                dict(ITEM, id=f"p{i}", is_complete=(i >= 2), type_name="Assignments")
                for i in range(5)
            ]
            st.needs_sync.return_value = False
            cadence = mock.Mock(
                max_per_pass=3,
                daily_budget=6,
                load_scale=1.0,
                capture_digest_threshold=99,
            )
            cadence.for_load.return_value = cadence
            cadence.in_class_hours.return_value = False
            rm.Cadence.from_env.return_value = cadence
            rm.due_for_reminder.return_value = None
            pipeline.run_sync_pass("test", send_reminders=True)
            cadence.for_load.assert_called_once_with(2)


class QuietHoursDoNotPoisonTheDailyBudget(unittest.TestCase):
    """
    Regression, caught in simulation 2026-07-30 before it ever shipped to
    the phone. A silent quiet-hours consume moves Last Reminded forward,
    and Last Reminded is what dates the daily counter. If the counter
    isn't rewritten at the same time, yesterday's total is carried into
    today and the day opens with a phantom spent budget.

    Quiet hours run every night, so this poisoned every morning: a
    simulated week went from 41 notifications to 7.
    """

    def _run(self, last_reminded, reminders_today):
        page = {"id": "page-1"}
        item = dict(ITEM, last_reminded=last_reminded, reminders_today=reminders_today)
        with (
            mock.patch.object(pipeline, "notion_client") as nc,
            mock.patch.object(pipeline, "calendar_client"),
            mock.patch.object(pipeline, "state") as st,
            mock.patch.object(pipeline, "reminders") as rm,
            mock.patch.object(pipeline, "notify", return_value=True) as nf,
        ):
            nc.get_all_items.return_value = [page]
            nc.extract_fields.return_value = item
            st.needs_sync.return_value = False
            cadence = mock.Mock(
                max_per_pass=3,
                daily_budget=6,
                load_scale=1.0,
                capture_digest_threshold=99,
            )
            cadence.for_load.return_value = cadence
            cadence.in_class_hours.return_value = False
            rm.Cadence.from_env.return_value = cadence
            rm.due_for_reminder.return_value = replace(REMINDER, silent=True)
            pipeline.run_sync_pass("test", send_reminders=True)
            nf.assert_not_called()
            return nc.mark_reminded.call_args

    def test_silent_consume_on_a_new_day_zeroes_the_counter(self):
        yesterday = (timeutil.now() - timedelta(days=1)).isoformat()
        args = self._run(yesterday, 5)
        self.assertEqual(args[0][2], 0)

    def test_silent_consume_preserves_todays_real_count(self):
        """Within the same day the count must NOT be cleared -- those
        notifications really were delivered and really did spend budget."""
        args = self._run(timeutil.now().isoformat(), 3)
        self.assertEqual(args[0][2], 3)

    def test_silent_consume_never_increments(self):
        args = self._run(timeutil.now().isoformat(), 2)
        self.assertNotEqual(args[0][2], 3)


class ClassHoursDeferRatherThanConsume(unittest.TestCase):
    """
    The asymmetry with quiet hours is the whole design, so it is pinned
    here in both directions.

    Quiet hours CONSUME the slot: nothing changes while Peter is asleep,
    and holding reminders produced a documented 05:00 burst. Class hours
    DEFER: 15:00 is strictly better than 09:00 for the same message, and
    the item's remaining time is materially unchanged. Consuming the slot
    during class would spend the reminder on a phone in a bag -- which is
    exactly the failure this exists to stop, since Last Reminded is
    stamped either way and nothing downstream can tell the difference.
    """

    def _cadence(self, in_class, max_per_pass=10, daily_budget=8):
        c = mock.Mock(
            max_per_pass=max_per_pass,
            daily_budget=daily_budget,
            load_scale=1.0,
            capture_digest_threshold=99,
        )
        c.for_load.return_value = c
        c.in_class_hours.return_value = in_class
        return c

    def _cand(self, item_id, priority, kind="recurring"):
        return (
            dict(ITEM, id=item_id, due_date=None, priority=None),
            replace(REMINDER, priority=priority, kind=kind),
        )

    def _allocate(self, candidates, in_class, **kw):
        report = pipeline.Report()
        with mock.patch.object(pipeline.reminders, "due_datetime", return_value=None):
            allocation = pipeline._allocate(
                candidates, set(), self._cadence(in_class, **kw), report, 0,
                now=timeutil.now(),
            )
        return [c[0]["id"] for c in allocation.sends], allocation, report

    def test_nags_are_held_during_class(self):
        ids, _, report = self._allocate([self._cand("essay", 5)], in_class=True)
        self.assertEqual(ids, [])
        self.assertEqual(report.deferred, 1)

    def test_a_held_nag_is_not_a_failure_and_is_not_stamped(self):
        """Deferred means reconsidered later, with Last Reminded
        untouched -- not lost, and not a red run."""
        _, _, report = self._allocate([self._cand("essay", 5)], in_class=True)
        self.assertTrue(report.ok)
        self.assertEqual(report.reminded, 0)
        self.assertEqual(report.suppressed, 0)  # NOT the quiet-hours path

    def test_the_log_line_names_class_hours_not_the_budget(self):
        """
        A silence whose cause cannot be read off the log is how a real
        3h20m outage hid for 137 passes. Three different waits, three
        different words.
        """
        _, _, report = self._allocate([self._cand("essay", 5)], in_class=True)
        summary = report.summary("local_sync")
        self.assertIn("held until school ends", summary)
        self.assertNotIn("held until tomorrow", summary)
        self.assertNotIn("deferred to next pass", summary)

    def test_urgent_announcements_still_get_through(self):
        """An assignment posted second period and due at 6pm is news he
        can still act on."""
        ids, _, _ = self._allocate(
            [self._cand("due-today", 4, kind="capture")], in_class=True
        )
        self.assertEqual(ids, ["due-today"])

    def test_unhurried_announcements_wait_for_the_bell(self):
        ids, _, report = self._allocate(
            [self._cand("syllabus", 3, kind="capture")], in_class=True
        )
        self.assertEqual(ids, [])
        self.assertEqual(report.deferred, 1)
        self.assertTrue(report.class_blocked)

    def test_outside_class_everything_flows_as_before(self):
        ids, _, report = self._allocate(
            [self._cand("essay", 5), self._cand("syllabus", 3, kind="capture")],
            in_class=False,
        )
        self.assertEqual(sorted(ids), ["essay", "syllabus"])
        self.assertEqual(report.deferred, 0)
        self.assertFalse(report.class_blocked)

    def test_class_blocked_is_not_set_when_class_held_nothing(self):
        """During class with only urgent announcements due, nothing was
        actually held -- the log must not claim otherwise."""
        _, _, report = self._allocate(
            [self._cand("due-today", 4, kind="capture")], in_class=True
        )
        self.assertFalse(report.class_blocked)


class TheDigestIsWiredIntoAllocation(unittest.TestCase):
    """
    Day one: eight teachers post at once, the sweep finds all of it in a
    single pass, and the only remaining bound is MAX_NOTIFICATIONS_PER_PASS
    against a two-minute dispatcher. Thirty items becomes thirty pushes
    over twenty minutes, in first period. That is the failure this
    collapses into one buzz.
    """

    def _cadence(self, threshold=3, max_per_pass=3, daily_budget=10):
        c = mock.Mock(
            max_per_pass=max_per_pass,
            daily_budget=daily_budget,
            load_scale=1.0,
            capture_digest_threshold=threshold,
        )
        c.for_load.return_value = c
        c.in_class_hours.return_value = False
        return c

    def _cap(self, item_id, category="AP Lang"):
        return (
            dict(ITEM, id=item_id, name=item_id, category=category, due_date=None),
            replace(REMINDER, priority=3, kind="capture"),
        )

    def _nag(self, item_id):
        return (
            dict(ITEM, id=item_id, name=item_id, due_date=None),
            replace(REMINDER, priority=5, kind="recurring"),
        )

    def _allocate(self, candidates, sent_today=0, **kw):
        report = pipeline.Report()
        with mock.patch.object(pipeline.reminders, "due_datetime", return_value=None):
            allocation = pipeline._allocate(
                candidates, set(), self._cadence(**kw), report, sent_today
            )
        return allocation, report

    def test_a_semester_start_becomes_one_push(self):
        allocation, report = self._allocate([self._cap(f"i{n}") for n in range(30)])
        self.assertIsNotNone(allocation.digest)
        self.assertEqual(len(allocation.digest_items), 30)
        self.assertEqual(allocation.sends, [])
        # The 30 are DELIVERED, not deferred -- the whole point.
        self.assertEqual(report.deferred, 0)

    def test_every_item_is_covered_not_just_the_per_pass_cap(self):
        """
        Without the digest, max_per_pass=3 let only three announce and
        deferred the other 27 to later passes -- which is how thirty
        pushes over twenty minutes happened in the first place.
        """
        allocation, _ = self._allocate([self._cap(f"i{n}") for n in range(30)])
        self.assertEqual(
            {i["id"] for i in allocation.digest_items},
            {f"i{n}" for n in range(30)},
        )

    def test_an_ordinary_day_is_untouched(self):
        allocation, _ = self._allocate([self._cap(f"i{n}") for n in range(2)])
        self.assertIsNone(allocation.digest)
        self.assertEqual(len(allocation.sends), 2)

    def test_the_digest_leaves_room_for_nags_in_the_same_pass(self):
        """It occupies ONE per-pass slot, not one per item."""
        allocation, _ = self._allocate(
            [self._cap(f"i{n}") for n in range(5)] + [self._nag("essay")],
            max_per_pass=3, daily_budget=99,
        )
        self.assertIsNotNone(allocation.digest)
        self.assertEqual([c[0]["id"] for c in allocation.sends], ["essay"])

    def test_digested_items_spend_nag_budget_by_item(self):
        """
        Announcements consuming nag budget is the existing rule, and the
        information load is what it is really about: one buzz carrying
        twenty new assignments is still twenty new assignments to absorb,
        so the evening's nagging stands down.
        """
        allocation, report = self._allocate(
            [self._cap(f"i{n}") for n in range(20)] + [self._nag("essay")],
            daily_budget=10,
        )
        self.assertIsNotNone(allocation.digest)
        self.assertEqual(allocation.sends, [])
        self.assertTrue(report.budget_blocked)
