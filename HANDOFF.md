# Handoff prompt

Copy the block below into a fresh Claude Code session to bring it up to
speed on this project.

**Trim the agenda before pasting.** A multi-item agenda invites a session
that half-finishes each one.

Keep this file current. It is the fast path back into the project after a
context reset, and **a stale handoff is worse than none: it will be
trusted.** This has already bitten once — an earlier draft still
described a missing `.gitignore`, absent dedup, no tests and no git repo,
long after all four were done. Verify before you write.

**Do not put real values in this file.** It is tracked in a public repo.
An earlier version quoted `SCHOOL_EMAIL_HINTS` in full, which named
Peter's actual school — PII about a minor, published. `scan_secrets.py`
now catches that; run it before pushing.

Last updated: 2026-07-31, on commit `c69d945` + the session below.
Working tree state and test count change often — confirm with
`git log --oneline origin/main..HEAD`, `git status`, and
`python3 -m unittest discover -s tests -t .` rather than trusting this
line.

---

````
You're picking up ~/school-sync, a Notion-driven school assignment system
(Notion → Google Calendar + phone push via ntfy, plus Gmail/Classroom
capture). CLAUDE.md auto-loads and has the full architecture, the live
Notion schema, every non-obvious decision, and every known gap — read the
"School Sync" section IN FULL before touching anything. Don't rediscover
what's already documented; do verify anything you're about to depend on.

## Where things actually stand

