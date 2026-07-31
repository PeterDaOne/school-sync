"""
Notion read/write layer.

Notion is the hub: the one place Peter types, taps, and checks things
off. Everything else (Calendar, notifications) is downstream of this
file's output.

Property names below were verified against the live database with a raw
curl to the Notion REST API on 2026-07-28 — not assumed, and not read
through a tool that might reformat them. That verification matters:
"Input Type" spent part of its life misspelled "Imput Type" in the live
schema, and at least one tool silently corrected the typo in its own
output while the real property was still wrong. Check with curl.

Live schema, re-verified by raw curl 2026-07-30:
    Title           title
    Type            select    Assignments / Tasks / Events
    For             select    8 classes + School / Personal / Friends / Work
                              (renamed from "Class" 2026-07-30; see classmap)
    Priority        select    High / Medium / Low
    Status          status    Not Started / In Progress / Done
    Input Type      select    Manual / Email / Syllabus / Classroom
    Task Type       multi_select  Peter's own; nothing here reads it
    Due Date        date
    Last Synced     date      written by us, not the user
    Last Reminded   date      written by us, not the user
    External ID     rich_text written by us — capture dedup key
    Source Link     url       written by us — link back to the original
    Reminders Today number    written by us — backs the daily cap
    Days Until Due  formula   for Peter's views only; see reminders.py
    Resources       rich_text reserved for a later phase, untouched
"""

import time
import sys

import requests

from . import config

NOTION_VERSION = "2022-06-28"
BASE_URL = "https://api.notion.com/v1"

# Status is a native Notion Status-type property (NOT a select — reading
# it as a select returns None for every row, which silently made every
# item look incomplete). These are the members of its "Complete" group.
COMPLETE_STATUSES = {"Done"}

# Property that holds the capture dedup key. See create_item().
EXTERNAL_ID_PROP = "External ID"

# "What is this for?" — a class, or a non-class context like Personal or
# Work. Renamed from "Class" 2026-07-30 (the rename preserved every
# existing value; verified against all 15 live pages). See
# shared/classmap.py for why non-class options must never be matched
# against automatically.
CATEGORY_PROP = "For"

# How many notifications this item has already had TODAY. Added
# 2026-07-30 to back a genuine daily cap (pipeline._allocate).
#
# Deliberately per-item rather than one global counter row: `Last
# Reminded` already tells us which day the count belongs to, so the
# counter self-resets at midnight with no cleanup job, no extra database,
# and no sentinel page polluting Peter's own views. Total sends today is
# just the sum across items whose Last Reminded is today.
REMINDER_COUNT_PROP = "Reminders Today"

# Both are populated by the capture sweeps via shared/tasktype.py. Like
# CATEGORY_PROP, whatever is sent must already exist as an option --
# Notion silently creates any select OR multi-select option it is handed.
TASK_TYPE_PROP = "Task Type"
PRIORITY_PROP = "Priority"

# Clickable link back to wherever a captured item came from: the Google
# Classroom assignment page, or the Gmail message. Only set for captured
# items -- a manually typed one has no source to point at.
#
# Named "Source Link", not "Source": `Input Type` already answers what
# KIND of source it was (Manual / Email / Syllabus / Classroom), and a
# bare "Source" reads like a duplicate of it.
#
# Notion type `url`, not `rich_text`: only `url` renders as an actually
# clickable link. (The separate `Resources` rich_text property is
# reserved for a later phase and is deliberately left alone.)
SOURCE_LINK_PROP = "Source Link"

MAX_ATTEMPTS = 5


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.require('NOTION_TOKEN')}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _database_id() -> str:
    return config.require("NOTION_DB_ID")


def _request(method: str, path: str, idempotent: bool = True, **kwargs):
    """
    Thin wrapper with backoff.

    429 is always retried — a rate-limited request definitionally did not
    execute. 5xx and connection errors are retried only when `idempotent`
    is True, because a create that timed out may actually have succeeded
    on Notion's side, and blindly retrying it would create a duplicate
    page. create_item() is the one caller that passes idempotent=False.
    """
    last_error: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            resp = requests.request(
                method, f"{BASE_URL}{path}", headers=_headers(), timeout=30, **kwargs
            )
        except requests.RequestException as e:
            if not idempotent or attempt == MAX_ATTEMPTS - 1:
                raise
            last_error = e
            time.sleep(2**attempt)
            continue

        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 2**attempt))
            time.sleep(wait)
            continue

        if resp.status_code >= 500 and idempotent and attempt < MAX_ATTEMPTS - 1:
            time.sleep(2**attempt)
            continue

        if resp.status_code >= 400:
            # Notion's error body says exactly what's wrong (bad property
            # name, wrong type, missing capability). Surface it — a bare
            # HTTPError here is what makes schema drift hard to diagnose.
            raise RuntimeError(
                f"Notion API {resp.status_code} on {method} {path}: {resp.text[:500]}"
            )
        return resp.json()

    raise RuntimeError(
        f"Notion API failed after {MAX_ATTEMPTS} attempts: {method} {path} ({last_error})"
    )


