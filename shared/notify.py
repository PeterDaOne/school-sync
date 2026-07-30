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

import sys

import requests

from . import config

TIMEOUT_SECONDS = 10


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
