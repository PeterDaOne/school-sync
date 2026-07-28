"""
One place that builds Google API clients.

calendar_client, gmail_scan, and classroom_scan each constructed the
same Credentials object from the same three env vars, differing only in
the scope list — and each needed to remember the OAUTHLIB_RELAX_TOKEN_SCOPE
quirk below. Three copies of a subtlety is three chances to forget it.

THE SCOPE QUIRK
---------------
Google's token endpoint sometimes returns a *different* scope string
than the one requested — asking for `classroom.coursework.me.readonly`
comes back granted as `classroom.student-submissions.me.readonly`.
That's real consolidation on Google's side (their own docs give the two
identical descriptions), not an error. But oauthlib compares the strings
and raises on the mismatch, taking down a sync that would otherwise
work fine. OAUTHLIB_RELAX_TOKEN_SCOPE tells it not to.

It must be set before the first token refresh, which is why it happens
at import rather than inside a function.
"""

import os

os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

from google.oauth2.credentials import Credentials  # noqa: E402
from googleapiclient.discovery import build  # noqa: E402

from . import config  # noqa: E402

TOKEN_URI = "https://oauth2.googleapis.com/token"


def credentials(scopes: list[str]) -> Credentials:
    """
    Build credentials from the stored refresh token.

    Note `scopes` describes what we *want*, not what was granted — the
    grant lives in the refresh token itself. Listing a scope here that
    Peter never consented to does not grant it; the API call using it
    just fails with a 403, which each caller handles.
    """
    return Credentials(
        token=None,
        refresh_token=config.require("GOOGLE_REFRESH_TOKEN"),
        client_id=config.require("GOOGLE_CLIENT_ID"),
        client_secret=config.require("GOOGLE_CLIENT_SECRET"),
        token_uri=TOKEN_URI,
        scopes=scopes,
    )


def service(api: str, version: str, scopes: list[str]):
    """Build a Google API client. cache_discovery=False keeps it quiet on
    read-only filesystems like GitHub's runners."""
    return build(api, version, credentials=credentials(scopes), cache_discovery=False)
