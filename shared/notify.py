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


def notify(title: str, message: str, click_url: str | None = None) -> bool:
    """
    Push a notification. Returns True on success, False on failure.

    Never raises: one failed push should not abort a sync pass that may
    still have other items to process. The failure is printed to stderr
    so it lands in sync-error.log rather than vanishing.
    """
    headers = {"Title": title, "Tags": "school"}
    if click_url:
        # Tapping the notification opens this URL — a notion.so page URL
        # is a registered universal link, so this hands off to the Notion
        # app on the phone if it's installed.
        headers["Click"] = click_url

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
