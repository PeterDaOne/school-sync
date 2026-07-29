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
        hours     = max(hours, ABSOLUTE_MIN_INTERVAL_HOURS)

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

Quiet hours suppress delivery but don't consume the slot: if a reminder
is due during quiet hours, Last Reminded is left untouched, so it fires
as soon as the next pass lands outside quiet hours.

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
from dataclasses import dataclass, field
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

# Hard safety rail applied after every multiplier -- no combination of
# High priority + a low jitter roll should ever produce a notification
# rate spammier than every 15 minutes.
ABSOLUTE_MIN_INTERVAL_HOURS = 0.25


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
        hours = raw_hours * multiplier * _jitter_factor(page_id, self.jitter_fraction)
        return max(hours, ABSOLUTE_MIN_INTERVAL_HOURS)


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
    priority: int = 3  # ntfy priority, 1-5
    tags: str = "school"


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


def _title(type_name: str, kind: str, class_name: str | None) -> str:
    label = _CATEGORY_LABELS.get((type_name, kind), "Reminder")
    emoji = classmap.class_emoji(class_name)
    return f"{emoji} {label}" if emoji else label


def _body(class_name: str | None, name: str, suffix: str) -> str:
    prefix = f"{class_name} · " if class_name else ""
    return f"{prefix}{name} — {suffix}" if suffix else f"{prefix}{name}"


def due_for_reminder(
    item: dict,
    now: datetime,
    cadence: Cadence | None = None,
    lag: timedelta = timedelta(0),
) -> Reminder | None:
    """
    Returns the Reminder to send right now, or None.

    `now` must be timezone-aware (callers pass timeutil.now()) since it
    is compared directly against offset-aware Notion timestamps.
    `lag` makes this caller defer to a faster one — see module docstring.
    """
    if item.get("is_complete"):
        return None

    cadence = cadence or Cadence.from_env()

    # Delivery gate: always the real `now`, never the lagged time.
    if cadence.in_quiet_hours(now):
        return None

    effective = now - lag
    type_name = item.get("type_name")
    class_name = item.get("class_name")
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
        return Reminder(
            title=_title(type_name, "capture", class_name),
            body=_body(class_name, name, due_phrase),
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
                title=_title(type_name, "reminder", class_name),
                body=_body(
                    class_name, name, f"starts in 1 hour ({_friendly_due(due, has_time)})"
                ),
                priority=4,
                tags="school,rotating_light",
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
                    title=_title(type_name, "reminder", class_name),
                    body=_body(class_name, name, relative_due(due, has_time, effective)),
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
        title=_title(type_name, "overdue" if overdue else "reminder", class_name),
        body=_body(class_name, name, f"{verb} {relative_due(due, has_time, effective)}"),
        priority=priority,
        tags="school,rotating_light" if priority >= 4 else "school",
    )
