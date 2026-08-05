"""
Tests for the capture digest.

The invariant that matters most here is NOT the wording — it is that a
digest never loses an item. The whole reason announcements are exempt
from the daily budget is that a capture is the only time an item is ever
announced; a digest that silently covered 6 of 8 new assignments would
reintroduce exactly the failure that exemption exists to prevent, while
looking like a feature.
"""

import unittest
from dataclasses import replace

import tests.context  # noqa: F401

from shared import digest, reminders

REMINDER = reminders.Reminder(title="📝 New assignment", body="x", kind="capture")


def announcement(item_id, category=None, type_name="Assignments", priority=3):
    return (
        {
            "id": item_id,
            "name": item_id,
            "category": category,
            "type_name": type_name,
        },
        replace(REMINDER, priority=priority),
    )


class BelowThreshold(unittest.TestCase):
    """The common case: send them individually, exactly as before."""

    def test_none_at_or_below_the_threshold(self):
        for count in range(0, 4):
            batch = [announcement(f"i{n}") for n in range(count)]
            self.assertIsNone(
                digest.build(batch, threshold=3),
                f"{count} announcements should not digest at threshold 3",
            )

    def test_builds_once_past_the_threshold(self):
        batch = [announcement(f"i{n}") for n in range(4)]
        self.assertIsNotNone(digest.build(batch, threshold=3))

    def test_a_zero_threshold_digests_anything_plural(self):
        self.assertIsNotNone(
            digest.build([announcement("a"), announcement("b")], threshold=0)
        )

    def test_a_single_item_never_digests_however_the_knob_is_set(self):
        """
        A digest of one is strictly worse than the push it replaces: same
        buzz, minus the due date and the Mark-done button. The floor is
        independent of the threshold so a misconfigured 0 can't produce
        one — and it is why the title is never "1 new assignments".
        """
        for threshold in (-5, 0, 1, 3):
            self.assertIsNone(digest.build([announcement("a")], threshold=threshold))
            self.assertIsNone(digest.build([], threshold=threshold))


class Wording(unittest.TestCase):
    def test_groups_by_category_biggest_first(self):
        batch = (
            [announcement(f"lang{n}", "AP Lang") for n in range(2)]
            + [announcement(f"phys{n}", "AP Physics") for n in range(3)]
            + [announcement("stats1", "AP Stats")]
        )
        summary = digest.build(batch, threshold=3)
        self.assertEqual(summary.title, "📝 6 new assignments")
        self.assertEqual(summary.body, "AP Physics (3) · AP Lang (2) · AP Stats (1)")

    def test_ordering_is_stable_regardless_of_input_order(self):
        """
        Ties break on name, so the same batch reads identically however
        Notion happened to return the rows.
        """
        a = [announcement("x", "AP Lang"), announcement("y", "AP Stats")] * 2
        b = list(reversed(a))
        self.assertEqual(
            digest.build(a, threshold=1).body, digest.build(b, threshold=1).body
        )

    def test_a_mixed_batch_uses_a_generic_noun(self):
        batch = [
            announcement("a", "AP Lang", "Assignments"),
            announcement("b", "AP Lang", "Tasks"),
            announcement("c", "Personal", "Events"),
            announcement("d", "Personal", "Tasks"),
        ]
        summary = digest.build(batch, threshold=3)
        self.assertEqual(summary.title, f"{digest.MIXED_EMOJI} 4 new items")

    def test_uncategorized_items_get_a_label_not_a_blank(self):
        batch = [announcement(f"i{n}") for n in range(4)]
        self.assertEqual(
            digest.build(batch, threshold=3).body,
            f"{digest.UNCATEGORIZED_LABEL} (4)",
        )

    def test_a_long_tail_is_summarised_rather_than_run_on(self):
        batch = [announcement(f"i{n}", f"Class {n}") for n in range(9)]
        body = digest.build(batch, threshold=3).body
        self.assertIn("+3 more", body)
        self.assertEqual(body.count("·"), digest.MAX_GROUPS_SHOWN)


class UrgencyIsPreserved(unittest.TestCase):
    """
    A batch containing something already overdue must not be delivered at
    the lock-screen weight of a batch of three-week-out syllabi.
    """

    def test_takes_the_loudest_constituent(self):
        batch = [announcement(f"i{n}", priority=3) for n in range(3)]
        batch.append(announcement("overdue", priority=5))
        summary = digest.build(batch, threshold=3)
        self.assertEqual(summary.priority, 5)
        self.assertEqual(summary.tags, "rotating_light")

    def test_a_quiet_batch_stays_quiet(self):
        batch = [announcement(f"i{n}", priority=3) for n in range(5)]
        summary = digest.build(batch, threshold=3)
        self.assertEqual(summary.priority, 3)
        self.assertEqual(summary.tags, "")

    def test_it_is_still_a_capture(self):
        """
        kind drives pipeline._allocate's rationing. A digest that came
        back as a nag would be budget-blocked on exactly the busy day it
        exists for.
        """
        batch = [announcement(f"i{n}") for n in range(4)]
        self.assertEqual(digest.build(batch, threshold=3).kind, "capture")


class NothingIsLost(unittest.TestCase):
    """The load-bearing property. See the module docstring."""

    def test_the_count_in_the_title_is_every_item(self):
        for count in (4, 9, 30, 100):
            batch = [announcement(f"i{n}", f"Class {n % 5}") for n in range(count)]
            self.assertIn(f"{count} new", digest.build(batch, threshold=3).title)

    def test_group_counts_sum_to_the_total(self):
        import re

        batch = [announcement(f"i{n}", f"Class {n % 4}") for n in range(20)]
        body = digest.build(batch, threshold=3).body
        self.assertEqual(sum(int(n) for n in re.findall(r"\((\d+)\)", body)), 20)


if __name__ == "__main__":
    unittest.main()
