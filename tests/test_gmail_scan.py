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
_classify_email a canned answer. Whether Claude actually tells a real
request from a corporate call to action is verified against live mail,
not here.
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

    def __init__(self, labels=None, messages=None, bodies=None, label_error=None,
                 address="peter@example.com", profile_error=None):
        self._labels = list(labels or [])
        self._messages = list(messages or [])
        self._bodies = bodies or {}
        self._label_error = label_error
        self._address = address
        self._profile_error = profile_error
        self.created_labels = []
        self.modified = []
        self.queries = []

    def users(self):
        return self

    def getProfile(self, userId=None):
        if self._profile_error:
            raise self._profile_error
        return _Exec({"emailAddress": self._address})

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
            {
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": subject},
                        {"name": "From", "value": "Someone <someone@example.com>"},
                    ]
                },
                "snippet": snippet,
            }
        )

    def modify(self, userId=None, id=None, body=None):
        self.p.modified.append(id)
        return _Exec({})


class CandidateQuery(unittest.TestCase):
    def test_the_domain_filter_is_applied(self):
        with mock.patch.object(gmail_scan.config, "optional", return_value="school.edu"):
            q = gmail_scan._candidate_query(has_label=True)
        self.assertIn("(from:school.edu)", q)

    def test_multiple_domain_hints_are_ored_together(self):
        with mock.patch.object(
            # Deliberately fake domains: this repo is public, and naming
            # the real school of a minor in it is the one piece of PII
            # the config actually holds. See scan_secrets.py.
            gmail_scan.config, "optional", return_value="school.example, district.example"
        ):
            q = gmail_scan._candidate_query(has_label=True)
        self.assertIn("(from:school.example OR from:district.example)", q)

    def test_no_hints_means_no_from_filter_rather_than_an_empty_one(self):
        """An empty `(from:)` clause would match nothing at all."""
        with mock.patch.object(gmail_scan.config, "optional", return_value=""):
            q = gmail_scan._candidate_query(has_label=True)
        self.assertNotIn("from:", q)

    def test_no_subject_keyword_whitelist(self):
        """
        REGRESSION GUARD, and the most important test in this class.

        A subject whitelist made sense while only schoolwork was being
        captured. Once the scope widened to "anything a real person asks
        Peter to do", it silently became the thing deciding what could be
        captured at all: of six real messages on 2026-07-31, it matched
        two. A birthday and a jiu jitsu tournament never reached the
        classifier, and were reported as capture failures.

        There is no finite word list covering "things a human might ask
        you to do". Do not reintroduce one.
        """
        with mock.patch.object(gmail_scan.config, "optional", return_value=""):
            q = gmail_scan._candidate_query(has_label=True)
        self.assertNotIn("subject:", q)

    def test_machine_generated_categories_are_excluded(self):
        with mock.patch.object(gmail_scan.config, "optional", return_value=""):
            q = gmail_scan._candidate_query(has_label=True)
        for category in ("promotions", "social", "forums"):
            self.assertIn(f"-category:{category}", q)

    def test_updates_is_deliberately_not_excluded(self):
        """
        Where automated-but-real school notification mail lands. Excluding
        it is tempting (it is most of the volume) and would reintroduce
        the exact class of silent miss this filter was rewritten to fix.
        """
        with mock.patch.object(gmail_scan.config, "optional", return_value=""):
            q = gmail_scan._candidate_query(has_label=True)
        self.assertNotIn("-category:updates", q)
        self.assertNotIn("updates", gmail_scan.EXCLUDED_CATEGORIES)

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


class SeenLabelVersioning(unittest.TestCase):
    def test_the_label_carries_a_version_suffix(self):
        """
        The label is permanent; the capture policy is not. A message
        rejected under a narrow policy stays excluded forever unless the
        label name changes with the policy — which is how a real chore
        email was permanently lost on 2026-07-31, rejected seven minutes
        before the rules that would have captured it went live.

        Bump the suffix whenever CLASSIFIER_SYSTEM, the schema, or the
        candidate query changes what can be captured.
        """
        self.assertRegex(gmail_scan.SEEN_LABEL, r"-v\d+$")


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
    "is_actionable": True,
    "item_type": "Assignments",
    "task_name": "Read chapter 4",
    "class_name": "AP Language & Composition Period 3",
    "due_date": "2026-08-01",
}

CHORE = {
    "is_actionable": True,
    "item_type": "Tasks",
    "task_name": "Mow the backyard",
    "class_name": None,
    "due_date": "2026-08-01",
}

