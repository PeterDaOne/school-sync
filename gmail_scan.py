#!/usr/bin/env python3
"""
gmail_scan.py — the email capture layer. Cloud-only (runs inside
cloud_sync.py via GitHub Actions).

WHAT IT CAPTURES (widened 2026-07-31, Peter's call)
---------------------------------------------------
Originally school assignments only. Now anything a real person -- or his
school -- is asking him to personally do: homework, a chore from a
family member, a form to return, a meeting to attend. Claude also
classifies it into Notion's `Type` (Assignments / Tasks / Events).

That type is not cosmetic: it selects the reminder cadence in
shared/reminders.py. Assignments nag early and often (alpha=3.4, 48h
ceiling); Tasks stay quiet until the due date is close (alpha=24, 72h);
Events get fixed-point reminders at 3 days, 1 day, morning-of, and an
hour before. A chore filed as an Assignment would nag far harder than it
deserves, which is why the classifier is asked for the type rather than
everything defaulting to Assignments.

The hard part is not the output shape but telling a real request from a
corporate call to action -- "sale ends Friday" is a deadline, not an
obligation. That judgement lives in CLASSIFIER_SYSTEM, with examples,
and the sender address is passed to the model because it carries most of
the signal (a person's address reads very differently from noreply@).

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

from shared import classmap, config, googleauth, notion_client, tasktype, timeutil

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

# The Notion `Type` options. Kept as a constant because it is also the
# schema enum below: structured outputs then make an invalid value
# impossible rather than merely unlikely, which matters because Notion
# silently CREATES any select option it is handed.
ITEM_TYPES = ["Assignments", "Tasks", "Events"]

# Fallback when the model somehow returns a type outside ITEM_TYPES, or
# none at all. "Tasks" rather than "Assignments" on purpose: the Tasks
# cadence (alpha=24, 72h ceiling) stays quiet until the due date is
# close, so a mis-typed item under-nags rather than over-nags. See
# shared/reminders.py.
DEFAULT_ITEM_TYPE = "Tasks"

# Structured outputs guarantee the response parses — no markdown fences
# to strip, no malformed JSON to catch.
ASSIGNMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "is_actionable": {
            "type": "boolean",
            "description": (
                "True only if a real person or Peter's school is asking Peter to "
                "personally do something. False for marketing, promotions, "
                "newsletters, receipts, and automated notifications."
            ),
        },
        # anyOf rather than a ["string", "null"] type-union array: the
        # structured-outputs spec documents anyOf as supported and does not
        # list type unions. Confirmed working on a live call 2026-07-31.
        "item_type": {
            "anyOf": [{"type": "string", "enum": ITEM_TYPES}, {"type": "null"}],
            "description": (
                "Assignments = schoolwork to produce or study for. "
                "Tasks = anything else to do (chores, errands, forms, replies). "
                "Events = something to show up to at a particular time. "
                "Null if is_actionable is false."
            ),
        },
        "task_name": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "Short imperative name, or null if is_actionable is false.",
        },
        "class_name": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "School class or course name if identifiable, otherwise null.",
        },
        "due_date": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": (
                "Due date as YYYY-MM-DD, resolved against the current date given "
                "in the message. Resolve relative dates like 'Thursday', 'next "
                "Friday' or 'tomorrow'. Null only if no date is stated or implied."
            ),
        },
    },
    "required": ["is_actionable", "item_type", "task_name", "class_name", "due_date"],
    "additionalProperties": False,
}

# The whole judgement lives here rather than in the schema descriptions,
# because the hard part isn't the output shape -- it's the corporate-CTA
# vs real-request distinction, which needs examples to land.
CLASSIFIER_SYSTEM = """\
You triage one email from a high school student's inbox and decide \
whether it describes something he personally has to DO.

CAPTURE when a real person is asking him to do something, or when his \
school is telling him work is owed. Examples: homework, an essay, a \
test to study for, a chore from a family member, a form to return, a \
shift someone asked him to cover, a meeting or rehearsal to attend.

DO NOT CAPTURE marketing, promotions, newsletters, receipts, shipping \
notices, social media notifications, or security alerts. Urgency in an \
advertisement is not a task: "sale ends Friday", "your trial expires \
tomorrow", "last chance to register", "act now" are all marketing \
deadlines, not obligations. The question is whether a specific human, \
or his school, is asking HIM for something. A company addressing all \
its customers at once is not.

If the sender is automated or promotional, answer false even when the \
subject contains words like assignment, project, due, or test.

When capturing, classify item_type:
  Assignments - schoolwork he must produce or study for
  Tasks       - anything else he must do: chores, errands, forms, replies
  Events      - something he must show up to at a particular time

