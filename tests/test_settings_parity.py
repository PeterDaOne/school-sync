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

import os
import re
import unittest
from pathlib import Path
from unittest import mock

import tests.context  # noqa: F401

import classroom_scan
import generate_plist
from shared import reminders

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "sync.yml"

# Settings that only cloud_sync.py ever reads. They are deliberately
# absent from the plist (generate_plist says so in its own comment):
# local_sync imports only config, log and pipeline, so the capture sweeps
# and the cloud lag cannot run on the Mac at all. But they must still
# reach the workflow, because that is the ONLY place they take effect.
CLOUD_ONLY_SETTINGS = (
    "CLASSROOM_LOOKBACK_HOURS",
    "ANTHROPIC_API_KEY",
    "SCHOOL_EMAIL_HINTS",
    "CLOUD_REMINDERS",
    "CLOUD_REMINDER_LAG_MINUTES",
)


def workflow_defaults() -> dict[str, str]:
    """
    Every `NAME: ${{ vars.NAME || 'default' }}` in the workflow, as
    {NAME: default}.

    Parsing the file is unavoidable — YAML cannot import Python, which is
    the whole reason these two can drift.
    """
    pattern = re.compile(
        r"^\s+([A-Z_]+):\s*\$\{\{\s*vars\.[A-Z_]+\s*\|\|\s*'([^']*)'\s*\}\}", re.M
    )
    return dict(pattern.findall(WORKFLOW.read_text()))


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

    def test_the_workflow_defaults_ARE_the_code_defaults(self):
        """
        Names agreeing is not enough — the VALUES have to agree too, and
        this is the check that was missing.

        `CLASSROOM_LOOKBACK_HOURS` was widened 48 -> 168 on 2026-07-31 in
        the code default, in .env.example, and in the docs. The workflow
        kept saying 48, and since classroom_scan runs ONLY in the cloud,
        the workflow line was the live value and the code default never
        applied to anything. The widening was documented as shipped for
        five days without ever having shipped.

        Rather than map each env name to its Cadence field by hand (25
        lines of boilerplate that would itself go stale), this builds a
        Cadence twice — once with nothing set, once with exactly the
        workflow's defaults — and asserts they are the same object. Any
        divergence in any knob, present or future, fails here.
        """
        defaults = workflow_defaults()
        tunables = {k: v for k, v in defaults.items() if k in reminders.TUNABLE_ENV_VARS}
        self.assertTrue(tunables, "parsed no tunable defaults — has the YAML shape changed?")

        cleared = {name: "" for name in reminders.TUNABLE_ENV_VARS}
        with mock.patch.dict(os.environ, cleared, clear=False):
            from_code = reminders.Cadence.from_env()
        with mock.patch.dict(os.environ, {**cleared, **tunables}, clear=False):
            from_workflow = reminders.Cadence.from_env()

        self.assertEqual(
            from_code, from_workflow,
            "the workflow's defaults produce a different Cadence than the code's "
            "own defaults — the cloud and the Mac would nag differently",
        )

    def test_the_cloud_only_settings_reach_the_workflow(self):
        """
        These never appear in the plist by design, so the workflow is
        their only home and nothing else would notice them missing.
        """
        text = WORKFLOW.read_text()
        missing = [name for name in CLOUD_ONLY_SETTINGS if f"{name}:" not in text]
        self.assertEqual(
            missing, [],
            "cloud-only settings absent from the workflow, where they are the "
            "only thing that takes effect: " + ", ".join(missing),
        )

    def test_the_classroom_lookback_default_matches_the_code(self):
        """
        The specific 2026-07-31 regression, pinned directly as well as by
        the generic check above — this one is worth naming, because the
        cost of getting it wrong is assignments ageing out of the scan
        and being lost permanently and silently.
        """
        default = workflow_defaults().get("CLASSROOM_LOOKBACK_HOURS")
        self.assertIsNotNone(default, "CLASSROOM_LOOKBACK_HOURS is not in the workflow")
        with mock.patch.dict(os.environ, {"CLASSROOM_LOOKBACK_HOURS": default}):
            self.assertEqual(
                classroom_scan._lookback_hours(),
                classroom_scan.DEFAULT_LOOKBACK_HOURS,
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
