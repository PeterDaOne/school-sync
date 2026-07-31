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

  - A deferred reminder (skipped because MAX_NOTIFICATIONS_PER_PASS or
    the daily budget was already spent) does NOT count toward the exit
    code — unlike notify_failures, this is expected, routine behavior,
    not a problem. Last Reminded is left untouched so it's simply
    reconsidered next pass, same as a quiet-hours skip.

    It is still reported in a way that distinguishes the two causes. A
    pass-capped reminder goes out a minute later; a budget-blocked one
    waits for midnight. Logging both as "deferred to next pass" is what
    made a real 3h20m notification outage indistinguishable from normal
    throttling on 2026-07-31.
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
    # Reminders that were due but not sent this pass, because either
    # MAX_NOTIFICATIONS_PER_PASS or the daily budget was already spent.
    # Routine, not a failure — see the module docstring's error policy.
    deferred: int = 0
    # True when the deferral above is the DAILY budget, not the per-pass
    # cap. The distinction is the whole point: a pass-capped reminder
    # really does go out on the next pass a minute later, but a
    # budget-blocked one is held until midnight. Both used to log the
    # identical phrase "deferred to next pass", which is how a genuine
    # 3h20m outage on 2026-07-31 looked exactly like healthy throttling
    # in sync.log — 137 consecutive passes of it.
    budget_blocked: bool = False
    # Mark-done commands (from the notification action button, see
    # shared/commands.py) successfully applied this pass.
    commands_applied: int = 0
    # Reminders that came due inside quiet hours. The slot was SPENT
    # (Last Reminded stamped) but nothing was pushed — see
    # reminders.due_for_reminder. Not a failure and not a deferral: the
    # item will not ask again until a full interval has passed.
    suppressed: int = 0
    # Diagnostics, not outcomes: how much the cadence was stretched for
    # the current workload, and how many notifications had already gone
    # out today when this pass started. Both are otherwise invisible and
    # both are the first things worth knowing when volume looks wrong.
    load_scale: float = 1.0
    sent_today: int = 0
    daily_budget: int = 0

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
            parts.append(
                # Says "nag budget" because that is what it governs:
                # capture announcements are exempt, so sent_today can
                # legitimately exceed the cap on a day with a lot of new
                # items. Reporting it as "15/6" read like a violation.
                f"{self.deferred} reminder(s) held until tomorrow "
                f"(nag budget of {self.daily_budget} spent; "
                f"{self.sent_today} notification(s) sent today)"
                if self.budget_blocked
                else f"{self.deferred} reminder(s) deferred to next pass"
            )
        if self.suppressed:
            parts.append(f"{self.suppressed} silenced by quiet hours")
        if self.commands_applied:
            parts.append(f"applied {self.commands_applied} mark-done command(s)")
        if self.notify_failures:
            parts.append(f"{self.notify_failures} reminder(s) UNDELIVERED")
        if self.failures:
            parts.append(f"{len(self.failures)} item(s) FAILED")
        if self.load_scale > 1.0 and (self.reminded or self.deferred):
            parts.append(f"cadence x{self.load_scale:.1f} for load")
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

    candidates: list[tuple[dict, "reminders.Reminder"]] = []
    reminded_today: set[str] = set()
    today = now.astimezone(timeutil.school_tz()).date()

    # Extract everything up front: both the load scaling and the daily
    # cap are properties of the WHOLE workload, so they have to be known
    # before the first reminder decision is made.
    parsed: list[tuple[dict, dict]] = []
    for page in notion_client.get_all_items():
        try:
            parsed.append((page, notion_client.extract_fields(page)))
        except Exception as e:
            report.failures.append(f"unparseable page {page.get('id', '?')}: {e}")
            traceback.print_exc(file=sys.stderr)

    def _is_today(item: dict) -> bool:
        if not item.get("last_reminded"):
            return False
        try:
            return timeutil.parse(item["last_reminded"]).astimezone(
                timeutil.school_tz()
            ).date() == today
        except (ValueError, TypeError):
            return False

    active = sum(
        1
        for _, i in parsed
        if not i.get("is_complete") and i.get("type_name") in ("Assignments", "Tasks")
    )
    cadence = cadence.for_load(active)
    # Exact count of notifications already delivered today, summed from
    # per-item counters dated by their own Last Reminded stamp.
    sent_today = sum(i.get("reminders_today", 0) for _, i in parsed if _is_today(i))
    report.load_scale = cadence.load_scale
    report.sent_today = sent_today
    report.daily_budget = cadence.daily_budget

    for page, item in parsed:
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

        if _is_today(item):
            reminded_today.add(item["id"])

        # Reminder timing depends on the clock (due date, quiet hours),
        # not on whether the item was just edited, so this runs on every
        # pass regardless of the needs_sync check above.
        try:
            reminder = reminders.due_for_reminder(item, now, cadence=cadence, lag=lag)
            if not reminder:
                continue
            if reminder.silent:
                # Came due inside quiet hours. Spend the slot without
                # pushing, so it does NOT pile up and detonate the moment
                # quiet hours end — see reminders.due_for_reminder.
                # The counter must still be REWRITTEN, even though nothing
                # was delivered. This stamp moves Last Reminded forward --
                # and Last Reminded is what dates the counter. Leaving the
                # count alone while moving its date carries yesterday's
                # total into today, so the day opens with a phantom spent
                # budget. That bug collapsed a simulated week from 41
                # pushes to 7 before it was caught: quiet hours run every
                # night, so it poisoned every single morning.
                #
                # Carry today's real delivered count (0 on a new day).
                notion_client.mark_reminded(
                    item["id"],
                    timeutil.utc_now_iso(),
                    item.get("reminders_today", 0) if _is_today(item) else 0,
                )
                report.suppressed += 1
                continue
            candidates.append((item, reminder))
        except Exception as e:
            report.failures.append(f"reminder failed for {item['name']!r}: {e}")
            traceback.print_exc(file=sys.stderr)

    for item, reminder in _allocate(candidates, reminded_today, cadence, report, sent_today):
        try:
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
                # Reset to 1 rather than increment when the item's own
                # previous reminder was on an earlier day -- that is what
                # makes the counter self-reset at midnight without a
                # cleanup job.
                count = (item.get("reminders_today", 0) + 1) if _is_today(item) else 1
                notion_client.mark_reminded(item["id"], timeutil.utc_now_iso(), count)
                report.reminded += 1
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