Public repo: https://github.com/PeterDaOne/school-sync. **416 tests**,
green, gating CI. launchd loaded. **Both capture layers are now proven
end to end AND proven running in the cloud** — Classroom on 2026-07-31,
Gmail on 2026-08-01 (row `Count 1 to 10` created inside dispatch run
#710's 48-second window; `local_sync.py` imports only `config, log,
pipeline` and cannot capture, so nothing else could have made it). That
Gmail proof doubles as proof `ANTHROPIC_API_KEY` is set as a GitHub
secret, which earlier notes called permanently unknowable — it stopped
being unknowable when the subject-keyword filter was removed.

**The external dispatcher runs every 2 minutes** (raised from 5 on
2026-08-01). Measured: consecutive run numbers, no gaps, ~48s per run,
so nothing queues behind the `concurrency` group. Capture latency is
bounded by this interval, not by `CLOUD_REMINDER_LAG_MINUTES` — captured
items carry an External ID and skip the lag.

**Capture, notification and the Gmail pre-filter were all repaired on
2026-07-31.** Three separate bugs, one reported symptom ("capture is
missing things"):

1. **Capture notifications were starved by the daily budget.** A
   Gmail-captured assignment sat in Notion, due the next day, unannounced
   for 3h20m / 137 passes, while the budget was spent re-nagging stale
   junk. `pipeline._allocate` now rations announcements and nags
   separately — announcements are exempt from the daily budget (an item
   can only be captured once, so they are self-limiting) and are still
   bounded by `MAX_NOTIFICATIONS_PER_PASS`.
2. **The Gmail subject-keyword whitelist was never widened** when the
   capture scope was. Measured live: it passed 2 of 6 real messages and
   silently dropped a chore, a birthday and a tournament. Replaced with a
   negative filter on Gmail's own promotions/social/forums categories.
3. **The `school-sync/seen` label outlived the policy that set it.** A
   chore rejected under the old schoolwork-only rules could never be
   reconsidered under the new ones. The label is now versioned
   (`school-sync/seen-v2`); **bump the suffix whenever the capture policy
   changes.**

All five missed items were recovered and verified in Notion.

**The classifier is now proven on real mail** — 6 for 6 on genuine
messages, including relative dates ("tomorrow", "end of day today"),
class resolution ("Physics" → "AP Physics"), and (after the `For` change
below) life categories: chore/birthday/tournament → `Personal`, with
exactly one of `class_name`/`category` set every time. That closes what
this file previously listed as the biggest unproven thing.

## What is NOT proven

- **The Gmail `Source Link` has not been clicked.** The URL shape is
  pinned by tests and the `authuser=` form is deliberate (`u/0` means
  "first signed-in account", which breaks once school + personal are both
  signed in). Whether it actually opens the message needs a human tap.
- **Real teacher mail at volume.** Still untested: forwarded threads and
  mail mentioning a deadline without setting one. (Multi-item digests
  are handled as of 2026-08-01 — see below — but only synthetic ones
  have been tested; a real teacher's formatting is still unproven.)
- **The classifier is non-deterministic.** The same email came back
  `Events` once and `Tasks` once across two runs. Both readings were
  defensible, but don't assume a verdict is stable.

## Open items

- **An unexplained observation from 2026-07-30**, never resolved: one run
  reported "nothing to do" when a reminder was demonstrably due. Did not
  reproduce in 3 attempts; a Notion query-lag hypothesis was tested and
  disproven. If a reminder silently fails to fire, this is a prior
  sighting, not a fresh one.
- **Two silent expiries.** README §9 has the full table. The
  cron-job.org PAT (~2026-10-27) and the GitHub `schedule:` cron
  (60 days of repo inactivity). Notion Tasks now exist for both, so the
  reminder system reminds you to maintain the reminder system. A real
  dead man's switch (healthchecks.io pinged at the end of `cloud_sync`)
  is the obvious upgrade if this ever bites.

## Ground rules that were earned the hard way

- **Never trust a schema or API assumption — verify it live.** This
  codebase has burned time on a Notion property typo, an MCP tool that
  silently "corrected" that typo in its own output, OAuth scopes Google
  renames on grant, a Notion API that silently CREATES any select OR
  multi-select option you hand it, http.client latin-1-encoding a UTF-8
  emoji into a crash, an ntfy tag that rendered as a visible emoji,
  proto3 JSON omitting a zero-valued field, and a classifier with no
  clock. Hit the real APIs before believing anything.
- **`if not x` and `x is None` are different questions.** Against a
  proto3 JSON API the difference only shows up on the all-zero value —
  the case least likely to appear in casual testing, and the one that
  shipped a day-shifting due-date bug.
- **Read the logs before theorising.** The 3h20m notification outage was
  sitting in `sync.log`, once a minute, for 137 consecutive lines. It was
  invisible because it was *worded* like healthy throttling. When you add
  a log line, make the bad case read differently from the normal case.
- **A filter written for one policy silently becomes the policy.** Both
  the keyword whitelist and the seen-label kept enforcing rules that had
  been deliberately replaced. When you widen what the system accepts,
  grep for every narrowing filter upstream of the thing you widened.
- **Run it against real data before calling it done.** Multiple bugs here
  were invisible to both code review and unit tests and surfaced within
  minutes of a real API call.
- **Re-check deferred fixes when the reason for deferring them changes.**
- **local_sync.py runs every 60s via launchd against real Notion data and
  pushes real notifications to Peter's phone.** Unload the job while
  editing (`launchctl unload ~/Library/LaunchAgents/com.peter.schoolsync.plist`)
  and reload only once verified.
- If you touch real Notion data to test, restore it — EXCEPT "Do dishes",
  "Pray", and "Assinment type shi", which are known junk items.
- **Never run `gmail_scan.run()` by hand while the cloud dispatcher is
  live.** Capture dedup assumes ONE capture runner. Production honours
  that (`local_sync.py` references the sweeps zero times; the workflow's
  `concurrency` group serializes cloud runs) but a manual sweep is a
  second runner with no guard — the cloud can fetch the still-unlabelled
  message and build its dedup index before your run finishes, producing
  duplicate rows. Pause the dispatcher first, or expect to dedup by hand.
- **Run `python3 scan_secrets.py` before any push.** The repo is public.
- Ask Peter on genuine judgment calls with real tradeoffs; make small
  implementation decisions yourself.
- Update CLAUDE.md's School Sync section AND this file with whatever
  changes. Don't leave either stale.
````

---

## What shipped 2026-07-31 (second session)

1. **Diagnosed "capture is missing things" into three distinct bugs**
   (above). The reported symptom was one thing; the causes were in the
   allocator, the Gmail query, and the label — three different layers.
   The Gmail task Peter thought was never captured *was* in Notion the
   whole time; he had simply never been told.
2. **`pipeline._allocate` now treats announcements and nags as different
   kinds of message.** `Reminder.kind` carries the distinction so it
   isn't re-derived by string-matching a title.
3. **Capture urgency now tracks the due date** instead of sitting at a
   flat priority 3.
4. **The summary line distinguishes "back in a minute" from "back
   tomorrow."** Both used to print "deferred to next pass".
5. **`Source Link`** (Notion `url`) on captured items, linking back to
   the Classroom assignment page or the Gmail message. Classroom uses the
   API's own `alternateLink`; Gmail is built with `?authuser=<address>`.
   Three pre-existing rows were backfilled.
6. **Three `ensure_*_property` functions became one**
   `ensure_managed_properties()` — one GET and one PATCH instead of one
   read per property.
7. **`reminders.TUNABLE_ENV_VARS`** is now the single source for the
   reminder knobs, imported by `generate_plist.py`. Five knobs added
   2026-07-30 had reached the workflow but never the plist, so tuning one
   in `.env` would have changed the cloud's behaviour and not the Mac's,
   silently. `tests/test_settings_parity.py` pins all three lists.
8. **`scan_secrets.py`** — the content-level credential scan is now a
   runnable, exit-coded check rather than prose in CLAUDE.md. It
   immediately found Peter's real school domain hardcoded in a test file
   and in this handoff.
9. **Classroom lookback 48h → 168h**, matching the Gmail window, for the
   same reason: an outage longer than the window loses work permanently
   and silently.
10. **Dead code and dead config removed** — `state.utc_now_iso` (unused
    re-export) and three `REMINDER_INTERVAL_HOURS*` settings left in
    `.env` from the pre-2026-07-29 tier system, which nothing had read
    for two days and which looked live.
11. **`For` on captured items.** `classmap.resolve_category()` accepts a
    life category (School/Personal/Friends/Work) that the classifier
    named outright — exact match only, no fuzzy matching — while
    `resolve()` keeps refusing those for course names. Disjoint
    allow-lists, pinned by a test, so "a course name can never become
    Personal" is structural rather than a fuzzy-threshold accident.
    Verified 6/6 on real mail; four existing rows backfilled.
12. **Gmail deep links: confirmed impossible, don't retry.**
    `mail.google.com/.well-known/apple-app-site-association` is **404**,
    so Gmail supports no iOS universal links and no https URL can open
    the app. `classroom.google.com` DOES publish one claiming `*` with
    35 exclusions that do not cover `/c/*/a/*/details` — so Classroom
    Source Links already open the app on iOS and the web on desktop,
    for free.
13. **Multi-item extraction (2026-08-01).** One email used to produce at
    most one row; extras were silently merged (one row, one wrong due
    date) or dropped, non-deterministically. Now `EXTRACTION_SCHEMA`
    returns a list, the classifier reads the real body (`format="full"`
    + `_message_text`; it used to see only the ~200-char snippet), and
    items get per-item External IDs (`gmail:<id>`, `gmail:<id>#2`, ...
    — the bare first form is load-bearing backward compatibility).
    Label bumped to `seen-v3`. **The API rejects `maxItems` on arrays**
    (400, found live); the cap is code-side. Verified: 9/9 items with
    individually correct due dates from 3 synthetic multi-item emails,
    and the bump recovered a REAL lost task from Peter's own mail
    ("Test max pushups" — dropped from the physics email under the old
    policy, nobody had noticed). **Proven end to end AND in the cloud:**
    from a clean slate the sweep reported `classified 1 message(s),
    added 2 item(s)` — two rows from one email in a single pass — and a
    cloud dispatch independently did the same on the same message.
