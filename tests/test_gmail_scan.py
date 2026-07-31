"""
Tests for gmail_scan.py's logic, with the Claude call stubbed out.

WHAT THIS PROVES AND WHAT IT DOESN'T
------------------------------------
Proves: the Gmail search query is built correctly, the seen-label is
found/created/degraded-from correctly, External ID dedup skips work
before it costs anything, the classification cap holds, a message is
labelled whatever the verdict, and a class name from Claude is resolved
against real Notion options rather than sent raw.

Does NOT prove: that Claude classifies real school email correctly, that
the prompt is any good, that its due_date strings are well-formed, or
that the whole sweep works against a live mailbox. Every test here feeds
_extract_assignment a canned answer. The classifier is the part with no
API key, and it is exactly the part still unverified.
"""

import unittest
from unittest import mock

from googleapiclient.errors import HttpError

import tests.context  # noqa: F401

import gmail_scan


class FakeHttpResponse:
    def __init__(self, status):
        self.status = status
        self.reason = "error"


def http_error(status, body=b"{}"):
    return HttpError(FakeHttpResponse(status), body)


class _Exec:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class FakeGmail:
    """Mimics the users().labels()/messages() chains gmail_scan walks."""

    def __init__(self, labels=None, messages=None, bodies=None, label_error=None):
        self._labels = list(labels or [])
        self._messages = list(messages or [])
        self._bodies = bodies or {}
        self._label_error = label_error
        self.created_labels = []
        self.modified = []
        self.queries = []

    def users(self):
        return self

    def labels(self):
        return _Labels(self)

    def messages(self):
        return _Messages(self)


class _Labels:
    def __init__(self, parent):
        self.p = parent

    def list(self, userId=None):
        if self.p._label_error:
            raise self.p._label_error
        return _Exec({"labels": self.p._labels})

    def create(self, userId=None, body=None):
        if self.p._label_error:
            raise self.p._label_error
        self.p.created_labels.append(body)
        return _Exec({"id": "created-label-id"})


class _Messages:
    def __init__(self, parent):
        self.p = parent

    def list(self, userId=None, q=None, maxResults=None):
        self.p.queries.append(q)
        return _Exec({"messages": [{"id": m} for m in self.p._messages]})

    def get(self, userId=None, id=None, format=None, metadataHeaders=None):
        subject, snippet = self.p._bodies.get(id, ("A Subject", "A snippet"))
        return _Exec(
            {"payload": {"headers": [{"name": "Subject", "value": subject}]}, "snippet": snippet}
        )

    def modify(self, userId=None, id=None, body=None):
        self.p.modified.append(id)
        return _Exec({})


class CandidateQuery(unittest.TestCase):
    def test_domain_and_keyword_filters_are_anded(self):
        with mock.patch.object(gmail_scan.config, "optional", return_value="school.edu"):
            q = gmail_scan._candidate_query(has_label=True)
        self.assertIn("(from:school.edu)", q)
        self.assertIn("subject:assignment", q)

    def test_multiple_domain_hints_are_ored_together(self):
        with mock.patch.object(
            gmail_scan.config, "optional", return_value="eldoradohs.org, aps.edu"
        ):
            q = gmail_scan._candidate_query(has_label=True)
        self.assertIn("(from:eldoradohs.org OR from:aps.edu)", q)

    def test_no_hints_means_no_from_filter_rather_than_an_empty_one(self):
        """An empty `(from:)` clause would match nothing at all."""
        with mock.patch.object(gmail_scan.config, "optional", return_value=""):
            q = gmail_scan._candidate_query(has_label=True)
        self.assertNotIn("from:", q)
        self.assertIn("subject:assignment", q)

    def test_with_the_label_the_window_is_wide_and_seen_mail_excluded(self):
        with mock.patch.object(gmail_scan.config, "optional", return_value=""):
            q = gmail_scan._candidate_query(has_label=True)
        self.assertIn(gmail_scan.LOOKBACK_WITH_LABEL, q)
        self.assertIn(f'-label:"{gmail_scan.SEEN_LABEL}"', q)

    def test_without_the_label_the_window_narrows_and_nothing_is_excluded(self):
        """
        No label means every run re-examines the window, so the window is
        the only cost control left -- it must shrink, and there is no
        -label: clause to add.
        """
        with mock.patch.object(gmail_scan.config, "optional", return_value=""):
            q = gmail_scan._candidate_query(has_label=False)
        self.assertIn(gmail_scan.LOOKBACK_WITHOUT_LABEL, q)
        self.assertNotIn("-label:", q)


