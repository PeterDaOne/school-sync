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

import base64
import json
import sys
from urllib.parse import quote

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

# VERSIONED ON PURPOSE — bump the suffix whenever the capture POLICY
# changes (CLASSIFIER_SYSTEM, the schema, or what counts as a candidate).
#
# The label is permanent but the policy is not, and that combination
# silently lost a real item on 2026-07-31. The scope was widened from
# schoolwork-only to "anything a real person asks Peter to do" at
# 22:17Z. A chore email ("mow the grass") had been classified and
# correctly rejected at ~22:10 under the OLD narrow policy — and because
# rejections are labelled, the `-label:` clause excluded it from every
# subsequent run forever. It could never be reconsidered under the rules
# that would have captured it.
#
# Bumping the version leaves the old label in place but stops excluding
# it, so every previously-rejected message gets exactly one fresh look
# under the new policy. Cost is bounded by the window and by
# MAX_CLASSIFICATIONS_PER_RUN; the alternative is silent permanent loss.
# v3 (2026-08-01): multi-item extraction. Every message seen under the
# one-item-per-email policy deserves a fresh look, because any of them
# could have carried a second assignment that was merged away or dropped.
# Cheap in practice -- a message whose items are already captured is
# skipped by External ID before any Claude call (see _item_external_id
# for why the first item keeps the bare `gmail:<id>` form).
SEEN_LABEL = "school-sync/seen-v3"

# Machine-generated bulk mail, excluded before Claude is ever called.
# Gmail's own tab classifier is free and is very good at precisely the
# distinction the model finds hardest — a marketing deadline versus a
# real obligation.
#
# `updates` is deliberately NOT in this list even though it is where most
# of the volume lives (excluding it took Peter's 7-day mailbox from 38
# candidates to 11). Automated-but-real school notifications land in
# Updates, and missing those is exactly the failure this filter is being
# rewritten to fix. Paying for a few more classifications is much cheaper
# than dropping one real assignment.
EXCLUDED_CATEGORIES = ("promotions", "social", "forums")

MODEL = "claude-sonnet-5"
MAX_TOKENS = 1000
MAX_MESSAGES_PER_RUN = 20

# How much of an email body to send. Long threads are mostly quoted
# history and the ask is near the top, so this bounds cost without
# losing much -- and the previous behaviour was ~200 characters of
# Gmail snippet, so this is already a large improvement.
MAX_BODY_CHARS = 4000

# Hard ceiling on Claude calls per run, independent of the label logic.
# If dedup ever breaks again, this caps the damage at a knowable number
# instead of letting it scale with the cron frequency.
MAX_CLASSIFICATIONS_PER_RUN = 10

# With labelling, a wide window is nearly free — a message is only ever
# looked at once, so in steady state everything inside the window is
# already labelled and the query returns almost nothing. Without it,
# every run re-examines everything in the window, so the window itself is
# the only cost control left.
#
# Widened from 1d to 7d on 2026-07-31. At 1d, any cloud outage longer
# than a day lost that day's mail PERMANENTLY: the messages simply fell
# out of scope, and the seen-label doesn't help with something that was
# never a candidate in the first place. A week of slack costs nothing in
# steady state and turns a permanent loss into a delayed capture.
LOOKBACK_WITH_LABEL = "newer_than:7d"
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

# Hard ceiling on rows one email may create. A pathological digest must
# not turn into fifty Notion pages and fifty phone notifications.
MAX_ITEMS_PER_EMAIL = 10

