#!/usr/bin/env python3
"""
generate_plist.py

Reads .env (plain KEY=value lines) and writes the launchd job that runs
local_sync.py every 60 seconds.

WHERE THIS WRITES, AND WHY IT MATTERS
-------------------------------------
Straight to ~/Library/LaunchAgents/, with 0600 permissions — NOT into
the project directory.

The generated plist bakes every secret in .env into its XML: the Notion
token, the Google client secret and refresh token, the ntfy topic. This
repo is meant to be pushed to a PUBLIC GitHub repo, so a secret-bearing
file sitting in the working tree is one `git add -A` away from being
world-readable forever. .gitignore covers it as a backstop, but the
better fix is for the file never to exist there at all.

Usage:
    python3 generate_plist.py            # write the plist
    python3 generate_plist.py --reload    # write it, then restart the job
"""

import os
import stat
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parent))

from shared import config

LABEL = "com.peter.schoolsync"
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
PLIST_PATH = LAUNCH_AGENTS / f"{LABEL}.plist"

REQUIRED_KEYS = [
    "NOTION_TOKEN",
    "NOTION_DB_ID",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_REFRESH_TOKEN",
    "GOOGLE_CALENDAR_ID",
    "NTFY_TOPIC",
]

# Optional — each has a sensible default in code, so the plist only
# carries a value when .env overrides it. Cloud-only settings
# (ANTHROPIC_API_KEY, SCHOOL_EMAIL_HINTS, CLOUD_*, CLASSROOM_*) are
# deliberately excluded: local_sync never reads them, and leaving them
# out keeps one fewer copy of a secret on disk.
OPTIONAL_KEYS = [
    "NTFY_SERVER",
    "NTFY_COMMAND_TOPIC",
    "SCHOOL_TIMEZONE",
    "GOOGLE_CALENDAR_TIMEZONE",
    "QUIET_HOURS_START",
    "QUIET_HOURS_END",
    "ASSIGNMENT_ALPHA_HOURS_PER_DAY",
    "ASSIGNMENT_FLOOR_HOURS",
    "ASSIGNMENT_CEILING_HOURS",
    "TASK_ALPHA_HOURS_PER_DAY",
    "TASK_FLOOR_HOURS",
    "TASK_CEILING_HOURS",
    "PRIORITY_MULTIPLIER_HIGH",
    "PRIORITY_MULTIPLIER_MEDIUM",
    "PRIORITY_MULTIPLIER_LOW",
    "REMINDER_JITTER_FRACTION",
    "MAX_NOTIFICATIONS_PER_PASS",
    "EVENT_REMINDER_HOUR",
]


def build_plist(env: dict, python_path: str, project_dir: Path) -> str:
    entries = []
    for key in REQUIRED_KEYS + OPTIONAL_KEYS:
        value = env.get(key)
        if not value or config.is_placeholder(value):
            continue
        entries.append(
            f"        <key>{escape(key)}</key>\n        <string>{escape(value)}</string>"
        )

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>{escape(python_path)}</string>
        <string>{escape(str(project_dir / 'local_sync.py'))}</string>
    </array>

    <key>StartInterval</key>
    <integer>60</integer>

    <key>StandardOutPath</key>
    <string>{escape(str(project_dir / 'sync.log'))}</string>

    <key>StandardErrorPath</key>
    <string>{escape(str(project_dir / 'sync-error.log'))}</string>

    <key>EnvironmentVariables</key>
    <dict>
{chr(10).join(entries)}
    </dict>

    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
"""


def reload_job() -> bool:
    # `launchctl unload` on a job that isn't loaded is a no-op that
    # returns non-zero, so its failure is not interesting here.
    subprocess.run(
        ["launchctl", "unload", str(PLIST_PATH)],
        capture_output=True,
        check=False,
    )
    result = subprocess.run(
        ["launchctl", "load", str(PLIST_PATH)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"ERROR: launchctl load failed: {result.stderr.strip()}", file=sys.stderr)
        return False
    return True


def main() -> int:
    project_dir = Path(__file__).resolve().parent
    env_path = project_dir / ".env"

    if not env_path.exists():
        print(f"ERROR: {env_path} doesn't exist yet.")
        print("Copy the example and fill in your real values first:")
        print(f"    cp {project_dir / '.env.example'} {env_path}")
        return 1

    env = config.parse_env_file(env_path)

    missing = [k for k in REQUIRED_KEYS if config.is_placeholder(env.get(k, ""))]
    if missing:
        print("ERROR: these values are still missing or unfilled in .env:")
        for key in missing:
            print(f"  - {key}")
        return 1

    # Use whichever Python is running THIS script — the one that has the
    # packages installed — rather than guessing a path. Pointing the
    # plist at a different, package-less Python is what caused the old
    # "No module named 'requests'" failures.
    python_path = sys.executable

    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.write_text(build_plist(env, python_path, project_dir))
    PLIST_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600 — it holds secrets

    print(f"Wrote {PLIST_PATH} (permissions 0600)")
    print(f"Using Python: {python_path}")

    # Clean up the secret-bearing copy older versions left in the repo.
    stale = project_dir / f"{LABEL}.plist"
    if stale.exists():
        stale.unlink()
        print(f"Removed stale in-repo copy: {stale}")

    if "--reload" in sys.argv:
        if not reload_job():
            return 1
        print(f"Reloaded {LABEL}. Watch it with: tail -f {project_dir / 'sync.log'}")
        return 0

    print()
    print("Next step — load (or reload) the job:")
    print(f"    launchctl unload {PLIST_PATH} 2>/dev/null; launchctl load {PLIST_PATH}")
    print("Or re-run this script with --reload to do that automatically.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
