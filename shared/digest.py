"""
Rolling several capture announcements into one push.

WHY THIS EXISTS
---------------
Capture announcements are exempt from DAILY_NOTIFICATION_BUDGET, and that
exemption is correct: an item can be captured exactly once, so the
lifetime total is bounded by the number of items that ever exist. There
is no runaway to guard against — on an ordinary day.

The first day of a semester is not an ordinary day. Eight teachers post
syllabi, first readings and setup work at once; the sweep finds all of it
in a single pass; and the only bound left is MAX_NOTIFICATIONS_PER_PASS
(3) against a dispatcher running every two minutes. Thirty new items
becomes thirty pushes over twenty minutes, in first period, on a phone
Peter may not be allowed to take out.

Severity here is not the volume, it is the timing. This system's
credibility with its only user is set in week one, and a phone that
buzzes thirty times before second period gets its topic muted — after
which every other guarantee in this codebase is irrelevant, because
nothing reaches him at all.

WHAT A DIGEST COSTS, AND WHY THERE IS A THRESHOLD
-------------------------------------------------
A digest cannot carry a per-item due date, and it cannot carry the
Mark-done button (one button, N items). Those are real losses, so they
are only paid when the alternative is worse: at or below
`threshold` announcements the individual pushes are kept exactly as they
were. This module returning None is the normal case.

Nothing is dropped or deferred to tomorrow. Every item in a digest is
announced, and pipeline stamps Last Reminded on all of them together, so
none re-announces.

Pure — no network, no Notion, no config. The caller supplies the
threshold (from Cadence) and the click URL.
"""

from . import classmap
from .reminders import Reminder

# Plural noun per Notion `Type`, so a same-type batch reads naturally
# ("6 new assignments") instead of generically ("6 new items").
_TYPE_PLURAL = {
    "Assignments": "assignments",
    "Tasks": "tasks",
    "Events": "events",
}

# Emoji for a batch spanning several types, where no single TYPE_EMOJI
# applies. Deliberately not a `tags` value: ntfy renders a tag matching an
# emoji short code and PREPENDS it to the title, so putting it in the
# title text keeps it exactly where it was intended.
MIXED_EMOJI = "📚"

# Categories listed in full before the tail is summarised. Past this, the
# body stops being scannable on a lock screen.
MAX_GROUPS_SHOWN = 6

# Items with no `For` set. Reads as a real grouping rather than a blank.
UNCATEGORIZED_LABEL = "No class"

# A digest of ONE is strictly worse than the individual push it replaces:
# same number of buzzes, but the due date and the Mark-done button are
# gone and nothing is gained. Enforced independently of the threshold so
# a misconfigured CAPTURE_DIGEST_THRESHOLD of 0 cannot produce one (and,
# incidentally, so the title is never "1 new assignments").
MIN_ITEMS = 2


def build(
    announcements: list[tuple[dict, Reminder]], threshold: int
) -> Reminder | None:
    """
    One Reminder summarising `announcements`, or None to send them
    individually.

    Returns None at or below `threshold`, which is the common case: this
    only fires on a bulk import (a semester start, a backfill, a long
    outage catching up).
    """
    if len(announcements) < MIN_ITEMS or len(announcements) <= max(0, threshold):
        return None

    items = [item for item, _ in announcements]
    reminders = [reminder for _, reminder in announcements]

    types = {item.get("type_name") for item in items}
    if len(types) == 1 and (only := next(iter(types))) in _TYPE_PLURAL:
        noun = _TYPE_PLURAL[only]
        emoji = classmap.TYPE_EMOJI.get(only, MIXED_EMOJI)
    else:
        noun = "items"
        emoji = MIXED_EMOJI

    counts: dict[str, int] = {}
    for item in items:
        label = item.get("category") or UNCATEGORIZED_LABEL
        counts[label] = counts.get(label, 0) + 1

    # Biggest group first, name as the tiebreak so the wording is stable
    # for a given batch rather than depending on Notion's row order.
    groups = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    shown = groups[:MAX_GROUPS_SHOWN]
    body = " · ".join(f"{name} ({n})" for name, n in shown)
    if len(groups) > MAX_GROUPS_SHOWN:
        body += f" · +{len(groups) - MAX_GROUPS_SHOWN} more"

    # The loudest constituent wins: a batch containing something already
    # overdue must not be delivered at the lock-screen weight of a batch
    # of three-week-out syllabi.
    priority = max(reminder.priority for reminder in reminders)

    return Reminder(
        title=f"{emoji} {len(items)} new {noun}".strip(),
        body=body,
        priority=priority,
        tags="rotating_light" if priority >= 4 else "",
        kind="capture",
    )