def get_all_items() -> list[dict]:
    """Pull every row from the school database, following pagination."""
    items: list[dict] = []
    payload = {"page_size": 100}
    while True:
        data = _request("POST", f"/databases/{_database_id()}/query", json=payload)
        items.extend(data["results"])
        if not data.get("has_more"):
            return items
        payload["start_cursor"] = data["next_cursor"]


def mark_synced(page_id: str, timestamp_iso: str):
    """Stamp a page as synced so we don't reprocess it next run."""
    _request(
        "PATCH",
        f"/pages/{page_id}",
        json={"properties": {"Last Synced": {"date": {"start": timestamp_iso}}}},
    )


def mark_reminded(page_id: str, timestamp_iso: str, count_today: int | None = None):
    """
    Stamp Last Reminded so the reminder engine knows when it last fired.

    `count_today` writes REMINDER_COUNT_PROP in the SAME request -- one
    round trip, and the pair can never disagree about which day the count
    belongs to, since Last Reminded is what dates it.
    """
    props = {"Last Reminded": {"date": {"start": timestamp_iso}}}
    if count_today is not None:
        props[REMINDER_COUNT_PROP] = {"number": count_today}
    _request("PATCH", f"/pages/{page_id}", json={"properties": props})


def mark_done(page_id: str):
    """
    Set Status to Done. Used by the mark-done notification button (see
    shared/commands.py) — setting it twice is harmless, so callers don't
    need to check current state first.
    """
    _request(
        "PATCH",
        f"/pages/{page_id}",
        json={"properties": {"Status": {"status": {"name": "Done"}}}},
    )


def get_last_reminded(page_id: str) -> str | None:
    """
    Re-read just this page's Last Reminded. Used by cloud_sync as a
    final check immediately before sending, to close the last sliver of
    the double-fire window with local_sync. See shared/reminders.py.
    """
    page = _request("GET", f"/pages/{page_id}")
    prop = page.get("properties", {}).get("Last Reminded", {}).get("date")
    return prop.get("start") if prop else None


def create_item(
    name: str,
    category: str | None,
    due_date: str | None,
    source: str,
    type_name: str = "Assignments",
    external_id: str | None = None,
    task_type: list[str] | None = None,
    priority: str | None = None,
    source_link: str | None = None,
):
    """
    Used by the Gmail and Classroom sweeps to add an item Peter hasn't
    typed in himself. `source` goes into "Input Type"; `type_name` goes
    into "Type"; `category` goes into "For" and must already be one of
    that property's existing options (see classmap.resolve — Notion
    silently creates any option it is handed).

    `external_id` is the capture dedup key — a stable identifier for the
    upstream thing this item came from ("gmail:<message_id>",
    "classroom:<course_id>:<coursework_id>"). It has to live in Notion
    rather than a local file because GitHub's runners are ephemeral:
    they keep no state between the ~48 runs that share a 24-hour scan
    window, so Notion is the only thing both sides can see.

    Leaves "Last Reminded" unset on purpose — that's what tells the
    reminder engine this item is newly captured and owes a capture
    notification.
    """
    properties = {
        "Title": {"title": [{"text": {"content": name}}]},
        CATEGORY_PROP: {"select": {"name": category}} if category else None,
        "Status": {"status": {"name": "Not Started"}},
        "Input Type": {"select": {"name": source}},
        "Type": {"select": {"name": type_name}},
    }
    # Both must already exist as options -- see shared/tasktype.py, which
    # filters against the live option list before returning anything.
    if task_type:
        properties[TASK_TYPE_PROP] = {"multi_select": [{"name": t} for t in task_type]}
    if priority:
        properties[PRIORITY_PROP] = {"select": {"name": priority}}
    if due_date:
        properties["Due Date"] = {"date": {"start": due_date}}
    if external_id:
        properties[EXTERNAL_ID_PROP] = {
            "rich_text": [{"text": {"content": external_id}}]
        }
    # One click from the Notion page back to the original assignment or
    # email. Omitted entirely when absent -- a manual item has no source.
    if source_link:
        properties[SOURCE_LINK_PROP] = {"url": source_link}
    properties = {k: v for k, v in properties.items() if v is not None}

    return _request(
        "POST",
        "/pages",
        idempotent=False,
        json={"parent": {"database_id": _database_id()}, "properties": properties},
    )


_select_options_cache: dict[str, list[str]] = {}