14. **Peter's `Type` taxonomy, and the PAIRING model (2026-08-01).**
    Assignments = anything a teacher assigns for a class, graded or not
    ("teacher-assigned" beats "for a grade" where they diverge, so
    ungraded teacher-set reading stays an Assignment). Events = it
    happens at a set time and you must be there. Tasks = everything
    else.

    **Some things are BOTH and are captured as two rows:** the preparing
    is an Assignment, the doing-it-live is an Event. A class
    presentation, a recital, a final exam. Ordinary submitted work is
    NOT split — handing something in is not an occasion. Verified 6/6
    live in both directions (three split correctly, three correctly
    stayed single).
15. **`EVENT_REMINDER_DAYS` exists but is back at its original
    `3,1,0`.** Briefly widened to `14,7,3,1,0` to give graded Events
    study runway; the pairing model made that unnecessary and it was
    reverted. **Prep runway is the paired Assignment's job.** If a
    graded Event looks under-reminded, check its Assignment was
    captured — do not add tiers. A regression test guards this.
16. **Tests 307 → 416.** New files: `test_settings_parity.py`, `test_calendar_client.py` (which had ZERO coverage despite `_event_times` encoding Google's exclusive all-day end date), `test_generate_plist.py`.

## Maintaining this file

When you finish a session, update these so the next one starts from truth
rather than a snapshot that has quietly rotted:

1. **The commit hash and test count**, in the header and the status block.
2. **The proven / not-proven split.** The most valuable content in the
   file — the difference between "the code exists" and "the code works",
   which have been very different things here. Note it has two axes:
   *verified locally* is not *verified in the cloud*, and both capture
   sweeps only ever run in the cloud.
3. **The agenda.** Replace it with what actually comes next, not what the
   last session happened to finish.
4. **Delete anything that is no longer true.** Stale leads are the main
   failure mode of this file; a fixed problem left described as open
   costs the next session real time.
