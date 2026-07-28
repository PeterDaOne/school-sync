"""
Tests for sync-state tracking.

The grace-window test is the important one here — without it, the
system re-synced every item on every 60-second pass forever, because
writing "Last Synced" is itself an edit and Notion stamps
last_edited_time after the value we sent.
"""

import unittest

import tests.context  # noqa: F401  (path + timezone setup)

from shared import state


def page(last_synced: str | None, last_edited: str | None) -> dict:
    props = {}
    if last_synced is not None:
        props["Last Synced"] = {"date": {"start": last_synced}}
    return {"id": "abc", "properties": props, "last_edited_time": last_edited}


class NeedsSync(unittest.TestCase):
    def test_never_synced(self):
        self.assertTrue(needs := state.needs_sync(page(None, "2026-07-28T05:20:00.000Z")))
        self.assertIs(needs, True)

    def test_edited_after_sync_beyond_grace(self):
        self.assertTrue(
            state.needs_sync(
                page("2026-07-28T05:20:00.000+00:00", "2026-07-28T05:25:00.000Z")
            )
        )

    def test_our_own_write_does_not_retrigger(self):
        """
        Regression test for the infinite re-sync loop.

        Notion stamps last_edited_time a few milliseconds after the
        timestamp we computed client-side and sent in the PATCH body,
        so last_edited is always fractionally newer than last_synced.
        Within SYNC_GRACE that must read as "already synced".
        """
        self.assertFalse(
            state.needs_sync(
                page("2026-07-28T05:20:00.000+00:00", "2026-07-28T05:20:00.300Z")
            )
        )

    def test_grace_boundary(self):
        # Exactly at the grace edge is still "not newer than", so no re-sync.
        self.assertFalse(
            state.needs_sync(
                page("2026-07-28T05:20:00.000+00:00", "2026-07-28T05:20:10.000Z")
            )
        )
        self.assertTrue(
            state.needs_sync(
                page("2026-07-28T05:20:00.000+00:00", "2026-07-28T05:20:10.001Z")
            )
        )

    def test_mixed_zulu_and_offset_formats_compare_correctly(self):
        # Notion returns Z for last_edited_time and +00:00 for date
        # properties. These are the same instant and must compare as such.
        self.assertFalse(
            state.needs_sync(
                page("2026-07-28T05:20:00.000+00:00", "2026-07-28T05:20:00.000Z")
            )
        )

    def test_missing_last_edited_time(self):
        self.assertTrue(state.needs_sync(page("2026-07-28T05:20:00.000+00:00", None)))


class ExternalId(unittest.TestCase):
    def test_prefixes_the_page_id(self):
        self.assertEqual(state.external_id_for({"id": "xyz"}), "notion-xyz")

    def test_accepts_an_extracted_item_too(self):
        self.assertEqual(
            state.external_id_for({"id": "xyz", "name": "Algebra"}), "notion-xyz"
        )


if __name__ == "__main__":
    unittest.main()
