# Handoff prompt

Copy the block below into a fresh Claude Code session to bring it up to
speed on this project.

**Trim the agenda before pasting.** A multi-item agenda invites a session
that half-finishes each one. The current agenda is a deliberate exception:
it is a debugging item that must come first, followed by an open-ended
audit that depends on what the debugging turns up.

Keep this file current. It is the fast path back into the project after a
context reset, and **a stale handoff is worse than none: it will be
trusted.** This has already bitten once — an earlier draft of this file
still described a missing `.gitignore`, absent dedup, no tests, and no git
repo, months after all four were done. Verify before you write.

Last updated: 2026-07-31, on commit `5602b5e`. Working tree clean, `main`
in sync with origin. Always confirm with `git log --oneline origin/main..HEAD`
and `git status` rather than trusting this line.

---

````
You're picking up ~/school-sync, a Notion-driven school assignment system
(Notion → Google Calendar + phone push via ntfy, plus Gmail/Classroom
capture). CLAUDE.md auto-loads and has the full architecture, the live
Notion schema, every non-obvious decision, and every known gap — read the
"School Sync" section IN FULL before touching anything. Don't rediscover
what's already documented; do verify anything you're about to depend on.

## Where things actually stand

Public repo: https://github.com/PeterDaOne/school-sync, pushed through
`5602b5e`. **307 stdlib-unittest tests**, green, gating CI. launchd
loaded and healthy. Nothing uncommitted.

**Both capture layers are proven end to end against real data.** This was
the project's longest-standing "written but never run" gap and it is
closed:

- **Classroom** — a real assignment became a real Notion row with the
  correct `For`, due date, Task Type, Priority and External ID, fired a
  capture notification confirmed by polling the live ntfy topic, and did
  not duplicate on the next pass.
- **Gmail** — a real email did the same, and both dedup layers were
  verified *independently*: the `school-sync/seen` label excludes a
  message from the query, and with the label bypassed the External ID
  skips it before spending a Claude call.

**Cloud Classroom capture is proven too, not merely inferred.** A newly
posted assignment appeared in Notion at 18:25:00Z with launchd unloaded,
and every field matched a prediction written down beforehand. The
structural proof is stronger than the unload: `local_sync.py` imports
only `config, log, pipeline` and references the sweeps zero times, so
**only `cloud_sync.py` can capture anything.** That also confirms the
deployed `GOOGLE_REFRESH_TOKEN` carries the new Classroom scope — a
missing scope 403s and turns the run red.

**Reuse this technique:** archive a captured row so its External ID
vanishes, wait ~5 min, see whether it comes back. It is the cheapest
end-to-end cloud check available without admin log access.

**`ANTHROPIC_API_KEY` as a GitHub secret cannot be verified from outside,
and that is permanent, not a to-do.** With `SCHOOL_EMAIL_HINTS` filtering
to school domains and Peter's personal mailbox receiving none, the sweep
finds 0 candidates and makes no Claude call — so present and missing keys
produce byte-identical green runs. Only a run log distinguishes them, and
that needs admin auth. Don't burn time inferring it from outside.

## Agenda item 1 — capture is missing things (do this first)

A Classroom assignment did not become a Notion item, and an email task
plus two email events were never captured either. **Diagnose before
changing anything.**

Highest-suspicion lead, check first: the `SCHOOL_EMAIL_HINTS` GitHub
secret may still hold the old two-domain value (`eldoradohs.org,aps.edu`).
`.env` was widened to `eldoradohs.org,aps.edu,gmail.com,yahoo.com` but
the secret update was left to Peter and never confirmed. **The sweeps run
ONLY in `cloud_sync.py`**, so `.env` governs nothing in production — a
stale secret filters personal-domain mail out before Claude ever sees it,
which would explain all three missing email items at once.

Other leads, roughly in order:
- Is the item absent from Notion, or present but never notified? Those
  are completely different bugs.
- `gmail_scan` uses `newer_than:1d`. Mail older than the window is never
  examined again — the seen-label doesn't help, the message simply falls
  out of scope. If the cloud missed a day, those are gone permanently.
  `CLASSROOM_LOOKBACK_HOURS` (48) is the same class of issue.
- For the Classroom miss: PUBLISHED or still a draft? Already turned in
  (`_submitted_coursework_ids` skips TURNED_IN/RETURNED)? Outside the
  lookback? Past `MAX_NEW_PER_RUN`?
- `MAX_CLASSIFICATIONS_PER_RUN` (10) vs `MAX_MESSAGES_PER_RUN` (20) —
  check the interaction when a batch exceeds the cap.
- Actions logs need admin auth; ask Peter to read them rather than
  trying from outside.

## Agenda item 2 — full system audit (after item 1)

Audit everything: every `.py` file, `generate_plist.py`, the README,
`.env.example`, the GitHub Actions workflow, the test suite, and CLAUDE.md
and HANDOFF.md themselves. Fix, streamline, or improve anything actually
wrong, fragile, redundant, or needlessly complicated.

**Real creative freedom here — this is not a narrow bug-fix pass.** If a
cleaner architecture, a better abstraction, a smarter dedup strategy, or
a more robust error-handling pattern occurs to you, pursue it. Read every
file completely and trace the actual call paths; don't skim. Target:
"obviously correct and well-built," not "technically works."

