# School Sync

One input, many outputs. You type or check things off in Notion.
Calendar, phone reminders, and captured email/Classroom items stay
current on their own.

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
    'https://www.googleapis.com/auth/gmail.modify',
    'https://www.googleapis.com/auth/classroom.courses.readonly',
    'https://www.googleapis.com/auth/classroom.coursework.me.readonly',
    # Added 2026-07-30 -- needed to read courseWork on a course you
    # TEACH rather than take (e.g. a self-created test class). Your real
    # school account, always a student, will never need this path, but
    # it's harmless to request and means a future TA/co-teacher
    # situation already works.
    'https://www.googleapis.com/auth/classroom.coursework.students.readonly',
])
print('Refresh token:', flow.run_local_server(port=0).refresh_token)
"
```

`OAUTHLIB_RELAX_TOKEN_SCOPE=1` is required, not optional. Google renames
**both** Classroom coursework scopes on grant (verified live 2026-07-30
against `tokeninfo`):

| Requested | Actually granted |
|---|---|
| `classroom.coursework.me.readonly` | `classroom.student-submissions.me.readonly` |
| `classroom.coursework.students.readonly` | `classroom.student-submissions.students.readonly` |

oauthlib treats that rename as a mismatch and raises without the relax
flag. The refresh token doesn't expire unless revoked.

**This makes scope strings useless for diagnosis, and actively
misleading.** Even with the flag set, oauthlib prints

```
Not all requested scopes were granted by the authorization server,
missing scopes .../classroom.coursework.me.readonly,
.../classroom.coursework.students.readonly
```

on *every token refresh*, including when everything is working. It goes
to stderr, so it lands in `sync-error.log`. Ignore it. **The only real
test is whether an API call returns 200** — checking a granted-scope list
against the requested names will tell you a working token is broken.

**Why `gmail.modify`, when everything else is read-only?** It's used for
exactly one thing: adding a hidden `school-sync/seen` label to messages
the scan has already classified. Without it, an email Claude decides
*isn't* an assignment gets re-classified on every run for as long as it
sits in the search window — hundreds of paid API calls a day to keep
getting the same answer. Nothing in this project reads, sends, or
deletes mail. If the scope isn't granted the scan still works; it just
narrows its window to keep the repeat cost bounded, and says so in the
log.

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

**Optional: the "Mark done" button.** Notifications can carry a button
that marks the item Done in Notion without opening any app. It works via
a *second, distinct* ntfy topic — never NOTION_TOKEN, never the same
topic as step 2 above:

5. Pick **another** random topic name, different from `NTFY_TOPIC`
6. Put it in `.env` as `NTFY_COMMAND_TOPIC` — no need to subscribe to it
   in the app, nothing is ever meant to be read from it directly
7. Leave it unset and the button is simply absent from every
   notification; nothing else breaks

Why a second topic instead of the button PATCHing Notion directly: ntfy
topics are unauthenticated, and the button fires straight from the
phone with no server in between. A button that carried a real Notion
credential would put that credential in every single notification,
forever, readable by anyone who ever learns the topic name. The command
topic carries only a bare page id — worst case if it leaks, someone can
mark your homework Done, which costs you two seconds in Notion to undo.
See `shared/commands.py` for the polling side.

The button clears itself almost instantly once tapped (ntfy accepting
its own publish is fast), but the item isn't actually marked Done in
Notion until the next sync pass picks up the command — up to ~2 minutes
while the Mac is awake, up to ~10 minutes from the cloud alone. Same
"not instant" honesty as everything else in this system.

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

`NTFY_COMMAND_TOPIC` (the "Mark done" button, see step 4 above) is
optional — add it here too if you set one up locally, using the same
value from `.env`, or skip it and the button just won't appear on
cloud-sent reminders.

Tuning values (quiet hours, reminder-cadence constants, timezone) go
under the **Variables** tab, not Secrets — the workflow has defaults
for all of them (see `.env.example` for what each one does and its
default), but `SCHOOL_TIMEZONE` is worth setting explicitly if you ever
move, since the runners are UTC.

**Watch the tab you are on.** Secrets and Variables live on the same
page, and a value saved under Variables appears in the list looking
exactly like a saved secret while `${{ secrets.NAME }}` resolves to an
empty string. The same goes for a misspelled name — `NFTY_TOPIC` cost a
full day of silently dropped reminders. Seeing the name in the list is
not verification; a green run is not verification either. The proof is
a push arriving on your phone.

### 8. Beating GitHub's scheduler

**The `cron:` in the workflow is a request, not a promise.** Measured
against ~25h of live history, a `2-59/5` cron (every 5 minutes)
actually delivered runs **~110 minutes apart on average, 204 minutes at
worst** — about 13 runs a day instead of 288. GitHub deprioritizes
sub-hourly schedules on public repos, and scheduled runs are explicitly
best-effort. Left alone, that is your worst-case reminder delay
whenever the Mac is shut.

**`workflow_dispatch` is not throttled.** A manual or API-triggered run
starts within about 30 seconds. So an external scheduler that calls the
dispatch API gives you the cadence the cron only pretends to:

```
POST https://api.github.com/repos/<you>/school-sync/actions/workflows/sync.yml/dispatches
Accept: application/vnd.github+json
Authorization: Bearer <fine-grained PAT>
X-GitHub-Api-Version: 2022-11-28

