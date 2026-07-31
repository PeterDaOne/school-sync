#!/usr/bin/env python3
"""
gmail_scan.py — the email capture layer. Cloud-only (runs inside
cloud_sync.py via GitHub Actions).

Email parsing is not infallible: unlike Classroom, where the title, due
date and course arrive as structured fields, everything here is inferred
by Claude from prose. So a captured item is worth a glance before it is
trusted.

That glance used to be prompted by a "[unconfirmed] " prefix on the
title. **Removed 2026-07-30 on Peter's call, and the reasoning is worth
keeping:** the provenance is already recorded structurally in
`Input Type = "Email"`, so the prefix duplicated a signal that already
existed — and it duplicated it into the Title, which is the one field
that rides along into every phone notification for the life of the item.
Filter or colour on `Input Type` in Notion instead; it costs nothing at
the lock screen. Do not reintroduce the prefix without a reason that
isn't already covered by that property.

TWO KINDS OF DEDUP, AND WHY BOTH ARE NEEDED
-------------------------------------------
The External ID (`gmail:<message_id>`) stops an email that BECAME a
Notion item from becoming a second one. But it says nothing about
emails Claude *rejected* — those write nothing to Notion, so there is
no External ID to find them by, and they were re-classified on every
single run for as long as they stayed inside the search window. At a
5-minute cron that is 288 Claude calls per junk email per day. A
handful of stuck emails would quietly cost tens of dollars a month to
keep answering the same question with the same "no".

So every message this script *looks at* gets a Gmail label
("school-sync/seen"), and the search excludes labelled mail. Each email
costs exactly one classification, ever, whatever the verdict.

Labelling needs the `gmail.modify` scope. If the refresh token predates
that scope the label call 403s; rather than fail, the scan falls back to
a much narrower time window so the repeat cost stays bounded, and says
so in the log. MAX_CLASSIFICATIONS_PER_RUN is a hard backstop under
both paths.

Sonnet rather than Opus: this is a cheap yes/no on two short strings,
run once per candidate email, so cost per call is the whole ballgame.
"""

import json
import sys

from googleapiclient.errors import HttpError

from shared import classmap, config, googleauth, notion_client, tasktype

# gmail.modify is required to apply the "seen" label. It is a broader
# scope than readonly, so it is requested deliberately and used for
# exactly one thing: adding a label to messages this script has already
# classified. Nothing here reads, sends, or deletes mail.
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

SEEN_LABEL = "school-sync/seen"

MODEL = "claude-sonnet-5"
MAX_TOKENS = 1000
MAX_MESSAGES_PER_RUN = 20

# Hard ceiling on Claude calls per run, independent of the label logic.
# If dedup ever breaks again, this caps the damage at a knowable number
# instead of letting it scale with the cron frequency.
MAX_CLASSIFICATIONS_PER_RUN = 10

# With labelling, a wide window is free — a message is only ever looked
# at once. Without it, every run re-examines everything in the window,
# so the window itself is the only cost control left.
LOOKBACK_WITH_LABEL = "newer_than:1d"
LOOKBACK_WITHOUT_LABEL = "newer_than:2h"

# Structured outputs guarantee the response parses — no markdown fences
# to strip, no malformed JSON to catch.
ASSIGNMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "is_assignment": {
            "type": "boolean",
            "description": "True only if this describes a specific school assignment, test, or task.",
        },
        # anyOf rather than a ["string", "null"] type-union array: the
        # structured-outputs spec documents anyOf as supported and does not
        # list type unions, and this code has never made a real call. Same
        # meaning, documented shape.
        "task_name": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "Short name for the assignment, or null if is_assignment is false.",
        },
        "class_name": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "Class or course name if identifiable, otherwise null.",
        },
        "due_date": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "Due date as YYYY-MM-DD, or null if no date is stated or implied.",
        },
    },
    "required": ["is_assignment", "task_name", "class_name", "due_date"],
    "additionalProperties": False,
}


def _gmail_service():
    return googleauth.service("gmail", "v1", GMAIL_SCOPES)


