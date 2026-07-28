#!/usr/bin/env python3
"""
gmail_scan.py — the email capture layer. Cloud-only (runs inside
cloud_sync.py via GitHub Actions).

Philosophy: email parsing is not infallible, so this never auto-commits
a deadline. It creates a Notion item with Input Type = "Email" and a
name prefixed "[unconfirmed]" — Peter glances at it in his daily check
and confirms or corrects it. A false positive costs five seconds of
review; a silent auto-add that's wrong costs a missed deadline.

DEDUP
-----
This re-scans a trailing 24-hour window on every run, and cloud_sync
runs every 30 minutes — so without dedup the same email would be
recreated as a new Notion item on ~48 consecutive runs. Each item now
carries External ID = "gmail:<message_id>", and the caller passes in
the set of IDs already present in Notion. Notion holds that state
because GitHub's runners are ephemeral and share nothing between runs.

Dedup is at message granularity, not thread: each email is reviewed
once. Two emails about the same assignment produce two unconfirmed
items, which is the correct failure direction — a duplicate to dismiss
beats a silently dropped deadline.

Uses one lightweight Claude call per candidate email — not a full agent
session — just "does this look like an assignment, and if so what's the
class and due date." Sonnet rather than Opus: this is a cheap
classification on two short strings, and it runs once per candidate
email, so the cost difference is the whole ballgame.
"""

import sys

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from shared import config, notion_client

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

MODEL = "claude-sonnet-5"
MAX_TOKENS = 1000
MAX_MESSAGES_PER_RUN = 20

# Structured outputs guarantee the response parses — no markdown fences
# to strip, no malformed JSON to catch. The old code asked for JSON in
# the prompt and hoped; this makes it the API's problem.
ASSIGNMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "is_assignment": {
            "type": "boolean",
            "description": "True only if this describes a specific school assignment, test, or task.",
        },
        "task_name": {
            "type": ["string", "null"],
            "description": "Short name for the assignment, or null if is_assignment is false.",
        },
        "class_name": {
            "type": ["string", "null"],
            "description": "Class or course name if identifiable, otherwise null.",
        },
        "due_date": {
            "type": ["string", "null"],
            "description": "Due date as YYYY-MM-DD, or null if no date is stated or implied.",
        },
    },
    "required": ["is_assignment", "task_name", "class_name", "due_date"],
    "additionalProperties": False,
}


def _gmail_service():
    creds = Credentials(
        token=None,
        refresh_token=config.require("GOOGLE_REFRESH_TOKEN"),
        client_id=config.require("GOOGLE_CLIENT_ID"),
        client_secret=config.require("GOOGLE_CLIENT_SECRET"),
        token_uri="https://oauth2.googleapis.com/token",
        scopes=GMAIL_SCOPES,
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _candidate_query() -> str:
    """
    Cheap pre-filter before spending a Claude call on anything. Domain
    and keyword filters are ANDed: a candidate must come from a school
    domain AND look assignment-shaped. Tune the keyword list once real
    teacher emails are flowing.
    """
    hints = [h.strip() for h in config.optional("SCHOOL_EMAIL_HINTS").split(",") if h.strip()]
    keywords = (
        "subject:assignment OR subject:due OR subject:homework OR "
        "subject:project OR subject:essay OR subject:quiz OR subject:test"
    )
    parts = [f"({keywords})", "newer_than:1d"]
    if hints:
        parts.insert(0, "(" + " OR ".join(f"from:{h}" for h in hints) + ")")
    return " ".join(parts)


def _extract_assignment(client, subject: str, snippet: str) -> dict | None:
    """One lightweight Claude call: does this look like an assignment?"""
    prompt = (
        f"Subject: {subject}\n"
        f"Snippet: {snippet}\n\n"
        "Does this email describe a specific school assignment, test, or task "
        "with (or implying) a due date?"
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        output_config={"format": {"type": "json_schema", "schema": ASSIGNMENT_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    if resp.stop_reason == "refusal":
        print(f"[gmail_scan] classification refused for {subject!r}", file=sys.stderr)
        return None

    import json

    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text:
        return None
    data = json.loads(text)  # schema-constrained, so this is safe
    if not data.get("is_assignment") or not data.get("task_name"):
        return None
    return data


def run(known_ids: set[str] | None = None):
    """
    Sweep recent mail for assignment-shaped messages.

    `known_ids` is the set of External IDs already in Notion; anything
    already captured is skipped without spending a Claude call on it.
    """
    known_ids = known_ids if known_ids is not None else set()

    api_key = config.optional("ANTHROPIC_API_KEY")
    if config.is_placeholder(api_key):
        # Deliberate, documented state: Peter hasn't bought a key yet.
        # Skipping cleanly keeps the cloud run green and lets the
        # Classroom sweep and the Calendar sync still happen.
        print("[gmail_scan] skipped — ANTHROPIC_API_KEY is not configured yet")
        return

    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    service = _gmail_service()

    results = (
        service.users()
        .messages()
        .list(userId="me", q=_candidate_query(), maxResults=MAX_MESSAGES_PER_RUN)
        .execute()
    )
    messages = results.get("messages", [])

    added = skipped = 0
    for msg_ref in messages:
        external_id = f"gmail:{msg_ref['id']}"
        if external_id in known_ids:
            skipped += 1
            continue

        msg = (
            service.users()
            .messages()
            .get(userId="me", id=msg_ref["id"], format="metadata", metadataHeaders=["Subject"])
            .execute()
        )
        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        parsed = _extract_assignment(client, headers.get("Subject", ""), msg.get("snippet", ""))
        if not parsed:
            continue

        notion_client.create_item(
            name=f"[unconfirmed] {parsed['task_name']}",
            class_name=parsed.get("class_name"),
            due_date=parsed.get("due_date"),
            source="Email",
            external_id=external_id,
        )
        known_ids.add(external_id)  # guard against duplicates within this run
        added += 1

    if added or skipped:
        print(f"[gmail_scan] added {added} unconfirmed item(s), skipped {skipped} already captured")


if __name__ == "__main__":
    config.load_dotenv()
    run()
