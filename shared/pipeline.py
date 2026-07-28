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
"""

import sys
import traceback
from dataclasses import dataclass, field
from datetime import timedelta

from . import calendar_client, notion_client, reminders, state, timeutil
from .notify import notify

NOTIFY_TITLE = "School Sync"


@dataclass
class Report:
    synced: int = 0
    reminded: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def summary(self, source: str) -> str:
        parts = []
        if self.synced:
            parts.append(f"synced {self.synced} item(s)")
        if self.reminded:
            parts.append(f"sent {self.reminded} reminder(s)")
        if self.failures:
            parts.append(f"{len(self.failures)} item(s) FAILED")
        return f"[{source}] " + (", ".join(parts) if parts else "nothing to do")


def _should_send(item: dict, message: str, recheck: bool) -> bool:
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
) -> Report:
    """
    One full Notion -> Calendar (+ optional reminder) pass.

    Returns a Report rather than printing and exiting, so the caller
    decides how loud to be and what exit code to use.
    """
    report = Report()
    cadence = reminders.Cadence.from_env()
    now = timeutil.now()

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
            message = reminders.due_for_reminder(item, now, cadence=cadence, lag=lag)
            if not message:
                continue
            if not _should_send(item, message, recheck_before_send):
                continue
            # Only stamp Last Reminded if the push actually landed —
            # stamping after a failed send consumes the slot and the
            # reminder is lost rather than retried next pass.
            if notify(NOTIFY_TITLE, message, click_url=item.get("url")):
                notion_client.mark_reminded(item["id"], timeutil.utc_now_iso())
                report.reminded += 1
        except Exception as e:
            report.failures.append(f"reminder failed for {item['name']!r}: {e}")
            traceback.print_exc(file=sys.stderr)

    return report


def finish(report: Report, source: str) -> int:
    """Print the summary and return the process exit code."""
    print(report.summary(source))
    for failure in report.failures:
        print(f"[{source}] FAILED: {failure}", file=sys.stderr)
    return 1 if report.failures else 0
