#!/usr/bin/env python3
"""
classroom_scan.py — the Classroom capture layer. Cloud-only (runs inside
cloud_sync.py via GitHub Actions), parallel to gmail_scan.py.

Unlike email, Classroom coursework arrives as structured data (title,
due date, course) straight from the API — there's no "does this look
like an assignment" guess to make. So this doesn't call Claude and
doesn't use an "[unconfirmed]" prefix: what lands in Notion is exactly
what's in Classroom.

DEDUP
-----
courses.courseWork.list has no "updated since" filter, so this scans a
trailing window and relies on External ID = "classroom:<course>:<work>"
to avoid re-creating the same assignment on the ~48 cloud_sync runs
that share that window. The ID is stable across edits, so an assignment
whose due date changes in Classroom is recognized rather than
duplicated.

Capture is create-only: an existing item is never overwritten. If Peter
retitles an assignment or adjusts its due date in Notion, a later
Classroom edit will not stomp his version. Notion stays the hub.

LOOKBACK
--------
CLASSROOM_LOOKBACK_HOURS (default 48) bounds the scan. It's wider than
the sync interval on purpose — a day of downtime shouldn't lose an
assignment, and dedup makes the overlap free. To backfill an existing
course at setup time, run once with a large value (e.g. 8760 for a
year); MAX_NEW_PER_RUN caps how much a single run can import so an
over-large lookback can't fire hundreds of notifications at once.

Scopes: classroom.courses.readonly + classroom.coursework.me.readonly.
Google may report the latter back as the granted scope
classroom.student-submissions.me.readonly — that's real consolidation
on Google's side, not an error, and oauthlib raises on the mismatch
unless OAUTHLIB_RELAX_TOKEN_SCOPE=1 is set.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from shared import config, notion_client, timeutil

# See the module docstring — oauthlib rejects Google's consolidated
# scope name without this, and it must be set before the token refresh.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

CLASSROOM_SCOPES = [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.me.readonly",
]

DEFAULT_LOOKBACK_HOURS = 48.0
MAX_NEW_PER_RUN = 25


def _lookback_hours() -> float:
    raw = config.optional("CLASSROOM_LOOKBACK_HOURS")
    try:
        value = float(raw) if raw else DEFAULT_LOOKBACK_HOURS
    except ValueError:
        print(f"[classroom_scan] bad CLASSROOM_LOOKBACK_HOURS {raw!r}", file=sys.stderr)
        return DEFAULT_LOOKBACK_HOURS
    return value if value > 0 else DEFAULT_LOOKBACK_HOURS


def _classroom_service():
    creds = Credentials(
        token=None,
        refresh_token=config.require("GOOGLE_REFRESH_TOKEN"),
        client_id=config.require("GOOGLE_CLIENT_ID"),
        client_secret=config.require("GOOGLE_CLIENT_SECRET"),
        token_uri="https://oauth2.googleapis.com/token",
        scopes=CLASSROOM_SCOPES,
    )
    return build("classroom", "v1", credentials=creds, cache_discovery=False)


def _raise_if_insufficient_scope(e: HttpError):
    """
    Turn Google's opaque 403 into a message that says what to do. A
    silent skip here would look exactly like "no new Classroom work
    today" — forever — which is why this is fatal rather than swallowed.
    """
    body = (e.content or b"").decode("utf-8", "ignore").lower()
    if e.resp.status in (401, 403) and ("scope" in body or "permission" in body):
        raise RuntimeError(
            "Google Classroom API rejected the request for insufficient OAuth "
            "scope. The refresh token in .env / GitHub secrets doesn't carry "
            "classroom.courses.readonly and classroom.coursework.me.readonly "
            "yet — re-run the OAuth consent flow (see README) with the "
            "expanded scope list and update GOOGLE_REFRESH_TOKEN everywhere "
            "it is stored."
        ) from e
    raise


def _active_courses(service) -> list[dict]:
    courses = []
    page_token = None
    while True:
        try:
            resp = (
                service.courses()
                .list(studentId="me", courseStates=["ACTIVE"], pageToken=page_token)
                .execute()
            )
        except HttpError as e:
            _raise_if_insufficient_scope(e)
            raise  # unreachable; keeps control flow obvious to readers
        courses.extend(resp.get("courses", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            return courses


def _recent_coursework(service, course_id: str) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=_lookback_hours())
    items: list[dict] = []
    page_token = None
    while True:
        try:
            resp = (
                service.courses()
                .courseWork()
                .list(
                    courseId=course_id,
                    courseWorkStates=["PUBLISHED"],
                    orderBy="updateTime desc",
                    pageToken=page_token,
                )
                .execute()
            )
        except HttpError as e:
            _raise_if_insufficient_scope(e)
            raise  # unreachable

        for work in resp.get("courseWork", []):
            updated = datetime.fromisoformat(work["updateTime"].replace("Z", "+00:00"))
            if updated < cutoff:
                # orderBy=updateTime desc means everything after this is
                # older still — stop paging this course.
                return items
            items.append(work)

        page_token = resp.get("nextPageToken")
        if not page_token:
            return items


def _due_date_iso(coursework: dict) -> str | None:
    """
    Classroom splits the deadline into a Date and a TimeOfDay, and the
    time is UTC. Emitting it without an offset would let it be read as
    local time — six hours off, in the direction that makes things look
    due earlier than they are. Convert to Peter's timezone and keep the
    offset so nothing downstream has to guess.
    """
    d = coursework.get("dueDate")
    if not d:
        return None
    date_str = f"{d['year']:04d}-{d['month']:02d}-{d['day']:02d}"

    t = coursework.get("dueTime")
    if not t:
        return date_str  # all-day: a calendar day carries no timezone

    utc = datetime(
        d["year"], d["month"], d["day"],
        t.get("hours", 0), t.get("minutes", 0),
        tzinfo=timezone.utc,
    )
    return utc.astimezone(timeutil.school_tz()).isoformat()


def run(known_ids: set[str] | None = None):
    """
    Import recently-updated Classroom coursework into Notion.

    `known_ids` is the set of External IDs already present, so an
    assignment seen on a previous run is skipped rather than duplicated.
    """
    known_ids = known_ids if known_ids is not None else set()

    service = _classroom_service()
    courses = _active_courses(service)
    if not courses:
        # Expected while Peter is still on his personal Google account —
        # auth and scopes are fine, there are simply no courses on it.
        print("[classroom_scan] no active courses on this Google account")
        return

    added = skipped = 0
    for course in courses:
        course_id = course["id"]
        for work in _recent_coursework(service, course_id):
            external_id = f"classroom:{course_id}:{work['id']}"
            if external_id in known_ids:
                skipped += 1
                continue
            if added >= MAX_NEW_PER_RUN:
                print(
                    f"[classroom_scan] hit the {MAX_NEW_PER_RUN}-item cap for this run; "
                    "remaining items will import on the next pass",
                    file=sys.stderr,
                )
                break

            notion_client.create_item(
                name=work["title"],
                class_name=course.get("name"),
                due_date=_due_date_iso(work),
                source="Classroom",
                type_name="Assignments",
                external_id=external_id,
            )
            known_ids.add(external_id)
            added += 1

    if added or skipped:
        print(f"[classroom_scan] added {added} item(s), skipped {skipped} already captured")


if __name__ == "__main__":
    config.load_dotenv()
    run()
