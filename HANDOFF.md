# Handoff prompt

Copy the block below into a fresh Claude Code session to bring it up to
speed on this project.

**Normally: trim the agenda to a single item first** — leaving several
invites a session that half-finishes each one. The current agenda is a
deliberate exception: it's one coherent arc (refine → define → test →
assess) where each step informs the next, and it carries its own
ordering note about what to drop if time runs short. Go back to
one-item-at-a-time after it.

Keep this file current. It is the fast path back into the project after a
context reset, and a stale handoff is worse than none: it will be trusted.

Last updated: 2026-07-30 (second session that day), on commit 9bba1e1.
Seven commits from this session are COMMITTED BUT NOT PUSHED — check
`git status` and `git log origin/main..HEAD` before assuming the cloud
is running current code.

---

````
You're picking up ~/school-sync, a Notion-driven school assignment system
(Notion → Google Calendar + phone push via ntfy, plus Gmail/Classroom
capture). CLAUDE.md auto-loads and has the full architecture, live schema,
every non-obvious decision, and every known gap — read the "School Sync"
section in full before touching anything. It was rewritten at the end of
the last session and is accurate. Don't rediscover what's already there.

## Where things actually stand

Repo: https://github.com/PeterDaOne/school-sync (public). **229**
stdlib-unittest tests, green locally. launchd loaded and healthy.

**UNPUSHED.** Seven commits sit on local `main`. Until they're pushed,
GitHub Actions runs the OLD cadence and cannot read the renamed `For`
property. This is the first thing to check.

**Verified working against real data:** the whole cloud path (secrets,
dispatch-based sub-6-min latency, Calendar sync); the reminder engine
end to end; the `Class`→`For` rename (all 15 pages kept their values);
the ntfy tag fix; the 2h hard floor; the load-scaling + true-daily-cap
volume work (including a live test that the `Reminders Today` counter
increments same-day, resets across days, and that an exhausted budget
defers instead of sending). **The mark-done button is confirmed working
from Peter's actual phone** — he tapped it on a real reminder.

**Written but NEVER run end to end:** the capture layer. `gmail_scan.py`
has never created a Notion item. `classroom_scan.py` has never created
one either, though as of this session it CAN finally see a real course.

## What just shipped (this session, 2026-07-30)

Agenda items 0 and 1 of the previous session's plan. Items 2, 3 and 4
(scenario design, capture-layer audit, productivity assessment) were NOT
started and carry forward.

1. **Loose-end triage, with the assumptions actually measured.** The
   external dispatch scheduler is healthy: 37 runs, gaps 4.9/5.0/5.1 min
   (min/avg/max) — the PAT is alive. The `schedule` cron delivered 2 runs
   in the same ~1.8 hours, still ~1/hour. And GitHub's 60-day
   auto-disable was checked against the real docs rather than assumed:
   it disables **only the `schedule` trigger**, not `workflow_dispatch`.
   So a keepalive protects only the already-degraded fallback — low
   value on its own. The combination is what matters: schedule
   auto-disables ~Sept 28 (60 days from last repo activity), the PAT
   expires ~Oct 27, and after that BOTH cloud paths are dead at once,
   silently, mid-semester. Setting cron-job.org's failure notification
   is the cheap alarm; it is the one genuinely time-sensitive item.

2. **`Class` -> `For`, expanded to "what is this for?"** — Peter's call.
   Options are now his 8 classes plus School / Personal / Friends /
   Work, so real commitments that belong to no course stop being
   categoryless. Notion property rename via the API, values preserved
   (verified on all 15 pages). Internal key is `category`; a fallback
   read of the old `Class` name guards against hand-editing. The
   important part is `classmap.NON_CLASS_CATEGORIES`: the capture
   resolver fuzzy-matches, so "Personal Finance" would otherwise file
   homework under "Personal" — those four options are excluded from
   matching entirely and are manual-entry-only.

3. **The 🏫 nobody knew about.** Polling the live ntfy topic (rather than
   reading the code) showed ntfy renders any tag matching an emoji short
   code and prepends it to the Title. The default tag was `school` = 🏫,
   so every notification really read `🏫✍️ Assignment reminder`, and
   `🏫🚨✍️ ...` when urgent. Tags now default to empty and the header is
   omitted entirely; `rotating_light` stays only where urgency is real.
   🏫 was reused as the "School" category emoji, where it means
   something. Type-fallback emoji (📝/☑️/📅) added so a categoryless item
   isn't the one bare text title among emoji.

**Still true from 2026-07-29:** the continuous cadence formula, the
message redesign, and the mark-done button all shipped then and are
unchanged.

**What Peter needs to do next, when ready:** set up NTFY_COMMAND_TOPIC
(README "Mark done" section has the exact steps) and tap a real button
on a real notification. Until then the button is silently absent from
every notification (by design), nothing else is affected. Separately:
turn on cron-job.org's "notify me when execution fails".

