#!/usr/bin/env python3
"""
cloud_sync.py — the safety net.

Runs every ~5 minutes on GitHub's servers, completely independent of
whether the Mac is on, asleep, or in a backpack. Does everything
local_sync.py does (Notion -> Calendar, reminders) PLUS the Gmail and
Classroom capture sweeps, neither of which needs sub-minute freshness.

That cadence comes from an EXTERNAL scheduler calling the
workflow_dispatch API, not from the `cron:` in the workflow -- GitHub
deprioritizes sub-hourly schedules badly enough that the cron alone
delivered runs ~110 minutes apart when measured. See README section 8.

REMINDERS FROM THE CLOUD
------------------------
This script fires reminders too, as of 2026-07-28. It previously did
not, which meant no reminders at all whenever the Mac was closed — i.e.
most of the school day, which is exactly when they matter.

It defers to local_sync rather than racing it: reminders are evaluated
with a lag (CLOUD_REMINDER_LAG_MINUTES, default 5), so a reminder is
only sent from here if it has been due that long without local_sync
firing it — which only happens if the Mac is asleep. Last Reminded is
also re-read immediately before each send as a final guard. See
shared/reminders.py for the full reasoning.

Set CLOUD_REMINDERS=false to turn this off and go back to local-only.

PHASE ISOLATION
---------------
The capture sweeps and the sync pass are independent phases. A Gmail
failure (expired token, Anthropic outage) must not stop Classroom from
being swept or, more importantly, stop Notion -> Calendar from running
for every existing item. Each phase is wrapped separately; the run exits
non-zero if any of them failed, so a broken sync shows up red in the
Actions tab instead of quietly looking healthy.
"""

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from shared import config, log, notion_client, pipeline, reminders

import classroom_scan
import gmail_scan

SOURCE = "cloud_sync"


def _run_phase(name: str, fn, *args, **kwargs) -> str | None:
    """Run one phase, returning an error description if it failed."""
    try:
        fn(*args, **kwargs)
        return None
    except Exception as e:
        print(f"[{SOURCE}] phase {name!r} failed: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return f"{name}: {e}"


def main() -> int:
    config.load_dotenv()
    log.install()
    failures: list[str] = []

    # Self-heal any property we write that Peter renamed or deleted in
    # the Notion UI. Each failure mode here is silent rather than loud:
    # a missing External ID brings duplicates back, a missing Reminders
    # Today reads as 0 and disables the daily cap, a missing Source Link
    # drops links. See notion_client._MANAGED_PROPERTIES.
    try:
        created = notion_client.ensure_managed_properties()
        if created:
            print(f"[{SOURCE}] created missing propert(ies): {', '.join(created)}")
    except Exception as e:
        failures.append(f"managed property check: {e}")
        print(f"[{SOURCE}] could not verify our properties: {e}", file=sys.stderr)

    # One query feeds both sweeps' dedup checks — zero extra API calls.
    known_ids: set[str] = set()
    try:
        known_ids = notion_client.external_id_index(notion_client.get_all_items())
    except Exception as e:
        # Without the index we cannot dedup, and creating duplicates is
        # worse than skipping capture for one run. Skip the sweeps.
        failures.append(f"dedup index: {e}")
        print(f"[{SOURCE}] skipping capture sweeps — no dedup index: {e}", file=sys.stderr)
    else:
        for name, module in (("gmail_scan", gmail_scan), ("classroom_scan", classroom_scan)):
            error = _run_phase(name, module.run, known_ids=known_ids)
            if error:
                failures.append(error)

    # Always runs, even if the sweeps above failed.
    try:
        send = config.flag("CLOUD_REMINDERS", default=True)
        report = pipeline.run_sync_pass(
            SOURCE,
            send_reminders=send,
            lag=reminders.cloud_lag(),
            recheck_before_send=True,
            # Wider than local_sync's default 2m -- cloud passes are
            # further apart, so this needs enough margin to not miss a
            # mark-done command posted between passes. Reprocessing
            # overlap is harmless (see shared/commands.py).
            command_window="10m",
        )
        exit_code = pipeline.finish(report, SOURCE)
    except Exception as e:
        print(f"[{SOURCE}] FATAL during sync pass: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        failures.append(f"sync pass: {e}")
        exit_code = 1

    for failure in failures:
        print(f"[{SOURCE}] FAILED: {failure}", file=sys.stderr)
    return 1 if failures else exit_code


if __name__ == "__main__":
    sys.exit(main())
