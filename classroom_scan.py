#!/usr/bin/env python3
"""
classroom_scan.py — the Classroom capture layer. Cloud-only (runs inside
cloud_sync.py via GitHub Actions), parallel to gmail_scan.py.

Unlike email, Classroom coursework arrives as structured data (title,
due date, course) straight from the API — there's no "does this look
like an assignment" guess to make. So this doesn't call Claude at all:
what lands in Notion is exactly what's in Classroom.

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

Scopes: classroom.courses.readonly + classroom.coursework.me.readonly +
classroom.coursework.students.readonly (added 2026-07-30, see
_active_courses). Google may report the second back as the granted scope
classroom.student-submissions.me.readonly — that's real consolidation
on Google's side, not an error, and oauthlib raises on the mismatch
unless OAUTHLIB_RELAX_TOKEN_SCOPE=1 is set.
"""

import sys
from datetime import datetime, timedelta, timezone

from googleapiclient.errors import HttpError

from shared import classmap, config, googleauth, notion_client, tasktype, timeutil

CLASSROOM_SCOPES = [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.me.readonly",
    # Added 2026-07-30, needed to read courseWork on a course Peter
    # TEACHES rather than takes -- see _active_courses. His real school
    # account will be a student everywhere and never exercise this path,
    # but a self-created test class necessarily makes him the owner, and
    # courseWork.list 403'd without it ("The caller does not have
    # permission") even though courses.list(teacherId="me") succeeded.
    "https://www.googleapis.com/auth/classroom.coursework.students.readonly",
]

DEFAULT_LOOKBACK_HOURS = 48.0
MAX_NEW_PER_RUN = 25

# Submission states that mean Peter has already handed the work in.
# RECLAIMED_BY_STUDENT is deliberately absent: he pulled it back, so it's
# outstanding again and he should still be reminded.
SUBMITTED_STATES = {"TURNED_IN", "RETURNED"}


def _lookback_hours() -> float:
    raw = config.optional("CLASSROOM_LOOKBACK_HOURS")
    try:
        value = float(raw) if raw else DEFAULT_LOOKBACK_HOURS
    except ValueError:
        print(f"[classroom_scan] bad CLASSROOM_LOOKBACK_HOURS {raw!r}", file=sys.stderr)
        return DEFAULT_LOOKBACK_HOURS
    return value if value > 0 else DEFAULT_LOOKBACK_HOURS


def _classroom_service():
    return googleauth.service("classroom", "v1", CLASSROOM_SCOPES)


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
            "scope. Re-run the consent flow (README section 2) and update "
            "GOOGLE_REFRESH_TOKEN in ALL THREE places: .env, the launchd "
            "plist (python3 generate_plist.py, then reload), and the GitHub "
            "secret. Missing the GitHub secret is the usual cause -- "
            "classroom_scan only runs in the cloud, so a token that works "
            "locally proves nothing about production.\n"
            "\n"
            "Do NOT diagnose this by comparing scope strings: Google renames "
            "both Classroom coursework scopes on grant "
            "(coursework.me.readonly -> student-submissions.me.readonly, and "
            "coursework.students.readonly -> "
            "student-submissions.students.readonly). oauthlib prints a "
            "'Not all requested scopes were granted' warning about them on "
            "every refresh even when everything works. Check whether the API "
            "call actually returns 200, not what the scope list says."
        ) from e
    raise