Set class_name ONLY for a school class or course. Leave it null for \
chores, errands, and anything not tied to a course.\
"""


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


def _classify_email(client, subject: str, sender: str, snippet: str) -> dict | None:
    """
    One lightweight Claude call: is this something Peter has to do, and
    if so, what kind of thing is it?

    The sender is included because it carries most of the signal for the
    distinction that matters -- a person's address reads very differently
    from noreply@ or marketing@, and the subject line alone often can't
    tell an ad's deadline from a real one.
    """
    # Today's date has to be in the prompt: the model has no clock, and
    # real mail overwhelmingly uses relative dates ("Thursday at 6pm",
    # "next Friday", "by tomorrow"). Without it those resolved to null,
    # and an item with no due date gets NO reminders at all if it's an
    # Event -- so the capture looked successful and then silently never
    # fired. In the user turn rather than the system prompt so the
    # system prompt stays byte-stable and cacheable.
    today = timeutil.now()
    prompt = (
        f"Today is {today:%A, %B %-d, %Y}.\n\n"
        f"From: {sender}\nSubject: {subject}\nSnippet: {snippet}"
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=CLASSIFIER_SYSTEM,
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
    if not data.get("is_actionable") or not data.get("task_name"):
        return None
    return data


def _item_type(parsed: dict, options: list[str]) -> str:
    """
    Claude's item_type, validated against the live Notion `Type` options.

    The schema enum already constrains this, so a bad value should be
    impossible -- but Notion silently creates any select option it is
    handed, and that failure is invisible until his views are wrong, so
    it is checked anyway.
    """
    chosen = parsed.get("item_type")
    if chosen in options:
        return chosen
    return DEFAULT_ITEM_TYPE if DEFAULT_ITEM_TYPE in options else options[0]


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
    type_options = notion_client.select_option_names("Type")
    added = skipped = classified = 0
    failures: list[str] = []

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

        # Per item, per the project's error policy (see shared/pipeline.py
        # and classroom_scan.run): one message that fails must not stop
        # the rest of the sweep. Re-raised at the end so the run still
        # goes red.
        try:
            msg = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=msg_ref["id"],
                    format="metadata",
                    metadataHeaders=["Subject", "From"],
                )
                .execute()
            )
            headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
            parsed = _classify_email(
                client,
                headers.get("Subject", ""),
                headers.get("From", ""),
                msg.get("snippet", ""),
            )
            classified += 1

            if not parsed:
                # A REJECTION is labelled immediately. Nothing was
                # created, so there is nothing to lose -- and not paying
                # to recompute a "no" is the entire reason the label
                # exists.
                _mark_seen(service, msg_ref["id"], label_id)
                continue

            item_type = _item_type(parsed, type_options)
            notion_client.create_item(
                name=parsed["task_name"],
                # Claude can invent a class name that isn't one of Peter's
                # Notion options, and Notion would happily create it. Resolve
                # against the real options or leave it blank.
                category=classmap.resolve(parsed.get("class_name"), category_options),
                due_date=parsed.get("due_date"),
                source="Email",
                type_name=item_type,
                external_id=external_id,
                # item_type, not a hardcoded "Assignments": it decides the
                # verb (Events -> Attend) and, more importantly, the whole
                # reminder cadence.
                task_type=tasktype.resolve(
                    parsed["task_name"], item_type, task_type_options
                ),
                priority=tasktype.priority(parsed["task_name"], priority_options),
            )
            known_ids.add(external_id)  # guard against duplicates within this run
            added += 1

            # An ACCEPTANCE is labelled only after the item exists.
            # Labelling before the create meant a create that threw --
            # a malformed due_date from Claude gets a 400 from Notion --
            # left the message labelled `school-sync/seen`, so the next
            # run's `-label:` clause excluded it forever while no Notion
            # item existed. The assignment was silently lost. Now a
            # failed create costs one extra classification next run
            # instead of the item.
            _mark_seen(service, msg_ref["id"], label_id)
        except Exception as e:
            failures.append(f"{msg_ref['id']}: {e}")
            print(f"[gmail_scan] could not process {msg_ref['id']}: {e}", file=sys.stderr)
            continue

    if added or skipped or classified:
        print(
            f"[gmail_scan] classified {classified}, added {added} item(s), "
            f"skipped {skipped} already captured"
        )

    # Raised only after every other message has had its chance, so
    # cloud_sync's _run_phase turns this into a red run rather than a
    # silent partial success.
    if failures:
        raise RuntimeError(
            f"{len(failures)} message(s) could not be processed: " + "; ".join(failures)
        )


if __name__ == "__main__":
    config.load_dotenv()
    run()
