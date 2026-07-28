"""
Shared test setup.

Two things every test module needs, done once here:

1. The project root on sys.path, so `from shared import ...` resolves
   the same way it does for the real entrypoints.

2. A pinned timezone. The reminder engine reads SCHOOL_TIMEZONE at call
   time, so without pinning it, these tests would pass on Peter's Mac
   (Mountain) and fail on GitHub's runners (UTC) — exactly the
   local/cloud split the engine exists to prevent. Setting it before
   anything imports shared.config matters: load_dotenv uses setdefault,
   so a value already in the environment wins over .env.
"""

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ["SCHOOL_TIMEZONE"] = "America/Denver"

# Keep tests hermetic: never let a real .env leak values into them.
os.environ.setdefault("NOTION_TOKEN", "test-token")
os.environ.setdefault("NOTION_DB_ID", "test-db")
os.environ.setdefault("NTFY_TOPIC", "test-topic")

from shared import timeutil  # noqa: E402

MOUNTAIN = timeutil.school_tz()


def at(iso: str):
    """Build an aware datetime in Peter's timezone from a plain string."""
    from datetime import datetime

    return datetime.fromisoformat(iso).replace(tzinfo=MOUNTAIN)
