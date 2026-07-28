"""
Tests for timezone handling.

These pin the invariant that made the reminder engine correct in both
places it runs: a naive timestamp means Peter's wall clock, never UTC,
regardless of whether the code is on his Mac or a UTC cloud runner.
"""

import unittest
from datetime import timedelta, timezone

import tests.context  # noqa: F401

from shared import timeutil


class Parsing(unittest.TestCase):
    def test_date_only_is_interpreted_in_school_timezone(self):
        dt = timeutil.parse("2026-07-30")
        self.assertEqual(dt.utcoffset(), timedelta(hours=-6))  # MDT, not UTC

    def test_zulu_timestamps(self):
        dt = timeutil.parse("2026-07-28T05:31:00.000Z")
        self.assertEqual(dt.utcoffset(), timedelta(0))

    def test_offset_aware_timestamps_are_left_alone(self):
        dt = timeutil.parse("2026-07-28T16:00:00.000-06:00")
        self.assertEqual(dt.hour, 16)
        self.assertEqual(dt.utcoffset(), timedelta(hours=-6))

    def test_winter_date_uses_standard_time(self):
        # DST correctness comes free from zoneinfo, but pin it so a
        # future refactor to a fixed -6 offset gets caught.
        dt = timeutil.parse("2026-01-15")
        self.assertEqual(dt.utcoffset(), timedelta(hours=-7))  # MST


class Helpers(unittest.TestCase):
    def test_has_time_component(self):
        self.assertFalse(timeutil.has_time_component("2026-07-30"))
        self.assertTrue(timeutil.has_time_component("2026-07-30T12:00:00"))

    def test_end_of_day_preserves_timezone(self):
        dt = timeutil.end_of_day(timeutil.parse("2026-07-30"))
        self.assertEqual((dt.hour, dt.minute, dt.second), (23, 59, 0))
        self.assertEqual(dt.utcoffset(), timedelta(hours=-6))

    def test_now_is_timezone_aware(self):
        self.assertIsNotNone(timeutil.now().tzinfo)

    def test_utc_now_iso_round_trips(self):
        parsed = timeutil.parse(timeutil.utc_now_iso())
        self.assertEqual(parsed.tzinfo, timezone.utc)

    def test_unknown_timezone_falls_back_instead_of_crashing(self):
        import os

        original = os.environ.get("SCHOOL_TIMEZONE")
        os.environ["SCHOOL_TIMEZONE"] = "Mars/Olympus_Mons"
        try:
            self.assertEqual(str(timeutil.school_tz()), timeutil.DEFAULT_TIMEZONE)
        finally:
            if original is None:
                del os.environ["SCHOOL_TIMEZONE"]
            else:
                os.environ["SCHOOL_TIMEZONE"] = original


if __name__ == "__main__":
    unittest.main()
