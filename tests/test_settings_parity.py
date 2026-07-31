"""
The three places a reminder tunable has to be listed must agree.

WHY THIS FILE EXISTS
--------------------
Reminder behaviour is configured in three separate places that no
compiler, linter or type checker connects:

  1. shared/reminders.py     Cadence.from_env() -- what the code reads
  2. generate_plist.py       OPTIONAL_KEYS      -- what the Mac gets
  3. .github/workflows/sync.yml `env:`          -- what the cloud gets

They drifted. Five knobs added on 2026-07-30 (OVERDUE_DECAY_BASE,
OVERDUE_MAX_INTERVAL_HOURS, DAILY_NOTIFICATION_BUDGET, MIN_INTERVAL_HOURS,
LOAD_SCALE_TARGET_ITEMS) were added to the workflow and to the code, and
never to the plist.

The failure that creates is silent and specifically bad for THIS system:
setting one in .env tunes the cloud while the Mac keeps its default, so
local_sync and cloud_sync disagree about how hard to nag the same item.
Nothing errors. Nothing looks wrong. The reminders are just wrong.

(1) and (2) are now structurally linked -- generate_plist imports the
list. (3) is a YAML file that cannot import Python, so it is checked
here by reading the file.
"""

import re
import unittest
from pathlib import Path

import tests.context  # noqa: F401

import generate_plist
from shared import reminders

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "sync.yml"


class TunableParity(unittest.TestCase):
    def test_every_tunable_is_actually_read_by_from_env(self):
        """
        Guards the other direction: a name listed but never read would
        make the plist and workflow carry a setting that does nothing.
        """
        source = Path(reminders.__file__).read_text()
        for name in reminders.TUNABLE_ENV_VARS:
            self.assertIn(
                f'"{name}"', source, f"{name} is listed as tunable but never read"
            )

    def test_the_plist_carries_every_tunable(self):
        missing = set(reminders.TUNABLE_ENV_VARS) - set(generate_plist.OPTIONAL_KEYS)
        self.assertEqual(
            missing, set(),
            "these tunables would be honoured by the cloud but silently "
            "ignored on the Mac: " + ", ".join(sorted(missing)),
        )

    def test_the_workflow_carries_every_tunable(self):
        env_names = set(re.findall(r"^\s{10}([A-Z_]+):", WORKFLOW.read_text(), re.M))
        missing = set(reminders.TUNABLE_ENV_VARS) - env_names
        self.assertEqual(
            missing, set(),
            "these tunables would be honoured on the Mac but silently "
            "ignored in the cloud: " + ", ".join(sorted(missing)),
        )

    def test_the_timezone_reaches_both(self):
        """
        Not a Cadence tunable, but the same class of bug and the worst
        one: GitHub's runners are UTC, so a missing SCHOOL_TIMEZONE puts
        quiet hours at 18:00-23:00 Mountain in the cloud.
        """
        self.assertIn("SCHOOL_TIMEZONE", generate_plist.OPTIONAL_KEYS)
        self.assertIn("SCHOOL_TIMEZONE:", WORKFLOW.read_text())


if __name__ == "__main__":
    unittest.main()
