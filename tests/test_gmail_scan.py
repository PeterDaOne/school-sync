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

import base64
import unittest
from unittest import mock

from googleapiclient.errors import HttpError

import tests.context  # noqa: F401

import gmail_scan
from shared import classmap


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
                 address="peter@example.com", profile_error=None, texts=None):
        self._labels = list(labels or [])
        self._messages = list(messages or [])
        self._bodies = bodies or {}
        self._texts = texts or {}
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
        body = self.p._texts.get(id, snippet)
        # format="full" shape: headers plus a base64url text/plain body.
        # The sweep asks for this so the model sees the real email rather
        # than Gmail's ~200-char snippet.
        return _Exec(
            {
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": subject},
                        {"name": "From", "value": "Someone <someone@example.com>"},
                    ],
                    "mimeType": "text/plain",
                    "body": {
                        "data": base64.urlsafe_b64encode(body.encode()).decode()
                    },
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


class ExtractionSchema(unittest.TestCase):
    def test_the_schema_does_not_use_maxItems(self):
        """
        REGRESSION GUARD, found by a live call rather than a test. The
        API rejects it outright:

          400 output_config.format.schema: For 'array' type, property
          'maxItems' is not supported

        The first draft had it and the first real request 400'd. The cap
        lives in _extract_items instead, which is the right place anyway
        -- what needs protecting is Notion rows and phone notifications.
        """
        self.assertNotIn("maxItems", gmail_scan.EXTRACTION_SCHEMA["properties"]["items"])

    def test_the_top_level_is_a_list_not_a_single_item(self):
        self.assertEqual(
            gmail_scan.EXTRACTION_SCHEMA["properties"]["items"]["type"], "array"
        )

    def test_there_is_no_is_actionable_flag(self):
        # An empty list already says "nothing to do"; a separate boolean
        # gave the model two ways to express the same answer.
        self.assertNotIn("is_actionable", gmail_scan.ITEM_SCHEMA["properties"])


class MessageText(unittest.TestCase):
    """
    The body the classifier actually receives. This used to be Gmail's
    ~200-char snippet, because the fetch asked for format="metadata".
    """

    def msg(self, parts=None, body_text=None, snippet="a snippet", mime="text/plain"):
        payload = {"mimeType": mime, "headers": []}
        if body_text is not None:
            payload["body"] = {
                "data": base64.urlsafe_b64encode(body_text.encode()).decode()
            }
        if parts:
            payload["parts"] = parts
        return {"payload": payload, "snippet": snippet}

    def part(self, mime, text):
        return {
            "mimeType": mime,
            "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()},
        }

    def test_decodes_a_simple_plain_text_body(self):
        self.assertEqual(gmail_scan._message_text(self.msg(body_text="hello")), "hello")

    def test_prefers_plain_text_over_html_in_a_multipart_message(self):
        m = self.msg(
            mime="multipart/alternative",
            parts=[self.part("text/plain", "plain version"),
                   self.part("text/html", "<p>html version</p>")],
        )
        self.assertEqual(gmail_scan._message_text(m), "plain version")

    def test_finds_text_nested_several_levels_down(self):
        inner = {"mimeType": "multipart/alternative",
                 "parts": [self.part("text/plain", "deep text")]}
        m = self.msg(mime="multipart/mixed", parts=[inner])
        self.assertEqual(gmail_scan._message_text(m), "deep text")

    def test_falls_back_to_the_snippet_for_html_only_mail(self):
        m = self.msg(mime="text/html", snippet="fallback snippet")
        self.assertEqual(gmail_scan._message_text(m), "fallback snippet")

    def test_is_length_capped(self):
        m = self.msg(body_text="x" * 99999)
        self.assertEqual(len(gmail_scan._message_text(m)), gmail_scan.MAX_BODY_CHARS)

    def test_handles_a_message_with_no_payload_at_all(self):
        self.assertEqual(gmail_scan._message_text({"snippet": "s"}), "s")


class ItemExternalIds(unittest.TestCase):
    def test_the_first_item_uses_the_bare_message_id(self):
        # Backward compatibility with every row captured before
        # multi-item extraction -- see _item_external_id.
        self.assertEqual(gmail_scan._item_external_id("abc", 0), "gmail:abc")

    def test_later_items_are_suffixed_from_two(self):
        self.assertEqual(gmail_scan._item_external_id("abc", 1), "gmail:abc#2")
        self.assertEqual(gmail_scan._item_external_id("abc", 2), "gmail:abc#3")

    def test_ids_are_unique_across_a_whole_email(self):
        ids = [gmail_scan._item_external_id("abc", i) for i in range(10)]
        self.assertEqual(len(set(ids)), 10)


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
        """
        `verdicts` is one answer per message. Each may be a single item
        dict (the common case), a LIST of item dicts (a multi-item
        email), or None for "nothing to do" -- all normalised to the list
        _extract_items really returns.
        """
        answers = [
            [] if v is None else ([v] if isinstance(v, dict) else list(v))
            for v in verdicts
        ]
        with mock.patch.object(gmail_scan, "_gmail_service", return_value=svc), mock.patch.object(
            gmail_scan, "_extract_items", side_effect=lambda *a, **k: answers.pop(0)
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

    def test_an_explicit_life_category_is_used_when_there_is_no_class(self):
        """
        Added 2026-07-31. Before this, a chore captured from a family
        member landed with `For` blank -- 4 of the 5 items recovered that
        day did -- because the classifier had no way to say "Personal".
        """
        svc = FakeGmail(labels=[{"id": "L1", "name": gmail_scan.SEEN_LABEL}], messages=["m1"])
        self._run(svc, [{**ASSIGNMENT, "class_name": None, "category": "Personal"}])
        self.assertEqual(self.created[0]["category"], "Personal")

    def test_a_class_wins_over_a_category_when_both_are_somehow_set(self):
        # The prompt says never set both; this pins what happens anyway.
        svc = FakeGmail(labels=[{"id": "L1", "name": gmail_scan.SEEN_LABEL}], messages=["m1"])
        self._run(svc, [{**ASSIGNMENT, "class_name": "AP Lang", "category": "Personal"}])
        self.assertEqual(self.created[0]["category"], "AP Lang")

    def test_a_course_name_in_the_category_field_is_still_refused(self):
        """
        The safety property the old blanket ban existed for, now enforced
        by resolve_category's exact-match-only allow-list rather than by
        forbidding the categories outright.
        """
        svc = FakeGmail(labels=[{"id": "L1", "name": gmail_scan.SEEN_LABEL}], messages=["m1"])
        self._run(svc, [{**ASSIGNMENT, "class_name": None, "category": "Personal Finance"}])
        self.assertIsNone(self.created[0]["category"])

    def test_an_invented_category_leaves_the_field_blank(self):
        svc = FakeGmail(labels=[{"id": "L1", "name": gmail_scan.SEEN_LABEL}], messages=["m1"])
        self._run(svc, [{**ASSIGNMENT, "class_name": None, "category": "Business"}])
        self.assertIsNone(self.created[0]["category"])

    def test_the_schema_only_offers_the_real_life_categories(self):
        prop = gmail_scan.ITEM_SCHEMA["properties"]["category"]
        enum = next(b["enum"] for b in prop["anyOf"] if "enum" in b)
        self.assertEqual(set(enum), set(classmap.NON_CLASS_CATEGORIES))
        self.assertIn("category", gmail_scan.ITEM_SCHEMA["required"])

    def test_one_email_can_produce_several_items(self):
        """
        The 2026-08-01 fix. A single object schema forced the model to
        compress a multi-assignment email, and it did so two different
        ways depending on its mood: merge everything into one row with
        one due date, or drop the extras entirely and silently.
        """
        svc = FakeGmail(labels=[{"id": "L1", "name": gmail_scan.SEEN_LABEL}], messages=["m1"])
        self._run(svc, [[
            {**ASSIGNMENT, "task_name": "Essay draft", "due_date": "2026-08-04"},
            {**ASSIGNMENT, "task_name": "Read Gatsby ch 5-7", "due_date": "2026-08-06"},
            {**ASSIGNMENT, "task_name": "Vocab quiz", "due_date": "2026-08-08"},
        ]])
        self.assertEqual(
            [c["name"] for c in self.created],
            ["Essay draft", "Read Gatsby ch 5-7", "Vocab quiz"],
        )

    def test_each_item_keeps_its_own_due_date(self):
        """
        The point of splitting. Merged into one row they all inherited
        the earliest date, so two of three nagged from the wrong day and
        the row went overdue while most of it wasn't.
        """
        svc = FakeGmail(labels=[{"id": "L1", "name": gmail_scan.SEEN_LABEL}], messages=["m1"])
        self._run(svc, [[
            {**ASSIGNMENT, "task_name": "A", "due_date": "2026-08-04"},
            {**ASSIGNMENT, "task_name": "B", "due_date": "2026-08-08"},
        ]])
        self.assertEqual(
            [c["due_date"] for c in self.created], ["2026-08-04", "2026-08-08"]
        )

    def test_the_first_item_keeps_the_bare_external_id(self):
        """
        BACKWARD COMPATIBILITY, and it is load-bearing. Every row captured
        before multi-item extraction carries `gmail:<id>`. The seen-label
        had to be bumped for this policy change, which re-opens every
        previously-seen message -- so if the first item used a new ID
        form, every already-captured email would be captured again.
        """
        svc = FakeGmail(labels=[{"id": "L1", "name": gmail_scan.SEEN_LABEL}], messages=["m1"])
        self._run(svc, [[
            {**ASSIGNMENT, "task_name": "A"}, {**ASSIGNMENT, "task_name": "B"},
        ]])
        self.assertEqual(
            [c["external_id"] for c in self.created], ["gmail:m1", "gmail:m1#2"]
        )

    def test_a_previously_captured_email_is_not_recaptured_after_the_label_bump(self):
        svc = FakeGmail(labels=[{"id": "L1", "name": gmail_scan.SEEN_LABEL}], messages=["m1"])
        self._run(svc, [[ASSIGNMENT]], known_ids={"gmail:m1"})
        self.assertEqual(self.created, [])

    def test_extra_items_are_still_added_when_the_first_is_known(self):
        """
        The recovery path: an email captured under the old one-item
        policy gets its extras added without duplicating item one.
        """
        svc = FakeGmail(labels=[{"id": "L1", "name": gmail_scan.SEEN_LABEL}], messages=["m1"])
        self._run(svc, [[
            {**ASSIGNMENT, "task_name": "already have this"},
            {**ASSIGNMENT, "task_name": "the one that was lost"},
        ]], known_ids={"gmail:m1"})
        self.assertEqual([c["name"] for c in self.created], ["the one that was lost"])
        self.assertEqual(self.created[0]["external_id"], "gmail:m1#2")

    def test_every_item_from_one_email_shares_the_source_link(self):
        svc = FakeGmail(labels=[{"id": "L1", "name": gmail_scan.SEEN_LABEL}], messages=["m1"])
        self._run(svc, [[
            {**ASSIGNMENT, "task_name": "A"}, {**ASSIGNMENT, "task_name": "B"},
        ]])
        links = {c["source_link"] for c in self.created}
        self.assertEqual(len(links), 1)
        self.assertIn("#all/m1", links.pop())

    def test_items_can_have_different_types_and_categories(self):
        svc = FakeGmail(labels=[{"id": "L1", "name": gmail_scan.SEEN_LABEL}], messages=["m1"])
        self._run(svc, [[
            {**ASSIGNMENT, "task_name": "Essay", "item_type": "Assignments",
             "class_name": "AP Lang", "category": None},
            {**ASSIGNMENT, "task_name": "Mow lawn", "item_type": "Tasks",
             "class_name": None, "category": "Personal"},
        ]])
        self.assertEqual([c["type_name"] for c in self.created], ["Assignments", "Tasks"])
        self.assertEqual([c["category"] for c in self.created], ["AP Lang", "Personal"])

    def test_an_empty_list_creates_nothing_but_still_labels(self):
        svc = FakeGmail(labels=[{"id": "L1", "name": gmail_scan.SEEN_LABEL}], messages=["m1"])
        self._run(svc, [[]])
        self.assertEqual(self.created, [])
        self.assertEqual(svc.modified, ["m1"])

    def test_without_the_label_a_captured_message_is_skipped_before_any_claude_call(self):
        """
        The degraded no-label path has no query-level dedup, so every run
        re-examines the (narrowed) window and this base-ID check is the
        only thing bounding repeat spend.
        """
        svc = FakeGmail(messages=["m1"], label_error=http_error(403))
        self._run(svc, [], known_ids={"gmail:m1"})
        self.assertEqual(self.created, [])
        self.assertEqual(svc.modified, [])  # not even labelled: nothing examined

    def test_with_the_label_a_captured_message_is_still_examined(self):
        """
        Deliberately NOT skipped on the base External ID when labelling
        works. The label already keeps processed mail out of the query,
        so this only fires after a label VERSION BUMP -- which exists
        precisely to re-open old messages so extras merged or dropped
        under a previous policy can be recovered. Skipping here would
        make every future bump a no-op, the same "filter outlives its
        policy" failure the versioning was introduced to fix.

        Nothing is duplicated: the per-item External ID catches item one.
        """
        svc = FakeGmail(labels=[{"id": "L1", "name": gmail_scan.SEEN_LABEL}], messages=["m1"])
        self._run(svc, [[ASSIGNMENT]], known_ids={"gmail:m1"})
        self.assertEqual(self.created, [])       # item 1 already known
        self.assertEqual(svc.modified, ["m1"])   # but it WAS looked at

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
                 gmail_scan, "_extract_items",
                 side_effect=lambda c, subj, sender, body: seen.update(
                     sender=sender, body=body) or [ASSIGNMENT]):
            gmail_scan.run(known_ids=set())
        self.assertIn("someone@example.com", seen["sender"])

    def test_the_real_body_is_passed_not_just_the_snippet(self):
        """
        The fetch used to be format="metadata", so the model saw Gmail's
        ~200-char preview and nothing else. A teacher's multi-assignment
        digest had everything past the first sentence invisible.
        """
        body = "Essay due Monday. Read chapters 5-7 by Wednesday. Quiz Friday."
        svc = FakeGmail(
            labels=[{"id": "L1", "name": gmail_scan.SEEN_LABEL}],
            messages=["m1"],
            bodies={"m1": ("Week of Aug 4", "Essay due Monday. Read chapt")},
            texts={"m1": body},
        )
        seen = {}
        with mock.patch.object(gmail_scan, "_gmail_service", return_value=svc), \
             mock.patch.object(
                 gmail_scan, "_extract_items",
                 side_effect=lambda c, subj, sender, b: seen.update(body=b) or [ASSIGNMENT]):
            gmail_scan.run(known_ids=set())
        self.assertEqual(seen["body"], body)
        self.assertIn("Quiz Friday", seen["body"])

    def test_without_the_modify_scope_the_sweep_still_runs(self):
        svc = FakeGmail(label_error=http_error(403), messages=["m1"])
        self._run(svc, [ASSIGNMENT])
        self.assertEqual(len(self.created), 1)
        self.assertEqual(svc.modified, [])  # nothing to label with
        self.assertIn(gmail_scan.LOOKBACK_WITHOUT_LABEL, svc.queries[0])


if __name__ == "__main__":
    unittest.main()
