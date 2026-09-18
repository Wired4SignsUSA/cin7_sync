#!/usr/bin/env python
"""inngest_worker.py — Inngest-scheduled background jobs for the Slack worker.

Phase 1 of the Inngest migration (2026-09-16). This replaces the
`seconds_since_X >= N` timer blocks in slack_loop.sh with Inngest cron
functions. Each function shells out to the SAME script the bash block ran,
so no job logic moves — only the scheduling, retry, concurrency and
observability layer changes.

What Inngest gives us over the bash loop
----------------------------------------
* Schedules survive restarts. The bash timers were relative to boot, so
  every redeploy either re-fired everything at once (the 2026-09-03 OOM
  crash loop) or pushed daily work to "tomorrow".
* Per-run history, output and failure alerts in the Inngest dashboard,
  instead of `|| echo failed (continuing)` into a disk log.
* Retries with backoff on non-zero exit, and one-at-a-time guarantees per
  job (Concurrency limit=1) plus a worker-wide cap that mirrors BG_MAX_JOBS.
* A shared "cin7" concurrency key so the long CIN7 pulls never overlap.

How it runs
-----------
`python inngest_worker.py` opens a persistent OUTBOUND connection to Inngest
(Inngest "Connect"). The worker is a Render background worker with no
inbound port, so the usual HTTP `serve` endpoint is not an option here.
slack_loop.sh starts this process under supervision when
INNGEST_EVENT_KEY and INNGEST_SIGNING_KEY are set, and its own `_run_bg`
/ `_run_fast` helpers become no-ops so the same job never runs twice.

Set INNGEST_JOBS=0 to fall back to the bash timers without a code change.

Env-var gates, mirror gates and the MemAvailable admission check are all
preserved: a gated job runs, notices its gate is closed and returns
"skipped" (visible in the dashboard) rather than silently not existing.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import os
import pathlib
import re
import shlex
import socket
import subprocess
import sys
import typing

import inngest

APP_ID = os.environ.get("INNGEST_APP_ID", "cin7-sync-worker")
DATA_DIR = pathlib.Path(os.environ.get("DATA_DIR", "/data"))
LOG_PATH = DATA_DIR / "output" / "inngest_worker.log"

# Mirrors slack_loop.sh: at most this many heavy jobs at once on the 2 GB
# worker, and never start one when the box is already tight on memory.
WORKER_MAX_JOBS = int(os.environ.get("BG_MAX_JOBS", "2"))
MIN_AVAILABLE_MB = int(os.environ.get("BG_MIN_AVAILABLE_MB", "500"))
FAST_TIMEOUT_S = 240            # was `timeout 240` in _run_fast
DEFAULT_TIMEOUT_S = 3 * 3600    # generous: BOM sync ~1h, dim weekly 5-10 min
LONG_TIMEOUT_S = 8 * 3600       # 730-day salelines backfill

CIN7_ENV = ("CIN7_ACCOUNT_ID", "CIN7_APPLICATION_KEY")
SHOPIFY_ENV = ("SHOPIFY_DOMAIN", "SHOPIFY_ACCESS_TOKEN")
GOOGLE_OAUTH_ENV = (
    "GOOGLE_ADS_CLIENT_ID",
    "GOOGLE_ADS_CLIENT_SECRET",
    "GOOGLE_ADS_REFRESH_TOKEN",
)


def _stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(msg: str) -> None:
    line = f"[{_stamp()}] {msg}"
    print(line, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def available_mb() -> int:
    """Linux MemAvailable in MB; a huge number when unreadable so a missing
    /proc never blocks work (same contract as _bg_available_mb)."""
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 999_999


def env_present(*names: str) -> bool:
    return all(os.environ.get(n) for n in names)


def mirror_available() -> bool:
    """True when the worker reads the dashboard's datasets from Postgres
    (WORKER_DATA_FROM_DB=1 and dataset_mirror.py status exits 0). Jobs that
    only exist for the legacy own-sync path skip when this is True."""
    if os.environ.get("WORKER_DATA_FROM_DB", "1") != "1":
        return False
    try:
        r = subprocess.run(
            [sys.executable, "dataset_mirror.py", "status"],
            capture_output=True, timeout=120,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# ---------------------------------------------------------------------------
# Job registry
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class Job:
    name: str                       # fn_id; matches the old _run_bg name
    cron: str                       # Inngest cron, optional `TZ=... ` prefix
    cmd: str                        # shell command, run from the repo root
    env: tuple[str, ...] = ()       # all must be non-empty or the job skips
    skip_if_mirror: bool = False    # legacy own-sync jobs only
    cin7: bool = False              # serialise behind the shared "cin7" key
    fast: bool = False              # light poll: 240s timeout, no worker cap
    timeout_s: int = DEFAULT_TIMEOUT_S
    retries: int = 2
    note: str = ""

    @property
    def heavy(self) -> bool:
        """Daily/weekly/monthly jobs count towards the worker-wide cap;
        sub-hourly polls (retries=0/1, minute-based cron) do not."""
        minute = self.cron.split()[-5]
        return "/" not in minute and "," not in minute and "-" not in minute


def _hour_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _locator_audit_cmd() -> str:
    ch = os.environ.get("SLACK_LOCATOR_AUDIT_CHANNEL_ID")
    base = "python stock_locator_audit.py post-summary"
    return f"{base} --channel-id {shlex.quote(ch)}" if ch else base


# Daily jobs are spread over 05:00-10:59 UTC (01:00-06:59 ET) so they never
# all start together and are done before the team's day. Polls keep the
# cadence the bash loop had. Cron minutes are staggered on purpose.
JOBS: list[Job] = [
    # --- frequent polls (were _run_bg/_run_fast with 3-30 min timers) ------
    Job("fablab_assembly_check_replies", "*/3 * * * *",
        "python fablab_assemblies.py check-replies", fast=True, retries=0),
    Job("fablab_assembly_check_po", "*/5 * * * *",
        "python fablab_assemblies.py check-po", fast=True, retries=0),
    Job("fablab_alert_check_replies", "1-59/5 * * * *",
        "python fablab_stock_alert.py check-replies", retries=0),
    Job("po_dispatch_reminder", "2-59/5 * * * *",
        "python po_dispatch_reminder.py daily",
        env=("SLACK_FULFILLMENT_CHANNEL_ID",), retries=0),
    Job("dropship_backorder", "3-59/5 * * * *",
        "python dropship_backorder.py daily",
        env=("SLACK_PURCHASE_BACKORDER_CHANNEL_ID",), retries=0),
    Job("stock_issues_check_replies", "4-59/5 * * * *",
        "python stock_issues_handler.py check-replies", retries=0),
    Job("bis_arrivals", "*/5 * * * *",
        "python back_in_stock_handler.py check-arrivals", retries=0),
    Job("stock_issues_escalate", "*/10 * * * *",
        "python stock_issues_handler.py escalate",
        env=("SLACK_STOCKKEEPER_DM_CHANNEL_ID",), retries=0),
    Job("notion_pull", "7,37 * * * *",
        "python notion_sync.py pull-playbooks",
        env=("NOTION_INTEGRATION_SECRET",), retries=1),
    Job("shipping_margin_monitor", "13,43 * * * *",
        "python shipping_margin_monitor.py daily",
        env=("SLACK_SHIPPING_ISSUES_CHANNEL_ID",), retries=1),

    # --- daily -------------------------------------------------------------
    Job("google_ads_sync", "0 5 * * *",
        "python google_ads_sync.py recent --days 7 && "
        "python google_ads_sync.py per-sku --days 7",
        env=("GOOGLE_ADS_DEVELOPER_TOKEN", "GOOGLE_ADS_CUSTOMER_ID")
        + GOOGLE_OAUTH_ENV),
    Job("ga4_sync", "20 5 * * *",
        "python ga4_sync.py recent --days 7",
        env=("GA4_PROPERTY_ID",) + GOOGLE_OAUTH_ENV),
    Job("merchant_sync", "40 5 * * *",
        "python merchant_sync.py daily --days 7",
        env=("GOOGLE_MERCHANT_ID",) + GOOGLE_OAUTH_ENV),
    Job("klaviyo_sync", "0 6 * * *",
        "python klaviyo_sync.py recent --days 90", env=("KLAVIYO_API_KEY",)),
    Job("reviewsio_sync", "20 6 * * *",
        "python reviewsio_sync.py recent --days 30",
        env=("REVIEWSIO_API_KEY", "REVIEWSIO_STORE_ID")),
    Job("fablab_corner_autotag", "40 6 * * *",
        "python fablab_corner_autotag.py run", env=CIN7_ENV, cin7=True),
    Job("fablab_stock_alert", "0 7 * * *",
        "python fablab_stock_alert.py run", cin7=True),
    Job("finishing_stock_alert", "10 7 * * *",
        "python fablab_stock_alert.py run --flow finishing", cin7=True,
        note="All Star finishing SKUs below reorder level -> "
             "#powdercoating-anodize-control (James 2026-09-18)"),
    Job("qbo_monthly_pl", "0 8 * * *", "python qbo_monthly_pl.py sync"),
    Job("shopify_discounts", "20 8 * * *",
        "python shopify_discounts.py sync", env=SHOPIFY_ENV),
    Job("shopify_sync", "40 8 * * *", "python shopify_sync.py",
        env=SHOPIFY_ENV,
        note="24h fallback for daily_sync.sh's 02:00 UTC run"),
    Job("bot_self_improvement", "0 9 * * *",
        "python bot_self_improvement.py daily --days 7"),
    Job("notion_push_slow_movers", "0 10 * * *",
        "python notion_sync.py slow-movers", env=("NOTION_INTEGRATION_SECRET",)),
    Job("notion_pull_dimensions", "20 10 * * *",
        "python notion_sync.py pull-product-dimensions",
        env=("NOTION_INTEGRATION_SECRET",)),
    Job("stock_locator_audit",
        f"TZ=America/New_York 30 {_hour_env('LOCATOR_AUDIT_MORNING_HOUR_ET', 7)} * * *",
        _locator_audit_cmd(),
        env=("SLACK_STOCK_ISSUES_CHANNEL_ID",), retries=1,
        note="read-only BOM bin-mismatch audit; channel override via "
             "SLACK_LOCATOR_AUDIT_CHANNEL_ID"),
    Job("stock_issues_morning",
        f"TZ=America/New_York 30 {_hour_env('STOCK_ISSUE_MORNING_HOUR_ET', 8)} * * *",
        "python stock_issues_handler.py morning-summary",
        env=("SLACK_STOCK_ISSUES_CHANNEL_ID",), retries=1),

    # --- weekly / monthly --------------------------------------------------
    Job("cin7_boms", "0 3 * * 0", "python cin7_sync.py boms",
        env=CIN7_ENV, skip_if_mirror=True, cin7=True, retries=1,
        note="legacy own-sync path only; dashboard owns BOMs when mirrored"),
    Job("ip_lead_times_sync", "0 4 * * 0", "python ip_lead_times.py sync",
        env=("IP_API_KEY", "IP_ACCOUNT")),
    Job("dim_weekly", "0 11 * * 0",
        "python extract_dimensions.py weekly-new-products",
        env=SHOPIFY_ENV + ("ANTHROPIC_API_KEY",)),
    Job("semrush_sync", "40 6 * * 1",
        "python semrush_sync.py weekly --limit 500", env=("SEMRUSH_API_KEY",)),
    Job("worker_salelines_backfill", "0 3 1 * *",
        "python cin7_sync.py salelines --days 730",
        env=CIN7_ENV, skip_if_mirror=True, cin7=True, retries=1,
        timeout_s=LONG_TIMEOUT_S,
        note="legacy own-sync path only"),
]

# The daily refresh chain (was an inline subshell in slack_loop.sh, PID
# /tmp/slack_loop_bg/dim_refresh.pid) followed by housekeeping_audit. It
# is the one multi-step job, so it is built explicitly below.
DAILY_REFRESH_CRON = "0 7 * * *"


# ---------------------------------------------------------------------------
# Execution helpers
# ---------------------------------------------------------------------------
class JobFailed(Exception):
    """Non-zero exit; Inngest retries according to the job's `retries`."""


