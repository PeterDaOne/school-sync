"""
Mark-done command queue, driven by a second ntfy topic.

WHY A SECOND TOPIC INSTEAD OF A DIRECT NOTION CALL
---------------------------------------------------
ntfy action buttons (see shared/notify.py's build_mark_done_action) fire
an HTTP request straight from the phone when tapped. If that request
PATCHed Notion directly, a full-access NOTION_TOKEN would have to travel
inside the button's URL/body in every single notification — and ntfy.sh
topics are unauthenticated, so anyone who ever learns the topic name gets
that token forever.

Instead, the button POSTs only a bare page id to NTFY_COMMAND_TOPIC — a
second, distinct topic that carries no credential at all. Every sync pass
polls it and marks the referenced pages Done in Notion. Worst case if
this topic leaks: someone can mark your homework Done. Annoying, and
fixed in two seconds by reopening the item in Notion — categorically
different from a leaked credential.

NO DEDUP STATE NEEDED
----------------------
Marking a page Done twice is a no-op, so this polls a rolling time
window (not a persisted cursor) and safely reprocesses any overlap.
That matters because GitHub's runners are ephemeral and keep no state
between runs — a cursor would have to live in Notion like External ID
does, which is real complexity this doesn't need.
"""

import json
import re
import sys

import requests

from . import config, notion_client

TIMEOUT_SECONDS = 10

# Notion page ids are UUIDs, with or without dashes. This is the one
# place an arbitrary internet POST (anyone who knows or guesses the
# command topic) turns directly into a Notion API path segment, so it's
# validated at that boundary rather than trusted.
_PAGE_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$"
)


def poll_mark_done(window: str) -> list[str]:
    """
    Page ids posted to NTFY_COMMAND_TOPIC in the last `window` (an ntfy
    duration string like "2m" or "10m"). Never raises: a poll failure —
    including the topic not being configured yet, which is the normal
    state until Peter sets it up — just means no commands this pass.
    """
    topic = config.optional("NTFY_COMMAND_TOPIC")
    if not topic:
        return []

    server = config.optional("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    url = f"{server}/{topic}/json?poll=1&since={window}"

    try:
        resp = requests.get(url, timeout=TIMEOUT_SECONDS)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[commands] mark-done poll failed, will retry next pass: {e}", file=sys.stderr)
        return []

    page_ids = []
    for line in resp.text.strip().splitlines():
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("event") != "message":
            continue
        candidate = (msg.get("message") or "").strip()
        if _PAGE_ID_RE.match(candidate):
            page_ids.append(candidate)
        elif candidate:
            print(f"[commands] ignoring malformed mark-done payload: {candidate!r}", file=sys.stderr)
    return page_ids


def apply_mark_done(page_ids: list[str]) -> tuple[int, list[str]]:
    """
    Mark each id Done in Notion. One bad id must not stop the rest — same
    per-item error policy as pipeline.py's Calendar sync. Returns
    (applied_count, error_messages); duplicates within the batch are
    collapsed since marking the same page Done twice is wasted work, not
    wrong work.
    """
    applied = 0
    errors: list[str] = []
    for page_id in dict.fromkeys(page_ids):
        try:
            notion_client.mark_done(page_id)
            applied += 1
        except Exception as e:
            errors.append(f"mark-done failed for {page_id}: {e}")
    return applied, errors
