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
    or type.
  - Assignments / Tasks: recurring until Status reaches the complete
    group, cadence scaling with urgency:
        > 3 days out       -> REMINDER_INTERVAL_HOURS (default 24)
        1-3 days out       -> REMINDER_INTERVAL_HOURS_SOON (default 4)
        due today/overdue  -> REMINDER_INTERVAL_HOURS_URGENT (default 2)
    Overdue items keep reminding at the urgent cadence forever; nothing
    caps it except Status reaching "Done". That is deliberate and was
    reconfirmed with Peter on 2026-07-28 when cloud reminders were
    turned on — he wants an overdue assignment to stay loud.
  - Events: exactly two extra reminders — 1 day before, 1 hour before.

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
Two schedulers now evaluate the same items: local_sync.py every 60s on
the Mac (only while it's awake) and cloud_sync.py every 30 min on
GitHub's runners (always). Notion has no compare-and-swap, so nothing
stops both from reading the same stale Last Reminded and both sending.

Rather than lock, the two are separated in time. local_sync passes
lag=0 and gets first refusal on everything. cloud_sync passes
CLOUD_REMINDER_LAG (default 10 min) and will only report a reminder
that came due at least that long ago. If the Mac were awake, local_sync
would have fired it within 60 seconds — so a reminder still unfired ten
minutes later is proof the Mac is asleep, and the cloud can safely take
over. The two windows do not overlap.

Note the split: `lag` shifts the *cadence* comparisons, but quiet hours
are always evaluated against the true `now`. Quiet hours are a question
about the delivery moment ("is it OK to buzz his phone right now"), not
about when the reminder became due.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, time as dtime

from . import config, timeutil

TYPE_LABELS = {"Assignments": "assignment", "Tasks": "task", "Events": "event"}

DEFAULT_QUIET_START = "00:00"
DEFAULT_QUIET_END = "05:00"
DEFAULT_INTERVAL_HOURS = 24.0
DEFAULT_SOON_INTERVAL_HOURS = 4.0
DEFAULT_URGENT_INTERVAL_HOURS = 2.0
# Lowered from 10 to 5 on 2026-07-28, alongside moving the cloud cron to
# every 5 minutes. The lag only needs to be long enough to prove the Mac
# is asleep, and local_sync polls every 60 seconds — five minutes is
# still 300x that, while halving how long a cloud-fired reminder waits.
DEFAULT_CLOUD_LAG_MINUTES = 5.0


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
    interval_hours: float = DEFAULT_INTERVAL_HOURS
    soon_interval_hours: float = DEFAULT_SOON_INTERVAL_HOURS
    urgent_interval_hours: float = DEFAULT_URGENT_INTERVAL_HOURS

    @classmethod
    def from_env(cls) -> "Cadence":
        return cls(
            quiet_start=_parse_hhmm(
                config.optional("QUIET_HOURS_START", DEFAULT_QUIET_START), DEFAULT_QUIET_START
            ),
            quiet_end=_parse_hhmm(
                config.optional("QUIET_HOURS_END", DEFAULT_QUIET_END), DEFAULT_QUIET_END
            ),
            interval_hours=_parse_float(
                config.optional("REMINDER_INTERVAL_HOURS"),
                DEFAULT_INTERVAL_HOURS,
                "REMINDER_INTERVAL_HOURS",
            ),
            soon_interval_hours=_parse_float(
                config.optional("REMINDER_INTERVAL_HOURS_SOON"),
                DEFAULT_SOON_INTERVAL_HOURS,
                "REMINDER_INTERVAL_HOURS_SOON",
            ),
            urgent_interval_hours=_parse_float(
                config.optional("REMINDER_INTERVAL_HOURS_URGENT"),
                DEFAULT_URGENT_INTERVAL_HOURS,
                "REMINDER_INTERVAL_HOURS_URGENT",
            ),
        )

    def in_quiet_hours(self, moment: datetime) -> bool:
        t = moment.time()
        if self.quiet_start == self.quiet_end:
            return False  # empty window — quiet hours disabled
        if self.quiet_start <= self.quiet_end:
            return self.quiet_start <= t < self.quiet_end
        return t >= self.quiet_start or t < self.quiet_end  # wraps past midnight

    def interval_for(self, days_until: float) -> float:
        if days_until > 3:
            return self.interval_hours
        if days_until >= 1:
            return self.soon_interval_hours
        return self.urgent_interval_hours  # due today or overdue


def cloud_lag() -> timedelta:
    """How far behind local_sync the cloud path stays. See module docstring."""
    minutes = _parse_float(
        config.optional("CLOUD_REMINDER_LAG_MINUTES"),
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
    local = due.astimezone(timeutil.school_tz())
    return local.strftime("%b %-d, %-I:%M %p") if has_time else local.strftime("%b %-d")


def _due_suffix(due: datetime | None, has_time: bool) -> str:
    return f", due {_friendly_due(due, has_time)}" if due else ""


def due_for_reminder(
    item: dict,
    now: datetime,
    cadence: Cadence | None = None,
    lag: timedelta = timedelta(0),
) -> str | None:
    """
    Returns the notification message to send right now, or None.

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
    label = TYPE_LABELS.get(item.get("type_name"), "item")
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
        return f"New {label} added: {item['name']}{_due_suffix(due, has_time)}."

    if item.get("type_name") == "Events":
        if not due:
            return None
        hour_before = due - timedelta(hours=1)
        day_before = due - timedelta(days=1)
        if effective >= hour_before and last_reminded < hour_before:
            return f"{item['name']} starts in 1 hour ({_friendly_due(due, has_time)})."
        if effective >= day_before and last_reminded < day_before:
            return f"{item['name']} is tomorrow ({_friendly_due(due, has_time)})."
        return None

    # Assignments / Tasks: recurring, cadence scales with urgency.
    if not due:
        return None
    days_until = (due - effective).total_seconds() / 86400
    interval = cadence.interval_for(days_until)
    if effective - last_reminded < timedelta(hours=interval):
        return None

    due_str = _friendly_due(due, has_time)
    if days_until < 0:
        return f"Overdue: {item['name']} was due {due_str}."
    return f"Reminder: {item['name']} due {due_str}."
