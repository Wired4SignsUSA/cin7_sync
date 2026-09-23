"""inngest_sync.py — nightly sync step function (Inngest Phase 2).

No Inngest server involved. Checks that (a) the step list is exactly the
command list of daily_sync.sh (the bash fallback), in order, (b) the
functions build into valid configs, (c) a failed step is recorded and the
run continues like daily_sync.sh, stale critical feeds fail the run and
skip the warm, and (d) start.sh hands off to inngest_sync.py only when
the keys are present and the kill switch is off.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

import inngest

import inngest_sync as isync

REPO_ROOT = Path(__file__).resolve().parents[1]


def _daily_sync_commands() -> list[str]:
    src = (REPO_ROOT / "daily_sync.sh").read_text(encoding="utf-8")
    src = re.sub(r"\\\n\s*", " ", src)          # join continuation lines
    cmds = []
    for line in src.splitlines():
        line = line.strip()
        if line.startswith("#"):
            continue
        m = re.match(r"^((?:[A-Z0-9_]+=\S+ )*python \S+\.py.*?)\s*>>", line)
        if m:
            cmds.append(re.sub(r"\s+", " ", m.group(1)))
    return cmds


class StepListTests(unittest.TestCase):
    def test_steps_match_daily_sync_sh_in_order(self):
        self.assertEqual([s.cmd for s in isync.STEPS], _daily_sync_commands())

    def test_step_ids_unique(self):
        ids = [s.id for s in isync.STEPS]
        self.assertEqual(len(ids), len(set(ids)))

    def test_env_gates_match_bash(self):
        by_id = {s.id: s for s in isync.STEPS}
        self.assertEqual(by_id["shopify-content"].env, isync.SHOPIFY_ENV)
        self.assertEqual(by_id["ip-pull-alternates"].env, isync.IP_ENV)
        self.assertEqual(by_id["shipstation-30d"].env, ("SHIPSTATION_API_KEY",))
        self.assertEqual(by_id["cin7-product-images"].when, "sunday_or_force")

    def test_product_images_sunday_or_force(self):
        step = next(s for s in isync.STEPS if s.id == "cin7-product-images")
        with mock.patch.dict(os.environ, {"PRODUCT_IMAGE_SYNC_FORCE": "0"}):
            self.assertIsNone(isync.step_should_run(step, 7))
            self.assertIsNotNone(isync.step_should_run(step, 3))
        with mock.patch.dict(os.environ, {"PRODUCT_IMAGE_SYNC_FORCE": "1"}):
            self.assertIsNone(isync.step_should_run(step, 3))


class BuildTests(unittest.TestCase):
    def test_functions_build(self):
        client = inngest.Inngest(app_id="test", is_production=False)
        fns = isync.build_functions(client)
        self.assertEqual(len(fns), 1 + len(isync.EXTRAS))
        cfgs = [f.get_config("http://localhost/api/inngest").main for f in fns]
        self.assertEqual(len({c.id for c in cfgs}), len(cfgs))
        daily = cfgs[0]
        kinds = sorted(type(t).__name__ for t in daily.triggers)
        self.assertEqual(kinds, ["TriggerCron", "TriggerEvent"])
        self.assertEqual(daily.triggers[0].cron, isync.DAILY_CRON)
        for c in cfgs:
            self.assertEqual(c.concurrency[0].limit, 1)
            # Whole-run exclusivity (concurrency alone lets runs interleave).
            self.assertEqual(c.singleton.mode, "skip")
            # Never the worker's env-scoped "cin7" key (would block its polls).
            self.assertTrue(all(not cc.key for cc in c.concurrency))

    def test_extras_mirror_sync_loop(self):
        by_id = {e.id: e for e in isync.EXTRAS}
        loop = (REPO_ROOT / "sync_loop.sh").read_text(encoding="utf-8")
        for e in isync.EXTRAS:
            for _, cmd in e.cmds:
                self.assertIn(cmd, loop, e.id)
        # Posting jobs must never retry (double post / double email).
        self.assertEqual(by_id["monthly_metrics_report"].retries, 0)
        self.assertEqual(by_id["weekly_slow_movers_email"].retries, 0)


class FakeStep:
    """Minimal ctx.step: runs steps inline; a StepFailed becomes the
    StepError Inngest raises once retries are exhausted."""

    def __init__(self):
        self.ran: list[str] = []
        self.sent: list[inngest.Event] = []
        self.slept: list[str] = []

    def run(self, step_id, fn, *args):
        self.ran.append(step_id)
        try:
            return fn(*args)
        except isync.StepFailed as exc:
            raise inngest.StepError(str(exc), "StepFailed", None) from exc

    def send_event(self, step_id, ev):
        self.sent.append(ev)
        return ["id"]

    def sleep(self, step_id, duration):
        self.slept.append(step_id)


class FakeCtx:
    def __init__(self, event):
        self.event = event
        self.step = FakeStep()
        self.run_id = "test"


class DailySyncFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        out = Path(self.tmp.name) / "output"
        out.mkdir()
        self.out = out
        self.patches = [
            mock.patch.object(isync, "OUTPUT_DIR", out),
            mock.patch.object(isync, "DAILY_LOG", out / "daily_sync.log"),
            mock.patch.object(isync, "LOOP_LOG", out / "sync_loop.log"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _fresh_feeds(self):
        for pattern, _ in isync.CRITICAL_FEEDS:
            (self.out / pattern.replace("*", "x")).write_text("a")

    def _run(self, event, fail=()):
        calls = []

        def fake_run_cmd(name, cmd, *a, **kw):
            calls.append(name)
            if name in fail:
                raise isync.StepFailed(f"{name} exited 1")
            return {"rc": 0, "seconds": 1, "stdout_tail": "", "stderr_tail": ""}

        ctx = FakeCtx(event)
        with mock.patch.object(isync, "run_cmd", side_effect=fake_run_cmd), \
             mock.patch.object(isync, "warm_engine",
                               return_value={"rc": 0, "seconds": 1}) as warm:
            try:
                res = isync.daily_sync(ctx)
                err = None
            except inngest.NonRetriableError as exc:
                res, err = None, exc
        return ctx, calls, warm, res, err

    def test_failed_step_is_recorded_and_run_continues(self):
        self._fresh_feeds()
        ctx, calls, warm, res, err = self._run(
            inngest.Event(name="inngest/scheduled.timer"), fail={"cin7-boms"})
        self.assertIsNone(err)
        self.assertEqual(res["failed_steps"], ["cin7-boms"])
        self.assertIn("publish-monthly-metrics", calls)   # kept going
        warm.assert_called_once()
        self.assertEqual(len(ctx.step.sent), 1)             # cron -> extras event
        self.assertEqual(ctx.step.sent[0].name, isync.EV_FINISHED)

    def test_stale_critical_feeds_fail_run_and_skip_warm(self):
        ctx, calls, warm, res, err = self._run(
            inngest.Event(name="inngest/scheduled.timer"))
        self.assertIsNotNone(err)
        warm.assert_not_called()

    def test_catchup_delays_warm_and_sends_no_extras(self):
        self._fresh_feeds()
        ctx, calls, warm, res, err = self._run(
            inngest.Event(name=isync.EV_REQUESTED, data={"reason": "catch-up"}))
        self.assertIsNone(err)
        self.assertEqual(ctx.step.slept, ["warm-boot-delay"])
        self.assertEqual(ctx.step.sent, [])
        warm.assert_called_once()

    def test_catchup_event_only_when_stale(self):
        self.assertIsNotNone(isync.catchup_event())
        self._fresh_feeds()
        self.assertIsNone(isync.catchup_event())

    def test_catchup_event_id_is_per_day(self):
        ev = isync.catchup_event()
        self.assertRegex(ev.id, r"^catchup-\d{4}-\d{2}-\d{2}$")


class RunCmdTests(unittest.TestCase):
    def test_env_prefixed_python_uses_this_interpreter(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "l.log"
            res = isync.run_cmd(
                "t", "FOO=1 python -c 'import os,sys;print(sys.executable, os.environ[\"FOO\"])'",
                60, log)
            self.assertIn(sys.executable, res["stdout_tail"])
            self.assertIn("1", res["stdout_tail"])
            self.assertIn("t done", log.read_text())

    def test_failure_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(isync.StepFailed):
                isync.run_cmd("t", "python -c 'raise SystemExit(3)'", 60,
                              Path(tmp) / "l.log")


class StartShHandoffTests(unittest.TestCase):
    """Run start.sh's sync-selection block with stubbed loops."""

    def _run(self, env: dict[str, str]) -> str:
        src = (REPO_ROOT / "start.sh").read_text(encoding="utf-8")
        start = src.index("_supervise() {")
        end = src.index("trap \"kill $NEARSYNC_PID")
        block = src[start:end]
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "output").mkdir()
            (t / "python").write_text(
                f"#!/usr/bin/env bash\necho INNGEST_SYNC_RAN >> {t}/out\nsleep 300\n")
            (t / "python").chmod(0o755)
            for name in ("nearsync_loop.sh", "sync_loop.sh"):
                (t / name).write_text(
                    f"#!/usr/bin/env bash\necho {name} >> {t}/out\nsleep 300\n")
                (t / name).chmod(0o755)
            (t / "case.sh").write_text(textwrap.dedent(f"""
                cd "{t}"
                export PATH="{t}:$PATH"
                export DATA_DIR="{t}"
            """) + block + "\nsleep 2\n", encoding="utf-8")
            full_env = {k: v for k, v in os.environ.items()
                        if not k.startswith("INNGEST_")}
            full_env.update(env)
            proc = subprocess.Popen(["bash", str(t / "case.sh")], env=full_env,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    start_new_session=True)
            try:
                proc.wait(timeout=30)
            finally:
                try:
                    os.killpg(proc.pid, 9)
                except ProcessLookupError:
                    pass
            time.sleep(0.2)
            return (t / "out").read_text(encoding="utf-8")

    def test_without_keys_bash_loop(self):
        out = self._run({})
        self.assertIn("sync_loop.sh", out)
        self.assertNotIn("INNGEST_SYNC_RAN", out)
        self.assertIn("nearsync_loop.sh", out)

    def test_with_keys_inngest(self):
        out = self._run({"INNGEST_EVENT_KEY": "e", "INNGEST_SIGNING_KEY": "s"})
        self.assertIn("INNGEST_SYNC_RAN", out)
        self.assertNotIn("sync_loop.sh\n", out.replace("nearsync_loop.sh\n", ""))
        self.assertIn("nearsync_loop.sh", out)

    def test_kill_switch(self):
        out = self._run({"INNGEST_EVENT_KEY": "e", "INNGEST_SIGNING_KEY": "s",
                         "INNGEST_SYNC": "0"})
        self.assertIn("sync_loop.sh", out.replace("nearsync_loop.sh", ""))
        self.assertNotIn("INNGEST_SYNC_RAN", out)


if __name__ == "__main__":
    unittest.main()
