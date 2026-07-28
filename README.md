# School Sync

One input, many outputs. You type or check things off in Notion.
Calendar, phone reminders, and an unconfirmed-email inbox stay current
on their own.

**Notion database:** https://app.notion.com/p/6524b82ad7dd499896f4c55a86de9290
Database ID (for `.env`): `6524b82ad7dd499896f4c55a86de9290`

---

## ⚠️ Read this before your first `git push`

This repo is meant to be **public** (public repos get unlimited free
GitHub Actions minutes). Everything not covered by `.gitignore` becomes
world-readable the moment you push.

Three files hold live credentials and are gitignored:

| File | Holds |
|---|---|
| `.env` | Notion token, Google client secret + refresh token, ntfy topic |
| `client_secret.json` | Google OAuth client ID + secret |
| `~/Library/LaunchAgents/com.peter.schoolsync.plist` | all of the above, baked into XML |

The plist lives outside the repo on purpose — `generate_plist.py`
writes it straight to `~/Library/LaunchAgents/`. Check before pushing:

```bash
git status --porcelain && git ls-files | grep -E '\.env$|client_secret|\.plist$' || echo "clean — no secrets tracked"
```

If a secret ever does get committed, deleting it later is not enough —
the old commit stays on GitHub. **Rotate the credential** (new Notion
token, new Google client secret, new ntfy topic).

---

## What runs where

| Script | Runs on | Frequency | Does |
|---|---|---|---|
| `local_sync.py` | Your Mac, via `launchd` | Every 60 sec, only while awake | Notion → Calendar, fires reminders |
| `cloud_sync.py` | GitHub Actions | Every 30 min, always | Gmail + Classroom capture, Notion → Calendar, fires reminders the Mac slept through |

Both fire reminders. They don't double up: `cloud_sync` waits
`CLOUD_REMINDER_LAG_MINUTES` (default 10) before sending anything, so
`local_sync` always gets first crack. A reminder still unfired ten
minutes after it came due means the Mac is asleep, and the cloud takes
over. Set `CLOUD_REMINDERS=false` to go back to local-only.

---

## Setup — do this once

