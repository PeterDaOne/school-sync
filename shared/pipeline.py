"""
The sync pass itself, shared by both entrypoints.

local_sync.py and cloud_sync.py were running near-identical loops with
subtly different error handling — local caught everything at the top
level and logged, cloud caught nothing at all, so a single malformed
item could kill a cloud run before Calendar sync happened for any of the
remaining items. One implementation, one error policy.

ERROR POLICY, stated deliberately rather than just made consistent:

  - Per item, errors are caught and recorded. One assignment with a bad
    due date must never stop the other twenty from syncing. This is the
    difference that matters most: the failure of one item is not
    evidence that the next item will fail.

  - Per run, if anything failed, the process exits non-zero. Locally
    that surfaces in sync-error.log; in GitHub Actions it turns the run
    red instead of letting a silently-broken sync look healthy for
    weeks. A sync system that fails invisibly is worse than one that
    fails loudly, because you keep trusting it.

  - Errors that mean *nothing* can work (a missing token, an
    unreachable Notion) are raised, not swallowed. Retrying them per
    item would just produce N copies of the same message.

  - An undelivered push counts toward the exit code even though it is
    not an item failure. This was a real hole: notify() returning False
    correctly skips the Last Reminded stamp so the reminder retries, but
    it used to touch nothing else, so a run that dropped every single
    reminder exited 0 and looked perfectly healthy in the Actions tab.
    A misspelled NTFY_TOPIC secret hid behind that for a full day on
    2026-07-29. Notifications are the product; silent total failure is
    the loudest thing this system can do wrong.

  - A deferred reminder (skipped only because MAX_NOTIFICATIONS_PER_PASS
    was already hit this pass) does NOT count toward the exit code —
    unlike notify_failures, this is expected, routine behavior, not a
    problem. Last Reminded is left untouched so it's simply retried next
    pass (5 min away on the cloud path), same as a quiet-hours skip.
"""

import sys
import traceback
from dataclasses import dataclass, field
from datetime import timedelta

from . import calendar_client, commands, notion_client, reminders, state, timeutil
from .notify import build_mark_done_action, notify


@dataclass
class Report:
    synced: int = 0
    reminded: int = 0
    failures: list[str] = field(default_factory=list)
    # Reminders that were due, cleared every guard, and still did not
    # reach the phone because notify() returned False. Counted apart
    # from `failures` because the item itself is fine — nothing about it
    # needs fixing and it will retry unprompted on the next pass. It
    # still has to affect the exit code: see `ok`.
    notify_failures: int = 0
    # Reminders that were due but skipped purely because this pass
    # already hit MAX_NOTIFICATIONS_PER_PASS. Routine, not a failure —
    # see the module docstring's error policy.
    deferred: int = 0
    # Mark-done commands (from the notification action button, see
    # shared/commands.py) successfully applied this pass.
    commands_applied: int = 0

    @property
    def ok(self) -> bool:
        return not self.failures and not self.notify_failures

    def summary(self, source: str) -> str:
        parts = []
        if self.synced:
            parts.append(f"synced {self.synced} item(s)")
        if self.reminded:
            parts.append(f"sent {self.reminded} reminder(s)")
        if self.deferred:
            parts.append(f"{self.deferred} reminder(s) deferred to next pass")
        if self.commands_applied:
            parts.append(f"applied {self.commands_applied} mark-done command(s)")
        if self.notify_failures:
            parts.append(f"{self.notify_failures} reminder(s) UNDELIVERED")
        if self.failures:
            parts.append(f"{len(self.failures)} item(s) FAILED")
        return f"[{source}] " + (", ".join(parts) if parts else "nothing to do")


def _should_send(item: dict, reminder: "reminders.Reminder", recheck: bool) -> bool:
    """
    Final guard against the cloud and the Mac both sending the same
    reminder. reminders.due_for_reminder's lag already separates the two
    in time; this closes the last sliver by re-reading Last Reminded
    immediately before the send and bailing if it changed underneath us.

    Only cloud_sync passes recheck=True — local_sync is the fast path and
    has nothing to defer to.
    """
    if not recheck:
        return True
    try:
        current = notion_client.get_last_reminded(item["id"])
    except Exception as e:
        print(f"[pipeline] recheck failed for {item['name']!r}: {e}", file=sys.stderr)
        return False  # when unsure, stay quiet rather than risk a duplicate
    if current != item.get("last_reminded"):
        return False  # local_sync got there first
    return True