def run_cmd(name: str, cmd: str, timeout_s: int) -> dict[str, typing.Any]:
    """Run `cmd` from the repo root, capture the tail of its output, and
    raise JobFailed on non-zero exit so Inngest can retry."""
    started = dt.datetime.now(dt.timezone.utc)
    # Run job scripts with the interpreter this worker runs under, so the
    # jobs see the same site-packages regardless of what `python` on PATH is.
    cmd = re.sub(r"(^|&&\s*)python\s", rf"\1{shlex.quote(sys.executable)} ", cmd)
    _log(f"[{name}] start: {cmd}")
    try:
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=timeout_s, cwd=pathlib.Path(__file__).resolve().parent,
        )
    except subprocess.TimeoutExpired as exc:
        _log(f"[{name}] TIMEOUT after {timeout_s}s")
        raise JobFailed(f"{name} timed out after {timeout_s}s") from exc
    secs = (dt.datetime.now(dt.timezone.utc) - started).total_seconds()
    tail = (proc.stdout or "")[-4000:]
    err_tail = (proc.stderr or "")[-4000:]
    # Keep the disk log useful: the same lines the bash loop used to write.
    if proc.stdout:
        _log(f"[{name}] stdout:\n{tail}")
    if proc.stderr:
        _log(f"[{name}] stderr:\n{err_tail}")
    _log(f"[{name}] done rc={proc.returncode} in {secs:.0f}s")
    result = {
        "rc": proc.returncode, "seconds": round(secs), "cmd": cmd,
        "stdout_tail": tail, "stderr_tail": err_tail,
    }
    if proc.returncode != 0:
        raise JobFailed(
            f"{name} exited {proc.returncode}: {err_tail[-500:] or tail[-500:]}"
        )
    return result