### 1. Notion integration token
1. [notion.so/my-integrations](https://www.notion.so/my-integrations) → **New integration**
2. Name it `school-sync`, copy the token
3. Open the database → `•••` → **Connections** → add `school-sync`

### 2. Google OAuth (Calendar + Gmail + Classroom)
1. [console.cloud.google.com](https://console.cloud.google.com) → new project → enable the **Calendar**, **Gmail**, and **Classroom** APIs
2. **OAuth consent screen** → External → add yourself as a test user
3. **Credentials** → Create OAuth client ID → Desktop app → download it as `client_secret.json` into this folder
4. Run this once to get a refresh token:

```bash
pip install google-auth-oauthlib
OAUTHLIB_RELAX_TOKEN_SCOPE=1 python3 -c "
from google_auth_oauthlib.flow import InstalledAppFlow
flow = InstalledAppFlow.from_client_secrets_file('client_secret.json', scopes=[
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/classroom.courses.readonly',
    'https://www.googleapis.com/auth/classroom.coursework.me.readonly',
])
print('Refresh token:', flow.run_local_server(port=0).refresh_token)
"
```

`OAUTHLIB_RELAX_TOKEN_SCOPE=1` is required, not optional: Google hands
back `classroom.student-submissions.me.readonly` in place of
`classroom.coursework.me.readonly`, and oauthlib treats that rename as
a mismatch and raises. The refresh token doesn't expire unless revoked.

> **Using your school Google account?** Re-run this whole step signed
> into it. Classroom returns zero courses on a personal account — auth
> works fine, there's just nothing there.

### 3. Anthropic API key
[console.anthropic.com](https://console.anthropic.com) → API Keys. Used
only by the Gmail sweep, one small Sonnet call per candidate email —
cents a month at this volume. Until it's set, the Gmail sweep skips
itself cleanly and everything else keeps running.

### 4. Phone notifications (ntfy)
1. Install the **ntfy** app (iOS/Android)
2. Pick a private topic name — treat it like a password, since anyone
   who knows it can read and post to it. Use a random string.
3. Subscribe to that exact topic in the app
4. Put it in `.env` as `NTFY_TOPIC`

### 5. Fill in `.env`

```bash
cp .env.example .env
# edit .env with your real values
```

### 6. Start the Mac job

```bash
pip3 install -r requirements.txt
python3 generate_plist.py --reload
```

That writes the launchd plist to `~/Library/LaunchAgents/` with `0600`
permissions and restarts the job. Watch it:

```bash
tail -f sync.log sync-error.log
```

### 7. Set up GitHub Actions

Push to a **public** repo (run the secrets check above first), then add
each of these under Settings → Secrets and variables → Actions:

`NOTION_TOKEN`, `NOTION_DB_ID`, `GOOGLE_CLIENT_ID`,
`GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`, `GOOGLE_CALENDAR_ID`,
`ANTHROPIC_API_KEY`, `SCHOOL_EMAIL_HINTS`, `NTFY_TOPIC`

Tuning values (quiet hours, intervals, timezone) go under the
**Variables** tab, not Secrets — the workflow has defaults for all of
them, but `SCHOOL_TIMEZONE` is worth setting explicitly if you ever
move, since the runners are UTC.

---

## Daily use

- **Add something:** type it into Notion.
- **Finish something:** set Status → Done. The Calendar event and all
  reminders stop on the next pass.
- **Email or Classroom adds something:** email items arrive prefixed
  `[unconfirmed]` — glance at those once a day, correct them, drop the
  prefix. Classroom items arrive as-is.
- **Everything else:** happens without you.

---

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

No dependencies — standard library only. They cover the reminder
cadence engine, timezone handling, sync-state tracking, and Notion
field extraction. They also run on every push via GitHub Actions and
gate the scheduled sync, so a cadence bug can't reach your phone.

---

## How it fits together

```
                  ┌──────────────────────────┐
                  │   Notion  (the hub)      │
                  │   — you type here        │
                  └────┬────────────────▲────┘
             read all  │                │  create captured items
                       ▼                │  stamp Last Synced / Last Reminded
        ┌──────────────────────────┐    │
        │ local_sync (60s, awake)  │────┤
        │ cloud_sync (30m, always) │────┘
        └──────┬────────────┬──────┘
               │            │
     Google Calendar    ntfy → phone
     (tagged with       (tap opens the
      the page ID)       Notion page)
```

**Notion is the source of truth.** Everything downstream is derived and
safe to delete — clear the Calendar and the next sync rebuilds it.

Key ideas worth knowing before you change anything:

- **Idempotency.** Calendar events carry the Notion page ID in
  `extendedProperties.private.notion_id`, so re-syncing updates the
  existing event instead of making a second one.
- **Capture dedup.** Items from Gmail and Classroom carry an
  `External ID` (`gmail:<msg>`, `classroom:<course>:<work>`). The scans
  re-scan a trailing window every run; this is what stops that window
  from producing dozens of copies. It lives in Notion because GitHub's
  runners keep no state between runs.
- **The sync grace window.** Writing `Last Synced` is itself an edit, so
  `state.py` allows 10 seconds of slack before treating an edit as new.
  Without it, every item re-syncs on every pass forever. If you ever see
  `synced N item(s)` on every single pass with nothing changing, look
  there first.
- **Timezone is explicit, never ambient.** `SCHOOL_TIMEZONE` pins both
  halves to the same wall clock. A date-only due date means 23:59 *your
  time* — reading it as UTC made everything go overdue six hours early.

---

## Known limits

- Not instant: 60 sec worst case while the Mac is awake, 30 min otherwise.
- Email parsing isn't perfect — that's why it lands as `[unconfirmed]`.
- Tapping a notification opens the ntfy app briefly before handing off
  to Notion. That's an OS-level constraint on third-party push apps, not
  something this code can fix.
- Overdue items remind every 2 hours forever until you mark them Done.
  That's deliberate.
