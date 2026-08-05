"""
Configuration loading.

Two things live here:

1. `.env` loading. The launchd plist bakes env vars in directly and
   GitHub Actions injects them from secrets, so in production nothing
   needs a .env file. But running `python3 local_sync.py` by hand to
   debug something used to fail with a bare KeyError because nothing
   loaded .env. Now every entrypoint calls load_dotenv() first, so the
   manual run and the scheduled run behave identically.

   Real environment variables always win over .env — that's standard
   dotenv semantics, and it means launchd/Actions values can't be
   silently shadowed by a stale local file.

2. Required-value access with an error message that says what to do.
   `os.environ["NOTION_TOKEN"]` at module import time raises a bare
   KeyError with no context, and because it fires at *import*, it takes
   down the whole process before any handler runs. require() is called
   lazily, inside functions, and explains the fix.
"""

import os
import re
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_DIR / ".env"

_loaded = False


def parse_env_file(path: Path) -> dict[str, str]:
    """
    Parse plain KEY=value lines. Deliberately minimal — no multiline
    values, no interpolation. Strips optional surrounding quotes so a
    value pasted with quotes still works.

    TRAILING COMMENTS ARE STRIPPED, and that was a real bug (2026-07-31).
    `.env.example` documents several tunables with an inline note:

        ASSIGNMENT_ALPHA_HOURS_PER_DAY=3.4   # -> ceiling at ~14 days

    and the README says `cp .env.example .env`. Without this, the value
    was the whole string including the comment. The numeric settings
    degraded loudly-ish (a "bad value, using <default>" warning on every
    single pass, five of them), but a *string* setting would have been
    silently corrupted — a commented NTFY_TOPIC would publish to a topic
    that doesn't exist, and a commented SCHOOL_TIMEZONE would fall back
    to Denver while looking configured.

    Only ` #` (whitespace then hash) starts a comment, so a value that
    legitimately contains a hash — `topic#1` — survives. A QUOTED value
    is taken verbatim, which is the escape hatch for a secret that really
    does contain " #".
    """
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            values[key.strip()] = value[1:-1]  # quoted: verbatim
            continue
        value = re.split(r"\s#", value, maxsplit=1)[0].strip()
        values[key.strip()] = value
    return values


def load_dotenv(path: Path | None = None) -> None:
    """Load .env into os.environ without overriding anything already set."""
    global _loaded
    if _loaded:
        return
    for key, value in parse_env_file(path or ENV_PATH).items():
        os.environ.setdefault(key, value)
    _loaded = True


def require(name: str) -> str:
    """Fetch a required env var, or fail with a message that says the fix."""
    load_dotenv()
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Required setting {name} is missing.\n"
            f"  - Running locally?  Add {name}=... to {ENV_PATH}\n"
            f"  - launchd job?      Add it to .env, then re-run "
            f"`python3 generate_plist.py` and reload the job.\n"
            f"  - GitHub Actions?   Add it under Settings > Secrets and "
            f"variables > Actions, and to the `env:` block in "
            f".github/workflows/sync.yml."
        )
    return value


def optional(name: str, default: str = "") -> str:
    load_dotenv()
    value = os.environ.get(name, "").strip()
    return value or default


def flag(name: str, default: bool = False) -> bool:
    raw = optional(name, "true" if default else "false").lower()
    return raw in ("1", "true", "yes", "on")


# Settings whose VALUES must never be written anywhere public — a tracked
# file (scan_secrets.py) or an unauthenticated ntfy topic
# (notify.publish_failure). Everything else in .env is a non-secret knob.
#
# It lives here rather than in scan_secrets.py, which owned it first,
# because it now has two consumers and this project has already been
# bitten twice by one list living in several places. scan_secrets imports
# it.
#
# SCHOOL_EMAIL_HINTS is not a credential but IS sensitive: on a public
# repo it names the real school of a minor.
SENSITIVE_KEYS = (
    "NOTION_TOKEN",
    "NOTION_DB_ID",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_REFRESH_TOKEN",
    "GOOGLE_CALENDAR_ID",
    "ANTHROPIC_API_KEY",
    "NTFY_TOPIC",
    "NTFY_COMMAND_TOPIC",
    "SCHOOL_EMAIL_HINTS",
)

# Below this, a "secret" is too short to match meaningfully and would
# redact or flag ordinary text.
MIN_SECRET_LENGTH = 8


def redact(text: str) -> str:
    """
    Replace every live secret VALUE in `text` with its name.

    Used before any error text leaves the process for somewhere
    unauthenticated. Notion surfaces 4xx response bodies in exception
    messages (deliberately — a bare HTTPError is what made schema drift
    undiagnosable), and Google's errors quote request URLs, so an
    exception string is not automatically safe to publish.
    """
    for key in SENSITIVE_KEYS:
        value = optional(key)
        if value and len(value) >= MIN_SECRET_LENGTH:
            text = text.replace(value, f"<{key}>")
    return text


def is_placeholder(value: str) -> bool:
    """
    True if a value is still an unfilled template value rather than a
    real credential. Used to skip a capture source cleanly instead of
    letting it fail mid-run against the API.
    """
    lowered = value.lower()
    return (
        not value
        or "replace" in lowered
        or "xxx" in lowered
        or lowered in ("changeme", "todo", "none")
    )
