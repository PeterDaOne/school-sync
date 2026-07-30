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

Last updated: 2026-07-29, built on commit 38fb2dd — this session's
changes (reminder cadence rewrite + mark-done button) are about to be
committed on top of it; check `git log -1` for the real current hash.

---

````
You're picking up ~/school-sync, a Notion-driven school assignment system
(Notion → Google Calendar + phone push via ntfy, plus Gmail/Classroom
capture). CLAUDE.md auto-loads and has the full architecture, live schema,
every non-obvious decision, and every known gap — read the "School Sync"
section in full before touching anything. It was rewritten at the end of
the last session and is accurate. Don't rediscover what's already there.

## Where things actually stand

Repo: https://github.com/PeterDaOne/school-sync (public). 171
stdlib-unittest tests (up from 115 this session), green locally and in
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

**Written but NEVER run against real data:** the mark-done button itself
(NTFY_COMMAND_TOPIC isn't set up yet — that's a step only Peter can do,
same pattern as every other secret this project has needed). The whole
capture layer (gmail_scan.py, classroom_scan.py) — unchanged this
session, still the largest untested surface in the project; four bugs
were found in it by code-tracing alone, assume more exist.

## Nothing is blocking (see the agenda below for what's next)

## What just shipped (this session, 2026-07-29)

Three things, in priority order Peter set:

1. **Cron latency** (~110min → sub-6min) — external dispatch scheduler,
   already covered in a previous handoff, unchanged since.
2. **Reminder cadence + message rewrite** — the three fixed tiers
   (24h/4h/2h) became a continuous formula (see CLAUDE.md's "Reminder
   engine" section for the exact math) because same-tier items stayed
   phase-locked together forever, causing real observed bursts (5
   pushes in 8 seconds). Priority (High/Medium/Low) is now read and
   actually affects cadence — it was a Notion column nothing looked at
   before. Deterministic per-item jitter + a per-pass cap break up
   bursts. Messages got a full redesign: category + class emoji in the
   ntfy Title, relative time ("due tomorrow at 3pm", "in 3 days") in the
   body instead of an absolute date, urgency-scaled ntfy priority/tags.
3. **"Mark done" notification button** — via a SECOND ntfy topic
   (NTFY_COMMAND_TOPIC) as a write-only command queue, specifically so
   no Notion credential ever has to live inside a notification. Every
   sync pass polls it and marks the referenced page Done; no dedup
   cursor needed since marking Done twice is a no-op.

**What Peter needs to do next, when ready:** set up NTFY_COMMAND_TOPIC
(README "Mark done" section has the exact steps — pick a second random
topic, add to .env, regenerate the plist, add as an optional GitHub
secret) and then tap a real button on a real notification to confirm
the whole loop end to end. Until then the button is silently absent
from every notification (by design — see build_mark_done_action in
shared/notify.py), nothing else is affected.

## What I want to do this session, in order

The notification system was just rewritten and is working well — I'm
happy with it. Treat it as a good baseline to refine, not a problem to
solve.

**0. First, check the loose ends above and tell me which actually
matter.** There are several deferred items (NTFY_COMMAND_TOPIC not set
up yet so the mark-done button is absent, the GitHub Actions keepalive
before workflows auto-disable in late September, the cron-job.org PAT
expiring ~2026-10-27 with a silent failure mode, the gmail.modify OAuth
re-consent, ANTHROPIC_API_KEY still a placeholder). Don't just list them
back at me — tell me which are genuinely worth doing now vs. which can
wait, and flag anything time-sensitive I'd regret missing.

**1. Small tweaks to how notifications look on the lock screen.** Minor
only. Right now: ntfy Title = category + class emoji ("📊 Assignment
overdue"), body = "Class · Name — relative time". Show me what a few
real notifications currently look like end to end (poll ntfy directly,
don't just read the code), then propose small refinements. I'll tell you
what to change from there.

**2. Define how the workflow should behave for specific scenarios.**
This is a design conversation before any code. I want to walk through
real situations — what happens when I add something last-minute, when
something's overdue for a week, when I have five things due the same
day, when I mark something done from my phone mid-class, etc. — and
decide what the system SHOULD do in each. Ask me about the scenarios
that matter rather than assuming.

**3. Test and audit the capture layer** (gmail_scan.py,
classroom_scan.py). This is the largest untested surface in the project:
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

**4. Then step back and assess the whole system as a productivity tool,
not as code.** This is the part I care most about. Does this actually
propel me toward my goals, or is it just technically impressive? What
should change, get added, or get REMOVED to make it more effective? Be
honest — if something is over-engineered, unused, or actively creating
noise rather than reducing it, say so. I'd rather hear "this feature
isn't earning its place" than have you defend everything that exists.

**Ordering note:** 1 and 2 come before 3 and 4 on purpose. Defining the
scenario behavior (2) will likely change what I want out of the capture
layer, so testing it first means auditing against a spec that's about to
move. And if the session starts running out of room, push item 3, not
item 4 — capture-layer auditing is open-ended, and "is this actually
helping me" is the better use of a session.

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
