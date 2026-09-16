"""inngest_worker.py — job registry and slack_loop.sh hand-off.

These tests do not talk to Inngest. They check that (a) every timer-driven
job in slack_loop.sh has a matching Inngest function so nothing silently
stops running, (b) the registry builds into valid Inngest function configs,
(c) the shell-out runner surfaces failures as retriable errors, and
(d) slack_loop.sh really does hand off when the Inngest keys are present.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import inngest

import inngest_worker as iw

REPO_ROOT = Path(__file__).resolve().parents[1]
SLACK_LOOP = (REPO_ROOT / "slack_loop.sh").read_text(encoding="utf-8")

CRON_FIELD = r"[\d\*\/,\-]+"
CRON_RE = re.compile(
    rf"^(TZ=[A-Za-z_]+/[A-Za-z_]+ )?{CRON_FIELD}( {CRON_FIELD}){{4}}$"
)


class RegistryTests(unittest.TestCase):
    def test_every_bash_job_has_an_inngest_function(self):
        bash_jobs = set(re.findall(r'_run_(?:bg|fast) "([a-z_0-9]+)"', SLACK_LOOP))
        registry = {j.name for j in iw.JOBS}
        self.assertTrue(bash_jobs, "no _run_bg jobs found in slack_loop.sh")
        self.assertEqual(bash_jobs - registry, set(),
                         "bash jobs missing from inngest_worker.JOBS")

    def test_names_unique_and_crons_valid(self):
        names = [j.name for j in iw.JOBS]
        self.assertEqual(len(names), len(set(names)))
        for job in iw.JOBS + [iw.Job("x", iw.DAILY_REFRESH_CRON, "true")]:
            self.assertRegex(job.cron, CRON_RE, job.name)

    def test_daily_jobs_are_staggered(self):
        # No two daily/weekly jobs share the same minute+hour: the herd on
        # boot is the failure mode this whole file exists to remove.
        seen = {}
        for job in iw.JOBS:
            fields = job.cron.split()[-5:]
            if "*" in fields[1] or "/" in fields[0]:
                continue  # frequent polls
            key = (fields[0], fields[1], fields[4], fields[2])
            self.assertNotIn(key, seen, f"{job.name} collides with {seen.get(key)}")
            seen[key] = job.name

    def test_build_functions_produces_valid_configs(self):
        client = inngest.Inngest(app_id="test", is_production=False)
        fns = iw.build_functions(client)
        self.assertEqual(len(fns), len(iw.JOBS) + 1)
        ids = set()
        for fn in fns:
            cfg = fn.get_config("http://localhost/api/inngest").main
            ids.add(cfg.id)
            self.assertTrue(cfg.triggers)
            self.assertTrue(cfg.concurrency)
            self.assertEqual(cfg.concurrency[0].limit, 1)
        self.assertEqual(len(ids), len(fns))

    def test_cin7_jobs_share_cin7_key_and_others_share_worker_cap(self):
        client = inngest.Inngest(app_id="test", is_production=False)
        by_name = {fn.get_config("http://x").main.name: fn.get_config("http://x").main
                   for fn in iw.build_functions(client)}
        self.assertEqual(by_name["cin7_boms"].concurrency[1].key, '"cin7"')
        self.assertEqual(by_name["klaviyo_sync"].concurrency[1].key, '"worker"')
        self.assertEqual(by_name["klaviyo_sync"].concurrency[1].limit,
                         iw.WORKER_MAX_JOBS)
        # polls (fast or not) only carry the per-function limit
        self.assertEqual(len(by_name["fablab_assembly_check_po"].concurrency), 1)
        self.assertEqual(len(by_name["stock_issues_check_replies"].concurrency), 1)
        self.assertEqual(len(by_name["notion_pull"].concurrency), 1)


class RunnerTests(unittest.TestCase):
    def test_run_cmd_returns_output_on_success(self):
        with mock.patch.object(iw, "LOG_PATH", Path(tempfile.mkdtemp()) / "l.log"):
            res = iw.run_cmd("t", "echo hello", 30)
        self.assertEqual(res["rc"], 0)
        self.assertIn("hello", res["stdout_tail"])

    def test_run_cmd_uses_this_interpreter(self):
        with mock.patch.object(iw, "LOG_PATH", Path(tempfile.mkdtemp()) / "l.log"):
            res = iw.run_cmd("t", "python -c 'import sys; print(sys.executable)' "
                                  "&& python -c 'print(1)'", 30)
        self.assertIn(os.path.realpath(sys.executable),
                      os.path.realpath(res["stdout_tail"].splitlines()[0]))
        self.assertEqual(res["cmd"].count(sys.executable), 2)

    def test_run_cmd_raises_on_failure(self):
        with mock.patch.object(iw, "LOG_PATH", Path(tempfile.mkdtemp()) / "l.log"):
            with self.assertRaises(iw.JobFailed) as cm:
                iw.run_cmd("t", "echo boom >&2; exit 3", 30)
        self.assertIn("exited 3", str(cm.exception))
        self.assertIn("boom", str(cm.exception))

    def test_run_cmd_timeout_is_a_failure(self):
        with mock.patch.object(iw, "LOG_PATH", Path(tempfile.mkdtemp()) / "l.log"):
            with self.assertRaises(iw.JobFailed):
                iw.run_cmd("t", "sleep 5", 1)

    def test_admission_defers_or_skips_on_low_memory(self):
        with mock.patch.object(iw, "available_mb", return_value=1), \
                mock.patch.object(iw, "LOG_PATH", Path(tempfile.mkdtemp()) / "l.log"):
            with self.assertRaises(inngest.RetryAfterError) as cm:
                iw.admission_check("t")
            self.assertGreater(cm.exception.retry_after,
                               dt.datetime.now() + dt.timedelta(seconds=60))
            self.assertIn("low memory", iw.admission_check("t", defer=False))
        with mock.patch.object(iw, "available_mb", return_value=10_000):
            self.assertIsNone(iw.admission_check("t"))

    def test_handler_skips_when_env_gate_closed(self):
        job = iw.Job("gated", "0 5 * * *", "exit 1", env=("DEFINITELY_UNSET_VAR_X",))
        ctx = mock.Mock()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEFINITELY_UNSET_VAR_X", None)
            out = iw.make_handler(job)(ctx)
        self.assertIn("skipped", out)
        ctx.step.run.assert_not_called()

    def test_handler_skips_when_mirror_available(self):
        job = iw.Job("legacy", "0 5 * * *", "exit 1", skip_if_mirror=True)
        ctx = mock.Mock()
        ctx.step.run.side_effect = lambda sid, fn, *a: True if sid == "mirror-status" else fn(*a)
        out = iw.make_handler(job)(ctx)
        self.assertEqual(out["skipped"], "data comes from shared DB mirror")


@unittest.skipIf(shutil.which("bash") is None, "bash not available")
class SlackLoopHandoffTests(unittest.TestCase):
    """Source the top of slack_loop.sh (helpers + Inngest switch) and check
    that _run_bg/_run_fast are no-ops exactly when the keys are present."""

    def _run(self, env: dict[str, str]) -> str:
        src = SLACK_LOOP
        head_end = src.index("# v2.67.58 — Bootstrap: first-boot data sync")
        head = src[:head_end]
        # Stub python so the Inngest supervisor doesn't actually start.
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            (tmpdir / "output").mkdir()
            (tmpdir / "python").write_text("#!/usr/bin/env bash\nexit 0\n")
            (tmpdir / "python").chmod(0o755)
            (tmpdir / "head.sh").write_text(head, encoding="utf-8")
            (tmpdir / "case.sh").write_text(textwrap.dedent(f"""
                export PATH="{tmpdir}:$PATH"
                export DATA_DIR="{tmpdir}"
                export BG_PID_DIR="{tmpdir}/bg"
                export SLACK_BOT_TOKEN=x
                source "{tmpdir}/head.sh"
                _run_bg demo "echo RAN_BG >> {tmpdir}/out"
                _run_fast demo2 "echo RAN_FAST >> {tmpdir}/out"
                sleep 1
                echo "USE_INNGEST=$USE_INNGEST" > {tmpdir}/result
                cat {tmpdir}/out >> {tmpdir}/result 2>/dev/null
                exit 0
            """), encoding="utf-8")
            full_env = {k: v for k, v in os.environ.items()
                        if not k.startswith("INNGEST_")}
            full_env.update(env)
            # Own session so the supervisor subshell (and its sleep) can be
            # killed as a group instead of holding the pipe open.
            proc = subprocess.Popen(["bash", str(tmpdir / "case.sh")], env=full_env,
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
            return (tmpdir / "result").read_text(encoding="utf-8")

    def test_without_keys_jobs_run_in_bash(self):
        out = self._run({})
        self.assertIn("USE_INNGEST=0", out)
        self.assertIn("RAN_BG", out)
        self.assertIn("RAN_FAST", out)

    def test_with_keys_jobs_are_delegated(self):
        out = self._run({"INNGEST_EVENT_KEY": "e", "INNGEST_SIGNING_KEY": "s"})
        self.assertIn("USE_INNGEST=1", out)
        self.assertNotIn("RAN_BG", out)
        self.assertNotIn("RAN_FAST", out)

    def test_kill_switch(self):
        out = self._run({"INNGEST_EVENT_KEY": "e", "INNGEST_SIGNING_KEY": "s",
                         "INNGEST_JOBS": "0"})
        self.assertIn("USE_INNGEST=0", out)
        self.assertIn("RAN_BG", out)


if __name__ == "__main__":
    unittest.main()
