# Handoff prompt

Copy the block below into a fresh Claude Code session to bring it up to
speed on this project. **Trim the "What I want you to work on" section to
a single item first** — leaving several invites a session that
half-finishes each one.

Keep this file current. It is the fast path back into the project after a
context reset, and a stale handoff is worse than none: it will be trusted.

Last updated: 2026-07-29 (commit 5f3f28e + uncommitted changes to
shared/pipeline.py and a new tests/test_pipeline.py)

---

````
You're picking up ~/school-sync, a Notion-driven school assignment system
(Notion → Google Calendar + phone push via ntfy, plus Gmail/Classroom
capture). CLAUDE.md auto-loads and has the full architecture, live schema,
every non-obvious decision, and every known gap — read the "School Sync"
section in full before touching anything. It was rewritten at the end of
the last session and is accurate. Don't rediscover what's already there.

## Where things actually stand

Repo: https://github.com/PeterDaOne/school-sync (public), at commit 5f3f28e.
115 stdlib-unittest tests, green in CI. launchd job loaded and healthy.

**Verified working against real data:** the reminder engine end-to-end
(escalating cadence, quiet hours, capture notifications, tap-to-open
links), Notion→Calendar sync with no duplicates, the cloud-vs-local
takeover lag, and timezone handling. **As of 2026-07-29, add the whole
cloud path:** all 7 secrets are in, cloud_sync completes real runs, and
a reminder fired from GitHub Actions to my phone with the launchd job
unloaded (run #21 — push at 18:50:54Z inside the run window).

**Written but NEVER run against real data:** the entire capture layer
(gmail_scan.py, classroom_scan.py). My personal Google account has zero
Classroom courses and ANTHROPIC_API_KEY is still the placeholder, so
neither sweep has processed a single real item. Four bugs were found in
it by code-tracing alone — assume more exist. This is unchanged and is
now the largest untested surface in the project.

## Nothing is blocking

The secrets are in and the cloud path works. Two things are worth
knowing before picking anything up:

1. **GitHub's `cron:` is throttled to ~110 min on public repos** (204
   worst, measured). Fixed 2026-07-29: a cron-job.org job POSTs to the
   workflow_dispatch API every 5 min, which is NOT throttled. Measured
   live at 296s between runs, 39s dispatch→sync-complete. The built-in
   cron stays on as a fallback, and the sync job's `concurrency` block
   is what keeps the two from overlapping — don't remove it. The PAT
   expires ~2026-10-27 and fails silently; latency just degrades.
2. **A failed push used to produce a green run — fixed 2026-07-29.**
   Report.notify_failures now reaches the exit code, so a dropped push
   turns the run red. Verified by reproducing the original failure
   (NTFY_TOPIC="" python3 cloud_sync.py → exit 1). Not yet committed.

## What I want you to work on

[EDIT THIS — pick one:]

1. Add a keepalive so GitHub doesn't disable the scheduled workflow
   after 60 days of no commits — it would go silent in late September
   with no warning.

2. Re-run the Google OAuth consent flow with the added gmail.modify
   scope (README has the command). Without it the Gmail sweep falls back
   to a narrow window; with it, each email is classified exactly once.

## Ground rules that were earned the hard way

- **Never trust a schema or API assumption — verify it live.** This
  codebase has burned time on a Notion property typo, an MCP tool that
  silently "cleaned up" that typo in its own output, an OAuth scope
  Google renames on grant, and a Notion API that silently CREATES any
  select option you hand it. Hit the real APIs with curl or a throwaway
  script before believing anything.
- **local_sync.py runs every 60s via launchd against my real Notion data
  and pushes real notifications to my phone.** Unload the job while
  editing (`launchctl unload ~/Library/LaunchAgents/com.peter.schoolsync.plist`)
  and only reload once verified.
- If you touch real Notion data to test, restore it. "Do dishes", "Pray",
  and "Assinment type shi" are known junk items you can use freely.
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
