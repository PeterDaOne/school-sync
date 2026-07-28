"""
Google Calendar read/write layer.

Idempotency: every event we create carries the Notion page ID in
`extendedProperties.private.notion_id`. Before creating an event we look
for one already carrying that ID. That's what stops local_sync and the
GitHub Action from creating duplicate events for the same item.

Auth: a refresh token obtained once via Google's OAuth consent flow (see
README). No service account, no interactive step at runtime.
"""

import os
from datetime import datetime, timedelta

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from . import config, state

CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar"]

# A due date is a deadline, not a meeting, but a zero-length timed event
# renders as an easy-to-miss sliver in most calendar UIs. Give it a
# visible block instead.
TIMED_EVENT_MINUTES = 30

_service_cache = None


def calendar_id() -> str:
    return config.optional("GOOGLE_CALENDAR_ID", "primary")


def timezone_name() -> str:
    return config.optional("GOOGLE_CALENDAR_TIMEZONE", "America/Denver")


def _service():
    """
    Build the Calendar client once per process and reuse it.

    This used to be rebuilt on every single upsert_event() call, which
    meant constructing a fresh client (and potentially a fresh OAuth
    token refresh) once per item, once per 60-second pass. Caching it is
    both faster and much gentler on Google's token endpoint.
    """
    global _service_cache
    if _service_cache is None:
        creds = Credentials(
            token=None,
            refresh_token=config.require("GOOGLE_REFRESH_TOKEN"),
            client_id=config.require("GOOGLE_CLIENT_ID"),
            client_secret=config.require("GOOGLE_CLIENT_SECRET"),
            token_uri="https://oauth2.googleapis.com/token",
            scopes=CALENDAR_SCOPES,
        )
        _service_cache = build("calendar", "v3", credentials=creds, cache_discovery=False)
    return _service_cache


def find_event_by_notion_id(service, notion_id: str):
    resp = (
        service.events()
        .list(
            calendarId=calendar_id(),
            privateExtendedProperty=f"notion_id={notion_id}",
            maxResults=1,
        )
        .execute()
    )
    items = resp.get("items", [])
    return items[0] if items else None


def _event_times(due_date: str) -> tuple[dict, dict]:
    """
    Build Google's start/end pair from a Notion due date.

    Notion gives an ISO date ("2026-08-26") for all-day due dates and a
    full ISO datetime ("2026-07-28T16:00:00.000-06:00") when a time was
    set. Google rejects a full datetime in the `date` field, so the two
    cases need different shapes.

    For all-day events Google's `end.date` is EXCLUSIVE — a one-day
    event ends on the following day. Setting end == start is accepted by
    the API but is a zero-length span, and clients other than Google's
    own web UI render that inconsistently.
    """
    if "T" in due_date:
        start_dt = datetime.fromisoformat(due_date)
        end_dt = start_dt + timedelta(minutes=TIMED_EVENT_MINUTES)
        tz = timezone_name()
        return (
            {"dateTime": start_dt.isoformat(), "timeZone": tz},
            {"dateTime": end_dt.isoformat(), "timeZone": tz},
        )

    start_day = datetime.fromisoformat(due_date).date()
    return (
        {"date": start_day.isoformat()},
        {"date": (start_day + timedelta(days=1)).isoformat()},
    )


def upsert_event(item: dict):
    """
    Create, update, or remove the Calendar event for a Notion item.

    The event is deleted when the item is complete, and also when its due
    date is cleared — otherwise removing a due date in Notion would leave
    an orphaned event on the calendar forever with nothing left pointing
    at it.
    """
    service = _service()
    notion_id = state.external_id_for(item)
    existing = find_event_by_notion_id(service, notion_id)

    if item["is_complete"] or not item["due_date"]:
        if existing:
            service.events().delete(
                calendarId=calendar_id(), eventId=existing["id"]
            ).execute()
        return

    start, end = _event_times(item["due_date"])
    body = {
        "summary": f"{item['class_name'] or 'School'}: {item['name']}",
        "start": start,
        "end": end,
        "extendedProperties": {"private": {"notion_id": notion_id}},
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": 24 * 60},      # 1 day before
                {"method": "popup", "minutes": 3 * 24 * 60},  # 3 days before
            ],
        },
    }
    if item.get("url"):
        body["source"] = {"title": "Open in Notion", "url": item["url"]}

    if existing:
        service.events().update(
            calendarId=calendar_id(), eventId=existing["id"], body=body
        ).execute()
    else:
        service.events().insert(calendarId=calendar_id(), body=body).execute()