def admission_check(name: str, *, defer: bool = True) -> str | None:
    """Memory guard — the Inngest equivalent of _run_bg queuing a job until
    there is room. With defer=True the run is retried in 2 minutes; with
    defer=False (frequent polls) the reason is returned so the caller can
    skip this tick and let the next cron fire instead."""
    avail = available_mb()
    if avail >= MIN_AVAILABLE_MB:
        return None
    reason = f"low memory: MemAvailable {avail}MB < {MIN_AVAILABLE_MB}MB"
    _log(f"[{name}] deferred: {reason}")
    if defer:
        raise inngest.RetryAfterError(
            reason, retry_after=dt.timedelta(minutes=2), quiet=True)
    return reason


def make_handler(job: Job) -> typing.Callable[[inngest.ContextSync], typing.Any]:
    def handler(ctx: inngest.ContextSync) -> typing.Any:
        if job.env and not env_present(*job.env):
            missing = [n for n in job.env if not os.environ.get(n)]
            return {"skipped": f"env not set: {', '.join(missing)}"}
        if job.skip_if_mirror and ctx.step.run("mirror-status", mirror_available):
            return {"skipped": "data comes from shared DB mirror"}
        deferred = admission_check(job.name, defer=job.retries > 0)
        if deferred:
            return {"skipped": deferred}
        return ctx.step.run("run", run_cmd, job.name, job.cmd, job.timeout_s)

    handler.__name__ = f"job_{job.name}"
    return handler


