# Handoff prompt

Copy the block below into a fresh Claude Code session to bring it up to
speed on this project.

**Trim the agenda to a single item before pasting.** Leaving several
invites a session that half-finishes each one. The previous multi-item
agenda was a deliberate exception (one coherent arc, with its own
ordering note); it is now finished, so go back to one item at a time.

Keep this file current. It is the fast path back into the project after a
context reset, and a stale handoff is worse than none: it will be trusted.

Last updated: 2026-07-30 (third session that day), on commit 4ff5290 plus
uncommitted work — see "What is and isn't committed" below. Always
confirm with `git log --oneline origin/main..HEAD` and `git status`
rather than trusting this line; it goes stale the moment anyone commits.

---

````
You're picking up ~/school-sync, a Notion-driven school assignment system
(Notion → Google Calendar + phone push via ntfy, plus Gmail/Classroom
capture). CLAUDE.md auto-loads and has the full architecture, live schema,
every non-obvious decision, and every known gap — read the "School Sync"
section in full before touching anything. It was rewritten at the end of
the last session and is accurate. Don't rediscover what's already there.

## Where things actually stand

Repo: https://github.com/PeterDaOne/school-sync (public). **297**
stdlib-unittest tests, green locally. launchd loaded and healthy.

**Classroom capture WORKS. It is proven end to end against real data as
of 2026-07-30.** This was the project's longest-standing "written but
never run" gap and it is closed: a real assignment posted in a real
Classroom class became a real Notion row with the correct `For`, due
date and External ID, fired a capture notification that was confirmed
landing on Peter's phone by polling the live ntfy topic, and did not
duplicate on the next pass. Full `cloud_sync.py` then ran clean, exit 0.

**Gmail capture ALSO works now.** `ANTHROPIC_API_KEY` is real in `.env`,
and a real email became a real `[unconfirmed]` Notion row with the right
`For`, Task Type, Priority and due date, plus a phone notification. Both
dedup layers were verified separately (seen-label excludes from the
query; External ID skips before spending a Claude call). The `anyOf`
schema change is confirmed working on a live call.

Note his personal mailbox has no school mail, so the sweep finds 0
candidates on its own — the end-to-end test overrode
`SCHOOL_EMAIL_HINTS=gmail.com` as an env var (not a `.env` edit).

**Nothing in the capture layer is unproven any more.** Both sweeps have
created real Notion items, fired real notifications, and been shown not
to duplicate.

**Cloud Classroom capture is PROVEN, not just assumed.** Verified
2026-07-31 by archiving the captured Notion row so its External ID
vanished, then watching it come back: Actions run **#620** succeeded at
18:22:40Z and the row was created at 18:23:00Z with the correct `For`,
Task Type, Priority and due date. `local_sync.py` cannot have done it —
it imports only `config, log, pipeline`, so the capture sweeps exist
only in `cloud_sync.py`. That single test also confirms the deployed
`GOOGLE_REFRESH_TOKEN` carries the new Classroom scope (a missing scope
would raise and turn the run red), and that `shared/tasktype.py` and the
`_due_date_iso` fix are live in the cloud. **Reuse this technique** —
archive a captured row and wait ~5 min — it is the cheapest end-to-end
cloud check available without admin log access.

**Still NOT independently verified: the `ANTHROPIC_API_KEY` secret.**
Peter says he added it, and there is no reason to doubt that, but it
cannot be confirmed from outside. With `SCHOOL_EMAIL_HINTS` now set as a
secret the cloud Gmail query filters to his school domains, his personal
mailbox receives none, so the sweep finds 0 candidates and makes no
Claude call — meaning the run is green and side-effect-free whether the
key is present or missing. The only way to tell them apart is to read a
run log (it prints `[gmail_scan] skipped — ANTHROPIC_API_KEY is not
configured yet` when absent), and per CLAUDE.md that needs admin auth,
so **Peter has to look while signed in.** It becomes self-evident the
moment real school mail arrives.

## What is and isn't committed

Committed and unpushed (3 commits): the earlier Classroom teacher-course
fix and handoff edits. **Uncommitted working-tree changes** from the
capture session: `classroom_scan.py`, `README.md`,
`tests/test_classroom_scan.py`, and the new `tests/test_gmail_scan.py`.
Nothing has been pushed. Run the README's content-level credential scan
before any push — the repo is public.

## What shipped (2026-07-30, third session)

1. **OAuth re-consent, and a scope-naming discovery.** Google renames
   BOTH Classroom coursework scopes on grant, not just the documented
   one. Scope strings are therefore useless — actively misleading — as a
   diagnostic; oauthlib prints a "not all requested scopes were granted"
   warning to stderr on every refresh even when everything works. The
   only valid test is whether an API call returns 200. Details and the
   mapping table are in CLAUDE.md and README §2.