class SeenLabel(unittest.TestCase):
    def test_an_existing_label_is_reused_not_recreated(self):
        svc = FakeGmail(labels=[{"id": "L1", "name": gmail_scan.SEEN_LABEL}])
        self.assertEqual(gmail_scan._seen_label_id(svc), "L1")
        self.assertEqual(svc.created_labels, [])

    def test_a_missing_label_is_created_hidden_from_the_ui(self):
        svc = FakeGmail(labels=[{"id": "L1", "name": "Some Other Label"}])
        self.assertEqual(gmail_scan._seen_label_id(svc), "created-label-id")
        body = svc.created_labels[0]
        self.assertEqual(body["labelListVisibility"], "labelHide")
        self.assertEqual(body["messageListVisibility"], "hide")

    def test_missing_modify_scope_degrades_instead_of_failing(self):
        """403 here is a working-but-degraded state, not an error."""
        svc = FakeGmail(label_error=http_error(403))
        self.assertIsNone(gmail_scan._seen_label_id(svc))

    def test_an_unrelated_error_is_not_swallowed(self):
        svc = FakeGmail(label_error=http_error(500))
        with self.assertRaises(HttpError):
            gmail_scan._seen_label_id(svc)


ASSIGNMENT = {
    "is_assignment": True,
    "task_name": "Read chapter 4",
    "class_name": "AP Language & Composition Period 3",
    "due_date": "2026-08-01",
}

OPTIONS = ["AP Lang", "AP Stats", "AP Physics", "School", "Personal"]


