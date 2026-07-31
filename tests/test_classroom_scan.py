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
from unittest import mock

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


if __name__ == "__main__":
    unittest.main()