{"ref": "main"}
```

Expect `204 No Content` on success. Any free scheduler works —
[cron-job.org](https://cron-job.org) needs no code, and a Cloudflare
Worker with a cron trigger keeps the token inside an account you
already control. Every 5 minutes is the sweet spot: it matches
`CLOUD_REMINDER_LAG_MINUTES`, and each dispatch spins up two runner
VMs, so every-minute pinging is a lot of machine time for four minutes
of latency.

**Make the token fine-grained**, scoped to this one repository, with
**Actions: Read and write** and nothing else, and give it an expiry.
Worst case if it leaks, someone can trigger your workflow — they cannot
read your secrets. Treat it like a password anyway; it lives outside
GitHub, in whatever service you point at this.

**Leave the `cron:` schedule enabled.** It costs nothing and becomes
the fallback for when the external scheduler is down — which is the
whole reason cloud_sync exists in the first place. The `concurrency`
block on the sync job is what makes running both safe: without it, a
scheduled run and a dispatched run can overlap, read the same
`Last Reminded`, and both send the same reminder.

---

## Daily use

- **Add something:** type it into Notion.
- **Finish something:** set Status → Done. The Calendar event and all
  reminders stop on the next pass.
- **Email or Classroom adds something:** it appears as a normal item.
  Email-sourced ones are inferred by Claude from prose rather than read
  from structured fields, so give them a glance — filter on
  `Input Type = Email` to find them. Classroom items come straight from
  the API and need no review.
- **Everything else:** happens without you.

---

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

The test cases use only the standard library — no pytest — but they
import the real modules, which need `requirements.txt` installed
(`notion_client` imports `requests`). They cover the reminder cadence
engine, timezone handling, sync-state tracking, Notion field
extraction, and Classroom→Notion category matching. They also run on every
push via GitHub Actions and gate the scheduled sync, so a cadence bug
can't reach your phone.

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

- Not instant: 60 sec worst case while the Mac is awake, under 6 min
  otherwise (via the external dispatch scheduler — see "Beating
  GitHub's scheduler" above; without it, up to ~3.5 hours on the
  built-in cron alone).
- Email parsing isn't perfect. Captured email items are Claude's
  reading of prose (title, class and due date are all inferred), so
  they're worth a glance; `Input Type = Email` is how you find them.
- Tapping a notification opens the ntfy app briefly before handing off
  to Notion. That's an OS-level constraint on third-party push apps, not
  something this code can fix.
- Overdue items remind forever until you mark them Done — at a rate
  that depends on type and priority (Tasks nag harder than Assignments
  once missed; High priority harder than Low), not a flat interval
  anymore. See `.env.example` for the exact constants.
- The "Mark done" button's tap effect is near-instant, but the Notion
  page doesn't actually flip to Done until the next sync pass notices
  the command — up to ~2 min locally, ~10 min from the cloud alone.
- `shared/classmap.py`'s `CATEGORY_EMOJI` dict is keyed by exact Notion
  `For` option names. Rename an option in Notion and it silently falls
  back to its Type emoji (📝/☑️/📅 — the title still works, it just
  stops being class-specific) until the dict is updated to match.
- **Adding a new non-class option to `For` requires a code edit.** Put
  it in `classmap.NON_CLASS_CATEGORIES` too, or the Classroom capture
  sweep can fuzzy-match a course name onto it — "Personal Finance" would
  land under "Personal". There is no way to tell a class from a life
  category by looking at the string, so the list is explicit.
- ntfy tags are **not** invisible metadata: a tag matching an emoji
  short code is rendered as an emoji and prepended to the title. Adding
  a decorative tag changes how every notification looks.