def select_option_names(property_name: str) -> list[str]:
    """
    The option names currently defined for a select property.

    Needed because Notion *creates* any select option you send it that
    doesn't already exist — there's no strict mode. So the only way to
    avoid polluting Peter's carefully-named Class list is to read the
    real options first and refuse to send anything else.

    Cached per process: the schema can't change mid-run, and this would
    otherwise be one extra API call per captured item.
    """
    if property_name not in _select_options_cache:
        db = _request("GET", f"/databases/{_database_id()}")
        prop = db.get("properties", {}).get(property_name, {})
        options = prop.get(prop.get("type", ""), {}).get("options", [])
        _select_options_cache[property_name] = [o["name"] for o in options]
    return _select_options_cache[property_name]


def external_id_index(pages: list[dict]) -> set[str]:
    """
    Every external ID already present in the database.

    Built from a list of pages the caller already fetched rather than
    issuing its own query, so the dedup check costs zero extra API calls.
    """
    found = set()
    for page in pages:
        value = _rich_text(page.get("properties", {}).get(EXTERNAL_ID_PROP))
        if value:
            found.add(value)
    return found


def _rich_text(prop: dict | None) -> str | None:
    if not prop:
        return None
    text = "".join(t.get("plain_text", "") for t in prop.get("rich_text", []))
    return text or None


def extract_fields(page: dict) -> dict:
    """Flatten a Notion page's properties into a plain dict for other modules."""
    props = page["properties"]

    def _title(p):
        return "".join(t["plain_text"] for t in p.get("title", [])) if p else ""

    def _select(p):
        return p.get("select", {}).get("name") if p and p.get("select") else None

    def _status(p):
        return p.get("status", {}).get("name") if p and p.get("status") else None

    def _date(p):
        return p.get("date", {}).get("start") if p and p.get("date") else None

    status = _status(props.get("Status")) or "Not Started"

    return {
        "id": page["id"],
        "url": page.get("url"),
        "name": _title(props.get("Title")),
        # "For" was called "Class" until 2026-07-30. The fallback is not
        # migration cruft — Peter edits this schema by hand in the Notion
        # UI between sessions, and this project has already had a
        # property come back under its old (typo'd) name that way. Read
        # both; a missing category degrades to a blank field, not a crash.
        "category": _select(props.get(CATEGORY_PROP) or props.get("Class")),
        "type_name": _select(props.get("Type")),
        "priority": _select(props.get("Priority")),
        "due_date": _date(props.get("Due Date")),
        "status": status,
        "is_complete": status in COMPLETE_STATUSES,
        "source": _select(props.get("Input Type")) or "Manual",
        "last_reminded": _date(props.get("Last Reminded")),
        "external_id": _rich_text(props.get(EXTERNAL_ID_PROP)),
        "source_link": (props.get(SOURCE_LINK_PROP) or {}).get("url"),
        # Only meaningful alongside last_reminded -- it counts sends on
        # the day that stamp names. pipeline decides whether that day is
        # today; extract_fields stays a faithful read.
        "reminders_today": (props.get(REMINDER_COUNT_PROP) or {}).get("number") or 0,
        # Notion's own timestamps. created_time is the reference point
        # for the cloud reminder lag on capture notifications, which have
        # no previous reminder to measure from.
        "created_time": page.get("created_time"),
        "last_edited_time": page.get("last_edited_time"),
    }


# The properties this system WRITES, and their Notion schema. Peter edits
# the database by hand in the Notion UI between sessions, so each of these
# is recreated if it goes missing rather than left to fail -- and in two
# of the three cases the failure would be silent rather than loud, which
# is the real reason this exists:
#
#   External ID     missing -> capture dedup breaks, duplicates return
#   Reminders Today missing -> reads as 0, silently disabling the daily cap
#   Source Link     missing -> links are silently dropped from new items
#
# Properties Peter owns (Title, Type, For, Priority, Status, Due Date,
# Input Type, Task Type) are deliberately NOT here: recreating one of his
# would paper over a rename he made on purpose.
_MANAGED_PROPERTIES = {
    EXTERNAL_ID_PROP: {"rich_text": {}},
    REMINDER_COUNT_PROP: {"number": {}},
    SOURCE_LINK_PROP: {"url": {}},
}


def ensure_managed_properties() -> list[str]:
    """
    Create any of our own properties that have gone missing. Returns the
    names created — an empty list is the normal case.

    One GET covering all of them, rather than one per property: this was
    two near-identical functions each issuing its own database read, and
    adding Source Link would have made it three reads per cloud run to
    answer a single question. Missing properties are also created in a
    single PATCH.
    """
    db = _request("GET", f"/databases/{_database_id()}")
    existing = db.get("properties", {})
    missing = {n: s for n, s in _MANAGED_PROPERTIES.items() if n not in existing}
    if not missing:
        return []
    for name in sorted(missing):
        print(f"[notion] '{name}' property missing — creating it", file=sys.stderr)
    _request("PATCH", f"/databases/{_database_id()}", json={"properties": missing})
    return sorted(missing)
