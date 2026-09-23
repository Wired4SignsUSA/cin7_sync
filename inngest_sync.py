#!/usr/bin/env python
"""inngest_sync.py — the web service's nightly sync as Inngest functions.

Phase 2 of the Inngest migration (2026-09-23). Replaces the scheduling
half of sync_loop.sh (sleep-until-02:00 loop, boot catch-up, marker-file
monthly jobs) with Inngest functions. Every step shells out to the SAME
command daily_sync.sh runs, in the same order — no sync logic moves.

What changes vs. the bash loop
------------------------------
* ``daily_sync`` is a step function: one Inngest step per command. A
  failed step is retried once (e.g. a CIN7 timeout, or a redeploy killing
  the step mid-run) and, if it still fails, the run records it and moves
  on — the same "FAILED (continuing)" policy as daily_sync.sh, but a
  redeploy at 03:00 no longer loses the rest of the night.
* The 02:00 UTC schedule is a real cron, not a sleep computed at boot.
* Boot catch-up (critical CSVs > SYNC_CATCHUP_STALE_HOURS old) sends an
  event to the same function instead of running the script inline. The
  event id is per-day, so a burst of redeploys triggers one catch-up.
* Friday digest, 13th 60-day sale-lines refresh, 15th PDF and 1st
  Shopify backfill are separate functions fired by the nightly run's
  "finished" event, filtered by date and de-duplicated by Inngest
  idempotency instead of /data marker files.

Unchanged: nearsync stays in bash; daily_sync.sh stays as the fallback.
start.sh runs this process instead of sync_loop.sh when INNGEST_EVENT_KEY
and INNGEST_SIGNING_KEY are set and INNGEST_SYNC is not 0.

Deliberately NOT applied: the worker's env-scoped "cin7" concurrency key.
The nightly run takes 2-3 h and would block the worker's CIN7 polls; the
bash loop never coordinated with the worker either.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import glob
import os
import pathlib
import re
import shlex
import socket
import subprocess
import sys
import typing

import inngest

APP_ID = os.environ.get("INNGEST_SYNC_APP_ID", "cin7-sync-web")
DATA_DIR = pathlib.Path(os.environ.get("DATA_DIR", "/data"))
OUTPUT_DIR = DATA_DIR / "output"
DAILY_LOG = OUTPUT_DIR / "daily_sync.log"       # same file daily_sync.sh wrote
LOOP_LOG = OUTPUT_DIR / "sync_loop.log"         # same file sync_loop.sh wrote
REPO_DIR = pathlib.Path(__file__).resolve().parent

SYNC_HOUR_UTC = int(os.environ.get("SYNC_HOUR_UTC", "2"))
DAILY_CRON = f"0 {SYNC_HOUR_UTC} * * *"
EV_REQUESTED = "cin7-sync-web/daily_sync.requested"
EV_FINISHED = "cin7-sync-web/daily_sync.finished"

STEP_TIMEOUT_S = int(os.environ.get("INNGEST_SYNC_STEP_TIMEOUT_S", str(5 * 3600)))
CRITICAL_MAX_AGE_H = int(os.environ.get("DAILY_SYNC_CRITICAL_MAX_AGE_HOURS", "30"))
CATCHUP_STALE_H = int(os.environ.get("SYNC_CATCHUP_STALE_HOURS", "20"))
WARM_BOOT_DELAY_MIN = int(os.environ.get("WARM_ENGINE_BOOT_DELAY_MIN", "30"))
WARM_TIMEOUT_S = int(os.environ.get("WARM_ENGINE_TIMEOUT_SECONDS", "1200"))
WARM_MIN_MB = os.environ.get("WARM_ENGINE_MIN_AVAILABLE_MB", "2500")

CRITICAL_FEEDS = (
    ("sales_last_30d_*.csv", "sales_last_30d CSV"),
    ("sale_lines_last_30d_*.csv", "sale_lines_last_30d CSV"),
    ("assemblies_last_30d_*.csv", "assemblies_last_30d CSV"),
)

SHOPIFY_ENV = ("SHOPIFY_DOMAIN", "SHOPIFY_ACCESS_TOKEN")
IP_ENV = ("IP_API_KEY", "IP_ACCOUNT")


# ---------------------------------------------------------------------------
# Step registry — mirrors daily_sync.sh top to bottom.
# tests/test_inngest_sync.py fails if a `python ...` command in
# daily_sync.sh has no step here (or vice versa).
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class Step:
    id: str
    cmd: str
    env: tuple[str, ...] = ()               # all non-empty or the step skips
    when: str = "always"                    # "always" | "sunday_or_force"


HOUSEKEEPING_LOG = "${DATA_DIR}/output/housekeeping.log"

STEPS: tuple[Step, ...] = (
    Step("cin7-quick-3d", "CIN7_QUICK_SKIP_ASSEMBLIES=1 python cin7_sync.py quick --days 3"),
    Step("cin7-product-images", "python cin7_sync.py product-images", when="sunday_or_force"),
    Step("cin7-boms", "python cin7_sync.py boms"),
    Step("fablab-bom-audit", "python fablab_bom_audit.py --post"),
    Step("cin7-sales-30d", "python cin7_sync.py sales --days 30"),
    Step("cin7-purchases-30d", "python cin7_sync.py purchases --days 30"),
    Step("cin7-salelines-30d", "python cin7_sync.py salelines --days 30"),
    Step("cin7-assemblies-30d", "python cin7_sync.py assemblies --days 30"),
    Step("cin7-purchaselines-30d", "python cin7_sync.py purchaselines --days 30"),
    Step("cin7-stockadjustments-30d", "python cin7_sync.py stockadjustments --days 30"),
    Step("cin7-stocktransfers-30d", "python cin7_sync.py stocktransfers --days 30"),
    Step("sync-sku-renames", "python sync_sku_renames.py --apply"),
    Step("sync-supplier-names", "python sync_supplier_names.py --apply"),
    Step("auto-finalize-pos", "python auto_finalize_pos.py --apply"),
    Step("shopify-content", "python shopify_sync.py", env=SHOPIFY_ENV),
    Step("shopify-orders-7d", "python shopify_sync.py --orders-recent 7", env=SHOPIFY_ENV),
    Step("ip-sync-notes", "python ip_sync_notes.py", env=IP_ENV),
    Step("ip-pull-alternates", "python ip_pull_alternates.py", env=IP_ENV),
    Step("shipstation-30d", "python shipstation_sync.py recent --days 30",
         env=("SHIPSTATION_API_KEY",)),
    Step("housekeeping-audit",
         f'python housekeeping_audit.py --verbose --log "{HOUSEKEEPING_LOG}"'),
    Step("dataset-mirror-publish", "python dataset_mirror.py publish"),
    Step("publish-monthly-metrics", "python publish_monthly_metrics.py"),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append(path: pathlib.Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(text if text.endswith("\n") else text + "\n")
    except OSError:
        pass


def _log(msg: str, path: pathlib.Path | None = None) -> None:
    line = f"[{_stamp()}] {msg}"
    print(line, flush=True)
    _append(path or LOOP_LOG, line)


def env_present(*names: str) -> bool:
    return all(os.environ.get(n) for n in names)


def step_should_run(step: Step, weekday: int) -> str | None:
    """None if the step runs; otherwise the skip reason (as the bash log said)."""
    if step.env and not env_present(*step.env):
        missing = [n for n in step.env if not os.environ.get(n)]
        return f"env not set: {', '.join(missing)}"
    if step.when == "sunday_or_force" and weekday != 7 \
            and os.environ.get("PRODUCT_IMAGE_SYNC_FORCE", "0") != "1":
        return "weekly Sunday refresh"
    return None


class StepFailed(Exception):
    """Non-zero exit; Inngest retries the step, then the run continues."""


def run_cmd(name: str, cmd: str, timeout_s: int = STEP_TIMEOUT_S,
            log_path: pathlib.Path | None = None,
            extra_env: dict[str, str] | None = None) -> dict[str, typing.Any]:
    """Run `cmd` from the repo root with this interpreter as `python`.
    Full output is appended to `log_path` (as daily_sync.sh did); the
    tail is returned for the Inngest run view. Raises StepFailed on
    non-zero exit or timeout."""
    log_path = log_path or DAILY_LOG
    cmd = re.sub(r"(^|&&\s*|=\S+\s+)python\s",
                 lambda m: f"{m.group(1)}{shlex.quote(sys.executable)} ", cmd)
    env = {**os.environ, "DATA_DIR": str(DATA_DIR), **(extra_env or {})}
    _log(f"{name}: {cmd}", log_path)
    started = dt.datetime.now(dt.timezone.utc)
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=timeout_s, cwd=REPO_DIR, env=env)
    except subprocess.TimeoutExpired as exc:
        _log(f"{name} FAILED: timed out after {timeout_s}s", log_path)
        raise StepFailed(f"{name} timed out after {timeout_s}s") from exc
    secs = round((dt.datetime.now(dt.timezone.utc) - started).total_seconds())
    if proc.stdout:
        _append(log_path, proc.stdout)
    if proc.stderr:
        _append(log_path, proc.stderr)
    tail, err_tail = (proc.stdout or "")[-3000:], (proc.stderr or "")[-3000:]
    if proc.returncode != 0:
        _log(f"{name} FAILED rc={proc.returncode} after {secs}s", log_path)
        raise StepFailed(f"{name} exited {proc.returncode}: "
                         f"{(err_tail or tail)[-500:]}")
    _log(f"{name} done in {secs}s", log_path)
    return {"rc": 0, "seconds": secs, "stdout_tail": tail, "stderr_tail": err_tail}


def feed_ages_hours() -> dict[str, float | None]:
    """Age in hours of the freshest file for each critical feed (None = missing)."""
    now = dt.datetime.now().timestamp()
    ages: dict[str, float | None] = {}
    for pattern, label in CRITICAL_FEEDS:
        files = glob.glob(str(OUTPUT_DIR / pattern))
        if not files:
            ages[label] = None
            continue
        ages[label] = round((now - max(os.path.getmtime(f) for f in files)) / 3600, 1)
    return ages


def stale_feeds(ages: dict[str, float | None], max_age_h: float) -> list[str]:
    return [label for label, age in ages.items() if age is None or age >= max_age_h]


def warm_engine(reason: str) -> dict[str, typing.Any]:
    """Same contract as sync_loop.sh `_start_warm_engine`: shared atomic
    lock, memory guard inside warm_engine.py, hard timeout. Never raises —
    a skipped/failed warm must not retry (memory) or fail the sync."""
    import json
    import engine_refresh_lock as erl

    lock = OUTPUT_DIR / "engine_refresh.lock"
    status = OUTPUT_DIR / "engine_refresh_status.json"
    engine_log = OUTPUT_DIR / "engine_refresh.log"
    if not erl.acquire({"started_at": _stamp(), "reason": f"inngest_sync: {reason}"}):
        _log(f"warm_engine already running; skipped ({reason})")
        return {"skipped": "engine refresh already running"}
    try:
        status.write_text(lock.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError:
        pass
    _log(f"warming engine cache ({reason})")
    try:
        res = run_cmd("warm_engine", "python warm_engine.py", WARM_TIMEOUT_S,
                      engine_log, {
                          "ENGINE_REFRESH_LOCK_PATH": str(lock),
                          "ENGINE_REFRESH_STATUS_PATH": str(status),
                          "ENGINE_REFRESH_REASON": reason,
                          "WARM_ENGINE_MIN_AVAILABLE_MB": WARM_MIN_MB,
                      })
        return {"rc": 0, "seconds": res["seconds"], "stdout_tail": res["stdout_tail"][-1500:]}
    except StepFailed as exc:
        erl.release()
        try:
            status.write_text(json.dumps({
                "state": "failed", "reason": reason, "error": str(exc)[:300],
                "updated_at": _stamp()}), encoding="utf-8")
        except OSError:
            pass
        _log(f"warm_engine failed/timed out ({reason}): {exc}")
        return {"failed": str(exc)[:500]}


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------
def run_context(reason: str, run_id: str) -> dict[str, typing.Any]:
    now = dt.datetime.now(dt.timezone.utc)
    _log(f"daily_sync start ({reason}) run={run_id}", DAILY_LOG)
    return {"date": now.strftime("%Y-%m-%d"), "month": now.strftime("%Y-%m"),
            "day": now.day, "weekday": now.isoweekday()}


def daily_sync(ctx: inngest.ContextSync) -> dict[str, typing.Any]:
    event = ctx.event
    reason = (event.data or {}).get("reason") if event.name == EV_REQUESTED else "cron"
    reason = str(reason or "manual")
    # NB: code outside ctx.step.run re-executes on every step replay, so
    # anything with side effects (logging included) lives inside a step.
    rc = ctx.step.run("run-context", run_context, reason, ctx.run_id)

    results: dict[str, typing.Any] = {}
    failed: list[str] = []
    for step in STEPS:
        skip = step_should_run(step, rc["weekday"])
        if skip:
            results[step.id] = {"skipped": skip}
            continue
        try:
            out = ctx.step.run(step.id, run_cmd, step.id, step.cmd)
            results[step.id] = {"rc": 0, "seconds": out["seconds"]}
        except inngest.StepError as exc:
            # Retries exhausted: record and continue, like daily_sync.sh.
            failed.append(step.id)
            results[step.id] = {"failed": exc.message[:300]}

    ages = ctx.step.run("verify-critical-feeds", feed_ages_hours)
    critical = stale_feeds(ages, CRITICAL_MAX_AGE_H)
    not_ready = stale_feeds(ages, CATCHUP_STALE_H)

    if not_ready and os.environ.get("WARM_ENGINE_ALLOW_STALE_INPUTS", "0") != "1":
        results["warm-engine"] = {"skipped": f"core inputs stale: {not_ready}"}
    else:
        if reason == "catch-up" and WARM_BOOT_DELAY_MIN > 0:
            # Same as the bash boot catch-up: don't pile onto Streamlit startup.
            ctx.step.sleep("warm-boot-delay", dt.timedelta(minutes=WARM_BOOT_DELAY_MIN))
        results["warm-engine"] = ctx.step.run(
            "warm-engine", warm_engine, "daily sync completed")

    # Weekly/monthly extras hang off the scheduled run only (the bash
    # catch-up path never ran them either). Idempotency keys stop repeats.
    if reason in ("cron", "extras"):
        ctx.step.send_event("emit-finished", inngest.Event(
            name=EV_FINISHED, data={**rc, "failed_steps": failed}))

    summary = {"reason": reason, "failed_steps": failed,
               "critical_stale": critical, "feed_ages_h": ages, "steps": results}
    ctx.step.run("log-summary", _log,
                 f"daily_sync done ({reason}); failed={failed} "
                 f"critical_stale={critical}", DAILY_LOG)
    if critical:
        # Same as daily_sync.sh exit 1: show the run red in the dashboard.
        raise inngest.NonRetriableError(
            f"critical feeds stale/missing: {critical}; failed steps: {failed}")
    return summary


@dataclasses.dataclass(frozen=True)
class Extra:
    id: str
    expression: str          # CEL on the finished event
    idempotency: str         # CEL key; Inngest drops repeats within 24 h
    cmds: tuple[tuple[str, str], ...]
    retries: int
    env: tuple[str, ...] = ()


EXTRAS: tuple[Extra, ...] = (
    Extra("weekly_slow_movers_email", "event.data.weekday == 5", "event.data.date",
          (("slow-movers-email", "python weekly_slow_movers_email.py"),), retries=0),
    Extra("salelines_60d_before_report", "event.data.day == 13", "event.data.month",
          (("cin7-salelines-60d", "python cin7_sync.py salelines --days 60"),
           ("dataset-mirror-publish", "python dataset_mirror.py publish")), retries=1),
    # retries=0: a retry after a partial success would double-post the PDF.
    Extra("monthly_metrics_report", "event.data.day == 15", "event.data.month",
          (("monthly-metrics-pdf", "python monthly_metrics_report.py"),), retries=0),
    Extra("shopify_orders_full_backfill", "event.data.day == 1", "event.data.month",
          (("shopify-orders-full-730d", "python shopify_sync.py --orders-full 730"),),
          retries=1),
)


def make_extra_handler(extra: Extra) -> typing.Callable[[inngest.ContextSync], typing.Any]:
    def handler(ctx: inngest.ContextSync) -> typing.Any:
        out: dict[str, typing.Any] = {}
        for step_id, cmd in extra.cmds:
            out[step_id] = ctx.step.run(step_id, run_cmd, step_id, cmd,
                                        STEP_TIMEOUT_S, LOOP_LOG)
        return out

    handler.__name__ = f"extra_{extra.id}"
    return handler


def build_functions(client: inngest.Inngest) -> list[inngest.Function[typing.Any]]:
    fns: list[inngest.Function[typing.Any]] = [client.create_function(
        fn_id="daily_sync",
        name="daily_sync (nightly CIN7/Shopify/IP/ShipStation sync)",
        trigger=[inngest.TriggerCron(cron=DAILY_CRON),
                 inngest.TriggerEvent(event=EV_REQUESTED)],
        # Concurrency limits *steps*, not runs: two runs would interleave.
        # Singleton(skip) = at most one nightly run at a time; a catch-up
        # or cron arriving mid-run is dropped (the running one covers it).
        singleton=inngest.Singleton(mode="skip"),
        concurrency=[inngest.Concurrency(limit=1)],
        retries=1,                       # per step
        timeouts=inngest.Timeouts(finish=dt.timedelta(hours=16)),
    )(daily_sync)]
    for extra in EXTRAS:
        fns.append(client.create_function(
            fn_id=extra.id,
            name=extra.id,
            trigger=inngest.TriggerEvent(event=EV_FINISHED, expression=extra.expression),
            idempotency=extra.idempotency,
            singleton=inngest.Singleton(mode="skip"),
            concurrency=[inngest.Concurrency(limit=1)],
            retries=extra.retries,
            timeouts=inngest.Timeouts(finish=dt.timedelta(hours=10)),
        )(make_extra_handler(extra)))
    return fns


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def build_client() -> inngest.Inngest:
    return inngest.Inngest(
        app_id=APP_ID,
        app_version=os.environ.get("RENDER_GIT_COMMIT", "")[:7] or None,
        is_production=os.environ.get("INNGEST_DEV") is None,
    )


def catchup_event() -> inngest.Event | None:
    """The bash boot catch-up, as an event. None when data is fresh."""
    stale = stale_feeds(feed_ages_hours(), CATCHUP_STALE_H)
    if not stale:
        _log("catch-up: data fresh, no immediate sync")
        return None
    _log(f"catch-up: stale/missing {stale}; requesting daily_sync")
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    # Same id all day -> Inngest de-duplicates repeated deploys.
    return inngest.Event(name=EV_REQUESTED, id=f"catchup-{today}",
                         data={"reason": "catch-up", "stale": stale})


def main() -> int:
    if "--list" in sys.argv:
        print(f"daily_sync  cron {DAILY_CRON} UTC + event {EV_REQUESTED}")
        for s in STEPS:
            gate = f"  [env {','.join(s.env)}]" if s.env else ""
            gate += "  [Sundays]" if s.when != "always" else ""
            print(f"  {s.id:28} {s.cmd}{gate}")
        for e in EXTRAS:
            print(f"{e.id:30} on {EV_FINISHED} if {e.expression}")
        return 0
    if not env_present("INNGEST_EVENT_KEY", "INNGEST_SIGNING_KEY") \
            and os.environ.get("INNGEST_DEV") is None:
        _log("inngest_sync: INNGEST_EVENT_KEY / INNGEST_SIGNING_KEY not set — exiting")
        return 2

    import asyncio
    from inngest.connect import connect

    client = build_client()
    fns = build_functions(client)
    instance = (os.environ.get("RENDER_SERVICE_NAME") or socket.gethostname()) + "-sync"
    _log(f"inngest_sync connecting app={APP_ID} instance={instance} functions={len(fns)}")
    conn = connect([(client, fns)], instance_id=instance, max_worker_concurrency=2)

    async def _catchup_after_connect() -> None:
        # Give Connect time to register the functions before sending.
        await asyncio.sleep(90)
        ev = catchup_event()
        if ev is not None:
            try:
                await client.send(ev)
            except Exception as exc:  # noqa: BLE001 — log and keep serving
                _log(f"catch-up event send failed: {exc}")

    async def _run() -> None:
        task = asyncio.create_task(_catchup_after_connect())
        try:
            await conn.start()
        finally:
            task.cancel()

    asyncio.run(_run())
    _log("inngest_sync connection closed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
