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

import tests.context  # noqa: F401

from shared import notion_client


def page(**overrides) -> dict:
    props = {
        "Title": {"title": [{"plain_text": "Algebra "}, {"plain_text": "Work"}]},
        "Type": {"select": {"name": "Assignments"}},
        "Class": {"select": {"name": "AP Stats"}},
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
