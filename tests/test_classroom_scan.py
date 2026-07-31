"""
Tests for classroom_scan.py's pure-ish logic -- the parts that don't need
a live Google API call to verify. The rest of this module (actually
listing courses, actually creating Notion items) is exercised against a
real Classroom account instead; see CLAUDE.md's "capture layer" section.

_active_courses is the one piece worth unit testing here: it merges two
API calls (student-side and teacher-side enrollment) and has to dedup
correctly, which is exactly the kind of logic that's easy to get subtly
wrong and easy to verify with a mock.
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from googleapiclient.errors import HttpError

import tests.context  # noqa: F401

import classroom_scan


def course(course_id, name="A Course"):
    return {"id": course_id, "name": name, "courseState": "ACTIVE"}


class FakeCoursesService:
    """
    Mimics service.courses().list(...).execute() closely enough for
    _active_courses / _list_courses: routes on whichever of
    studentId/teacherId was passed, paginating from a canned page list.
    """

    def __init__(self, student_pages=None, teacher_pages=None):
        self._student_pages = list(student_pages or [[]])
        self._teacher_pages = list(teacher_pages or [[]])

    def courses(self):
        return self

    def list(self, courseStates=None, pageToken=None, studentId=None, teacherId=None):
        assert studentId or teacherId, "must filter by one enrollment role"
        pages = self._student_pages if studentId else self._teacher_pages
        index = pageToken or 0
        page = pages[index]
        next_token = index + 1 if index + 1 < len(pages) else None
        return _Exec({"courses": page, **({"nextPageToken": next_token} if next_token else {})})


class _Exec:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class ActiveCourses(unittest.TestCase):
    def test_student_only_course_is_included(self):
        svc = FakeCoursesService(student_pages=[[course("s1")]])
        self.assertEqual([c["id"] for c in classroom_scan._active_courses(svc)], ["s1"])

    def test_teacher_only_course_is_included(self):
        """
        The case that motivated this: Peter's real school account is
        always a student, but a self-created test class necessarily
        makes him the teacher. studentId="me" alone silently returned
        zero courses for it.
        """
        svc = FakeCoursesService(teacher_pages=[[course("t1")]])
        self.assertEqual([c["id"] for c in classroom_scan._active_courses(svc)], ["t1"])

    def test_courses_from_both_roles_are_combined(self):
        svc = FakeCoursesService(student_pages=[[course("s1")]], teacher_pages=[[course("t1")]])
        ids = {c["id"] for c in classroom_scan._active_courses(svc)}
        self.assertEqual(ids, {"s1", "t1"})

    def test_a_course_appearing_in_both_lists_is_not_duplicated(self):
        svc = FakeCoursesService(
            student_pages=[[course("x1", "Same Course")]],
            teacher_pages=[[course("x1", "Same Course")]],
        )
        courses = classroom_scan._active_courses(svc)
        self.assertEqual(len(courses), 1)

    def test_no_courses_at_all_returns_empty_not_none(self):
        svc = FakeCoursesService()
        self.assertEqual(classroom_scan._active_courses(svc), [])

    def test_pagination_is_followed_for_both_roles(self):
        svc = FakeCoursesService(
            student_pages=[[course("s1")], [course("s2")]],
            teacher_pages=[[course("t1")], [course("t2")]],
        )
        ids = {c["id"] for c in classroom_scan._active_courses(svc)}
        self.assertEqual(ids, {"s1", "s2", "t1", "t2"})


class FakeHttpResponse:
    def __init__(self, status):
        self.status = status
        self.reason = "error"


def http_error(status, body=b"{}"):
    return HttpError(FakeHttpResponse(status), body)


class FakeCourseWorkService:
    """
    Mimics the two chains _recent_coursework and _submitted_coursework_ids
    walk: service.courses().courseWork().list(...) and
    ...courseWork().studentSubmissions().list(...).
    """

    def __init__(self, coursework_pages=None, submission_pages=None, raise_on_work=None):
        self._coursework_pages = list(coursework_pages or [[]])
        # Not list()-ed: an HttpError may be passed here to simulate the
        # call failing, and it isn't iterable.
        self._submission_pages = (
            submission_pages if submission_pages is not None else [[]]
        )
        self._raise_on_work = raise_on_work
        self.submission_calls = []

    def courses(self):
        return self

    def courseWork(self):
        return self

    def studentSubmissions(self):
        return _Submissions(self)

    def list(self, courseId=None, courseWorkStates=None, orderBy=None, pageToken=None):
        if self._raise_on_work:
            raise self._raise_on_work
        index = pageToken or 0
        page = self._coursework_pages[index]
        next_token = index + 1 if index + 1 < len(self._coursework_pages) else None
        return _Exec(
            {"courseWork": page, **({"nextPageToken": next_token} if next_token else {})}
        )


class _Submissions:
    def __init__(self, parent):
        self._parent = parent

    def list(self, courseId=None, courseWorkId=None, userId=None, pageToken=None):
        self._parent.submission_calls.append(
            {"courseId": courseId, "courseWorkId": courseWorkId, "userId": userId}
        )
        pages = self._parent._submission_pages
        if isinstance(pages, HttpError):
            raise pages
        index = pageToken or 0
        page = pages[index]
        next_token = index + 1 if index + 1 < len(pages) else None
        return _Exec(
            {
                "studentSubmissions": page,
                **({"nextPageToken": next_token} if next_token else {}),
            }
        )


# Distinguishes "caller said nothing" from "caller explicitly wants no
# alternateLink", which is a real case: the field is absent on drafts.
_DEFAULT_LINK = object()


def work_item(work_id, hours_ago=1.0, title="Some Work", alternate_link=_DEFAULT_LINK):
    updated = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    item = {
        "id": work_id,
        "title": title,
        "updateTime": updated.isoformat().replace("+00:00", "Z"),
    }
    # Real coursework always carries alternateLink (verified live against
    # Peter's own course, 2026-07-31).
    if alternate_link is _DEFAULT_LINK:
        item["alternateLink"] = f"https://classroom.google.com/c/CID/a/{work_id}/details"
    elif alternate_link is not None:
        item["alternateLink"] = alternate_link
    return item


class DueDateIso(unittest.TestCase):
    """
    Timezone conversion -- per CLAUDE.md the single most bug-prone thing
    in this codebase, and previously untested.
    """

    def test_date_only_stays_a_bare_calendar_day(self):
        """No dueTime key at all means all-day; a calendar day carries no
        timezone, and timeutil.parse interprets it in the school tz."""
        got = classroom_scan._due_date_iso({"dueDate": {"year": 2026, "month": 8, "day": 1}})
        self.assertEqual(got, "2026-08-01")

    def test_no_due_date_at_all_is_none(self):
        self.assertIsNone(classroom_scan._due_date_iso({}))
        self.assertIsNone(classroom_scan._due_date_iso({"dueTime": {"hours": 5}}))

    def test_utc_is_converted_to_mountain_and_keeps_its_offset(self):
        """The classic 11:59 PM deadline: Classroom stores it as 05:59 UTC
        the NEXT day, and it must come back as 23:59-06:00 the day before."""
        got = classroom_scan._due_date_iso(
            {"dueDate": {"year": 2026, "month": 8, "day": 2}, "dueTime": {"hours": 5, "minutes": 59}}
        )
        self.assertEqual(got, "2026-08-01T23:59:00-06:00")

    def test_omitted_minutes_default_to_zero(self):
        got = classroom_scan._due_date_iso(
            {"dueDate": {"year": 2026, "month": 8, "day": 1}, "dueTime": {"hours": 23}}
        )
        self.assertEqual(got, "2026-08-01T17:00:00-06:00")

    def test_omitted_hours_default_to_zero(self):
        got = classroom_scan._due_date_iso(
            {"dueDate": {"year": 2026, "month": 8, "day": 2}, "dueTime": {"minutes": 30}}
        )
        self.assertEqual(got, "2026-08-01T18:30:00-06:00")

    def test_empty_dueTime_is_midnight_utc_not_an_all_day_item(self):
        """
        REGRESSION. Google serializes TimeOfDay as proto3 JSON, which omits
        zero-valued fields -- so midnight UTC arrives as `{}`, not
        {"hours": 0}. The old `if not t:` check treated that empty dict as
        "no time set" and returned a bare calendar day.

        Midnight UTC is 6:00 PM the PREVIOUS day in Mountain, so an
        assignment due 6pm was recorded as all-day on the following date
        and read downstream as 23:59 -- roughly 30 hours late.
        """
        got = classroom_scan._due_date_iso(
            {"dueDate": {"year": 2026, "month": 8, "day": 2}, "dueTime": {}}
        )
        self.assertEqual(got, "2026-08-01T18:00:00-06:00")
        self.assertIn("T", got)  # the bug's signature was a date with no time

    def test_winter_date_uses_standard_time_offset(self):
        """DST is not hardcoded: January is -07:00, not -06:00."""
        got = classroom_scan._due_date_iso(
            {"dueDate": {"year": 2027, "month": 1, "day": 15}, "dueTime": {"hours": 18}}
        )
        self.assertEqual(got, "2027-01-15T11:00:00-07:00")


class RecentCoursework(unittest.TestCase):
    def test_work_inside_the_lookback_window_is_returned(self):
        svc = FakeCourseWorkService(coursework_pages=[[work_item("w1", hours_ago=1)]])
        got = classroom_scan._recent_coursework(svc, "c1")
        self.assertEqual([w["id"] for w in got], ["w1"])

    def test_work_older_than_the_window_is_dropped(self):
        svc = FakeCourseWorkService(coursework_pages=[[work_item("old", hours_ago=1000)]])
        self.assertEqual(classroom_scan._recent_coursework(svc, "c1"), [])

    def test_paging_stops_at_the_first_too_old_item(self):
        """
        orderBy=updateTime desc means everything past the cutoff is older
        still, so the scan returns early rather than paging the whole
        course history.
        """
        svc = FakeCourseWorkService(
            coursework_pages=[
                [work_item("new", hours_ago=1), work_item("old", hours_ago=1000)],
                [work_item("never_reached", hours_ago=0.5)],
            ]
        )
        got = classroom_scan._recent_coursework(svc, "c1")
        self.assertEqual([w["id"] for w in got], ["new"])

    def test_pagination_is_followed_while_items_stay_recent(self):
        svc = FakeCourseWorkService(
            coursework_pages=[[work_item("w1", hours_ago=1)], [work_item("w2", hours_ago=2)]]
        )
        got = classroom_scan._recent_coursework(svc, "c1")
        self.assertEqual([w["id"] for w in got], ["w1", "w2"])

    def test_scope_failure_is_fatal_with_an_actionable_message(self):
        """
        A silent skip here is indistinguishable from "no new work today"
        -- forever. It must raise.
        """
        svc = FakeCourseWorkService(
            raise_on_work=http_error(403, b'{"error":{"message":"insufficient permission"}}')
        )
        with self.assertRaises(RuntimeError) as ctx:
            classroom_scan._recent_coursework(svc, "c1")
        self.assertIn("OAuth", str(ctx.exception))

    def test_unrelated_http_errors_are_not_swallowed(self):
        svc = FakeCourseWorkService(raise_on_work=http_error(500, b'{"error":{"message":"boom"}}'))
        with self.assertRaises(HttpError):
            classroom_scan._recent_coursework(svc, "c1")


class SubmittedCourseworkIds(unittest.TestCase):
    def test_turned_in_and_returned_count_as_submitted(self):
        svc = FakeCourseWorkService(
            submission_pages=[
                [
                    {"courseWorkId": "a", "state": "TURNED_IN"},
                    {"courseWorkId": "b", "state": "RETURNED"},
                ]
            ]
        )
        self.assertEqual(classroom_scan._submitted_coursework_ids(svc, "c1"), {"a", "b"})

    def test_new_and_created_are_still_outstanding(self):
        svc = FakeCourseWorkService(
            submission_pages=[
                [
                    {"courseWorkId": "a", "state": "NEW"},
                    {"courseWorkId": "b", "state": "CREATED"},
                ]
            ]
        )
        self.assertEqual(classroom_scan._submitted_coursework_ids(svc, "c1"), set())

    def test_reclaimed_by_student_is_outstanding_again(self):
        """
        Deliberately NOT in SUBMITTED_STATES: he pulled the work back, so
        he still owes it and should still be reminded.
        """
        svc = FakeCourseWorkService(
            submission_pages=[[{"courseWorkId": "a", "state": "RECLAIMED_BY_STUDENT"}]]
        )
        self.assertEqual(classroom_scan._submitted_coursework_ids(svc, "c1"), set())

    def test_uses_the_wildcard_to_avoid_one_call_per_assignment(self):
        svc = FakeCourseWorkService(submission_pages=[[]])
        classroom_scan._submitted_coursework_ids(svc, "c1")
        self.assertEqual(svc.submission_calls[0]["courseWorkId"], "-")
        self.assertEqual(svc.submission_calls[0]["userId"], "me")

    def test_pagination_is_followed(self):
        svc = FakeCourseWorkService(
            submission_pages=[
                [{"courseWorkId": "a", "state": "TURNED_IN"}],
                [{"courseWorkId": "b", "state": "TURNED_IN"}],
            ]
        )
        self.assertEqual(classroom_scan._submitted_coursework_ids(svc, "c1"), {"a", "b"})

    def test_a_failure_degrades_to_importing_rather_than_hiding_homework(self):
        """
        Non-fatal on purpose: importing an already-submitted item is an
        annoyance, but skipping the course would hide real homework.
        """
        svc = FakeCourseWorkService(submission_pages=http_error(403))
        self.assertEqual(classroom_scan._submitted_coursework_ids(svc, "c1"), set())


class FakeFullService(FakeCourseWorkService):
    """Courses + coursework + submissions, enough to drive run()."""

    def __init__(self, courses=None, **kwargs):
        super().__init__(**kwargs)
        self._courses = courses or []

    def courses(self):
        return self

    def list(self, courseId=None, courseStates=None, courseWorkStates=None,
             orderBy=None, pageToken=None, studentId=None, teacherId=None):
        # courses().list() and courses().courseWork().list() both land
        # here; the courseId argument is what distinguishes them.
        if courseId is None:
            return _Exec({"courses": self._courses if teacherId else []})
        return super().list(courseId=courseId, courseWorkStates=courseWorkStates,
                            orderBy=orderBy, pageToken=pageToken)


class RunErrorPolicy(unittest.TestCase):
    """
    CLAUDE.md's stated policy: tolerate failures PER ITEM, but never let a
    run with failures look healthy.
    """

    def _run(self, create_side_effect, work_items):
        created = []

        def create(**kwargs):
            created.append(kwargs)
            if create_side_effect:
                create_side_effect(kwargs)

        svc = FakeFullService(
            courses=[course("871376160217", "AP Language & Composition Period 3")],
            coursework_pages=[work_items],
        )
        with mock.patch.object(classroom_scan, "_classroom_service", return_value=svc), \
             mock.patch.object(classroom_scan.notion_client, "create_item", create), \
             mock.patch.object(
                 classroom_scan.notion_client, "select_option_names",
                 return_value=["AP Lang", "AP Stats", "Personal"]):
            error = None
            try:
                classroom_scan.run(known_ids=set())
            except RuntimeError as e:
                error = e
        return created, error

    def test_one_bad_item_does_not_stop_the_others(self):
        def boom(kwargs):
            if kwargs["name"] == "bad":
                raise ValueError("Notion rejected this")

        created, error = self._run(
            boom,
            [work_item("w1", title="ok one"), work_item("w2", title="bad"),
             work_item("w3", title="ok two")],
        )
        names = [c["name"] for c in created]
        self.assertEqual(names, ["ok one", "bad", "ok two"])  # all three attempted
        self.assertIsNotNone(error)  # but the run still fails loudly

    def test_a_clean_run_raises_nothing(self):
        created, error = self._run(None, [work_item("w1", title="fine")])
        self.assertIsNone(error)
        self.assertEqual(len(created), 1)

    def test_the_course_name_resolves_to_the_right_notion_option(self):
        created, _ = self._run(None, [work_item("w1", title="Essay")])
        self.assertEqual(created[0]["category"], "AP Lang")
        self.assertEqual(created[0]["source"], "Classroom")
        self.assertEqual(created[0]["type_name"], "Assignments")
        self.assertEqual(created[0]["external_id"], "classroom:871376160217:w1")

    def test_a_captured_item_carries_googles_link_to_the_assignment(self):
        created, _ = self._run(None, [work_item("w1", title="Essay")])
        self.assertEqual(
            created[0]["source_link"],
            "https://classroom.google.com/c/CID/a/w1/details",
        )

    def test_the_link_is_passed_through_not_reconstructed(self):
        # The real URL embeds base64 of Google's numeric ids, so building
        # it ourselves would hardcode an undocumented encoding. Whatever
        # the API returns is what gets stored, verbatim.
        odd = "https://classroom.google.com/c/ODcxMzc2MTYwMjE3/a/ODcxNDI2NjU2MDc5/details"
        created, _ = self._run(None, [work_item("w1", alternate_link=odd)])
        self.assertEqual(created[0]["source_link"], odd)

    def test_a_missing_link_is_none_rather_than_fatal(self):
        # Drafts have no alternateLink. A missing convenience must never
        # cost the capture itself.
        created, error = self._run(None, [work_item("w1", alternate_link=None)])
        self.assertIsNone(error)
        self.assertEqual(len(created), 1)
        self.assertIsNone(created[0]["source_link"])

    def test_an_already_captured_assignment_is_not_recreated(self):
        created = []
        svc = FakeFullService(
            courses=[course("871376160217", "AP Language & Composition Period 3")],
            coursework_pages=[[work_item("w1")]],
        )
        with mock.patch.object(classroom_scan, "_classroom_service", return_value=svc), \
             mock.patch.object(
                 classroom_scan.notion_client, "create_item",
                 lambda **k: created.append(k)), \
             mock.patch.object(
                 classroom_scan.notion_client, "select_option_names", return_value=["AP Lang"]):
            classroom_scan.run(known_ids={"classroom:871376160217:w1"})
        self.assertEqual(created, [])


if __name__ == "__main__":
    unittest.main()