def _list_courses(service, **filter_kwargs) -> list[dict]:
    courses = []
    page_token = None
    while True:
        try:
            resp = (
                service.courses()
                .list(courseStates=["ACTIVE"], pageToken=page_token, **filter_kwargs)
                .execute()
            )
        except HttpError as e:
            _raise_if_insufficient_scope(e)
            raise  # unreachable; keeps control flow obvious to readers
        courses.extend(resp.get("courses", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            return courses


def _active_courses(service) -> list[dict]:
    """
    Every ACTIVE course Peter can see, as student OR teacher.

    Added the teacher side 2026-07-30. His real school account will be a
    student everywhere, so studentId="me" alone was the right call at
    first -- but it silently returns zero courses for one he teaches or
    co-teaches, which is exactly the situation testing against a
    self-created Classroom class puts him in. Two calls, deduped by
    course id (a course could theoretically list under both if Classroom
    ever allows that, though it shouldn't in practice).
    """
    by_id = {c["id"]: c for c in _list_courses(service, studentId="me")}
    for c in _list_courses(service, teacherId="me"):
        by_id.setdefault(c["id"], c)
    return list(by_id.values())


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


def _submitted_coursework_ids(service, course_id: str) -> set[str]:
    """
    Coursework IDs in this course that Peter has already turned in.

    `courseWorkStates=["PUBLISHED"]` means "the teacher posted it", NOT
    "Peter still owes it" — so without this, work submitted last week
    gets imported as a fresh to-do the moment a teacher edits it.

    `courseWorkId="-"` is Classroom's wildcard: one call returns every
    submission for the course, rather than one call per assignment.

    A failure here is not fatal. Importing an already-submitted item is
    an annoyance; skipping the whole course because a secondary call
    failed would hide real homework.
    """
    submitted: set[str] = set()
    page_token = None
    while True:
        try:
            resp = (
                service.courses()
                .courseWork()
                .studentSubmissions()
                .list(
                    courseId=course_id,
                    courseWorkId="-",
                    userId="me",
                    pageToken=page_token,
                )
                .execute()
            )
        except HttpError as e:
            print(
                f"[classroom_scan] could not read submission state for course "
                f"{course_id} ({e.resp.status}); importing without it",
                file=sys.stderr,
            )
            return submitted

        for submission in resp.get("studentSubmissions", []):
            if submission.get("state") in SUBMITTED_STATES:
                submitted.add(submission["courseWorkId"])

        page_token = resp.get("nextPageToken")
        if not page_token:
            return submitted


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
    # `is None`, NOT a falsiness check. Google serializes TimeOfDay as
    # proto3 JSON, which OMITS zero-valued fields -- so midnight UTC
    # arrives as the empty dict `{}`, not as {"hours": 0}. `if not t`
    # treated that as "no time set" and returned a bare calendar day.
    # Midnight UTC is 6pm the previous day in Mountain, so an assignment
    # due 6:00 PM landed in Notion as all-day on the FOLLOWING date --
    # read downstream as 23:59, ~30 hours late. An absent key means
    # all-day; a present-but-empty one means every component is zero,
    # which the .get() defaults below already handle correctly.
    if t is None:
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

    # Read once: Notion invents any select option it's handed, so course
    # names are matched against what already exists rather than sent raw.
    # Same hazard on Task Type (multi-select) and Priority.
    category_options = notion_client.select_option_names(notion_client.CATEGORY_PROP)
    task_type_options = notion_client.select_option_names(notion_client.TASK_TYPE_PROP)
    priority_options = notion_client.select_option_names(notion_client.PRIORITY_PROP)

    added = skipped = submitted = unmatched = 0
    capped = False
    failures: list[str] = []

    for course in courses:
        if capped:
            break
        course_id = course["id"]
        coursework = _recent_coursework(service, course_id)
        if not coursework:
            continue

        already_submitted = _submitted_coursework_ids(service, course_id)
        category = classmap.resolve(course.get("name"), category_options)
        if course.get("name") and category is None:
            unmatched += 1
            print(
                f"[classroom_scan] no Notion For option matches "
                f"{course.get('name')!r} — importing with For left blank. "
                f"Add the option in Notion, or set CLASS_ALIASES.",
                file=sys.stderr,
            )

        for work in coursework:
            external_id = f"classroom:{course_id}:{work['id']}"
            if external_id in known_ids:
                skipped += 1
                continue
            if work["id"] in already_submitted:
                submitted += 1
                continue
            if added >= MAX_NEW_PER_RUN:
                # Breaks out of BOTH loops — a bare `break` here only
                # ended this course's loop, so the warning reprinted once
                # per remaining course.
                capped = True
                print(
                    f"[classroom_scan] hit the {MAX_NEW_PER_RUN}-item cap for this run; "
                    "remaining items will import on the next pass",
                    file=sys.stderr,
                )
                break

            # Per item, per the project's error policy (see
            # shared/pipeline.py): one assignment Notion rejects -- an
            # over-long title, a due date it won't accept -- must not
            # stop the rest of the course importing. Bare, this aborted
            # the whole sweep, and since a failed create writes no
            # External ID it would abort again on every subsequent run,
            # permanently blocking every assignment behind it.
            try:
                notion_client.create_item(
                    name=work["title"],
                    category=category,
                    due_date=_due_date_iso(work),
                    source="Classroom",
                    type_name="Assignments",
                    external_id=external_id,
                    task_type=tasktype.resolve(
                        work["title"], "Assignments", task_type_options
                    ),
                    priority=tasktype.priority(work["title"], priority_options),
                )
            except Exception as e:
                failures.append(f"{work.get('title', work['id'])!r}: {e}")
                print(
                    f"[classroom_scan] could not import {work.get('title')!r}: {e}",
                    file=sys.stderr,
                )
                continue
            known_ids.add(external_id)
            added += 1

    if added or skipped or submitted:
        print(
            f"[classroom_scan] added {added}, skipped {skipped} already captured, "
            f"{submitted} already turned in"
            + (f", {unmatched} course(s) with no Class match" if unmatched else "")
        )

    # Raised only after every other item has had its chance. cloud_sync's
    # _run_phase turns this into a red run rather than a silent partial
    # success -- the other half of the policy: tolerate per item, but
    # never let a broken sweep look healthy.
    if failures:
        raise RuntimeError(
            f"{len(failures)} Classroom item(s) could not be imported: "
            + "; ".join(failures)
        )


if __name__ == "__main__":
    config.load_dotenv()
    run()