Known-real starting points (not exhaustive — keep digging):

- **Gmail `due_date` carries no time**, so an Event captured from email
  can never reach the hour-before reminder tier (which needs
  `timeutil.has_time_component`). "Rehearsal Thursday 6pm" reminds
  morning-of but not an hour before. Documented, unfixed.
- **`classmap.NON_CLASS_CATEGORIES`** blocks automated capture from ever
  selecting School/Personal/Friends/Work, so a captured chore lands with
  `For` blank. That rule was written when the only mechanism was fuzzy
  string matching on a course name; Claude now classifies explicitly,
  which is a different mechanism with different risk. **Genuine judgment
  call — raise it with Peter, don't decide unilaterally.**
- **An unexplained observation from 2026-07-30**, recorded and never
  resolved: one run reported "nothing to do" when a reminder was
  demonstrably due. Did not reproduce in 3 attempts; a Notion query-lag
  hypothesis was tested and disproven. If a reminder silently fails to
  fire, this is a prior sighting, not a fresh one.
- **Two silent time bombs:** the `schedule` cron auto-disables ~Sept 28
  (60 days from last repo activity) and the cron-job.org PAT expires
  ~Oct 27. After that both cloud paths die within a month of each other,
  mid-semester, with nothing appearing broken. The cron-job.org failure
  alarm is on, which covers the dispatcher but not the PAT expiry itself.

Use a task list. Report what you found, what you fixed, and what you
deliberately left alone and why.

## What is NOT proven

**The classifier's behaviour on real school mail.** The sample is 11
hand-constructed cases (7 triage + 4 relative-date), all of which passed
— but they are cases *the assistant wrote*, which is not the same as real
teacher mail. Untested: forwarded threads, digests carrying several
assignments, and mail that mentions a deadline without setting one.

Gmail capture has also **never run in the cloud** — every real call so
far was local.

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
- **Run it against real data before calling it done.** Multiple bugs here
  were invisible to both code review and unit tests and surfaced within
  minutes of a real API call — the empty `dueTime`, the missing clock in
  the classifier, the `🏫` emoji nobody knew was being prepended.
- **Re-check deferred fixes when the reason for deferring them changes.**
  The Gmail label-ordering bug sat documented-but-unfixed on the grounds
  that Gmail couldn't run at all; that justification expired the moment
  an API key went in, and it should have been revisited then.
- **Writing tests surfaces real bugs.** Treat test-writing as bug
  hunting, not a separate chore.
- **local_sync.py runs every 60s via launchd against real Notion data and
  pushes real notifications to Peter's phone.** Unload the job while
  editing (`launchctl unload ~/Library/LaunchAgents/com.peter.schoolsync.plist`)
  and reload only once verified.
- If you touch real Notion data to test, restore it — EXCEPT "Do dishes",
  "Pray", and "Assinment type shi", which are known junk items.
- Never commit or expose `.env`, `client_secret.json`, or the generated
  plist. Run the README's content-level credential scan before any push —
  the repo is public.
- Ask Peter on genuine judgment calls with real tradeoffs; make small
  implementation decisions yourself.
- Update CLAUDE.md's School Sync section AND this file with whatever
  changes. Don't leave either stale.
````

---

## What shipped 2026-07-30 → 07-31

1. **OAuth re-consent, and a scope-naming discovery.** Google renames
   BOTH Classroom coursework scopes on grant. Scope strings are therefore
   useless — actively misleading — as a diagnostic; oauthlib prints a
   "not all requested scopes were granted" warning to stderr on every
   refresh even when everything works. Only a 200 from a real API call
   means anything.
2. **A day-shifting bug in `_due_date_iso`**, found by tracing and
   confirmed against live Google data. Proto3 JSON omits zero-valued
   fields, so midnight UTC arrives as `dueTime: {}`, and `if not t:`
   treated that as all-day — an assignment due 6:00 PM was recorded as
   all-day on the FOLLOWING date: **1 day 5:59 late.**
3. **Per-item error policy** applied to both sweeps, which neither
   followed: one bad item aborted the rest, permanently in Classroom's
   case since a failed create writes no External ID.
4. **`shared/tasktype.py`** — captured items now get Task Type and
   Priority. One verb + one noun, read off Peter's real rows. Priority is
   High-or-Medium only, never Low: it multiplies the reminder interval,
   and an automated guess must not make an item nag *less* than default.
5. **Gmail capture widened beyond schoolwork**, and Claude now classifies
   into `Type` — which selects the reminder cadence, so it is not
   cosmetic. Verified live on 7 triage cases including three
   deliberately keyword-loaded corporate CTAs, all correctly rejected.
6. **The classifier had no clock** — relative dates resolved to null, and
   an Event with no due date gets no reminders at all, so capture looked
   successful and silently never fired. Today's date now goes in the
   prompt.
7. **The `[unconfirmed]` title prefix was removed** — `Input Type =
   Email` already records provenance structurally, and the prefix
   duplicated it into the one field that rides into every notification.
8. **Tests 229 → 307.**

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
