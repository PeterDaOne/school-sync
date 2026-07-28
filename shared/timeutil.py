"""
Timezone-correct time handling.

This module exists because of one specific class of bug, worth spelling
out so it doesn't get reintroduced.

**Naive datetimes must be interpreted in Peter's timezone, not UTC.**
Notion returns a date-only due date as the bare string "2026-07-30".
The old code did `datetime.fromisoformat(...).replace(tzinfo=utc)`,
which turned "due at end of day July 30" into 23:59 *UTC* — 17:59
Mountain. Every date-only assignment therefore went "Overdue" six hours
before it actually was, and every urgency tier boundary was shifted by
the same six hours.

**The timezone must be explicit, not ambient.** Reminders now also fire
from cloud_sync.py on GitHub's runners, and those runners are UTC.
Ambient local time would mean quiet hours of 00:00-05:00 land at
00:00-05:00 Mountain on the Mac but 18:00-23:00 Mountain in the cloud —
the two schedulers would disagree about when it's night. SCHOOL_TIMEZONE
pins both to the same wall clock regardless of where the code runs.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config

DEFAULT_TIMEZONE = "America/Denver"


def school_tz() -> ZoneInfo:
    """
    The timezone Peter actually lives in — the frame every reminder
    decision is made in. Falls back to GOOGLE_CALENDAR_TIMEZONE so
    there's one less thing to configure twice.
    """
    name = config.optional("SCHOOL_TIMEZONE") or config.optional(
        "GOOGLE_CALENDAR_TIMEZONE", DEFAULT_TIMEZONE
    )
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        # A typo'd timezone should not silently become UTC and shift
        # every reminder — but it also shouldn't take the sync down.
        print(f"[timeutil] unknown timezone {name!r}, falling back to {DEFAULT_TIMEZONE}")
        return ZoneInfo(DEFAULT_TIMEZONE)


def now(tz: ZoneInfo | None = None) -> datetime:
    """Current moment, as an aware datetime in the school timezone."""
    return datetime.now(tz or school_tz())


def utc_now_iso() -> str:
    """Current UTC time as ISO 8601 — what we stamp into Notion."""
    return datetime.now(timezone.utc).isoformat()


def parse(value: str, tz: ZoneInfo | None = None) -> datetime:
    """
    Parse a Notion timestamp into an aware datetime.

    Notion hands back three shapes, all of which land here:
      - "2026-07-30"                      date-only, naive
      - "2026-07-28T16:00:00.000-06:00"   offset-aware
      - "2026-07-28T05:31:00.000Z"        Zulu (last_edited_time)

    A naive value is interpreted in the school timezone. See the module
    docstring for why that matters.
    """
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=tz or school_tz())


def has_time_component(value: str) -> bool:
    """True if a Notion date string carries a time, not just a calendar day."""
    return "T" in value


def end_of_day(dt: datetime) -> datetime:
    """23:59 on the same calendar day, preserving timezone."""
    return dt.replace(hour=23, minute=59, second=0, microsecond=0)