def build_functions(client: inngest.Inngest) -> list[inngest.Function[typing.Any]]:
    fns: list[inngest.Function[typing.Any]] = []
    for job in JOBS:
        concurrency = [inngest.Concurrency(limit=1)]
        if job.cin7:
            concurrency.append(
                inngest.Concurrency(key='"cin7"', limit=1, scope="env"))
        elif job.heavy:
            # Worker-wide cap for the daily/weekly pulls (BG_MAX_JOBS). The
            # frequent polls stay outside it so a 1h BOM sync can never
            # starve the 5-minute Slack-reply checks.
            concurrency.append(
                inngest.Concurrency(key='"worker"', limit=WORKER_MAX_JOBS,
                                    scope="env"))
        timeout_s = FAST_TIMEOUT_S if job.fast else job.timeout_s
        fn = client.create_function(
            fn_id=job.name,
            name=job.name,
            trigger=inngest.TriggerCron(cron=job.cron),
            concurrency=concurrency,
            retries=job.retries,
            timeouts=inngest.Timeouts(finish=dt.timedelta(seconds=timeout_s + 300)),
        )(make_handler(dataclasses.replace(job, timeout_s=timeout_s)))
        fns.append(fn)

    fns.append(client.create_function(
        fn_id="worker_daily_refresh",
        name="worker_daily_refresh",
        trigger=inngest.TriggerCron(cron=DAILY_REFRESH_CRON),
        concurrency=[inngest.Concurrency(limit=1),
                     inngest.Concurrency(key='"cin7"', limit=1, scope="env")],
        retries=1,
        timeouts=inngest.Timeouts(finish=dt.timedelta(hours=6)),
    )(daily_refresh))
    return fns


