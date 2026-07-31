"""
Tests for shared/tasktype.py -- inferring Task Type and Priority for
captured items.

Pure logic, no I/O, same coverage philosophy as test_classmap.py. The
most important cases here are the pinnings that stop the capture layer
from polluting Peter's Notion schema or his notification volume:

  - nothing outside the live option list is ever returned (Notion
    silently CREATES any multi-select option it is handed)
  - Priority is never "Low" (Low doubles the interval; an automated guess
    must not make an item nag less than the default)
"""

import unittest

import tests.context  # noqa: F401

from shared import tasktype

# The real live options, verified via the Notion REST API 2026-07-30.
TASK_TYPES = [
    "Execute", "Attend", "Remember", "Action", "Meeting", "Test/Quiz Prep",
    "Essay/Writing", "Reading", "Research", "Presentation", "Problem Set",
    "Project", "Lab",
]
PRIORITIES = ["High", "Medium", "Low"]


class Verbs(unittest.TestCase):
    def test_assignments_default_to_execute(self):
        self.assertEqual(tasktype.verb("Chapter 4 questions", "Assignments"), "Execute")

    def test_events_default_to_attend(self):
        self.assertEqual(tasktype.verb("Spring Concert", "Events"), "Attend")

    def test_attend_keyword_overrides_the_assignment_default(self):
        """
        Everything Classroom sends is typed Assignments, so a keyword has
        to be able to beat the type default or Attend/Remember would be
        unreachable for captured items.
        """
        self.assertEqual(tasktype.verb("Study session before the final", "Assignments"), "Attend")
        self.assertEqual(tasktype.verb("Guest lecture on the New Deal", "Assignments"), "Attend")

    def test_remember_keyword_overrides_the_assignment_default(self):
        self.assertEqual(tasktype.verb("Field Trip Permission Slip", "Assignments"), "Remember")
        self.assertEqual(tasktype.verb("Bring your calculator Friday", "Assignments"), "Remember")

    def test_remember_wins_over_attend(self):
        """
        "Bring your permission slip to the assembly" is something not to
        forget, not something to attend -- Remember is checked first.
        """
        self.assertEqual(
            tasktype.verb("Bring permission slip to the assembly", "Assignments"), "Remember"
        )

    def test_unknown_type_falls_back_to_execute(self):
        self.assertEqual(tasktype.verb("Something", None), "Execute")
        self.assertEqual(tasktype.verb(None, None), "Execute")


