"""
Phone push notification, via ntfy.sh.

Unlike the old macOS-only osascript approach this is a plain HTTPS POST,
so it works identically from local_sync.py on the Mac and from
cloud_sync.py on GitHub's runners — delivery no longer depends on the
Mac being awake or unlocked.

notify() returns True only if ntfy actually accepted the push. That
return value matters: the caller must not stamp Last Reminded unless
the send succeeded. Stamping after a failed send silently consumes the
reminder slot and the reminder is never retried — the notification is
simply lost, which is the worst possible failure mode for this system.
"""

import json
import sys

import requests

from . import config

TIMEOUT_SECONDS = 10

# Where a failed cloud run reports itself. Derived from the command topic
# rather than being its own secret, so it needs no setup and works on
# every deployment that already has the Mark-done button configured. ntfy
# topics are created on demand.
#
# It is NOT the command topic itself: commands.poll_mark_done reads that
# one and prints "ignoring malformed mark-done payload" for anything that
# is not a UUID, so error text posted there would produce a fresh line of
# noise in sync-error.log on every pass inside the poll window.
OPS_TOPIC_SUFFIX = "-ops"

# Error text is truncated before publishing. A traceback tail is not
# useful on a notification service and the point is to know THAT and
# roughly WHY, then read the real log.
MAX_FAILURE_CHARS = 600


