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

Public repo: https://github.com/PeterDaOne/school-sync. **345 tests**,
green, gating CI. launchd loaded. Both capture layers proven end to end
against real data; Classroom additionally proven running in the cloud.

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
messages, including relative dates ("tomorrow", "end of day today") and
class resolution ("Physics" → "AP Physics"). That closes what this file
previously listed as the biggest unproven thing.

## What is NOT proven

- **Gmail capture has never run in the cloud.** Every real call so far
  has been local. `ANTHROPIC_API_KEY` as a GitHub secret cannot be
  verified from outside and that is permanent, not a to-do: with
  `SCHOOL_EMAIL_HINTS` filtering to school domains and Peter's personal
  mailbox receiving none, present and missing keys produce byte-identical
  green runs. Only a run log distinguishes them, and that needs admin
  auth. Ask Peter to look; don't burn time inferring it.
- **The Gmail `Source Link` has not been clicked.** The URL shape is
  pinned by tests and the `authuser=` form is deliberate (`u/0` means
  "first signed-in account", which breaks once school + personal are both
  signed in). Whether it actually opens the message needs a human tap.
- **Real teacher mail at volume.** Still untested: forwarded threads,
  digests carrying several assignments, mail mentioning a deadline
  without setting one.
- **The classifier is non-deterministic.** The same email came back
  `Events` once and `Tasks` once across two runs. Both readings were
  defensible, but don't assume a verdict is stable.

## Open items

- **`classmap.NON_CLASS_CATEGORIES`** still blocks automated capture from
  selecting School/Personal/Friends/Work, so captured chores and personal
  events land with `For` blank (4 of the 5 recovered items did). Peter
  decided on 2026-07-31 to **let Claude choose the category explicitly
  while keeping fuzzy course-name matching locked out** — the hazard was
  always fuzzy matching, not explicit classification. **Not yet
  implemented.** It needs a `category` field in the Gmail classifier
  schema (enum: the four non-class options + null) and a resolver path
  that accepts an exact category name from the model but still refuses
  one inferred from a course name.
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
11. **Tests 307 → 345.**

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