**Noted, not fixed:** `README.md` lines 7-8 carry the real Notion
database id on a public repo. Not a credential (unusable without the
integration token) and it's his own documentation link, so it was left
alone — but `.env.example` was carrying it too, which is just wrong for
a `.example` file, and that one was replaced with a placeholder.

## What I want to do this session, in order

**0. FIRST, before anything else: print the OAuth re-consent command as
a runnable bash code block so I can actually click it.** Last session it
was printed as command *output* and I couldn't run it. It is the literal
blocker for everything below. The exact scope list lives in README §2 —
read it from there rather than retyping it from memory, and note it now
includes `classroom.coursework.students.readonly`, which the old refresh
token does NOT carry.

After I run it and paste back the refresh token, walk me through
updating it in all three places: `.env`, then `python3 generate_plist.py`
+ reload launchd, then the `GOOGLE_REFRESH_TOKEN` GitHub secret. Missing
the third is a known silent-failure mode in this project.

**1. Then get Classroom capture actually working, end to end.** I have a
real test class on my personal Google account: `AP Language &
Composition Period 3` (course id `871376160217`), which I OWN as the
teacher. Last session fixed `_active_courses` to see teacher-owned
courses and added the scope, but nothing has been run against it yet
because the token predates the scope.

What I want to see: a real assignment I post in that class appears as a
real row in Notion, with the right `For` (it should resolve to "AP
Lang"), the right due date, an `External ID`, and a capture notification
on my phone. Then verify it does NOT duplicate on the next pass.

Assume more bugs. Four were found by code-tracing alone, and this
session found two more the moment real data touched it — a wrong OAuth
scope and a hardcoded `studentId="me"` that made the course invisible
without any error at all.

**2. Audit the rest of the capture layer while you're in there.**
`classroom_scan.py` got its first tests this session (6 cases, all on
`_active_courses`). `_recent_coursework`, `_submitted_coursework_ids`,
and `_due_date_iso` still have none, and `_due_date_iso` does timezone
conversion, which is the single most bug-prone thing in this codebase.

**3. Gmail capture is still blocked and I know it.** `ANTHROPIC_API_KEY`
is still the literal placeholder `sk-ant-xxxx`. Do NOT push me to buy one
yet — I want Classroom proven first. What you CAN do without it: test
query construction, the `school-sync/seen` label logic, External ID
dedup, and `classmap` resolution with a stubbed classifier. Tell me
plainly what that does and doesn't prove.

**Ordering note:** if the session runs short, stop after 1. A working
Classroom capture is worth more than a broad audit of code that still
hasn't run.

## Ground rules that were earned the hard way

- **Never trust a schema or API assumption — verify it live.** This
  codebase has burned time on a Notion property typo, an MCP tool that
  silently "cleaned up" that typo in its own output, an OAuth scope
  Google renames on grant, a Notion API that silently CREATES any
  select option you hand it, and (this session) Python's http.client
  silently latin-1-encoding a UTF-8 emoji into a crash. Hit the real
  APIs — or run the real code against real data — before believing
  anything, especially anything involving Unicode, timezones, or an
  external service's exact wire format.
- **Writing tests can surface real bugs — don't treat test-writing as
  separate from bug-hunting.** This session's event-reminder tests
  caught a real "starts in 1 hour" message firing hours after an
  early-morning event had already happened, because the hour-before
  check had no upper bound. Found by trying to write the test case, not
  by a separate review pass.
- **local_sync.py runs every 60s via launchd against my real Notion data
  and pushes real notifications to my phone.** Unload the job while
  editing (`launchctl unload ~/Library/LaunchAgents/com.peter.schoolsync.plist`)
  and only reload once verified.
- If you touch real Notion data to test, restore it — EXCEPT "Do
  dishes", "Pray", and "Assinment type shi", which are known junk items
  Peter said are fine to leave with real timestamps from actual test
  fires. Real assignments/tasks/events still need restoring.
- Never commit or expose .env, client_secret.json, or the generated
  plist. Run the content-level credential scan in the README before any
  push — the repo is public.
- Update CLAUDE.md's School Sync section with whatever changes, same as
  previous sessions. Don't leave it stale.
````

---

## Maintaining this file

When you finish a session, update three things here so the next one
starts from truth rather than from a snapshot that has quietly rotted:

1. **The commit hash and test count** in the header and status block.
2. **The verified / never-run split.** This is the most valuable line in
   the file — it is the difference between "the code exists" and "the
   code works", and those have been very different things in this
   project.
3. **The blocking item.** If nothing is blocking, say so explicitly
   rather than deleting the section; "nothing is blocking" is
   information.
