"""
Shared sync-state tracking.

Both local_sync.py (every 60s on the Mac, only while awake) and
cloud_sync.py (every ~5 min on GitHub Actions, always) need to agree on
"what have we already synced" so they don't create duplicate Calendar
events for the same item.

State lives in Notion properties, not a local file, because a local file
can't be shared between a Mac and an ephemeral cloud runner. Every page
carries a "Last Synced" timestamp, and every Calendar event we create is
tagged with the Notion page ID so we can always ask "does this already
exist" before creating anything.
"""

from datetime import timedelta

from . import timeutil

# Writing "Last Synced" (or "Last Reminded") to a page is itself an edit —
# Notion stamps last_edited_time when the server processes that PATCH,
# which is always at least a few milliseconds after the timestamp value
# we computed client-side and sent in the body. Without a grace window,
# "last_edited > last_synced" is true again on the very next check,
# forever, for every item ever synced: the sync can never catch up to its
# own writes. That is what made every item re-sync on every 60s pass.
#
# If you ever see "synced N item(s)" logged every pass with nothing
# actually changing, this is the first thing to check.
SYNC_GRACE = timedelta(seconds=10)


def needs_sync(notion_page: dict) -> bool:
    """
    True if a page has never been synced, or was edited more recently
    than its last sync stamp (beyond SYNC_GRACE, so our own writes don't
    trigger an immediate re-sync).

    Takes the raw page object from the Notion API.
    """
    props = notion_page.get("properties", {})
    last_synced_prop = props.get("Last Synced", {}).get("date")
    last_edited = notion_page.get("last_edited_time")

    if not last_synced_prop:
        return True  # never synced

    last_synced = last_synced_prop.get("start")
    if not last_synced or not last_edited:
        return True

    return timeutil.parse(last_edited) > timeutil.parse(last_synced) + SYNC_GRACE


def external_id_for(item_or_page: dict) -> str:
    """
    Stable ID used to tag Calendar events, so re-running sync updates the
    existing event instead of duplicating it. Accepts either a raw Notion
    page or an extract_fields() dict — both carry "id".
    """
    return f"notion-{item_or_page['id']}"