def _urgency(item: dict, reminder: "reminders.Reminder") -> tuple:
    """
    Sort key, most urgent first. ntfy priority is the primary signal
    because it already encodes overdue-vs-due-today-vs-later; soonest due
    date breaks ties, then High priority ahead of Low.
    """
    due = reminders.due_datetime(item)
    rank = {"High": 0, "Medium": 1, "Low": 2}.get(item.get("priority") or "Medium", 1)
    return (-reminder.priority, due.timestamp() if due else float("inf"), rank)


def _allocate(
    candidates: list[tuple[dict, "reminders.Reminder"]],
    reminded_today: set[str],
    cadence: "reminders.Cadence",
    report: "Report",
    sent_today: int = 0,
) -> list[tuple[dict, "reminders.Reminder"]]:
    """
    Decide which of this pass's due reminders actually get sent.

    ANNOUNCEMENTS ARE NOT NAGS, AND THE BUDGET MUST NOT TREAT THEM ALIKE
    -------------------------------------------------------------------
    This is the ordering rule that matters most, and getting it wrong was
    a real, user-visible failure on 2026-07-31: a Gmail-captured
    assignment sat in Notion, due the next day, and was never announced
    for over three hours, while the daily budget was spent re-nagging two
    stale junk Tasks Peter had already been told about days earlier. He
    reasonably concluded the Gmail capture layer was broken. It wasn't —
    the notification was.

    Two things went wrong and both are fixed here:

      1. `_urgency` ranks by ntfy priority, and capture reminders used to
         carry the default 3 while an overdue nag carries 5. So the ONE
         message that says "this thing exists" always sorted BEHIND
         messages about things Peter already knew about.
      2. The daily budget was applied to both alike, so once it was
         spent, announcements stopped entirely for the rest of the day.

    A capture reminder is the only time an item is ever announced. Drop
    it and that information is never re-sent — the item quietly joins the
    recurring pool as though Peter had always known. A recurring reminder
    is the opposite: repeating is its entire purpose, and one dropped
    today is re-offered an interval later having lost nothing.

    So announcements go FIRST and are exempt from the daily budget. That
    is safe because they are self-limiting in a way nags are not: an item
    can be captured exactly once (the capture branch fires only while
    Last Reminded is unset), so the lifetime total is bounded by the
    number of items that ever exist. There is no runaway to guard
    against. `max_per_pass` still bounds a bulk import to a trickle.

    Nags then take whatever budget is left, keeping Peter's 2026-07-30
    two-phase rule among themselves:

      1. Nags for items not yet reminded today go first, most urgent
         first — the "one each before anyone gets a second" guarantee.
      2. Items already reminded today compete for what remains.

    Announcements sent this pass DO count against the nag budget. That is
    deliberate: on a day with a lot of new items, Peter's attention is
    already spent on real news, and stacking the usual nagging on top of
    it is exactly the volume problem the budget exists to prevent.

    Nothing is dropped, only DEFERRED: Last Reminded is untouched for
    anything not sent, so it is reconsidered next pass (and tomorrow, on
    a fresh budget).
    """
    ordered = sorted(candidates, key=lambda c: _urgency(*c))

    announcements = [c for c in ordered if c[1].kind == "capture"]
    nags = [c for c in ordered if c[1].kind != "capture"]

    # Exempt from the daily budget; bounded only by the per-pass cap.
    sending = announcements[: cadence.max_per_pass]

    # Whatever is left of both the daily budget and this pass's cap.
    room = max(0, cadence.daily_budget - sent_today - len(sending))
    slots_left = max(0, cadence.max_per_pass - len(sending))
    first_time = [c for c in nags if c[0]["id"] not in reminded_today]
    repeats = [c for c in nags if c[0]["id"] in reminded_today]
    sending += (first_time + repeats)[: min(room, slots_left)]

    report.deferred += len(candidates) - len(sending)
    # Distinguish "come back in a minute" from "come back tomorrow".
    if report.deferred and room == 0:
        report.budget_blocked = True
    return sending


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
