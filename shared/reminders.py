"""
Reminder cadence engine.

Decides, for a single Notion item and the current moment, whether a
notification is due right now — and if so, what it should say. This
module sends nothing (that's shared/notify.py) and touches Notion not
at all: the caller is responsible for calling mark_reminded() after a
*successful* send. Pure logic, no I/O — which is why it's the one part
of this system with real test coverage (tests/test_reminders.py).

Cadence rules:
  - Capture: fires once, the first time an item is seen with no Last
    Reminded stamp, regardless of source (Email / Classroom / Manual)
    or type. Keeps an absolute due date (see relative_due() below for
    why recurring reminders use relative wording instead).

  - Assignments / Tasks: recurring until Status reaches the complete
    group, on a CONTINUOUS formula rather than fixed tiers (replaced
    2026-07-29 — the old three-tier system put every item in the same
    tier on an identical interval, so items that happened to get
    reminded in the same pass stayed phase-locked together forever,
    producing bursts of 5+ simultaneous pushes):

        raw_hours = clamp(alpha[type] * days_until, floor[type], ceiling[type])
        hours     = raw_hours * priority_multiplier[priority] * jitter_factor(id)
        hours     = max(hours, min_interval)          # hard floor, 2h

    `days_until` is signed and fractional — negative means overdue. One
    clamp handles both directions: a very negative days_until makes
    `alpha * days_until` very negative, which the clamp pins to `floor`
    — so "remind at a constant rate forever once overdue" falls out of
    the same formula as "decay toward the floor as due-time approaches,"
    with no special-case branch and no discontinuity at the due moment.

    Assignments and Tasks intentionally have different floors: Peter
    gets assignments nagged early (high ceiling reached ~14 days out),
    so their overdue floor (2h) doesn't need to be aggressive. Tasks
    stay quiet until close (~3 days out) and so DO need an aggressive
    push once missed — their overdue floor is 1h. Priority multiplies
    the whole thing, including the floor, so a High-priority overdue
    item still nags harder than a Low-priority one.

    OVERDUE DECAY (added 2026-07-30) then backs that off again: the
    interval doubles for each full day an item stays overdue, up to once
    a day. "Constant rate forever once overdue" was the previous
    behavior and it was measurably wrong -- simulated against Peter's
    real items it produced 238 pushes in 7 days, 167 from a single
    overdue task. See Cadence.overdue_decay.

    Jitter is a deterministic (same item -> same factor, always) ±25%
    perturbation derived from the page id. It's what actually breaks
    the phase-lock: two items in the same tier separate on their very
    first reminder and never re-converge, without needing any shared
    state between them.

  - Events: exactly three fixed-point reminders — 3 days before, 1 day
    before, and the morning of (all at EVENT_REMINDER_HOUR, a fixed
    local time, computed from CALENDAR-day difference so a 9pm event 3
    days out fires at 7am, not at hour-72-exactly) — plus a 4th,
    1-hour-before, but ONLY for events with a time component (a
    date-only event has no "1 hour before" to compute). Priority and
    jitter don't apply to Events — four fixed points don't need
    desynchronizing.

Every type is gated on Status: nothing reminds once an item reaches the
complete group. An earlier version of this docstring claimed Events were
exempt, but the code has always checked is_complete first for all types,
and that behavior is the correct one — marking an event Done is Peter
saying he's handled it, and calendar_client deletes its Calendar entry at
the same moment. Docs now match code; tests pin it.

Quiet hours CONSUME the slot rather than queueing it (changed
2026-07-30): a reminder that comes due at 2am is stamped but never
pushed, and the item waits a full interval for its next one. Previously
the slot was left untouched, which meant everything held overnight fired
the instant quiet hours ended -- observed live as three pushes at
05:00:48/:49/:50. due_for_reminder returns those as `silent=True`
Reminders; the caller must stamp Last Reminded and NOT send.

THE `lag` PARAMETER — how double-firing is prevented
----------------------------------------------------
Two schedulers evaluate the same items: local_sync.py every 60s on the
Mac (only while it's awake) and cloud_sync.py on GitHub's runners
(always). Notion has no compare-and-swap, so nothing stops both from
reading the same stale Last Reminded and both sending.

Rather than lock, the two are separated in time. local_sync passes
lag=0 and gets first refusal on everything. cloud_sync passes
CLOUD_REMINDER_LAG (default 5 min) and will only report a reminder
that came due at least that long ago. If the Mac were awake, local_sync
would have fired it within 60 seconds — so a reminder still unfired that
long later is proof the Mac is asleep, and the cloud can safely take
over. The two windows do not overlap.

Note the split: `lag` shifts the *cadence* comparisons, but quiet hours
are always evaluated against the true `now`. Quiet hours are a question
about the delivery moment ("is it OK to buzz his phone right now"), not
about when the reminder became due.
"""