OPTIONS = ["AP Lang", "AP Stats", "AP Physics", "School", "Personal"]
TASK_TYPES = ["Execute", "Attend", "Remember", "Action", "Reading", "Essay/Writing"]
PRIORITIES = ["High", "Medium", "Low"]
TYPES = ["Assignments", "Tasks", "Events"]


def options_for(prop):
    """select_option_names is called per property; route by name."""
    return {
        "For": OPTIONS,
        "Task Type": TASK_TYPES,
        "Priority": PRIORITIES,
        "Type": TYPES,
    }[prop]


class MessageLink(unittest.TestCase):
    """
    The Source Link back to the original email.

    Whether these URLs actually open the message can only be settled by
    clicking one -- that was verified by hand against real mail. What is
    pinned here is the shape, and specifically the account selector.
    """

    def test_names_the_account_explicitly_when_the_address_is_known(self):
        # `u/0` means "first signed-in Google account", NOT "the account
        # holding this message" -- so it opens the wrong mailbox once
        # Peter is signed into both school and personal. authuser= is
        # immune to sign-in order, which is the whole reason it is used.
        link = gmail_scan._message_link("abc123", "peter@example.com")
        self.assertEqual(
            link, "https://mail.google.com/mail/?authuser=peter@example.com#all/abc123"
        )

    def test_falls_back_to_u0_when_the_address_is_unknown(self):
        # getProfile failing must cost the better link, never the capture.
        self.assertEqual(
            gmail_scan._message_link("abc123", None),
            "https://mail.google.com/mail/u/0/#all/abc123",
        )

    def test_uses_the_all_scope_so_archived_mail_still_resolves(self):
        # Capture routinely archives nothing, but Peter does -- an #inbox
        # link would 404 on anything he had already filed away.
        self.assertIn("#all/", gmail_scan._message_link("abc123", "p@e.com"))

    def test_the_address_is_url_encoded(self):
        link = gmail_scan._message_link("abc123", "a b+c@example.com")
        self.assertNotIn(" ", link)
        self.assertIn("%20", link)

    def test_a_profile_failure_degrades_instead_of_raising(self):
        svc = FakeGmail(profile_error=http_error(403))
        self.assertIsNone(gmail_scan._mailbox_address(svc))