2. **A real day-shifting bug in `_due_date_iso`, found by tracing and
   then confirmed against live Google data.** Proto3 JSON omits
   zero-valued fields, so midnight UTC arrives as `dueTime: {}` — and
   `if not t:` treated that empty dict as "all-day". An assignment due
   6:00 PM was recorded as all-day on the FOLLOWING date, read
   downstream as 23:59: **1 day 5:59 late.** Peter deliberately set his
   test assignment to 6:00 PM to settle it, and Google returned `{}`
   exactly as predicted.

3. **`classroom_scan.run()` now honours the project's own per-item error
   policy.** `create_item` was bare in the loop, so one rejected
   assignment aborted the sweep — permanently, since a failed create
   writes no External ID.

4. **Captured items now get `Task Type` and `Priority`** (`shared/tasktype.py`,
   new). One verb + one noun, read off Peter's real rows; all three
   verbs (Execute/Attend/Remember) reachable from capture. Priority is
   **High-or-Medium only, never Low** — Peter's call, because Priority
   multiplies the reminder interval and an automated guess must not make
   an item nag less than the default. Everything is filtered against the
   live Notion option list (multi-selects get silently created too).

5. **Tests 229 → 297.** `test_classroom_scan.py` 6 → 29;
   `test_gmail_scan.py` new at 20; `test_tasktype.py` new at 25.

## Known unfixed bug — do this before the first real Gmail run

`gmail_scan.py` calls `_mark_seen` BEFORE `create_item`. If the create
throws (a malformed `due_date` from Claude → Notion 400), the email is
already labelled `school-sync/seen`, the next run's `-label:` clause
excludes it forever, and **the assignment is silently lost**. Fix: label
rejects immediately (that is the cost control and must stay), but label
accepts only after the item exists. Left unfixed deliberately — Gmail
cannot run at all yet, and changing unverifiable code was judged worse
than documenting it.

## Suggested next item (pick ONE)

- **Confirm the cloud half.** Verify the GitHub secret, then read an
  Actions run's log for a `[classroom_scan]` line. Cheapest, and it
  closes the one unverified gap above.
- **Fix the Gmail label-ordering bug** above, with a test.
- **Turn on cron-job.org's "notify me when execution fails".** Still the
  one genuinely time-sensitive item: the `schedule` cron auto-disables
  ~Sept 28 and the PAT expires ~Oct 27, after which BOTH cloud paths die
  at once, silently, mid-semester.
- **Backfill an existing course.** `CLASSROOM_LOOKBACK_HOURS=8760` for
  one run; `MAX_NEW_PER_RUN` caps the import at 25.

## Ground rules that were earned the hard way

- **Never trust a schema or API assumption — verify it live.** This
  codebase has burned time on a Notion property typo, an MCP tool that
  silently "cleaned up" that typo in its own output, OAuth scopes Google
  renames on grant, a Notion API that silently CREATES any select option
  you hand it, Python's http.client latin-1-encoding a UTF-8 emoji into
  a crash, an ntfy tag that turned out to render as a visible emoji, and
  proto3 JSON omitting a zero-valued field. Hit the real APIs before
  believing anything.
- **`if not x` and `x is None` are different questions.** Against a
  proto3 JSON API the difference only shows up on the all-zero value —
  the case least likely to appear in casual testing, and the one that
  shipped the due-date bug.
- **Writing tests surfaces real bugs.** Treat test-writing as bug
  hunting, not as a separate chore.
- **local_sync.py runs every 60s via launchd against real Notion data
  and pushes real notifications to Peter's phone.** Unload the job while
  editing (`launchctl unload ~/Library/LaunchAgents/com.peter.schoolsync.plist`)
  and only reload once verified.
- If you touch real Notion data to test, restore it — EXCEPT "Do
  dishes", "Pray", and "Assinment type shi", which are known junk items
  Peter said are fine to leave with real timestamps from actual test
  fires. Real assignments/tasks/events still need restoring.
- Never commit or expose .env, client_secret.json, or the generated
  plist. Run the content-level credential scan in the README before any
  push — the repo is public.
- Update CLAUDE.md's School Sync section AND this file with whatever
  changes. Don't leave either stale.
````

---

## Maintaining this file

When you finish a session, update three things here so the next one
starts from truth rather than from a snapshot that has quietly rotted:

1. **The commit hash and test count** in the header and status block.
2. **The verified / never-run split.** This is the most valuable line in
   the file — it is the difference between "the code exists" and "the
   code works", and those have been very different things in this
   project. Note that "verified" now has a second axis: *verified
   locally* is not *verified in the cloud*, and `classroom_scan` and
   `gmail_scan` only ever run in the cloud.
3. **The blocking item.** If nothing is blocking, say so explicitly
   rather than deleting the section; "nothing is blocking" is
   information.