import hashlib
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, time as dtime

from . import classmap, config, timeutil

DEFAULT_QUIET_START = "00:00"
DEFAULT_QUIET_END = "05:00"
DEFAULT_CLOUD_LAG_MINUTES = 5.0

DEFAULT_ASSIGNMENT_ALPHA = 3.4      # hours/day -> ceiling (~48h) at ~14 days out
DEFAULT_ASSIGNMENT_FLOOR = 2.0
DEFAULT_ASSIGNMENT_CEILING = 48.0
DEFAULT_TASK_ALPHA = 24.0           # hours/day -> ceiling (~72h) at ~3 days out
DEFAULT_TASK_FLOOR = 1.0            # more aggressive than Assignments once overdue
DEFAULT_TASK_CEILING = 72.0
DEFAULT_PRIORITY_MULTIPLIER_HIGH = 0.5
DEFAULT_PRIORITY_MULTIPLIER_MEDIUM = 1.0
DEFAULT_PRIORITY_MULTIPLIER_LOW = 2.0
DEFAULT_JITTER_FRACTION = 0.25
DEFAULT_MAX_PER_PASS = 3
DEFAULT_EVENT_REMINDER_HOUR = "07:00"

# Overdue back-off (added 2026-07-30). Interval multiplies by
# OVERDUE_DECAY_BASE for each full day an item stays overdue, until it
# hits OVERDUE_MAX_INTERVAL. See Cadence.overdue_decay for why.
DEFAULT_OVERDUE_DECAY_BASE = 2.0
DEFAULT_OVERDUE_MAX_INTERVAL = 24.0
# Enough doublings to blow past any sane OVERDUE_MAX_INTERVAL_HOURS.
_MAX_DECAY_DOUBLINGS = 16

# HARD CEILING on notifications delivered in one local day, across all
# items. Backed by real per-item counts (notion_client.REMINDER_COUNT_PROP),
# so unlike the earlier version of this constant it is an exact daily
# total rather than a per-pass guess.
#
# It exists because notification volume was roughly LINEAR in item count:
# 4 items -> 41 pushes/week, but a realistic 24-item load -> 333/week
# with a peak of 69 in a single day. Load scaling smooths the cadence;
# this is the backstop for the days it isn't enough.
DEFAULT_DAILY_BUDGET = 6

# THE HARD FLOOR: no item may ever notify twice inside this window.
# Peter's rule, 2026-07-30, and it is deliberately blunt -- it overrides
# every other knob in this file.
#
# It was 0.25 (15 minutes), which was not a real limit: a High-priority
# overdue Task took the 1h task floor x 0.5 priority multiplier and could
# legitimately fire every THIRTY MINUTES. Two hours is the rate at which
# a stack cannot build in the first place, which is the actual goal --
# repeats stack on iOS and cannot be collapsed (see notify.py), so the
# only real defense is not generating them.
#
# Enforced in TWO places on purpose:
#   1. Cadence.interval_hours clamps the computed interval (covers the
#      Assignments/Tasks cadence math).
#   2. due_for_reminder refuses outright if Last Reminded is inside the
#      window (covers EVERY path, including Events' fixed tiers and any
#      future one, and survives someone re-tuning the constants above).
DEFAULT_MIN_INTERVAL_HOURS = 2.0

# LOAD SCALING (added 2026-07-30). Every interval is multiplied by
# (active items / LOAD_SCALE_TARGET_ITEMS), floored at 1.0.
#
# Without it the system's notification volume is roughly LINEAR in item
# count, because each item is independently entitled to its own cadence.
# Simulated against a realistic post-capture load: 4 items -> 41
# pushes/week, but 24 items -> 333/week and a peak of 69 in one day.
# That is what made "turn on Classroom capture" unshippable.
#
# With it, more items means each one nags proportionally less and total
# volume stays roughly flat (~65/week) at ANY item count. Stateless: the
# pipeline already knows how many active items there are, so this needs
# no schema change and nothing persisted.
DEFAULT_LOAD_SCALE_TARGET = 5