def _seen_label_id(service) -> str | None:
    """
    Find or create the "already looked at" label. Returns None if the
    refresh token doesn't carry gmail.modify, which is a degraded but
    working state, not a failure.
    """
    try:
        labels = service.users().labels().list(userId="me").execute().get("labels", [])
        for label in labels:
            if label["name"] == SEEN_LABEL:
                return label["id"]
        created = (
            service.users()
            .labels()
            .create(
                userId="me",
                body={
                    "name": SEEN_LABEL,
                    # Hidden in the Gmail UI — this is bookkeeping, and
                    # Peter shouldn't have to look at it.
                    "labelListVisibility": "labelHide",
                    "messageListVisibility": "hide",
                },
            )
            .execute()
        )
        return created["id"]
    except HttpError as e:
        if e.resp.status in (401, 403):
            print(
                "[gmail_scan] cannot label messages (needs the gmail.modify "
                "scope). Falling back to a 2-hour window so rejected emails "
                "aren't re-classified all day. Re-run the OAuth consent flow "
                "from the README to fix this properly.",
                file=sys.stderr,
            )
            return None
        raise


def _candidate_query(has_label: bool) -> str:
    """
    Cheap pre-filter before spending a Claude call on anything. Domain
    and keyword filters are ANDed: a candidate must come from a school
    domain AND look assignment-shaped.
    """
    hints = [h.strip() for h in config.optional("SCHOOL_EMAIL_HINTS").split(",") if h.strip()]
    keywords = (
        "subject:assignment OR subject:due OR subject:homework OR "
        "subject:project OR subject:essay OR subject:quiz OR subject:test"
    )
    parts = [f"({keywords})", LOOKBACK_WITH_LABEL if has_label else LOOKBACK_WITHOUT_LABEL]
    if hints:
        parts.insert(0, "(" + " OR ".join(f"from:{h}" for h in hints) + ")")
    if has_label:
        parts.append(f'-label:"{SEEN_LABEL}"')
    return " ".join(parts)


def _mark_seen(service, message_id: str, label_id: str | None):
    if not label_id:
        return
    try:
        service.users().messages().modify(
            userId="me", id=message_id, body={"addLabelIds": [label_id]}
        ).execute()
    except HttpError as e:
        # Worst case this message gets classified again next run — a few
        # cents, not a broken sync.
        print(f"[gmail_scan] could not label {message_id}: {e}", file=sys.stderr)


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
    label_id = _seen_label_id(service)

    results = (
        service.users()
        .messages()
        .list(userId="me", q=_candidate_query(bool(label_id)), maxResults=MAX_MESSAGES_PER_RUN)
        .execute()
    )
    messages = results.get("messages", [])

    category_options = notion_client.select_option_names(notion_client.CATEGORY_PROP)
    task_type_options = notion_client.select_option_names(notion_client.TASK_TYPE_PROP)
    priority_options = notion_client.select_option_names(notion_client.PRIORITY_PROP)
    added = skipped = classified = 0

    for msg_ref in messages:
        external_id = f"gmail:{msg_ref['id']}"
        if external_id in known_ids:
            skipped += 1
            continue
        if classified >= MAX_CLASSIFICATIONS_PER_RUN:
            print(
                f"[gmail_scan] hit the {MAX_CLASSIFICATIONS_PER_RUN}-classification cap; "
                "the rest will be looked at next run",
                file=sys.stderr,
            )
            break

        msg = (
            service.users()
            .messages()
            .get(userId="me", id=msg_ref["id"], format="metadata", metadataHeaders=["Subject"])
            .execute()
        )
        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        parsed = _extract_assignment(client, headers.get("Subject", ""), msg.get("snippet", ""))
        classified += 1

        # Mark it seen whatever the verdict — a "no" is exactly the
        # answer we must not pay to recompute.
        _mark_seen(service, msg_ref["id"], label_id)

        if not parsed:
            continue

        notion_client.create_item(
            name=parsed["task_name"],
            # Claude can invent a class name that isn't one of Peter's
            # Notion options, and Notion would happily create it. Resolve
            # against the real options or leave it blank.
            category=classmap.resolve(parsed.get("class_name"), category_options),
            due_date=parsed.get("due_date"),
            source="Email",
            external_id=external_id,
            task_type=tasktype.resolve(
                parsed["task_name"], "Assignments", task_type_options
            ),
            priority=tasktype.priority(parsed["task_name"], priority_options),
        )
        known_ids.add(external_id)  # guard against duplicates within this run
        added += 1

    if added or skipped or classified:
        print(
            f"[gmail_scan] classified {classified}, added {added} item(s), "
            f"skipped {skipped} already captured"
        )


if __name__ == "__main__":
    config.load_dotenv()
    run()
