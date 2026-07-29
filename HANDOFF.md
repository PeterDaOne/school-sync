# Handoff prompt

Copy the block below into a fresh Claude Code session to bring it up to
speed on this project. **Trim the "What I want you to work on" section to
a single item first** — leaving several invites a session that
half-finishes each one.

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

## Nothing is blocking (see "What I want you to work on" for what's next)

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

## What I want you to work on

[EDIT THIS — pick one, per Peter's stated order:]

1. Testing and auditing the capture layer (gmail_scan.py,
   classroom_scan.py) against real data — this is explicitly what Peter
   said comes after the notification rework. Written but never run for
   real; four known bugs were fixed by code-tracing alone last time,
   more likely exist. Needs a real Gmail account with real school email
   and/or a real Classroom course to test against meaningfully.

2. Set up NTFY_COMMAND_TOPIC with Peter and verify the mark-done button
   end-to-end against a real notification.

3. Add a keepalive so GitHub doesn't disable the scheduled workflow
   after 60 days of no commits — it would go silent in late September
   with no warning. (Old item, still not done, still low urgency.)

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