def run_sync_pass(
    source: str,
    *,
    send_reminders: bool = True,
    lag: timedelta = timedelta(0),
    recheck_before_send: bool = False,
    command_window: str = "2m",
) -> Report:
    """
    One full Notion -> Calendar (+ optional reminder) pass.

    Returns a Report rather than printing and exiting, so the caller
    decides how loud to be and what exit code to use.
    """
    report = Report()
    cadence = reminders.Cadence.from_env()
    now = timeutil.now()

    # Mark-done commands are a distinct user action from whether
    # automatic reminders are enabled, so this runs unconditionally —
    # not gated behind send_reminders / CLOUD_REMINDERS. commands.py
    # never raises on its own, but the try/except here is defense in
    # depth: a genuinely unexpected error here must not stop Calendar
    # sync for every other item.
    try:
        page_ids = commands.poll_mark_done(command_window)
        applied, errors = commands.apply_mark_done(page_ids)
        report.commands_applied = applied
        report.failures.extend(errors)
    except Exception as e:
        report.failures.append(f"mark-done polling failed: {e}")
        traceback.print_exc(file=sys.stderr)

    sent_this_pass = 0

    for page in notion_client.get_all_items():
        # extract_fields is outside the try only so a genuinely
        # unparseable page still gets named in the error below.
        try:
            item = notion_client.extract_fields(page)
        except Exception as e:
            report.failures.append(f"unparseable page {page.get('id', '?')}: {e}")
            traceback.print_exc(file=sys.stderr)
            continue

        try:
            if state.needs_sync(page):
                calendar_client.upsert_event(item)
                notion_client.mark_synced(item["id"], timeutil.utc_now_iso())
                report.synced += 1
        except Exception as e:
            report.failures.append(f"calendar sync failed for {item['name']!r}: {e}")
            traceback.print_exc(file=sys.stderr)

        if not send_reminders:
            continue

        # Reminder timing depends on the clock (due date, quiet hours),
        # not on whether the item was just edited, so this runs on every
        # pass regardless of the needs_sync check above.
        try:
            reminder = reminders.due_for_reminder(item, now, cadence=cadence, lag=lag)
            if not reminder:
                continue
            if sent_this_pass >= cadence.max_per_pass:
                # Left untouched, not lost: retried next pass. This is
                # what actually breaks up bursts jitter alone can't
                # smooth out (the 05:00 quiet-hours release, or adding
                # ten items by hand at once).
                report.deferred += 1
                continue
            if not _should_send(item, reminder, recheck_before_send):
                continue
            # Only stamp Last Reminded if the push actually landed —
            # stamping after a failed send consumes the slot and the
            # reminder is lost rather than retried next pass.
            if notify(
                reminder.title,
                reminder.body,
                click_url=item.get("url"),
                priority=reminder.priority,
                tags=reminder.tags,
                actions=build_mark_done_action(item["id"]),
            ):
                notion_client.mark_reminded(item["id"], timeutil.utc_now_iso())
                report.reminded += 1
                sent_this_pass += 1
            else:
                # Deliberately NOT added to `failures`: the item synced
                # fine and the unstamped Last Reminded means it retries
                # next pass. But a run where every push was dropped must
                # not look healthy — that exact situation (a misspelled
                # NTFY_TOPIC secret) stayed green for a full day.
                report.notify_failures += 1
        except Exception as e:
            report.failures.append(f"reminder failed for {item['name']!r}: {e}")
            traceback.print_exc(file=sys.stderr)

    return report


def finish(report: Report, source: str) -> int:
    """Print the summary and return the process exit code."""
    print(report.summary(source))
    for failure in report.failures:
        print(f"[{source}] FAILED: {failure}", file=sys.stderr)
    if report.notify_failures:
        print(
            f"[{source}] {report.notify_failures} reminder(s) were due but could "
            f"not be delivered — see the [notify] line above for the cause. They "
            f"were NOT marked as reminded and will retry on the next pass.",
            file=sys.stderr,
        )
    return 0 if report.ok else 1