def _endpoint() -> str:
    server = config.optional("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    return f"{server}/{config.require('NTFY_TOPIC')}"


def notify(
    title: str,
    message: str,
    click_url: str | None = None,
    priority: int | None = None,
    tags: str | None = None,
    actions: str | None = None,
) -> bool:
    """
    Push a notification. Returns True on success, False on failure.

    Never raises: one failed push should not abort a sync pass that may
    still have other items to process. The failure is printed to stderr
    so it lands in sync-error.log rather than vanishing.

    `priority` is ntfy's 1-5 urgency scale; `tags` is ntfy's own
    comma-separated tag list — note that a tag matching an emoji short
    code is rendered as an emoji and prepended to the title, so tags are
    part of how the notification LOOKS, not just metadata ("school" is
    🏫, "rotating_light" is 🚨); `actions` is a pre-built ntfy `Actions:`
    header value (see build_mark_done_action) — this function stays
    ignorant of what the action does, it just passes the header through.
    """
    # Title can carry a class emoji (non-ASCII). http.client's default
    # header encoding is latin-1 for `str` values and raises on anything
    # outside it -- passing pre-encoded UTF-8 bytes instead skips that
    # encode step entirely (bytes headers pass through as-is). Confirmed
    # against a real ntfy send: the emoji renders correctly on the phone.
    headers = {"Title": title.encode("utf-8")}
    if tags:
        # Omitted entirely when empty. ntfy turns any tag matching an
        # emoji short code into an emoji and prepends it to the title, so
        # a default tag is not free decoration -- the old default of
        # "school" put a 🏫 in front of every notification Peter got.
        headers["Tags"] = tags
    if click_url:
        # Tapping the notification opens this URL — a notion.so page URL
        # is a registered universal link, so this hands off to the Notion
        # app on the phone if it's installed.
        headers["Click"] = click_url
    if priority is not None:
        headers["Priority"] = str(priority)
    if actions:
        headers["Actions"] = actions

    try:
        resp = requests.post(
            _endpoint(),
            data=message.encode("utf-8"),
            headers=headers,
            timeout=TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"[notify] ntfy push failed, will retry next pass: {e}", file=sys.stderr)
        return False
    except RuntimeError as e:
        # Missing NTFY_TOPIC — config.require's message explains the fix.
        print(f"[notify] {e}", file=sys.stderr)
        return False


def ops_topic() -> str | None:
    """
    The topic failed cloud runs report to, or None if unconfigured.

    Tried in order: NTFY_ERROR_TOPIC, then NTFY_COMMAND_TOPIC, then
    NTFY_TOPIC — each with the "-ops" suffix, so none of them is ever
    the topic itself. Deriving rather than requiring a new secret is the
    point: the failure this exists to surface is one nobody can see, so
    a reporter that needs setup before it works is a reporter that isn't
    working when you need it.

    THE NTFY_TOPIC FALLBACK IS NOT BELT-AND-BRACES, IT IS THE ONE THAT
    ACTUALLY FIRES. NTFY_COMMAND_TOPIC was deliberately never added as a
    GitHub secret (the Mark-done button is Mac-only by choice), so in the
    cloud — the ONLY place cloud_sync runs — this returned None and
    publish_failure was a silent no-op. It shipped 2026-08-05 and sat
    inert through 36 failed runs the next day, which is exactly the
    class of bug it was written to catch. NTFY_TOPIC is required in the
    cloud (the workflow fails fast without it), so deriving from it means
    the reporter cannot be silently unconfigured.

    Note the suffix keeps this off NTFY_TOPIC itself, which Peter's phone
    IS subscribed to — error text must never reach it.
    """
    explicit = config.optional("NTFY_ERROR_TOPIC")
    if explicit:
        return explicit
    for name in ("NTFY_COMMAND_TOPIC", "NTFY_TOPIC"):
        base = config.optional(name)
        if base:
            return f"{base}{OPS_TOPIC_SUFFIX}"
    return None


def _already_reported(url: str, window: str) -> bool:
    """
    True if something was already published to the ops topic inside
    `window` — a stateless rate limit.

    Stateless because GitHub's runners are ephemeral and share nothing:
    the same reason commands.py polls a rolling window instead of keeping
    a cursor. On any error checking, returns False and lets the report
    through: a duplicate report is a far smaller problem than the silence
    this whole function exists to break.
    """
    try:
        resp = requests.get(f"{url}/json?poll=1&since={window}", timeout=TIMEOUT_SECONDS)
        resp.raise_for_status()
    except requests.RequestException:
        return False
    for line in resp.text.strip().splitlines():
        if not line.strip():
            continue
        try:
            if json.loads(line).get("event") == "message":
                return True
        except ValueError:
            continue
    return False


def publish_failure(summary: str, window: str = "1h") -> bool:
    """
    Publish why a cloud run went red, somewhere readable WITHOUT a GitHub
    login. Returns True if something was published.

    WHY THIS EXISTS
    ---------------
    GitHub job logs need admin auth: the REST API returns 403 for an
    anonymous request even on a public repo, and an anonymous browser
    renders the step list but never loads the log lines. So when
    `Run cloud sync` fails, the reason is visible to nobody — not to a
    future session, and not to Peter unless he is signed in on a laptop.
    Seven runs failed between 2026-08-03 and 2026-08-05 and the cause was
    unknowable from outside.

    This is the project's own "make the bad case read differently" rule
    applied to the one surface that had no reader at all.

    Rate-limited to one report per `window`, so a persistent fault at a
    two-minute cadence does not publish 30 times an hour. Never raises:
    the reporting of a failure must not itself become a failure.
    """
    topic = ops_topic()
    if not topic:
        return False
    server = config.optional("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    url = f"{server}/{topic}"

    if _already_reported(url, window):
        return False

    # Exception text is not automatically safe to publish: notion_client
    # deliberately surfaces Notion's 4xx response bodies, and Google's
    # errors quote request URLs. The topic is unauthenticated.
    body = config.redact(summary)[:MAX_FAILURE_CHARS]
    try:
        resp = requests.post(
            url,
            data=body.encode("utf-8"),
            headers={
                "Title": "school-sync cloud run failed".encode("utf-8"),
                # Min priority: nothing should be subscribed to this
                # topic, but if Peter ever subscribes it must not buzz.
                "Priority": "1",
            },
            timeout=TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"[notify] could not publish failure report: {e}", file=sys.stderr)
        return False


def build_mark_done_action(page_id: str) -> str | None:
    """
    An ntfy `Actions:` header value for a "Mark done" button, or None if
    NTFY_COMMAND_TOPIC isn't configured — the button is simply absent
    until Peter sets it up, not broken. See shared/commands.py for what
    receives this.

    The action's URL is ntfy's own second topic, not Notion — this is
    deliberate, see commands.py's module docstring. The body is a bare
    page id (a UUID: no commas or semicolons, so it needs no escaping
    inside ntfy's own comma-delimited action syntax). `clear=true` lets
    the phone dismiss the notification as soon as ntfy accepts the
    publish, which is near-instant — independent of, and faster than,
    the up-to-several-minutes it takes the next sync pass to actually
    apply the Done in Notion.
    """
    topic = config.optional("NTFY_COMMAND_TOPIC")
    if not topic:
        return None
    server = config.optional("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    return f"http, Mark done, {server}/{topic}, method=POST, body={page_id}, clear=true"