# EVERY environment variable Cadence.from_env() reads, in one list.
#
# It exists because these names live in three places that must agree —
# this module, generate_plist.py's OPTIONAL_KEYS (the Mac), and the
# `env:` block of .github/workflows/sync.yml (the cloud) — and they
# drifted. Five knobs added on 2026-07-30 (OVERDUE_DECAY_BASE,
# OVERDUE_MAX_INTERVAL_HOURS, DAILY_NOTIFICATION_BUDGET,
# MIN_INTERVAL_HOURS, LOAD_SCALE_TARGET_ITEMS) reached the workflow but
# never reached the plist.
#
# That divergence is silent and nasty: setting one in .env would tune the
# cloud while the Mac quietly kept its default, so the two schedulers
# would disagree about how hard to nag the same item — the exact failure
# the workflow's own comments warn about. generate_plist imports this
# list, and a test asserts the workflow carries every name in it.
TUNABLE_ENV_VARS = (
    "QUIET_HOURS_START",
    "QUIET_HOURS_END",
    "ASSIGNMENT_ALPHA_HOURS_PER_DAY",
    "ASSIGNMENT_FLOOR_HOURS",
    "ASSIGNMENT_CEILING_HOURS",
    "TASK_ALPHA_HOURS_PER_DAY",
    "TASK_FLOOR_HOURS",
    "TASK_CEILING_HOURS",
    "PRIORITY_MULTIPLIER_HIGH",
    "PRIORITY_MULTIPLIER_MEDIUM",
    "PRIORITY_MULTIPLIER_LOW",
    "REMINDER_JITTER_FRACTION",
    "MAX_NOTIFICATIONS_PER_PASS",
    "EVENT_REMINDER_HOUR",
    "OVERDUE_DECAY_BASE",
    "OVERDUE_MAX_INTERVAL_HOURS",
    "DAILY_NOTIFICATION_BUDGET",
    "MIN_INTERVAL_HOURS",
    "LOAD_SCALE_TARGET_ITEMS",
)


def _parse_hhmm(s: str, fallback: str) -> dtime:
    try:
        hh, mm = s.strip().split(":")
        return dtime(int(hh), int(mm))
    except (ValueError, AttributeError):
        print(f"[reminders] bad quiet-hours value {s!r}, using {fallback}")
        hh, mm = fallback.split(":")
        return dtime(int(hh), int(mm))


