"""
Tests for flattening Notion pages into plain dicts.

extract_fields is where a schema change does its damage quietly. The
Status test is a regression guard: Status is a native Status-type
property, and reading it as a `select` returned None for every row,
which made every item look incomplete and kept reminding forever.

Page fixtures below match the live 2026-07-28 schema, verified by raw
curl against the Notion REST API.
"""

import unittest
from unittest import mock

import tests.context  # noqa: F401

from shared import notion_client


def page(**overrides) -> dict:
    props = {
        "Title": {"title": [{"plain_text": "Algebra "}, {"plain_text": "Work"}]},
        "Type": {"select": {"name": "Assignments"}},
        "For": {"select": {"name": "AP Stats"}},
        "Priority": {"select": {"name": "High"}},
        "Status": {"status": {"name": "Not Started"}},
        "Input Type": {"select": {"name": "Classroom"}},
        "Due Date": {"date": {"start": "2026-08-26"}},
        "Last Synced": {"date": {"start": "2026-07-28T05:20:00.000+00:00"}},
        "Last Reminded": {"date": None},
        "External ID": {"rich_text": [{"plain_text": "classroom:123:456"}]},
    }
    props.update(overrides.pop("properties", {}))
    base = {
        "id": "page-1",
        "url": "https://notion.so/page-1",
        "created_time": "2026-07-27T18:00:00.000Z",
        "last_edited_time": "2026-07-28T05:20:00.000Z",
        "properties": props,
    }
    base.update(overrides)
    return base


class ExtractFields(unittest.TestCase):
    def test_reads_status_as_a_status_property(self):
        """Regression: reading Status as `select` yielded None for every row."""
        fields = notion_client.extract_fields(page())
        self.assertEqual(fields["status"], "Not Started")
        self.assertFalse(fields["is_complete"])

    def test_done_marks_complete(self):
        fields = notion_client.extract_fields(
            page(properties={"Status": {"status": {"name": "Done"}}})
        )
        self.assertTrue(fields["is_complete"])

    def test_missing_status_defaults_to_not_started(self):
        fields = notion_client.extract_fields(page(properties={"Status": {"status": None}}))
        self.assertEqual(fields["status"], "Not Started")
        self.assertFalse(fields["is_complete"])

    def test_title_joins_rich_text_runs(self):
        # Notion splits a title into runs whenever formatting changes;
        # taking only the first run silently truncates the name.
        self.assertEqual(notion_client.extract_fields(page())["name"], "Algebra Work")

    def test_external_id_is_read(self):
        self.assertEqual(
            notion_client.extract_fields(page())["external_id"], "classroom:123:456"
        )

    def test_missing_external_id_is_none(self):
        fields = notion_client.extract_fields(page(properties={"External ID": {"rich_text": []}}))
        self.assertIsNone(fields["external_id"])

    def test_absent_external_id_property_is_none(self):
        # Peter edits the schema by hand; the property may not exist yet.
        props = page()["properties"]
        del props["External ID"]
        fields = notion_client.extract_fields({**page(), "properties": props})
        self.assertIsNone(fields["external_id"])

    def test_null_date_property_is_none(self):
        self.assertIsNone(notion_client.extract_fields(page())["last_reminded"])

    def test_notion_timestamps_are_carried_through(self):
        fields = notion_client.extract_fields(page())
        self.assertEqual(fields["created_time"], "2026-07-27T18:00:00.000Z")
        self.assertEqual(fields["last_edited_time"], "2026-07-28T05:20:00.000Z")

    def test_source_defaults_to_manual(self):
        fields = notion_client.extract_fields(page(properties={"Input Type": {"select": None}}))
        self.assertEqual(fields["source"], "Manual")

    def test_url_is_carried_through_for_the_notification_click_target(self):
        self.assertEqual(
            notion_client.extract_fields(page())["url"], "https://notion.so/page-1"
        )

    def test_priority_is_read(self):
        self.assertEqual(notion_client.extract_fields(page())["priority"], "High")

    def test_missing_priority_is_none_not_a_guessed_default(self):
        # extract_fields stays a faithful read of Notion -- defaulting an
        # unset Priority to Medium is shared/reminders.py's job, not this
        # module's, so this must come back as None, not "Medium".
        fields = notion_client.extract_fields(page(properties={"Priority": {"select": None}}))
        self.assertIsNone(fields["priority"])

    def test_reads_the_for_property_as_category(self):
        self.assertEqual(notion_client.extract_fields(page())["category"], "AP Stats")

    def test_falls_back_to_the_old_class_property_name(self):
        """Peter edits this schema by hand in the Notion UI between
        sessions, and a property has already come back under an old name
        that way once. Reading only "For" would make every item's
        category silently vanish -- blank notifications, blank Calendar
        summaries -- with nothing raising."""
        p = page()
        del p["properties"]["For"]
        p["properties"]["Class"] = {"select": {"name": "AP Lang"}}
        self.assertEqual(notion_client.extract_fields(p)["category"], "AP Lang")

    def test_missing_both_names_is_none_not_a_crash(self):
        p = page()
        del p["properties"]["For"]
        self.assertIsNone(notion_client.extract_fields(p)["category"])


