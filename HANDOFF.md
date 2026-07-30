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

Last updated: 2026-07-30, built on commit 5e81a19 — this session's
changes (`Class` -> `For` category rename + ntfy tag fix) are about to
be committed on top of it; check `git log -1` for the real current hash.

---

````
You're picking up ~/school-sync, a Notion-driven school assignment system
(Notion → Google Calendar + phone push via ntfy, plus Gmail/Classroom
capture). CLAUDE.md auto-loads and has the full architecture, live schema,
every non-obvious decision, and every known gap — read the "School Sync"
section in full before touching anything. It was rewritten at the end of
the last session and is accurate. Don't rediscover what's already there.

## Where things actually stand

Repo: https://github.com/PeterDaOne/school-sync (public). 190
stdlib-unittest tests (up from 171 this session), green locally and in
CI. launchd job loaded and healthy under the new code.

**Verified working against real data:** the whole cloud path (secrets,
dispatch-based sub-6-min latency, Calendar sync), the reminder engine's
gating logic (quiet hours, is_complete, capture), and — as of this
session — the ENTIRE rewritten cadence and message system, live:
- The continuous formula's real production output was checked against
  two live items (Task/High/overdue → ~0.5-0.6h interval, exactly the
  "nag as soon as possible" behavior Peter asked for).
- A real send caught a real bug: the class emoji in the ntfy Title
  header broke on `UnicodeEncodeError` (http.client latin-1-encodes str
  headers by default) — fixed by sending Title as UTF-8 bytes, confirmed
  against a second live send afterward (emoji rendered correctly).
- Polled ntfy directly after sending to confirm the actual delivered
  title/body/priority/tags/click matched what was intended, not just
  what the code claimed to send.
- Two consecutive local_sync passes ran clean after reloading launchd
  with the new code + regenerated plist.

**Also verified live 2026-07-30:** the `For` rename preserved every
value on all 15 real pages; the notification title/body renders
correctly for all 10 live incomplete items; three real ntfy sends came
back with the new emoji intact and the `Tags` header correctly ABSENT;
both entrypoints ran clean end to end; launchd reloaded and fired.

**Written but NEVER run against real data:** the mark-done button itself
(NTFY_COMMAND_TOPIC isn't set up yet — that's a step only Peter can do,
same pattern as every other secret this project has needed). The whole
capture layer (gmail_scan.py, classroom_scan.py) — its `For` call sites
were updated and the non-class-category exclusion is unit-tested against
the live option list, but neither sweep has still ever created a real
item; four bugs were found in it by code-tracing alone, assume more
exist.

## Nothing is blocking (see the agenda below for what's next)

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

Carried over from 2026-07-30, which finished items 0 and 1 and never
started these. The ordering note still applies: 2 comes before 3
because defining scenario behavior will change what I want out of the
capture layer, and if the session runs short, push 3 rather than 4.

**1. Define how the workflow should behave for specific scenarios.**
This is a design conversation before any code. I want to walk through
real situations — what happens when I add something last-minute, when
something's overdue for a week, when I have five things due the same
day, when I mark something done from my phone mid-class, etc. — and
decide what the system SHOULD do in each. Ask me about the scenarios
that matter rather than assuming.

One concrete input for that conversation, observed live 2026-07-30:
**the quiet-hours release is still a burst.** Three notifications landed
at 05:00:48, :49 and :50 — one second apart. MAX_NOTIFICATIONS_PER_PASS
capped the count at 3, but jitter can't separate them because they
weren't phase-locked, they were all *held* overnight and released in the
same pass. That's a different problem from the one the jitter solved,
and it's a scenario worth deciding on deliberately. Related: the same
item ("The Odessy") fired 4x in one day with byte-identical text — no
escalation, no sense of "this is the 4th time".

**2. Test and audit the capture layer** (gmail_scan.py,
classroom_scan.py). Still the largest untested surface in the project:
written, never run against real data, and four bugs were already found
in it by code-tracing alone — assume more exist.

   HEADS UP, this is partly blocked and you should say so early rather
   than working around it silently: gmail_scan needs a real
   ANTHROPIC_API_KEY (still the literal placeholder sk-ant-xxxx) and
   classroom_scan needs a Google account with actual courses (the OAuth
   account, petadaone@gmail.com, is my personal one and has zero —
   verified live, courses.list just returns empty). Tell me what you CAN
   meaningfully test without those, what needs me to provide something
   first, and what it'd take to test it properly.

   Note: don't push me to buy the Anthropic key before item 3 decides
   whether Gmail capture earns its place. Paying to unblock a feature
   that might get deleted is backwards.

**3. Then step back and assess the whole system as a productivity tool,
not as code.** This is the part I care most about. Does this actually
propel me toward my goals, or is it just technically impressive? What
should change, get added, or get REMOVED to make it more effective? Be
honest — if something is over-engineered, unused, or actively creating
noise rather than reducing it, say so. I'd rather hear "this feature
isn't earning its place" than have you defend everything that exists.

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
