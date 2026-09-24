# Inngest — scheduled jobs for the Slack worker

Phase 1 (2026-09-16). The `cin7-sync-slack-bot` worker's timer-driven jobs
run as Inngest cron functions defined in `inngest_worker.py`. Job *logic*
is unchanged — every function shells out to the same script the bash block
in `slack_loop.sh` used to run.

## Why

`slack_loop.sh` grew into a hand-built scheduler: 30 `seconds_since_X`
timers, disk marker files for once-a-day posts, a PID/queue admission
controller and `|| echo failed (continuing)` error handling. Timers were
relative to boot, so redeploys either fired everything at once (OOM crash
loop, 2026-09-03) or pushed daily work to "tomorrow". Failures went to a
disk log nobody reads. Inngest gives real cron schedules, retries,
per-job concurrency, a shared CIN7 lock and a run history UI.

## How it runs

* `inngest_worker.py` opens an **outbound** Inngest Connect session
  (websocket). The worker has no inbound port, so HTTP `serve` is not used.
* `slack_loop.sh` starts it as a supervised child when
  `INNGEST_EVENT_KEY` and `INNGEST_SIGNING_KEY` are set and `INNGEST_JOBS`
  is not `0`. In that mode `_run_bg`/`_run_fast` are no-ops and the inline
  daily-refresh + housekeeping blocks are skipped, so nothing runs twice.
* Slack poll / listener / dataset-mirror pull stay in the bash loop — they
  are the bot, not scheduled jobs.

## Env (Render `cin7-shared` group)

| Var | Purpose |
| --- | --- |
| `INNGEST_EVENT_KEY` | Inngest → Manage → Event Keys (Production) |
| `INNGEST_SIGNING_KEY` | Inngest → Manage → Signing Key |
| `INNGEST_JOBS` | `0` = kill switch: fall back to bash timers, no code change |
| `INNGEST_APP_ID` | optional, default `cin7-sync-worker` |
| `BG_MAX_JOBS`, `BG_MIN_AVAILABLE_MB` | reused: worker-wide cap + memory guard |

Env-group edits do **not** redeploy; trigger a deploy after adding keys.

## Semantics preserved

| bash | Inngest |
| --- | --- |
| env-var gate (`[ -n "$X" ]`) | function runs and returns `{"skipped": "env not set: X"}` |
| `! _mirror_available` | `skip_if_mirror` step → `{"skipped": "data comes from shared DB mirror"}` |
| PID file (no self-overlap) | `Concurrency(limit=1)` per function |
| `BG_MAX_JOBS` | `Concurrency(key="worker", limit=BG_MAX_JOBS, scope=env)` on heavy jobs |
| `BG_MIN_AVAILABLE_MB` queueing | `RetryAfterError(2 min)`; frequent polls skip the tick instead |
| `timeout 240` on `_run_fast` | `fast=True` → 240 s subprocess timeout |
| CIN7 rate budget | `Concurrency(key="cin7", limit=1)` on every CIN7-writing/pulling job |
| marker files for morning posts | `TZ=America/New_York 30 8 * * *` cron — fires once |

Daily jobs are staggered 05:00–10:59 UTC; polls keep their 3/5/10/30-min
cadence with offset minutes. `python inngest_worker.py --list` prints the
schedule.

## Operating

* Dashboard: Inngest → Functions, app `cin7-sync-worker`. Every run shows
  the command, exit code and stdout/stderr tail.
* Disk log mirror: `/data/output/inngest_worker.log` on the worker.
* Trigger a job manually: Inngest → Functions → *name* → Invoke.
* Roll back: set `INNGEST_JOBS=0`, redeploy. Bash timers resume.
* Adding a job: append a `Job(...)` to `JOBS` in `inngest_worker.py`.
  `tests/test_inngest_worker.py` fails if a `_run_bg` name in
  `slack_loop.sh` has no matching entry.

## Phase 2 — web service nightly sync (2026-09-23)

`inngest_sync.py` (app id `cin7-sync-web`) replaces the scheduling half of
`sync_loop.sh` on `wired4signs-app`. `start.sh` runs it instead of
`sync_loop.sh` when the same two keys are set and `INNGEST_SYNC` is not `0`.

| bash (`sync_loop.sh` / `daily_sync.sh`) | Inngest |
| --- | --- |
| sleep until `SYNC_HOUR_UTC`, recomputed every boot | `daily_sync` cron `0 {SYNC_HOUR_UTC} * * *` |
| 21 commands, `\|\| echo FAILED (continuing)` | one step per command; retried once, then recorded in `failed_steps` and the run continues |
| boot catch-up runs `daily_sync.sh` inline | event `cin7-sync-web/daily_sync.requested` (id `catchup-YYYY-MM-DD`, so repeat deploys = one run) |
| `verify_critical_csv` → exit 1 | `verify-critical-feeds` step; stale → run marked failed (NonRetriableError) and warm skipped |
| `_start_warm_engine` (lock, guard, timeout) | `warm-engine` step, same lock/env/timeout; 30-min delay on catch-up runs |
| Friday / 13th / 15th / 1st blocks + `/data/.last_*` markers | functions on `cin7-sync-web/daily_sync.finished` filtered by date, idempotent per day/month |

* Runs are exclusive (`Singleton(mode="skip")`) — Inngest `Concurrency`
  limits steps, not runs, so without it two runs interleave.
* The worker's env-scoped `"cin7"` key is deliberately **not** used: the
  nightly run is 2–3 h and would starve the worker's CIN7 polls.
* Extras fire only after scheduled runs (as in bash). The 60-day sale-lines
  refresh is the 13th only (bash also retried on the 14th if missed); the
  PDF and Friday email never retry, to avoid double posts.
* Logs: same `/data/output/daily_sync.log` and `sync_loop.log`.
* Safety net: if `inngest_sync.py` dies within 2 min three times in a row,
  `start.sh` falls back to `sync_loop.sh`.
* Roll back: `INNGEST_SYNC=0`, redeploy the web service.
* Adding a nightly step: add the command to **both** `daily_sync.sh` and
  `STEPS`; `tests/test_inngest_sync.py` fails if they differ.
* Manual run: Inngest → `cin7-sync-web` → `daily_sync` → Invoke, or send
  `cin7-sync-web/daily_sync.requested` with `{"reason": "extras"}` to also
  fire the date-matched extras.

`nearsync` stays in bash.

### Detached steps (2026-09-24)

The first cron run (2026-09-24) died with `request_duration_too_long`:
Inngest caps one step request at a few hours (~3 h observed) and that
error ends the whole run, not just the step. Every nightly/extra command
now runs detached: `<step>-start` launches it (`launch_job`, own
session, exit code written to `/data/output/inngest_jobs/<job>.rc`),
then `<step>-wait-N` sleeps (15 s → 5 min → 10 min) and `<step>-poll-N`
reads the result. A job launched by a previous container (redeploy) is
"lost" and relaunched once (`-retry1`); CIN7 checkpoints resume it.
`INNGEST_SYNC_STEP_TIMEOUT_S` (5 h) is now a real kill timeout.

Root cause of the long night: `cin7_sync.py assemblies --days 30`
re-fetched every completed FG task in a ~210-day candidate window
(~10k detail calls, ~7 h). It now caches task details in
`/data/output/.assembly_detail_cache.json` (key Date|Quantity|ProductCode,
14-day TTL for in-window tasks; `CIN7_ASSEMBLY_DETAIL_CACHE=0` disables).
The first run after deploy warms the cache (still ~7 h); later runs
fetch only new tasks.
