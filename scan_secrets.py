#!/usr/bin/env python3
"""
scan_secrets.py — run this before every push. The repo is PUBLIC.

Two checks, because they catch different mistakes:

  1. Is a secret-bearing FILE tracked by git? (.env, client_secret.json,
     a generated .plist). .gitignore should prevent it; this verifies.

  2. Does any live credential VALUE from .env appear inside a tracked
     file? This is the one .gitignore cannot catch — a token pasted into
     a README example, a database ID in a doc, a school domain quoted in
     a handoff note. It has caught real issues twice.

WHY THERE IS A DENYLIST INSTEAD OF "FLAG EVERY .env VALUE"
----------------------------------------------------------
Several .env values are not secrets and legitimately appear in tracked
files: NTFY_SERVER is "https://ntfy.sh", SCHOOL_TIMEZONE is
"America/Denver". Flagging those produced 15 hits of which 13 were
noise, and a check that cries wolf is a check nobody runs. Only keys
that are genuinely sensitive are scanned — see SENSITIVE_KEYS.

PII COUNTS AS SENSITIVE HERE
-----------------------------
SCHOOL_EMAIL_HINTS names the real school of a minor. It is not a
credential and leaking it breaks nothing technically, which is exactly
why it slipped into a tracked file once already. On a public repo it is
the most personal thing in the config.

Exit code is 1 if anything is found, so this can gate a push.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from shared import config

PROJECT_DIR = Path(__file__).resolve().parent

# Files that must never be tracked. Matched precisely rather than by
# substring: ".env" as a substring also matches ".env.example", which is
# committed on purpose and holds nothing real.
FORBIDDEN_EXACT = {".env"}
FORBIDDEN_PREFIXES = ("client_secret",)
FORBIDDEN_SUFFIXES = (".plist",)

# Public consumer mail domains. They legitimately appear in .env.example
# and in docs, and they identify nobody -- unlike a school domain, which
# names the actual school of a minor.
GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com",
    "icloud.com", "aol.com", "proton.me",
}

# Keys whose VALUES must never appear in a tracked file. Everything else
# in .env is a non-secret setting -- see the module docstring.
#
# Defined in shared/config.py, not here, because it grew a second
# consumer on 2026-08-05: notify.publish_failure redacts the same values
# before posting error text to an unauthenticated ntfy topic. Two copies
# of a denylist is one copy that gets updated.
SENSITIVE_KEYS = config.SENSITIVE_KEYS

# Too short to be a meaningful match; would produce noise.
MIN_VALUE_LENGTH = config.MIN_SECRET_LENGTH


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=PROJECT_DIR, capture_output=True, text=True, check=True
    )
    return out.stdout.split()


def main() -> int:
    problems: list[str] = []
    files = tracked_files()

    for path in files:
        name = Path(path).name
        if (
            name in FORBIDDEN_EXACT
            or name.startswith(FORBIDDEN_PREFIXES)
            or name.endswith(FORBIDDEN_SUFFIXES)
        ):
            problems.append(f"tracked file holds credentials: {path}")

    env = config.parse_env_file(PROJECT_DIR / ".env")
    values = {
        key: value
        for key, value in env.items()
        if key in SENSITIVE_KEYS
        and len(value) >= MIN_VALUE_LENGTH
        and not config.is_placeholder(value)
    }

    for path in files:
        try:
            text = (PROJECT_DIR / path).read_text(errors="ignore")
        except (OSError, UnicodeDecodeError):
            continue
        for key, value in values.items():
            if value in text:
                problems.append(f"{key} value appears in tracked file: {path}")
            # Comma-separated settings leak one element at a time --
            # SCHOOL_EMAIL_HINTS naming a real school is the case that
            # matters. Generic consumer domains are skipped: they appear
            # in .env.example on purpose and identify nobody.
            for part in (p.strip() for p in value.split(",")):
                if (
                    len(part) >= MIN_VALUE_LENGTH
                    and part != value
                    and part.lower() not in GENERIC_DOMAINS
                    and part in text
                ):
                    problems.append(
                        f"{key} element {part!r} appears in tracked file: {path}"
                    )

    print(f"scanned {len(files)} tracked file(s) against {len(values)} sensitive value(s)")
    if not problems:
        print("clean — safe to push")
        return 0

    print(f"\n{len(problems)} PROBLEM(S):", file=sys.stderr)
    for problem in dict.fromkeys(problems):
        print(f"  - {problem}", file=sys.stderr)
    print(
        "\nIf a credential was already pushed, deleting it is NOT enough — the "
        "old commit stays on GitHub. Rotate it.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