class MarkDone(unittest.TestCase):
    """
    No network: patches _request the same way the rest of this test
    suite avoids hitting Notion, verifying only the request shape.
    """

    def test_patches_status_to_done(self):
        with mock.patch.object(notion_client, "_request") as request:
            notion_client.mark_done("page-1")
        request.assert_called_once_with(
            "PATCH",
            "/pages/page-1",
            json={"properties": {"Status": {"status": {"name": "Done"}}}},
        )


class SourceLink(unittest.TestCase):
    """The clickable link back to the Classroom page or Gmail message."""

    def test_is_read_from_the_url_property(self):
        fields = notion_client.extract_fields(
            page(properties={"Source Link": {"url": "https://classroom.google.com/c/A/a/B/details"}})
        )
        self.assertEqual(
            fields["source_link"], "https://classroom.google.com/c/A/a/B/details"
        )

    def test_absent_property_is_none(self):
        # Manual items never have one, and Peter may delete the property.
        self.assertIsNone(notion_client.extract_fields(page())["source_link"])

    def test_null_url_is_none(self):
        fields = notion_client.extract_fields(page(properties={"Source Link": {"url": None}}))
        self.assertIsNone(fields["source_link"])

    def test_create_item_sends_it_as_a_url_property(self):
        with mock.patch.object(notion_client, "_request") as request:
            notion_client.create_item(
                name="Essay", category=None, due_date=None, source="Classroom",
                source_link="https://classroom.google.com/c/A/a/B/details",
            )
        props = request.call_args.kwargs["json"]["properties"]
        self.assertEqual(
            props["Source Link"],
            {"url": "https://classroom.google.com/c/A/a/B/details"},
        )

    def test_omitted_entirely_when_absent(self):
        # Notion rejects some property shapes outright; sending
        # {"url": None} for every manual item is a needless risk.
        with mock.patch.object(notion_client, "_request") as request:
            notion_client.create_item(
                name="Essay", category=None, due_date=None, source="Manual"
            )
        self.assertNotIn("Source Link", request.call_args.kwargs["json"]["properties"])


class EnsureManagedProperties(unittest.TestCase):
    """
    Self-healing for the properties WE write. Peter hand-edits the schema,
    and each of these fails silently rather than loudly when missing.
    """

    def _db(self, present: list[str]) -> dict:
        return {"properties": {name: {} for name in present}}

    def test_no_op_when_all_present(self):
        all_names = list(notion_client._MANAGED_PROPERTIES)
        with mock.patch.object(notion_client, "_request") as request:
            request.return_value = self._db(all_names)
            created = notion_client.ensure_managed_properties()
        self.assertEqual(created, [])
        # One GET, and crucially no PATCH.
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args_list[0].args[0], "GET")

    def test_creates_only_what_is_missing_in_one_patch(self):
        present = [n for n in notion_client._MANAGED_PROPERTIES
                   if n != notion_client.SOURCE_LINK_PROP]
        with mock.patch.object(notion_client, "_request") as request:
            request.side_effect = [self._db(present), {}]
            created = notion_client.ensure_managed_properties()
        self.assertEqual(created, [notion_client.SOURCE_LINK_PROP])
        patch = request.call_args_list[1]
        self.assertEqual(patch.args[0], "PATCH")
        self.assertEqual(
            patch.kwargs["json"]["properties"],
            {notion_client.SOURCE_LINK_PROP: {"url": {}}},
        )

    def test_source_link_is_declared_as_a_url_property(self):
        # rich_text would store the string but never render it clickable.
        self.assertEqual(
            notion_client._MANAGED_PROPERTIES[notion_client.SOURCE_LINK_PROP],
            {"url": {}},
        )

    def test_does_not_manage_properties_peter_owns(self):
        # Recreating one of his would paper over a rename he made on purpose.
        for owned in ("Title", "Type", "For", "Priority", "Status", "Due Date",
                      "Input Type", "Task Type", "Resources"):
            self.assertNotIn(owned, notion_client._MANAGED_PROPERTIES)


class ExternalIdIndex(unittest.TestCase):
    def test_collects_ids_and_ignores_pages_without_one(self):
        blank = page()
        blank["properties"]["External ID"] = {"rich_text": []}
        index = notion_client.external_id_index([page(), blank])
        self.assertEqual(index, {"classroom:123:456"})

    def test_empty_input(self):
        self.assertEqual(notion_client.external_id_index([]), set())


if __name__ == "__main__":
    unittest.main()