class Run(unittest.TestCase):
    """The sweep, with _classify_email stubbed -- see module docstring."""

    def setUp(self):
        self.created = []
        patches = [
            mock.patch.object(gmail_scan.notion_client, "create_item", self._create),
            mock.patch.object(
                gmail_scan.notion_client, "select_option_names", side_effect=options_for
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
            gmail_scan, "_classify_email", side_effect=lambda *a, **k: answers.pop(0)
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

    def test_a_captured_item_carries_a_link_back_to_the_email(self):
        svc = FakeGmail(
            labels=[{"id": "L1", "name": gmail_scan.SEEN_LABEL}],
            messages=["m1"],
            address="peter@example.com",
        )
        self._run(svc, [ASSIGNMENT])
        self.assertEqual(
            self.created[0]["source_link"],
            "https://mail.google.com/mail/?authuser=peter@example.com#all/m1",
        )

    def test_the_link_still_works_when_the_profile_cannot_be_read(self):
        svc = FakeGmail(
            labels=[{"id": "L1", "name": gmail_scan.SEEN_LABEL}],
            messages=["m1"],
            profile_error=http_error(403),
        )
        self._run(svc, [ASSIGNMENT])
        self.assertEqual(
            self.created[0]["source_link"], "https://mail.google.com/mail/u/0/#all/m1"
        )

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

    def test_a_failed_create_does_not_label_the_message(self):
        """
        REGRESSION, and the one that mattered most. _mark_seen used to run
        BEFORE create_item, so a create that threw left the email
        labelled `school-sync/seen` -- the next run's `-label:` clause
        excluded it forever while no Notion item existed, and the
        assignment was silently lost.

        Unlabelled means it gets retried: one extra classification
        instead of a lost assignment.
        """
        svc = FakeGmail(labels=[{"id": "L1", "name": gmail_scan.SEEN_LABEL}], messages=["m1"])

        def boom(**kwargs):
            raise ValueError("Notion rejected the due date")

        with mock.patch.object(gmail_scan.notion_client, "create_item", boom):
            with self.assertRaises(RuntimeError):
                self._run(svc, [ASSIGNMENT])
        self.assertEqual(svc.modified, [], "a failed create must NOT label the message")

    def test_a_rejection_is_still_labelled_immediately(self):
        """
        The other half: a "no" creates nothing, so there is nothing to
        lose by labelling at once -- and not paying to recompute it is
        the whole reason the label exists.
        """
        svc = FakeGmail(labels=[{"id": "L1", "name": gmail_scan.SEEN_LABEL}], messages=["m1"])
        self._run(svc, [None])
        self.assertEqual(svc.modified, ["m1"])

    def test_one_bad_message_does_not_stop_the_others(self):
        svc = FakeGmail(
            labels=[{"id": "L1", "name": gmail_scan.SEEN_LABEL}], messages=["m1", "m2", "m3"]
        )
        calls = {"n": 0}

        def sometimes_boom(**kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise ValueError("Notion said no")
            self.created.append(kwargs)

        with mock.patch.object(gmail_scan.notion_client, "create_item", sometimes_boom):
            with self.assertRaises(RuntimeError):  # still fails loudly
                self._run(svc, [ASSIGNMENT, ASSIGNMENT, ASSIGNMENT])
        self.assertEqual(calls["n"], 3, "all three should have been attempted")
        self.assertEqual(len(self.created), 2, "the two good ones still landed")
        self.assertEqual(svc.modified, ["m1", "m3"], "only the successes were labelled")

    def test_a_chore_is_captured_as_a_task_not_an_assignment(self):
        """
        Widened 2026-07-31: capture is no longer schoolwork-only. The Type
        matters because it selects the reminder cadence -- a chore filed
        as an Assignment would nag far harder than it deserves.
        """
        svc = FakeGmail(labels=[{"id": "L1", "name": gmail_scan.SEEN_LABEL}], messages=["m1"])
        self._run(svc, [CHORE])
        item = self.created[0]
        self.assertEqual(item["type_name"], "Tasks")
        self.assertEqual(item["name"], "Mow the backyard")
        self.assertIsNone(item["category"], "a chore belongs to no class")

    def test_the_type_drives_the_task_type_verb(self):
        """An Event is something to Attend, not Execute."""
        svc = FakeGmail(labels=[{"id": "L1", "name": gmail_scan.SEEN_LABEL}], messages=["m1"])
        self._run(svc, [{**CHORE, "item_type": "Events", "task_name": "Band concert"}])
        self.assertIn("Attend", self.created[0]["task_type"])

    def test_an_invalid_type_falls_back_rather_than_polluting_notion(self):
        """
        The schema enum makes this near-impossible, but Notion silently
        CREATES any select option it is handed, so it is checked anyway.
        """
        svc = FakeGmail(labels=[{"id": "L1", "name": gmail_scan.SEEN_LABEL}], messages=["m1"])
        self._run(svc, [{**CHORE, "item_type": "Chores"}])
        self.assertEqual(self.created[0]["type_name"], gmail_scan.DEFAULT_ITEM_TYPE)
        self.assertIn(self.created[0]["type_name"], TYPES)

    def test_a_missing_type_falls_back_to_tasks(self):
        """
        Tasks rather than Assignments on purpose: the Tasks cadence stays
        quiet until the due date is close, so a mis-typed item under-nags
        rather than over-nags.
        """
        svc = FakeGmail(labels=[{"id": "L1", "name": gmail_scan.SEEN_LABEL}], messages=["m1"])
        self._run(svc, [{**CHORE, "item_type": None}])
        self.assertEqual(self.created[0]["type_name"], "Tasks")

    def test_a_non_actionable_email_creates_nothing(self):
        """is_actionable False is how a promotional email is rejected."""
        svc = FakeGmail(labels=[{"id": "L1", "name": gmail_scan.SEEN_LABEL}], messages=["m1"])
        self._run(svc, [None])
        self.assertEqual(self.created, [])

    def test_the_sender_is_passed_to_the_classifier(self):
        """
        The From header carries most of the signal for the corporate-CTA
        distinction, so it must actually reach the model.
        """
        svc = FakeGmail(labels=[{"id": "L1", "name": gmail_scan.SEEN_LABEL}], messages=["m1"])
        seen = {}
        with mock.patch.object(gmail_scan, "_gmail_service", return_value=svc), \
             mock.patch.object(
                 gmail_scan, "_classify_email",
                 side_effect=lambda c, subj, sender, snip: seen.update(sender=sender) or ASSIGNMENT):
            gmail_scan.run(known_ids=set())
        self.assertIn("someone@example.com", seen["sender"])

    def test_without_the_modify_scope_the_sweep_still_runs(self):
        svc = FakeGmail(label_error=http_error(403), messages=["m1"])
        self._run(svc, [ASSIGNMENT])
        self.assertEqual(len(self.created), 1)
        self.assertEqual(svc.modified, [])  # nothing to label with
        self.assertIn(gmail_scan.LOOKBACK_WITHOUT_LABEL, svc.queries[0])


if __name__ == "__main__":
    unittest.main()
