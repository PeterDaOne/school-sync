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
from datetime import timedelta
from unittest import mock

import tests.context  # noqa: F401  (path + timezone setup)

from shared import pipeline, reminders


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
            cadence = mock.Mock(max_per_pass=max_per_pass)
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