class Run(unittest.TestCase):
    """The sweep, with _extract_assignment stubbed -- see module docstring."""

    def setUp(self):
        self.created = []
        patches = [
            mock.patch.object(gmail_scan.notion_client, "create_item", self._create),
            mock.patch.object(
                gmail_scan.notion_client, "select_option_names", return_value=OPTIONS
            ),
            mock.patch.object(gmail_scan.config, "optional", self._optional),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _optional(self, key, default=""):
        return {"ANTHROPIC_API_KEY": "sk-ant-real", "SCHOOL_EMAIL_HINTS": ""}.get(key, default)

    def _create(self, **kwargs):
        self.created.append(kwargs)

    def _run(self, svc, verdicts, known_ids=None):
        answers = list(verdicts)
        with mock.patch.object(gmail_scan, "_gmail_service", return_value=svc), mock.patch.object(
            gmail_scan, "_extract_assignment", side_effect=lambda *a, **k: answers.pop(0)
        ):
            gmail_scan.run(known_ids=known_ids)

    def test_placeholder_api_key_skips_cleanly_without_touching_gmail(self):
        """
        The documented current state. It must not raise -- the Classroom
        sweep and the Calendar sync still have to run.
        """
        with mock.patch.object(gmail_scan.config, "optional", return_value="sk-ant-xxxx"):
            with mock.patch.object(gmail_scan, "_gmail_service") as svc:
                gmail_scan.run(known_ids=set())
        svc.assert_not_called()

    def test_a_classified_assignment_becomes_a_notion_item(self):
        svc = FakeGmail(labels=[{"id": "L1", "name": gmail_scan.SEEN_LABEL}], messages=["m1"])
        self._run(svc, [ASSIGNMENT])
        self.assertEqual(len(self.created), 1)
        item = self.created[0]
        self.assertEqual(item["name"], ASSIGNMENT["task_name"])
        self.assertEqual(item["source"], "Email")
        self.assertEqual(item["external_id"], "gmail:m1")

    def test_the_title_carries_no_prefix(self):
        """
        REGRESSION. Titles used to be prefixed "[unconfirmed] ". Removed
        2026-07-30: `Input Type = "Email"` already records provenance
        structurally, and the Title is the one field that rides into
        every phone notification for the life of the item.
        """
        svc = FakeGmail(labels=[{"id": "L1", "name": gmail_scan.SEEN_LABEL}], messages=["m1"])
        self._run(svc, [ASSIGNMENT])
        self.assertNotIn("unconfirmed", self.created[0]["name"].lower())

    def test_claudes_class_name_is_resolved_against_real_notion_options(self):
        """
        Notion CREATES any select option it is handed, so the raw string
        from Claude must never reach it.
        """
        svc = FakeGmail(labels=[{"id": "L1", "name": gmail_scan.SEEN_LABEL}], messages=["m1"])
        self._run(svc, [ASSIGNMENT])
        self.assertEqual(self.created[0]["category"], "AP Lang")

    def test_an_invented_class_name_leaves_the_field_blank(self):
        svc = FakeGmail(labels=[{"id": "L1", "name": gmail_scan.SEEN_LABEL}], messages=["m1"])
        self._run(svc, [{**ASSIGNMENT, "class_name": "Underwater Basket Weaving"}])
        self.assertIsNone(self.created[0]["category"])

    def test_a_non_class_category_is_never_selected_by_capture(self):
        """
        classmap.NON_CLASS_CATEGORIES: "Personal Finance" must not file
        homework under Peter's "Personal" life category.
        """
        svc = FakeGmail(labels=[{"id": "L1", "name": gmail_scan.SEEN_LABEL}], messages=["m1"])
        self._run(svc, [{**ASSIGNMENT, "class_name": "Personal Finance"}])
        self.assertIsNone(self.created[0]["category"])

    def test_an_already_captured_message_is_skipped_before_any_claude_call(self):
        svc = FakeGmail(labels=[{"id": "L1", "name": gmail_scan.SEEN_LABEL}], messages=["m1"])
        self._run(svc, [], known_ids={"gmail:m1"})
        self.assertEqual(self.created, [])
        self.assertEqual(svc.modified, [])  # not even labelled: nothing was examined

    def test_a_rejected_message_is_still_labelled_seen(self):
        """
        The whole point of the label: a "no" must cost exactly one
        classification, ever. Without this it was re-answered every run.
        """
        svc = FakeGmail(labels=[{"id": "L1", "name": gmail_scan.SEEN_LABEL}], messages=["m1"])
        self._run(svc, [None])
        self.assertEqual(self.created, [])
        self.assertEqual(svc.modified, ["m1"])

    def test_an_accepted_message_is_labelled_too(self):
        svc = FakeGmail(labels=[{"id": "L1", "name": gmail_scan.SEEN_LABEL}], messages=["m1"])
        self._run(svc, [ASSIGNMENT])
        self.assertEqual(svc.modified, ["m1"])

    def test_the_classification_cap_bounds_spend_even_if_dedup_breaks(self):
        n = gmail_scan.MAX_CLASSIFICATIONS_PER_RUN
        svc = FakeGmail(
            labels=[{"id": "L1", "name": gmail_scan.SEEN_LABEL}],
            messages=[f"m{i}" for i in range(n + 5)],
        )
        self._run(svc, [ASSIGNMENT] * (n + 5))
        self.assertEqual(len(self.created), n)

    def test_two_messages_in_one_run_cannot_produce_the_same_item_twice(self):
        """known_ids is mutated in-flight, not just read at the start."""
        svc = FakeGmail(
            labels=[{"id": "L1", "name": gmail_scan.SEEN_LABEL}], messages=["m1", "m1"]
        )
        self._run(svc, [ASSIGNMENT, ASSIGNMENT])
        self.assertEqual(len(self.created), 1)

    def test_without_the_modify_scope_the_sweep_still_runs(self):
        svc = FakeGmail(label_error=http_error(403), messages=["m1"])
        self._run(svc, [ASSIGNMENT])
        self.assertEqual(len(self.created), 1)
        self.assertEqual(svc.modified, [])  # nothing to label with
        self.assertIn(gmail_scan.LOOKBACK_WITHOUT_LABEL, svc.queries[0])


if __name__ == "__main__":
    unittest.main()