def daily_refresh(ctx: inngest.ContextSync) -> dict[str, typing.Any]:
    """Port of the slack_loop.sh daily refresh chain + housekeeping audit.
    Each stage is its own step, so a CIN7 timeout in stage 2 resumes at
    stage 2 instead of redoing the products pull."""
    out: dict[str, typing.Any] = {}
    admission_check("worker_daily_refresh")
    mirrored = ctx.step.run("mirror-status", mirror_available)
    out["mirror"] = mirrored
    if not mirrored and env_present(*CIN7_ENV):
        for step, cmd in (
            ("cin7-products", "python cin7_sync.py products"),
            ("cin7-salelines-30d", "python cin7_sync.py salelines --days 30"),
            ("cin7-sales-365d", "python cin7_sync.py sales --days 365"),
            ("cin7-purchaselines-30d", "python cin7_sync.py purchaselines --days 30"),
        ):
            out[step] = ctx.step.run(step, run_cmd, step, cmd, DEFAULT_TIMEOUT_S)
    if not mirrored and env_present("SHIPSTATION_API_KEY"):
        out["shipstation-30d"] = ctx.step.run(
            "shipstation-30d", run_cmd, "shipstation-30d",
            "python shipstation_sync.py recent --days 30", DEFAULT_TIMEOUT_S)
    if env_present(*SHOPIFY_ENV):
        out["dim-refresh-classifications"] = ctx.step.run(
            "dim-refresh-classifications", run_cmd, "dim-refresh-classifications",
            "python extract_dimensions.py refresh-classifications", DEFAULT_TIMEOUT_S)
    # Informational; always exits 0 in the bash loop too.
    out["housekeeping-audit"] = ctx.step.run(
        "housekeeping-audit", run_cmd, "housekeeping-audit",
        f"python housekeeping_audit.py --verbose --log "
        f"{shlex.quote(str(DATA_DIR / 'output' / 'housekeeping.log'))} || true",
        1800)
    return out


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def build_client() -> inngest.Inngest:
    return inngest.Inngest(
        app_id=APP_ID,
        app_version=os.environ.get("RENDER_GIT_COMMIT", "")[:7] or None,
        is_production=os.environ.get("INNGEST_DEV") is None,
    )


def main() -> int:
    if "--list" in sys.argv:
        for job in JOBS:
            print(f"{job.name:32} {job.cron:36} {job.cmd}")
        print(f"{'worker_daily_refresh':32} {DAILY_REFRESH_CRON:36} (multi-step)")
        return 0
    if not env_present("INNGEST_EVENT_KEY", "INNGEST_SIGNING_KEY") \
            and os.environ.get("INNGEST_DEV") is None:
        _log("INNGEST_EVENT_KEY / INNGEST_SIGNING_KEY not set — exiting")
        return 2

    from inngest.connect import connect

    client = build_client()
    fns = build_functions(client)
    instance = os.environ.get("RENDER_SERVICE_NAME") or socket.gethostname()
    _log(f"connecting app={APP_ID} instance={instance} functions={len(fns)}")
    conn = connect([(client, fns)], instance_id=instance,
                   max_worker_concurrency=WORKER_MAX_JOBS + 2)
    import asyncio
    asyncio.run(conn.start())
    _log("connection closed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