class Nouns(unittest.TestCase):
    def test_common_assignment_shapes(self):
        cases = {
            "Rhetorical Analysis Essay": "Essay/Writing",
            "Read chapters 4-6": "Reading",
            "Unit 3 Test": "Test/Quiz Prep",
            "Pop quiz Monday": "Test/Quiz Prep",
            "Problem Set 7": "Problem Set",
            "Lab Report: Projectile Motion": "Lab",
            "Group presentation on the New Deal": "Presentation",
            "Research paper bibliography": "Research",
            "Build a bridge project": "Project",
            "Parent teacher meeting": "Meeting",
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(tasktype.noun(title), expected)

    def test_no_match_falls_back_to_action(self):
        self.assertEqual(tasktype.noun("Untitled assignment"), "Action")
        self.assertEqual(tasktype.noun(""), "Action")
        self.assertEqual(tasktype.noun(None), "Action")

    def test_buy_book_is_not_reading(self):
        """
        REGRESSION, caught against Peter's real data: a bare \\bbook\\b
        matched "Buy book", which he tags Execute + Action. Reading now
        needs an actual reading signal.
        """
        self.assertEqual(tasktype.noun("Buy book"), "Action")
        self.assertEqual(tasktype.noun("Reading book"), "Reading")

    def test_algebra_work_is_a_problem_set(self):
        """REGRESSION against real data: this fell through to Action."""
        self.assertEqual(tasktype.noun("Algebra Work"), "Problem Set")

    def test_reading_homework_is_not_a_problem_set(self):
        """
        Math subject names live in Problem Set rather than a generic
        \\bhomework\\b pattern, precisely so this stays Reading -- Problem
        Set is matched before Reading.
        """
        self.assertEqual(tasktype.noun("Reading homework chapter 4"), "Reading")

    def test_lab_report_is_a_lab_not_an_essay(self):
        """Ordering: Lab is matched before Essay/Writing."""
        self.assertEqual(tasktype.noun("Lab report write-up"), "Lab")

    def test_research_paper_is_research_not_essay(self):
        self.assertEqual(tasktype.noun("Research paper"), "Research")


class Resolve(unittest.TestCase):
    def test_returns_one_verb_and_one_noun(self):
        self.assertEqual(
            tasktype.resolve("Rhetorical Analysis Essay", "Assignments", TASK_TYPES),
            ["Execute", "Essay/Writing"],
        )

    def test_verb_comes_first(self):
        tags = tasktype.resolve("Unit 3 Test", "Assignments", TASK_TYPES)
        self.assertEqual(tags[0], "Execute")

    def test_nothing_outside_the_live_options_is_ever_returned(self):
        """
        THE important pinning. Notion CREATES any multi-select option it
        is handed, so a tag that isn't already defined must be dropped,
        not sent -- otherwise capture silently pollutes his schema.
        """
        for title in (
            "Rhetorical Analysis Essay", "Unit 3 Test", "Read chapter 4",
            "Lab Report", "Field Trip Permission Slip", "Bring calculator",
            "Study session", "Untitled", "Problem Set 7", "Research paper",
            "Build a bridge project", "Parent teacher meeting", "",
        ):
            for type_name in ("Assignments", "Tasks", "Events", None):
                with self.subTest(title=title, type_name=type_name):
                    for tag in tasktype.resolve(title, type_name, TASK_TYPES):
                        self.assertIn(tag, TASK_TYPES)

    def test_missing_options_are_dropped_rather_than_invented(self):
        """A stripped-down option list yields fewer tags, never new ones."""
        tags = tasktype.resolve("Rhetorical Analysis Essay", "Assignments", ["Execute"])
        self.assertEqual(tags, ["Execute"])

    def test_empty_option_list_yields_no_tags(self):
        self.assertEqual(tasktype.resolve("Essay", "Assignments", []), [])

    def test_no_duplicate_tags(self):
        tags = tasktype.resolve("Meeting", "Events", TASK_TYPES)
        self.assertEqual(len(tags), len(set(tags)))


class Priority(unittest.TestCase):
    def test_assessments_and_major_deliverables_are_high(self):
        for title in (
            "Unit 3 Test", "Pop quiz", "Final exam", "Midterm review",
            "Rhetorical Analysis Essay", "Research paper", "Bridge project",
            "Group presentation", "Lab report",
        ):
            with self.subTest(title=title):
                self.assertEqual(tasktype.priority(title, PRIORITIES), "High")

    def test_routine_work_is_medium(self):
        for title in (
            "Read chapters 4-6", "Problem Set 7", "Bring your calculator",
            "Untitled assignment", "Warm-up questions", "",
        ):
            with self.subTest(title=title):
                self.assertEqual(tasktype.priority(title, PRIORITIES), "Medium")

    def test_never_returns_low(self):
        """
        Peter's explicit call. Low DOUBLES the reminder interval, so an
        automated guess must never make an item nag less than the
        default; he can still set Low by hand.
        """
        titles = [
            "Read chapters 4-6", "optional extra credit", "low priority filler",
            "quick warm-up", "Untitled", "", "practice problems", "reading",
        ]
        for title in titles:
            with self.subTest(title=title):
                self.assertNotEqual(tasktype.priority(title, PRIORITIES), "Low")

    def test_falls_back_to_medium_when_high_is_undefined(self):
        self.assertEqual(tasktype.priority("Final exam", ["Medium", "Low"]), "Medium")

    def test_returns_none_when_neither_option_exists(self):
        """
        Unset already behaves as Medium in the reminder engine, so
        degrading to None is safe -- inventing an option is not.
        """
        self.assertIsNone(tasktype.priority("Final exam", ["Urgent", "Whenever"]))

    def test_none_title_is_medium(self):
        self.assertEqual(tasktype.priority(None, PRIORITIES), "Medium")


if __name__ == "__main__":
    unittest.main()