def _parse_float(raw: str, fallback: float, label: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        print(f"[reminders] bad {label} value {raw!r}, using {fallback}")
        return fallback
    if value <= 0:
        # A zero or negative interval would re-fire on every single pass —
        # every 60 seconds, forever. Refuse rather than spam his phone.
        print(f"[reminders] {label} must be > 0 (got {value}), using {fallback}")
        return fallback
    return value


def _parse_int(raw: str, fallback: int, label: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        print(f"[reminders] bad {label} value {raw!r}, using {fallback}")
        return fallback
    if value <= 0:
        print(f"[reminders] {label} must be > 0 (got {value}), using {fallback}")
        return fallback
    return value


def _jitter_factor(page_id: str, fraction: float) -> float:
    """
    Deterministic pseudo-random multiplier in [1-fraction, 1+fraction].

    Same page id always produces the same factor -- this has to be pure
    and stateless, since it's recomputed from scratch on every pass with
    no memory of what it returned last time. That determinism is exactly
    what lets two items in the same tier separate on their very first
    reminder and never re-converge, without any shared state between
    them or between runs.
    """
    digest = hashlib.sha256(page_id.encode()).digest()
    unit = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF  # -> [0.0, 1.0]
    return 1.0 + fraction * (2 * unit - 1)  # -> [1-fraction, 1+fraction]


@dataclass(frozen=True)
class Cadence:
    """
    Reminder tuning, read from the environment once per run.

    This is a value object rather than module-level globals so tests can
    construct one directly instead of monkeypatching module state — the
    reason the old module-level env reads were awkward to test at all.
    """

    quiet_start: dtime = dtime(0, 0)
    quiet_end: dtime = dtime(5, 0)
    assignment_alpha: float = DEFAULT_ASSIGNMENT_ALPHA
    assignment_floor: float = DEFAULT_ASSIGNMENT_FLOOR
    assignment_ceiling: float = DEFAULT_ASSIGNMENT_CEILING
    task_alpha: float = DEFAULT_TASK_ALPHA
    task_floor: float = DEFAULT_TASK_FLOOR
    task_ceiling: float = DEFAULT_TASK_CEILING
    priority_multiplier: dict = field(
        default_factory=lambda: {
            "High": DEFAULT_PRIORITY_MULTIPLIER_HIGH,
            "Medium": DEFAULT_PRIORITY_MULTIPLIER_MEDIUM,
            "Low": DEFAULT_PRIORITY_MULTIPLIER_LOW,
        }
    )
    jitter_fraction: float = DEFAULT_JITTER_FRACTION
    max_per_pass: int = DEFAULT_MAX_PER_PASS
    event_reminder_hour: dtime = dtime(7, 0)
    overdue_decay_base: float = DEFAULT_OVERDUE_DECAY_BASE
    overdue_max_interval: float = DEFAULT_OVERDUE_MAX_INTERVAL
    daily_budget: int = DEFAULT_DAILY_BUDGET
    min_interval: float = DEFAULT_MIN_INTERVAL_HOURS
    # Set per pass by pipeline.run_sync_pass via dataclasses.replace --
    # it is a property of the current workload, not of configuration.
    load_scale: float = 1.0
    load_scale_target: int = DEFAULT_LOAD_SCALE_TARGET

    @classmethod
    def from_env(cls) -> "Cadence":
        return cls(
            quiet_start=_parse_hhmm(
                config.optional("QUIET_HOURS_START", DEFAULT_QUIET_START), DEFAULT_QUIET_START
            ),
            quiet_end=_parse_hhmm(
                config.optional("QUIET_HOURS_END", DEFAULT_QUIET_END), DEFAULT_QUIET_END
            ),
            assignment_alpha=_parse_float(
                config.optional("ASSIGNMENT_ALPHA_HOURS_PER_DAY", str(DEFAULT_ASSIGNMENT_ALPHA)),
                DEFAULT_ASSIGNMENT_ALPHA,
                "ASSIGNMENT_ALPHA_HOURS_PER_DAY",
            ),
            assignment_floor=_parse_float(
                config.optional("ASSIGNMENT_FLOOR_HOURS", str(DEFAULT_ASSIGNMENT_FLOOR)),
                DEFAULT_ASSIGNMENT_FLOOR,
                "ASSIGNMENT_FLOOR_HOURS",
            ),
            assignment_ceiling=_parse_float(
                config.optional("ASSIGNMENT_CEILING_HOURS", str(DEFAULT_ASSIGNMENT_CEILING)),
                DEFAULT_ASSIGNMENT_CEILING,
                "ASSIGNMENT_CEILING_HOURS",
            ),
            task_alpha=_parse_float(
                config.optional("TASK_ALPHA_HOURS_PER_DAY", str(DEFAULT_TASK_ALPHA)),
                DEFAULT_TASK_ALPHA,
                "TASK_ALPHA_HOURS_PER_DAY",
            ),
            task_floor=_parse_float(
                config.optional("TASK_FLOOR_HOURS", str(DEFAULT_TASK_FLOOR)),
                DEFAULT_TASK_FLOOR,
                "TASK_FLOOR_HOURS",
            ),
            task_ceiling=_parse_float(
                config.optional("TASK_CEILING_HOURS", str(DEFAULT_TASK_CEILING)),
                DEFAULT_TASK_CEILING,
                "TASK_CEILING_HOURS",
            ),
            priority_multiplier={
                "High": _parse_float(
                    config.optional(
                        "PRIORITY_MULTIPLIER_HIGH", str(DEFAULT_PRIORITY_MULTIPLIER_HIGH)
                    ),
                    DEFAULT_PRIORITY_MULTIPLIER_HIGH,
                    "PRIORITY_MULTIPLIER_HIGH",
                ),
                "Medium": _parse_float(
                    config.optional(
                        "PRIORITY_MULTIPLIER_MEDIUM", str(DEFAULT_PRIORITY_MULTIPLIER_MEDIUM)
                    ),
                    DEFAULT_PRIORITY_MULTIPLIER_MEDIUM,
                    "PRIORITY_MULTIPLIER_MEDIUM",
                ),
                "Low": _parse_float(
                    config.optional(
                        "PRIORITY_MULTIPLIER_LOW", str(DEFAULT_PRIORITY_MULTIPLIER_LOW)
                    ),
                    DEFAULT_PRIORITY_MULTIPLIER_LOW,
                    "PRIORITY_MULTIPLIER_LOW",
                ),
            },
            jitter_fraction=_parse_float(
                config.optional("REMINDER_JITTER_FRACTION", str(DEFAULT_JITTER_FRACTION)),
                DEFAULT_JITTER_FRACTION,
                "REMINDER_JITTER_FRACTION",
            ),
            max_per_pass=_parse_int(
                config.optional("MAX_NOTIFICATIONS_PER_PASS", str(DEFAULT_MAX_PER_PASS)),
                DEFAULT_MAX_PER_PASS,
                "MAX_NOTIFICATIONS_PER_PASS",
            ),
            event_reminder_hour=_parse_hhmm(
                config.optional("EVENT_REMINDER_HOUR", DEFAULT_EVENT_REMINDER_HOUR),
                DEFAULT_EVENT_REMINDER_HOUR,
            ),
            overdue_decay_base=_parse_float(
                config.optional("OVERDUE_DECAY_BASE", str(DEFAULT_OVERDUE_DECAY_BASE)),
                DEFAULT_OVERDUE_DECAY_BASE,
                "OVERDUE_DECAY_BASE",
            ),
            overdue_max_interval=_parse_float(
                config.optional("OVERDUE_MAX_INTERVAL_HOURS", str(DEFAULT_OVERDUE_MAX_INTERVAL)),
                DEFAULT_OVERDUE_MAX_INTERVAL,
                "OVERDUE_MAX_INTERVAL_HOURS",
            ),
            daily_budget=_parse_int(
                config.optional("DAILY_NOTIFICATION_BUDGET", str(DEFAULT_DAILY_BUDGET)),
                DEFAULT_DAILY_BUDGET,
                "DAILY_NOTIFICATION_BUDGET",
            ),
            min_interval=_parse_float(
                config.optional("MIN_INTERVAL_HOURS", str(DEFAULT_MIN_INTERVAL_HOURS)),
                DEFAULT_MIN_INTERVAL_HOURS,
                "MIN_INTERVAL_HOURS",
            ),
            load_scale_target=_parse_int(
                config.optional("LOAD_SCALE_TARGET_ITEMS", str(DEFAULT_LOAD_SCALE_TARGET)),
                DEFAULT_LOAD_SCALE_TARGET,
                "LOAD_SCALE_TARGET_ITEMS",
            ),
        )

    def for_load(self, active_items: int) -> "Cadence":
        """
        This same Cadence, scaled for how much is actually on Peter's
        plate right now. Called once per pass; see DEFAULT_LOAD_SCALE_TARGET.
        """
        return replace(
            self, load_scale=max(1.0, active_items / max(1, self.load_scale_target))
        )

    def in_quiet_hours(self, moment: datetime) -> bool:
        t = moment.time()
        if self.quiet_start == self.quiet_end:
            return False  # empty window — quiet hours disabled
        if self.quiet_start <= self.quiet_end:
            return self.quiet_start <= t < self.quiet_end
        return t >= self.quiet_start or t < self.quiet_end  # wraps past midnight

    def interval_hours(
        self, type_name: str, days_until: float, priority: str | None, page_id: str
    ) -> float:
        """
        Hours until the next reminder for a recurring (Assignments/Tasks)
        item. See the module docstring for the full derivation — this is
        deliberately one formula with no branch between "not yet due" and
        "overdue": a very negative `days_until` clamps to the floor the
        same way a near-zero one does, which is what makes "constant rate
        forever once overdue" fall out for free.
        """
        if type_name == "Tasks":
            alpha, floor, ceiling = self.task_alpha, self.task_floor, self.task_ceiling
        else:  # Assignments (and any future non-Events recurring type)
            alpha, floor, ceiling = (
                self.assignment_alpha,
                self.assignment_floor,
                self.assignment_ceiling,
            )
        raw_hours = max(floor, min(ceiling, alpha * days_until))
        multiplier = self.priority_multiplier.get(priority or "Medium", 1.0)
        hours = raw_hours * multiplier * self.overdue_decay(days_until)

        # The overdue cap is applied BEFORE jitter, deliberately. Applying
        # it after would hand every deeply-overdue item the identical
        # value (exactly overdue_max_interval), which re-creates the exact
        # phase-lock jitter exists to prevent — every long-overdue item
        # firing together, once a day, forever. Caught by
        # test_breaks_up_a_shared_tier failing when the clamp was last.
        if days_until < 0:
            hours = min(hours, self.overdue_max_interval)

        hours *= _jitter_factor(page_id, self.jitter_fraction)
        # Load scaling last, so a heavy workload stretches everything
        # uniformly rather than distorting the urgency ordering.
        hours *= self.load_scale
        return max(hours, self.min_interval)

    def overdue_decay(self, days_until: float) -> float:
        """
        Multiplier that backs the cadence OFF the longer an item stays
        overdue: 1x on the first day, then `overdue_decay_base` per full
        day after that, until interval_hours clamps it at
        `overdue_max_interval` (once a day by default).

        Added 2026-07-30, Peter's call, after simulating the shipped
        cadence against his real items: it produced 238 notifications in
        7 days from 5 items, 167 of them from ONE overdue task sitting on
        the 1-hour floor and re-firing every waking hour forever with
        byte-identical text. The old "constant rate forever once overdue"
        behavior was a deliberate choice, and it was the wrong one — past
        about the twentieth identical push you have stopped reading them,
        so the pressure isn't pressure any more, it's just training you
        to swipe the app away. Decaying to once a day keeps the item
        present without spending your attention on it.

        Returns 1.0 for anything not overdue, so the not-yet-due path is
        untouched.
        """
        if days_until >= 0:
            return 1.0
        # Capped exponent: interval_hours clamps the result to
        # overdue_max_interval anyway, so raising 2 to the power of a
        # 400-day-overdue item buys nothing and computes a number with
        # 120 digits in it.
        full_days_overdue = min(int(-days_until), _MAX_DECAY_DOUBLINGS)
        return self.overdue_decay_base ** full_days_overdue


def cloud_lag() -> timedelta:
    """How far behind local_sync the cloud path stays. See module docstring."""
    minutes = _parse_float(
        config.optional("CLOUD_REMINDER_LAG_MINUTES", str(DEFAULT_CLOUD_LAG_MINUTES)),
        DEFAULT_CLOUD_LAG_MINUTES,
        "CLOUD_REMINDER_LAG_MINUTES",
    )
    return timedelta(minutes=minutes)


def due_datetime(item: dict) -> datetime | None:
    """
    Best-effort due moment, as an aware datetime.

    A date-only due date means "end of that day" — 23:59 in Peter's
    timezone, NOT 23:59 UTC. Getting this wrong made every date-only
    assignment go overdue six hours early; see shared/timeutil.py.
    """
    raw = item.get("due_date")
    if not raw:
        return None
    dt = timeutil.parse(raw)
    if not timeutil.has_time_component(raw):
        dt = timeutil.end_of_day(dt)
    return dt


def _friendly_due(due: datetime, has_time: bool) -> str:
    """Absolute date, used only for capture messages — see relative_due()
    for why recurring/overdue/event messages use relative wording instead."""
    local = due.astimezone(timeutil.school_tz())
    return local.strftime("%b %-d, %-I:%M %p") if has_time else local.strftime("%b %-d")


def relative_due(due: datetime, has_time: bool, now: datetime) -> str:
    """
    "today"/"tomorrow"/"yesterday"/"in N days"/"N days ago", with a time
    suffix when the due date has a time component and is today/tomorrow/
    yesterday (a suffix on "in 12 days" doesn't add anything).

    Uses CALENDAR-day difference (timeutil.calendar_days_between), not a
    24-hour bucket — this codebase has twice already shipped a bug from
    treating "a day" as exactly 24 elapsed hours instead of a calendar
    boundary in Peter's timezone (the date-only-UTC due date, and the
    ambient-vs-explicit timezone bug in quiet hours). An evening `now`
    against an early-morning `due` the very next calendar day is only
    a few hours apart but must say "tomorrow", not "today".
    """
    diff = timeutil.calendar_days_between(due, now)
    time_str = due.astimezone(timeutil.school_tz()).strftime(" at %-I:%M %p") if has_time else ""
    if diff == 0:
        return f"today{time_str}"
    if diff == 1:
        return f"tomorrow{time_str}"
    if diff == -1:
        return f"yesterday{time_str}"
    if diff > 1:
        return f"in {diff} days"
    return f"{-diff} days ago"


@dataclass(frozen=True)
class Reminder:
    """
    What to say and how urgently, deliberately ignorant of HOW it's sent.
    click_url and the mark-done action aren't here -- pipeline.py already
    knows about `item` and assembles those at the send call site, exactly
    as it does for click_url today. Keeping ntfy wire-format knowledge out
    of this module is what keeps due_for_reminder pure and directly
    testable without touching shared/notify.py at all.
    """

    title: str
    body: str
    # True when this reminder came due inside quiet hours. The caller must
    # NOT push it, but must still stamp Last Reminded -- the slot is spent
    # silently. See due_for_reminder for why that's the behavior Peter
    # chose over queueing it until morning.
    silent: bool = False
    priority: int = 3  # ntfy priority, 1-5
    # "capture" or "recurring". NOT cosmetic, and not derivable from the
    # title without string-matching it back apart: pipeline._allocate
    # rations these two completely differently, because they are not the
    # same kind of message.
    #
    # A "capture" reminder is the ONLY time Peter is ever told that an item
    # exists. Miss it and the information is never re-sent in that form --
    # the item silently joins the recurring pool as though he already knew
    # about it. A "recurring" reminder is by construction repeatable: drop
    # one and the next lands an interval later having lost nothing.
    #
    # Treating them as interchangeable is what let the daily budget starve
    # every capture notification for 3+ hours on 2026-07-31 while Peter
    # concluded, reasonably, that capture itself was broken.
    kind: str = "recurring"
    # ntfy converts any tag matching an emoji short code into an emoji and
    # PREPENDS it to the title. The old default of "school" therefore put
    # a 🏫 on the front of every single notification — verified against
    # ntfy's own emoji table 2026-07-30, after polling the live topic and
    # finding the tag on all 9 cached messages. It carried no information
    # (everything here is school) and it crowded out the category emoji
    # that does. Default is now no tags at all; "rotating_light" (🚨) is
    # added back only where urgency is real.
    tags: str = ""


_CATEGORY_LABELS = {
    ("Assignments", "reminder"): "Assignment reminder",
    ("Assignments", "overdue"): "Assignment overdue",
    ("Assignments", "capture"): "New assignment",
    ("Tasks", "reminder"): "Task reminder",
    ("Tasks", "overdue"): "Task overdue",
    ("Tasks", "capture"): "New task",
    ("Events", "reminder"): "Event reminder",
    ("Events", "capture"): "New event",
}


def _title(type_name: str, kind: str, category: str | None) -> str:
    label = _CATEGORY_LABELS.get((type_name, kind), "Reminder")
    emoji = classmap.category_emoji(category, type_name)
    return f"{emoji} {label}" if emoji else label


def _body(category: str | None, name: str, suffix: str) -> str:
    prefix = f"{category} · " if category else ""
    return f"{prefix}{name} — {suffix}" if suffix else f"{prefix}{name}"


def due_for_reminder(
    item: dict,
    now: datetime,
    cadence: Cadence | None = None,
    lag: timedelta = timedelta(0),
) -> Reminder | None:
    """
    Returns the Reminder to act on right now, or None.

    A returned Reminder with `silent=True` must be STAMPED BUT NOT SENT —
    see below.

    `now` must be timezone-aware (callers pass timeutil.now()) since it
    is compared directly against offset-aware Notion timestamps.
    `lag` makes this caller defer to a faster one — see module docstring.

    QUIET HOURS CONSUME THE SLOT, THEY DON'T QUEUE IT (changed 2026-07-30)
    ---------------------------------------------------------------------
    This used to return None during quiet hours and leave Last Reminded
    untouched, so everything that came due overnight stayed due and fired
    the instant quiet hours ended. Observed live: three pushes at
    05:00:48, :49 and :50 — one second apart, before Peter was even up.
    Jitter cannot fix that, because those items were never phase-locked;
    they were all *held* and released by the same gate.

    Peter's call: don't hold them at all. A reminder that comes due at 2am
    is spent silently — Last Reminded is stamped, nothing is pushed — and
    the item simply waits a full interval for its next one, which lands
    naturally during the day, spread out by its own jitter.

    The cost, and it is a real one: Last Reminded now means "when the
    reminder slot was last spent", not "when your phone last buzzed".
    A 2am timestamp on an item you were never notified about is expected,
    not a bug.
    """
    cadence = cadence or Cadence.from_env()
    reminder = _evaluate(item, now, cadence, lag)
    if reminder is None:
        return None

    # THE HARD FLOOR (Peter's rule, 2026-07-30): nothing notifies twice
    # inside min_interval, no matter which path produced the reminder or
    # what the cadence math said. Deliberately a separate, blunter check
    # than interval_hours' clamp -- that one only governs the
    # Assignments/Tasks formula, while this also catches Events (whose
    # morning-of and 1-hour-before tiers can otherwise land ~1h apart for
    # an early event) and anything added later.
    #
    # Measured against the real clock, not `effective`: this is a
    # question about how recently the phone actually buzzed.
    # No try/except: _evaluate already parsed this same value before it
    # could return a Reminder, so reaching here means it parses. An
    # unparseable stamp raises there and pipeline.py catches it per item
    # and fails the run loudly, which is the policy this project wants --
    # swallowing it here would silence the item instead.
    if item.get("last_reminded"):
        if now - timeutil.parse(item["last_reminded"]) < timedelta(hours=cadence.min_interval):
            return None

    # Evaluated against the real `now`, never the lagged time: quiet hours
    # are a question about this moment, not about when the reminder came due.
    if cadence.in_quiet_hours(now):
        return replace(reminder, silent=True)
    return reminder


def _evaluate(item: dict, now: datetime, cadence: Cadence, lag: timedelta) -> Reminder | None:
    if item.get("is_complete"):
        return None

    effective = now - lag
    type_name = item.get("type_name")
    category = item.get("category")
    name = item["name"]
    raw_due = item.get("due_date")
    due = due_datetime(item)
    has_time = bool(raw_due and timeutil.has_time_component(raw_due))

    last_reminded = timeutil.parse(item["last_reminded"]) if item.get("last_reminded") else None

    # Capture: the first time this item has ever been seen by the
    # reminder engine. There's no previous reminder to measure the lag
    # from, so it's measured from when the page was created.
    if last_reminded is None:
        # ...except for items the capture sweeps created themselves,
        # which carry an External ID. Those announce immediately, no
        # matter which runner sees them first.
        #
        # The lag exists so local_sync can win a race for an item the
        # cloud might also be looking at. But local_sync cannot have
        # seen an item that did not exist on its previous pass, so
        # there is no race to lose — and making a Classroom assignment
        # wait a whole extra cron cycle for its "added" notification is
        # the opposite of what this system is for. Stamping Last
        # Reminded right after a successful send is what actually
        # closes the window.
        if not item.get("external_id"):
            created = timeutil.parse(item["created_time"]) if item.get("created_time") else None
            if lag and created and created > effective:
                return None  # typed by hand just now — let local_sync go first
        due_phrase = f"due {_friendly_due(due, has_time)}" if due else ""
        # Urgency of a capture matches the recurring rule, rather than
        # sitting at the default 3 forever. An assignment Peter has never
        # heard of that is due tomorrow is not a low-priority message, and
        # announcing it at the same lock-screen weight as one due in three
        # weeks threw away the only signal the notification had.
        capture_days_until = (due - effective).total_seconds() / 86400 if due else None
        priority = 3
        if capture_days_until is not None:
            priority = 5 if capture_days_until < 0 else (4 if capture_days_until < 1 else 3)
        return Reminder(
            title=_title(type_name, "capture", category),
            body=_body(category, name, due_phrase),
            priority=priority,
            tags="rotating_light" if priority >= 4 else "",
            kind="capture",
        )

    if type_name == "Events":
        if not due:
            return None

        # 1-hour-before: only meaningful for a timed event, and only
        # inside the window [hour_before, due) -- an upper bound matters
        # here, not just a lower one. Without `effective < due`, checking
        # this hours after an early-morning event (say a 6am event, at
        # 8am) would still say "starts in 1 hour," because the only
        # other condition (last_reminded < hour_before) stays true
        # forever if the hour-before tier never got a chance to fire.
        hour_before = due - timedelta(hours=1)
        if has_time and hour_before <= effective < due and last_reminded < hour_before:
            return Reminder(
                title=_title(type_name, "reminder", category),
                body=_body(
                    category, name, f"starts in 1 hour ({_friendly_due(due, has_time)})"
                ),
                priority=4,
                tags="rotating_light",
            )

        if effective >= due:
            return None  # already happened -- nothing more to announce

        # 3-days-before / 1-day-before / morning-of, all at a fixed local
        # hour. `days_before` is exact by construction: whichever of
        # 0/1/3 it equals, today's date at event_reminder_hour is the
        # marker for that tier.
        days_before = timeutil.calendar_days_between(due, effective)
        if days_before in (0, 1, 3):
            today = effective.astimezone(timeutil.school_tz()).date()
            marker = datetime.combine(
                today, cadence.event_reminder_hour, tzinfo=timeutil.school_tz()
            )
            if effective >= marker and last_reminded < marker:
                return Reminder(
                    title=_title(type_name, "reminder", category),
                    body=_body(category, name, relative_due(due, has_time, effective)),
                    priority=4 if days_before == 0 else 3,
                )
        return None

    # Assignments / Tasks: recurring, cadence a continuous function of
    # urgency and priority (see Cadence.interval_hours).
    if not due:
        return None
    days_until = (due - effective).total_seconds() / 86400
    interval = cadence.interval_hours(type_name, days_until, item.get("priority"), item["id"])
    if effective - last_reminded < timedelta(hours=interval):
        return None

    overdue = days_until < 0
    verb = "was due" if overdue else "due"
    priority = 5 if overdue else (4 if days_until < 1 else 3)
    return Reminder(
        title=_title(type_name, "overdue" if overdue else "reminder", category),
        body=_body(category, name, f"{verb} {relative_due(due, has_time, effective)}"),
        priority=priority,
        tags="rotating_light" if priority >= 4 else "",
    )