# ONE EMAIL CAN CARRY SEVERAL THINGS TO DO, so this extracts a LIST.
#
# It used to be a single object with one task_name, which silently lost
# data in two different ways depending on the model's mood (measured live
# 2026-08-01 on three realistic multi-item emails):
#
#   MERGE  "Complete rhetorical analysis essay draft; read Gatsby ch.5-7;
#          study for vocab quiz" as ONE item with ONE due date -- so two
#          of the three nagged from the wrong date, the whole thing went
#          overdue while most of it wasn't, and finishing the essay could
#          not be checked off without marking the reading and quiz done
#          too, since Status is per item.
#   DROP   A physics email listing a problem set, a lab report and a test
#          produced only the problem set. The other two vanished with no
#          trace -- and the message is labelled seen, so they could never
#          be reconsidered.
#
# An EMPTY list is how "nothing here to do" is expressed. That replaced a
# separate is_actionable boolean, which was redundant with it and gave
# the model two ways to say no.
ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        # anyOf rather than a ["string", "null"] type-union array: the
        # structured-outputs spec documents anyOf as supported and does not
        # list type unions. Confirmed working on a live call 2026-07-31.
        "item_type": {
            "type": "string",
            "enum": ITEM_TYPES,
            "description": (
                "Assignments = schoolwork to produce or study for. "
                "Tasks = anything else to do (chores, errands, forms, replies). "
                "Events = something to show up to at a particular time."
            ),
        },
        "task_name": {
            "type": "string",
            "description": (
                "Short imperative name for THIS ONE thing. Do not combine "
                "several tasks into one name -- list them separately instead."
            ),
        },
        "class_name": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "School class or course name if identifiable, otherwise null.",
        },
        # Only consulted when class_name doesn't resolve to a real course.
        # Enum'd to the live non-class options so the model cannot invent
        # a category -- and it is validated again on the way out, because
        # Notion silently creates any select option it is handed.
        "category": {
            "anyOf": [
                {"type": "string", "enum": sorted(classmap.NON_CLASS_CATEGORIES)},
                {"type": "null"},
            ],
            "description": (
                "What this is for, when it is NOT tied to a school course. "
                "School = general school business not tied to one class. "
                "Personal = own life, errands, appointments, family. "
                "Friends = something with or for friends. "
                "Work = a job or shift. Null if class_name is set, or if "
                "none of these clearly fits."
            ),
        },
        "due_date": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": (
                "Due date for THIS ONE item as YYYY-MM-DD, resolved against the "
                "current date given in the message. Resolve relative dates like "
                "'Thursday', 'next Friday' or 'tomorrow'. Items in the same email "
                "usually have DIFFERENT due dates -- give each its own. Null only "
                "if no date is stated or implied."
            ),
        },
    },
    "required": ["item_type", "task_name", "class_name", "category", "due_date"],
    "additionalProperties": False,
}

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        # NO `maxItems` HERE. The API rejects it outright:
        #   400 output_config.format.schema: For 'array' type, property
        #   'maxItems' is not supported
        # (verified live 2026-08-01 -- it was in the first draft and the
        # first real call failed). The cap is enforced in _extract_items
        # instead, which is where it belongs anyway: the things being
        # protected are Notion rows and phone notifications, not tokens.
        "items": {
            "type": "array",
            "items": ITEM_SCHEMA,
            "description": (
                "Every distinct thing Peter personally has to do, one entry each. "
                "Empty if the email asks nothing of him."
            ),
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

# The whole judgement lives here rather than in the schema descriptions,
# because the hard part isn't the output shape -- it's the corporate-CTA
# vs real-request distinction, which needs examples to land.
CLASSIFIER_SYSTEM = """\
You read one email from a high school student's inbox and list \
EVERYTHING he personally has to DO. One email often contains several \
separate things; return one entry per thing, each with its own due date.

Return an EMPTY list if the email asks nothing of him.

LIST SEPARATELY, never combine. An email saying "essay draft due Monday, \
read chapters 5-7 by Wednesday, vocab quiz Friday" is THREE entries with \
three different due dates -- not one entry named "essay, reading and \
quiz". He checks these off one at a time, and a combined entry cannot be \
half-finished.

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

If the sender is automated or promotional, return an empty list even \
when the subject contains words like assignment, project, due, or test.

For each entry, classify item_type. Peter's own definitions:

  Assignments - anything a TEACHER ASSIGNS FOR A CLASS. Graded or not: \
an ungraded reading or practice set a teacher assigned is still an \
Assignment, because it is schoolwork with a deadline.
  Events      - something that HAPPENS AT A SET TIME and he has to be \
there: a rehearsal, an appointment, a trip, a dance, a game.
  Tasks       - everything else he has to do: chores, errands, forms, \
replies, personal admin. Not assigned by a teacher and not for a grade.

SOME THINGS ARE BOTH, AND YOU MUST SPLIT THEM INTO TWO ENTRIES
--------------------------------------------------------------
When a teacher assigns work that he then DELIVERS AT A SCHEDULED TIME, \
the preparing and the delivering are two different things and he tracks \
them separately. Emit BOTH:

  1. an Assignments entry for the work he has to prepare, and
  2. an Events entry for the occasion itself.

"You're presenting your Gatsby project to the class on Tuesday" is:
  - Assignments  "Prepare Gatsby class presentation"   due Tuesday
  - Events       "Present Gatsby project to the class" due Tuesday

Split like this for presentations, speeches, recitals, performances, \
oral exams, debates, science-fair boards, and sit-down exams (studying \
is the Assignment, sitting it is the Event). Give BOTH entries the same \
date unless the email states a separate prep deadline.

DO NOT split ordinary submitted work. An essay, a problem set, a lab \
report or a reading is ONE Assignments entry -- handing something in is \
not an occasion. The test is whether there is a specific moment he has \
to BE somewhere and do it live. If there isn't, do not invent an Event.

For EACH entry, say what it is FOR, using exactly one of two fields:

  class_name - ONLY for a school class or course ("AP Lang", "Physics"). \
Leave null for anything not tied to a course.
  category   - ONLY when class_name is null. One of:
                 School   - school business not tied to one class \
(forms, fees, picture day, counselor meetings)
                 Personal - his own life: chores, errands, appointments, \
family, hobbies
                 Friends  - something with or for friends
                 Work     - a job or a shift

Never set both. If it is schoolwork, set class_name and leave category \
null. If neither clearly fits, leave both null -- a blank field is easy \
to fix and a wrong one is not.\
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
    Cheap pre-filter before spending a Claude call on anything.

    NO SUBJECT-KEYWORD WHITELIST — that was the bug, not the design
    -------------------------------------------------------------
    This used to require a subject matching assignment/due/homework/
    project/essay/quiz/test. That made sense when the only thing being
    captured was schoolwork. When the scope was widened on 2026-07-31 to
    "anything a real person asks Peter to do", the whitelist was left
    behind, and it silently became the thing that decided what could be
    captured at all.

    Measured against real mail that same day: of six genuine messages
    (a chore, a birthday, a jiu jitsu tournament, two homework mails and
    an essay), the domain filter matched all six and the keyword filter
    matched TWO. "Birthday" and "Jiu jitsu tournament" contain no
    schoolwork keyword and never reached the classifier at all — Peter
    reported them as capture failures, and they were, one layer earlier
    than anyone was looking.

    The deeper problem is that the whitelist is unfixable in kind: there
    is no finite list of words that covers "things a human might ask you
    to do". Widening it is whack-a-mole.

    So the positive filter is gone, replaced by a NEGATIVE one on
    machine-generated bulk mail (see EXCLUDED_CATEGORIES). Gmail already
    classifies that better than a keyword list ever could, for free. The
    remaining cost control is the seen-label (one classification per
    message, ever) plus MAX_CLASSIFICATIONS_PER_RUN.
    """
    hints = [h.strip() for h in config.optional("SCHOOL_EMAIL_HINTS").split(",") if h.strip()]
    parts = [LOOKBACK_WITH_LABEL if has_label else LOOKBACK_WITHOUT_LABEL]
    parts += [f"-category:{c}" for c in EXCLUDED_CATEGORIES]
    if hints:
        parts.insert(0, "(" + " OR ".join(f"from:{h}" for h in hints) + ")")
    if has_label:
        parts.append(f'-label:"{SEEN_LABEL}"')
    return " ".join(parts)


def _mailbox_address(service) -> str | None:
    """
    The address of the mailbox being swept, used to build Source Links.

    Gmail deep links need an account selector. The familiar form is
    `/mail/u/0/`, but `u/0` means "the FIRST Google account signed in to
    this browser", NOT "the account this message lives in". That is fine
    today on one account and quietly wrong the moment Peter is signed
    into both his school and personal accounts -- the link would open the
    wrong mailbox and show nothing, which looks like a broken link rather
    than a wrong one. `?authuser=<address>` names the account explicitly
    and does not care about sign-in order.

    One extra API call per run. Returns None on failure: a link is a
    convenience, and losing it must never cost us a capture.
    """
    try:
        return service.users().getProfile(userId="me").execute().get("emailAddress")
    except Exception as e:
        print(f"[gmail_scan] could not read mailbox address: {e}", file=sys.stderr)
        return None


def _message_link(message_id: str, address: str | None) -> str:
    """Deep link to one message. `#all/` finds it whether or not it's archived."""
    if address:
        # `@` left literal (it is legal in a query string, and it is the
        # form Google's own UI emits); everything else percent-encoded.
        return f"https://mail.google.com/mail/?authuser={quote(address, safe='@')}#all/{message_id}"
    return f"https://mail.google.com/mail/u/0/#all/{message_id}"


def _item_external_id(message_id: str, index: int) -> str:
    """
    Dedup key for one item extracted from one email.

    The FIRST item keeps the bare `gmail:<id>` form, deliberately: that
    is what every row captured before multi-item extraction carries, and
    changing it would make all of them look uncaptured. Later items get
    `gmail:<id>#2`, `#3`, ... so each row still dedups independently.

    That backward compatibility is load-bearing rather than tidy. The
    seen-label had to be bumped for this policy change, which re-opens
    every previously-seen message for one fresh look -- so without the
    bare first ID, every already-captured email would have been captured
    a second time the moment this shipped.
    """
    return f"gmail:{message_id}" if index == 0 else f"gmail:{message_id}#{index + 1}"


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


def _message_text(msg: dict) -> str:
    """
    The readable body of a Gmail message, decoded and length-capped.

    Falls back to the snippet, which is what this used to send EXCLUSIVELY
    -- the fetch asked for format="metadata", so the model only ever saw
    Gmail's ~200-character preview. Peter's own test mails are shorter
    than that, so it looked fine; a teacher's five-assignment digest would
    have had everything past the first sentence or two simply invisible,
    which made the multi-item loss above much likelier in real use than in
    testing.

    text/plain is preferred over text/html because stripping tags well is
    its own problem, and virtually every mail client sends both.
    """
    def walk(part):
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")
        if data and mime == "text/plain":
            yield base64.urlsafe_b64decode(data).decode("utf-8", "replace")
        for sub in part.get("parts", []) or []:
            yield from walk(sub)

    payload = msg.get("payload", {}) or {}
    text = "\n".join(walk(payload)).strip()
    if not text:
        # Either an HTML-only mail or an unexpected structure. The snippet
        # is worse but never wrong, so degrade rather than skip the mail.
        text = msg.get("snippet", "") or ""
    # Long threads are mostly quoted history; the ask is near the top, and
    # sending 50KB per message would cost real money for no accuracy.
    return text[:MAX_BODY_CHARS]


def _extract_items(client, subject: str, sender: str, body: str) -> list[dict]:
    """
    One Claude call: everything in this email that Peter has to do.

    Returns a LIST, empty when the email asks nothing of him. It used to
    return one optional item, which silently merged or dropped the extras
    of any multi-item email -- see EXTRACTION_SCHEMA.

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
        f"From: {sender}\nSubject: {subject}\n\n{body}"
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=CLASSIFIER_SYSTEM,
        output_config={"format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    if resp.stop_reason == "refusal":
        print(f"[gmail_scan] classification refused for {subject!r}", file=sys.stderr)
        return []

    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text:
        return []
    data = json.loads(text)  # schema-constrained, so this is safe
    items = data.get("items") or []
    # maxItems is in the schema, but Notion rows and phone notifications
    # are the things being protected here, so the cap is enforced where
    # it actually matters rather than trusted upstream.
    kept = [i for i in items if i.get("task_name")][:MAX_ITEMS_PER_EMAIL]
    if len(items) > len(kept):
        print(
            f"[gmail_scan] {subject!r} yielded {len(items)} items; keeping {len(kept)}",
            file=sys.stderr,
        )
    return kept


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
    # Read once per run, not per message — see _mailbox_address.
    mailbox = _mailbox_address(service)

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
        # Whole-message skip, ONLY on the degraded no-label path.
        #
        # When labelling works, the label already excludes a processed
        # message from the query, so this check almost never fires -- and
        # when it does fire it is precisely the case we must NOT skip: a
        # label version bump deliberately re-opens old messages so their
        # extras can be recovered under the new policy. Skipping them on
        # the base External ID would make the bump accomplish nothing,
        # which is the same "a filter outlives the policy that wrote it"
        # mistake the versioned label exists to fix.
        #
        # Without the label there is no query-level dedup at all, every
        # run re-examines the (narrowed) window, and this is the only
        # thing stopping repeat spend. So it stays, conditionally.
        if not label_id and f"gmail:{msg_ref['id']}" in known_ids:
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
            # format="full" (not "metadata") so the model sees the actual
            # email. See _message_text -- metadata gave it Gmail's
            # ~200-character snippet and nothing else.
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=msg_ref["id"], format="full")
                .execute()
            )
            headers = {
                h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])
            }
            items = _extract_items(
                client,
                headers.get("Subject", ""),
                headers.get("From", ""),
                _message_text(msg),
            )
            classified += 1

            if not items:
                # A REJECTION is labelled immediately. Nothing was
                # created, so there is nothing to lose -- and not paying
                # to recompute a "no" is the entire reason the label
                # exists.
                _mark_seen(service, msg_ref["id"], label_id)
                continue

            link = _message_link(msg_ref["id"], mailbox)
            for index, parsed in enumerate(items):
                item_external_id = _item_external_id(msg_ref["id"], index)
                if item_external_id in known_ids:
                    skipped += 1
                    continue
                item_type = _item_type(parsed, type_options)
                notion_client.create_item(
                    name=parsed["task_name"],
                    # A course name goes through the fuzzy matcher, which
                    # can never return a life category; a life category the
                    # model named outright goes through an exact-match-only
                    # check, which can never be reached by a course name.
                    # Both filter against the live Notion options, because
                    # Notion happily CREATES any select option it is handed.
                    # See shared/classmap.py for why the two are separate.
                    category=(
                        classmap.resolve(parsed.get("class_name"), category_options)
                        or classmap.resolve_category(
                            parsed.get("category"), category_options
                        )
                    ),
                    due_date=parsed.get("due_date"),
                    source="Email",
                    type_name=item_type,
                    external_id=item_external_id,
                    # item_type, not a hardcoded "Assignments": it decides
                    # the verb (Events -> Attend) and, more importantly, the
                    # whole reminder cadence.
                    task_type=tasktype.resolve(
                        parsed["task_name"], item_type, task_type_options
                    ),
                    priority=tasktype.priority(parsed["task_name"], priority_options),
                    # One click from the Notion page back to the email this
                    # was inferred from. Worth more here than for Classroom:
                    # Gmail items are parsed out of prose, so checking the
                    # original is how Peter confirms a captured item is
                    # right. Every item from one email shares the link.
                    source_link=link,
                )
                known_ids.add(item_external_id)  # no duplicates within this run
                added += 1

            # An ACCEPTANCE is labelled only after the items exist.
            # Labelling before the create meant a create that threw --
            # a malformed due_date from Claude gets a 400 from Notion --
            # left the message labelled seen, so the next run's `-label:`
            # clause excluded it forever while no Notion item existed. The
            # assignment was silently lost. Now a failed create costs one
            # extra classification next run instead of the item.
            #
            # Reaching this line means every item in the loop above either
            # was created or was already known: the loop has no inner
            # try, so a create that throws jumps straight to the outer
            # handler and never labels. That is what makes partial failure
            # recover correctly -- the message stays unlabelled and is
            # retried, and whichever items did land are skipped next time
            # by their own External IDs.
            _mark_seen(service, msg_ref["id"], label_id)
        except Exception as e:
            failures.append(f"{msg_ref['id']}: {e}")
            print(f"[gmail_scan] could not process {msg_ref['id']}: {e}", file=sys.stderr)
            continue

    if added or skipped or classified:
        print(
            f"[gmail_scan] classified {classified} message(s), added {added} item(s), "
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
