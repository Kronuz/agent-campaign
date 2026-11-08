"""The job queue: identity, reuse, scheduling, timeouts, crashes, cleanup and contention."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from harness import CampaignCase, alive, campaign_module, session_members


class Races(unittest.TestCase):
    def test_a_heartbeat_removed_between_checks_reads_as_no_worker(self) -> None:
        module = campaign_module()
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"CAMPAIGN_JOBS": directory}):
            (Path(directory) / ".worker").mkdir()
            # the worker removes its heartbeat right after an existence check says it is there
            with mock.patch.object(Path, "exists", return_value=True):
                self.assertFalse(module.worker_alive())


class Identity(CampaignCase):
    def test_submit_warns_about_inputs_outside_the_jobs_checkout(self) -> None:
        wt = self.started("fix", files="app.py")
        first = self.submit(wt, "--quest", "fix", "--", "true")
        other = self.root / "jobs" / first / "a1/out/oracle"
        r = self.ok(
            wt, "job", "submit", "--quest", "fix", "--", f"{other} {self.home}/.scratch/expected.bin x=/usr/bin/env"
        )
        self.assertIn(f"warning: {other}, {self.home}/.scratch/expected.bin are outside this job's own checkout", r.err)
        self.assertNotIn("/usr/bin/env", r.err)  # a system path is not the job's code
        r = self.ok(wt, "job", "submit", "--quest", "fix", "--", 'python3 app.py --out "$JOB_OUT/x"')
        self.assertEqual(r.err, "")

    def test_explicit_head_uses_the_submitting_task_worktree(self) -> None:
        self.started("fix", files="app.py,lib/**")
        self.ok(self.home, "start", "fix", "--task", "index", "--files", "lib/**")
        wt = self.wt("fix--index")
        head = self.commit(wt, {"lib/util.py": "print('task')\n"})
        jid = self.submit(wt / "lib", "--quest", "fix", "--commit", "HEAD", "--", 'python3 util.py > "$JOB_OUT/seen"')
        self.ok(wt, "wait", jid)
        spec = json.loads((self.root / "jobs" / jid / "spec.json").read_text())
        self.assertEqual(spec["commit"], head)
        self.assertEqual(spec["cwd"], "lib")
        self.assertEqual((self.root / "jobs" / jid / "a1/out/seen").read_text(), "task\n")

    def test_a_job_runs_the_submitted_commit_while_the_branch_moves_on(self) -> None:
        wt = self.started("fix", files="app.py")
        first = self.commit(wt, {"app.py": "print('first')\n"})
        jid = self.submit(wt, "--what", "slow read", "--", 'sleep 1; python3 app.py > "$JOB_OUT/seen"')
        self.commit(wt, {"app.py": "print('second')\n"})  # the contributor keeps working
        (wt / "app.py").write_text("print('uncommitted')\n")
        self.ok(wt, "wait", jid, "--timeout", "60")
        seen = (self.root / "jobs" / jid / "a1" / "out" / "seen").read_text().strip()
        self.assertEqual(seen, "first")
        self.assertEqual(json.loads((self.root / "jobs" / jid / "spec.json").read_text())["commit"], first)

    def test_identical_jobs_are_reused_and_force_reruns(self) -> None:
        jid = self.submit(self.home, "--", "true")
        self.ok(self.home, "wait", jid)
        again = self.ok(self.home, "job", "submit", "--", "true").out
        self.assertEqual(again.split()[0], jid)
        self.assertIn("reused", again)
        forced = self.submit(self.home, "--force", "--", "true")
        self.assertNotEqual(forced, jid)

    def test_a_different_toolchain_is_a_different_identity(self) -> None:
        jid = self.submit(self.home, "--", "true")
        self.ok(self.home, "wait", jid)
        self.env["CAMPAIGN_FINGERPRINT"] = "echo compiler-2"
        self.assertNotEqual(self.submit(self.home, "--", "true"), jid)

    def test_refs_check_out_other_commits_and_repositories(self) -> None:
        lib = self.repo("lib", {"version": "1\n"})
        old = self.git(lib, "rev-parse", "HEAD")
        self.commit(lib, {"version": "2\n"})
        base = self.git(self.home, "rev-parse", "HEAD")
        self.commit(self.home, {"README.md": "changed\n"})
        jid = self.submit(
            self.home,
            "--ref",
            f"base={base}",
            "--ref",
            f"lib={lib}@{old}",
            "--",
            'cat "$JOB_REF_BASE/README.md" "$JOB_REF_LIB/version" > "$JOB_OUT/seen"',
        )
        self.ok(self.home, "wait", jid)
        self.assertEqual((self.root / "jobs" / jid / "a1" / "out" / "seen").read_text(), "app\n1\n")

    def test_sync_runs_without_a_worker_and_records_the_same_result(self) -> None:
        r = self.ok(self.home, "job", "submit", "--sync", "--", "echo hi")
        jid = r.out.split()[0]
        self.assertEqual(self.job(jid)["state"], "done")
        self.assertIn("hi", (self.root / "jobs" / jid / "a1" / "log").read_text())
        failing = self.run_cli(self.home, "job", "submit", "--sync", "--", "exit 4")
        self.assertEqual(failing.code, 1)

    def test_a_job_runs_with_its_submitters_path(self) -> None:
        """The worker may have been started by another session; the job runs with the PATH its toolchain
        fingerprint was taken with, the submitter's."""
        first = self.submit(self.home, "--", "sleep 4")  # starts a worker with this PATH
        tools = self.root / "tools"
        tools.mkdir()
        (tools / "greet").write_text("#!/bin/sh\necho hi\n")
        (tools / "greet").chmod(0o755)
        self.env["PATH"] = f"{tools}:{self.env['PATH']}"
        second = self.submit(self.home, "--", "greet")
        self.ok(self.home, "wait", first, second, "--timeout", "60")
        self.assertEqual(self.job(second)["state"], "done")


class Scheduling(CampaignCase):
    def test_after_runs_in_order_and_skips_on_failure(self) -> None:
        a = self.submit(self.home, "--", "exit 1")
        b = self.submit(self.home, "--after", a, "--", "true")
        c = self.submit(self.home, "--", "sleep 1")
        d = self.submit(self.home, "--after", c, "--", "true")
        self.run_cli(self.home, "wait", a, b, c, d, "--timeout", "60")
        self.assertEqual(self.job(a)["state"], "failed")
        self.assertEqual(self.job(b)["state"], "skipped")
        self.assertEqual(self.job(d)["state"], "done")
        self.assertGreaterEqual(self.job(d)["started"], self.job(c)["ended"])

    def test_a_pending_job_is_reused_only_when_it_runs_after_the_same_jobs(self) -> None:
        slow = self.submit(self.home, "--", "sleep 3")
        queued = self.submit(self.home, "--after", slow, "--", "true")
        other = self.submit(self.home, "--", "exit 1")
        self.assertNotEqual(self.submit(self.home, "--after", other, "--", "true"), queued)
        self.assertEqual(self.submit(self.home, "--after", slow, "--", "true"), queued)
        self.run_cli(self.home, "wait", slow, queued, other, "--timeout", "60")
        self.assertEqual(self.job(queued)["state"], "done")

    def test_priority_orders_the_queue(self) -> None:
        self.env["CAMPAIGN_LIGHT_JOBS"] = "1"
        blocker = self.submit(self.home, "--", "sleep 2")
        low = self.submit(self.home, "--", "echo low")
        high = self.submit(self.home, "--priority", "5", "--", "echo high")
        self.ok(self.home, "wait", blocker, low, high, "--timeout", "60")
        self.assertLess(self.job(high)["started"], self.job(low)["started"])

    def test_deferred_work_never_takes_every_light_slot(self) -> None:
        self.env["CAMPAIGN_LIGHT_JOBS"] = "2"
        late = [self.submit(self.home, "--priority", "-1", "--force", "--", "sleep 4") for _ in range(2)]
        self.wait_until(lambda: self.job(late[0])["state"] == "running")
        gate = self.submit(self.home, "--priority", "1", "--", "true")
        self.ok(self.home, "wait", gate, "--timeout", "30")
        self.assertLess(self.job(gate)["ended"], max(self.job(x).get("started", 1e18) for x in late))

    def test_the_worker_stays_while_jobs_run_and_exits_when_idle(self) -> None:
        jid = self.submit(self.home, "--", "sleep 3")
        beat = self.root / "jobs" / ".worker" / "heartbeat"
        self.wait_until(lambda: self.job(jid)["state"] == "running")
        time.sleep(1.5)
        self.assertTrue(beat.exists(), "the worker left while a job was running")
        self.ok(self.home, "wait", jid)
        self.wait_until(lambda: not (self.root / "jobs" / ".worker").exists(), timeout=10)


class Limits(CampaignCase):
    def test_a_no_hold_job_is_stopped_at_its_timeout(self) -> None:
        jid = self.submit(self.home, "--level", "none", "--minutes", "1", "--", "sleep 300")
        began = self.root / "jobs" / jid / "a1" / "began"
        # the shell creates the file before date writes it, and that write would reset a backdated mtime
        self.wait_until(lambda: began.exists() and began.read_text().strip())
        os.utime(began, (time.time() - 120, time.time() - 120))  # its one minute has passed
        self.run_cli(self.home, "wait", jid, "--timeout", "30")
        self.assertEqual(self.job(jid)["state"], "timeout")

    def test_a_measurement_is_stopped_at_its_hold_deadline(self) -> None:
        self.env["QUIET_MINUTE"] = "2"
        jid = self.submit(self.home, "--level", "exclusive", "--minutes", "1", "--", "sleep 60")
        self.assertEqual(self.run_cli(self.home, "wait", jid, "--timeout", "60").code, 1)
        self.assertEqual(self.job(jid)["state"], "timeout")
        self.assertEqual(self.job(jid)["exit"], 124)

    def test_long_measurements_are_refused(self) -> None:
        self.fails(self.home, "job", "submit", "--level", "strict", "--minutes", "45", "--", "true", match="at most 30")

    def test_helpers_never_outlive_their_job(self) -> None:
        jid = self.submit(self.home, "--", 'sleep 600 & echo $! > "$JOB_OUT/helper"; true')
        self.ok(self.home, "wait", jid)
        helper = int((self.root / "jobs" / jid / "a1" / "out" / "helper").read_text())
        time.sleep(0.5)
        with self.assertRaises(ProcessLookupError):
            os.kill(helper, 0)

    def test_cancel_stops_queued_and_running_jobs(self) -> None:
        run = self.submit(self.home, "--", "sleep 300")
        self.wait_until(lambda: self.job(run)["state"] == "running" and "pgid" in self.job(run))
        self.ok(self.home, "job", "cancel", run)
        self.run_cli(self.home, "wait", run, "--timeout", "30")
        self.assertEqual(self.job(run)["state"], "cancelled")


class Crashes(CampaignCase):
    def kill_runner(self, jid: str) -> int:
        self.wait_until(lambda: self.job(jid)["state"] == "running" and "pgid" in self.job(jid))
        state = self.job(jid)
        os.kill(state["runner"], signal.SIGKILL)
        return int(state["sid"])

    def test_a_lost_runner_is_reaped_its_orphans_killed_and_the_job_retried(self) -> None:
        first_attempt_hangs = 'if [ "$(basename "$JOB_DIR")" = a1 ]; then sleep 60; fi'
        jid = self.submit(self.home, "--retries", "1", "--", first_attempt_hangs)
        sid = self.kill_runner(jid)
        self.assertTrue(session_members({sid}), "the command should still run, orphaned")
        self.ok(self.home, "wait", jid, "--timeout", "90", timeout=120)
        state = self.job(jid)
        self.assertEqual(state["state"], "done")
        self.assertEqual(state["attempt"], 2)
        self.assertEqual(session_members({sid}), [], "the orphaned attempt must be killed")

    def test_without_retries_a_lost_runner_leaves_the_job_interrupted(self) -> None:
        jid = self.submit(self.home, "--", "sleep 30")
        self.kill_runner(jid)
        self.assertEqual(self.run_cli(self.home, "wait", jid, "--timeout", "60").code, 1)
        self.assertEqual(self.job(jid)["state"], "interrupted")
        self.assertIn("interrupted", self.ok(self.home, "check").out)
        again = self.submit(self.home, "--", "sleep 30")  # resubmitting runs it anew
        self.assertNotEqual(again, jid)
        self.assertEqual(self.submit(self.home, "--", "sleep 30"), again)  # and only once
        self.ok(self.home, "job", "cancel", again)

    def test_a_result_written_before_the_runner_died_is_kept(self) -> None:
        jid = self.submit(self.home, "--", "sleep 2")
        self.wait_until(lambda: self.job(jid)["state"] == "running" and "pgid" in self.job(jid))
        state = self.job(jid)
        os.kill(state["runner"], signal.SIGSTOP)  # frozen: no heartbeat, no recording
        (self.root / "jobs" / jid / "a1" / "exit").write_text("0")
        self.run_cli(self.home, "wait", jid, "--timeout", "60")
        self.assertEqual(self.job(jid)["state"], "done")
        self.assertIn("recorded after its runner died", self.job(jid)["note"])

    def test_a_stale_runner_cannot_overwrite_a_newer_attempt(self) -> None:
        jid = self.submit(self.home, "--retries", "1", "--", "sleep 2")
        self.wait_until(lambda: self.job(jid)["state"] == "running" and "pgid" in self.job(jid))
        state = self.job(jid)
        (self.root / "jobs" / jid / "state.json").write_text(json.dumps({**state, "attempt": 7}))
        self.wait_until(lambda: not alive(state["runner"]), timeout=30)
        self.assertEqual(self.job(jid)["attempt"], 7)
        self.assertNotEqual(self.job(jid)["state"], "done")

    def test_the_reaper_spares_a_session_that_reused_a_dead_runners_id(self) -> None:
        bystander = subprocess.Popen(["sleep", "60"], start_new_session=True)  # its pid is its session's id
        try:
            d = self.root / "jobs" / "261108-000000-abcdef"
            d.mkdir(parents=True)
            spec: dict[str, object] = {"cmd": "true", "level": "none", "minutes": 1, "repo": "", "commit": ""}
            (d / "spec.json").write_text(
                json.dumps({**spec, "refs": {}, "env": {}, "cwd": str(self.root), "retries": 0})
            )
            boot = campaign_module().boot_id()
            state = {"state": "running", "attempt": 1, "sid": bystander.pid, "boot": boot, "runner": bystander.pid}
            (d / "state.json").write_text(json.dumps(state))
            (d / "heartbeat").touch()
            os.utime(d / "heartbeat", (0, 0))
            self.ok(self.home, "job", "list", "--all")  # reaps
            self.assertEqual(self.job(d.name)["state"], "interrupted")
            self.assertIsNone(bystander.poll(), "the reaper killed a session that is not the job's")
        finally:
            bystander.kill()
            bystander.wait()

    def test_waiting_on_a_corrupt_job_ends(self) -> None:
        jid = self.submit(self.home, "--sync", "--", "true")
        (self.root / "jobs" / jid / "state.json").write_text("{not json")
        r = self.run_cli(self.home, "wait", jid, "--timeout", "20")
        self.assertEqual(r.code, 1)
        self.assertIn("corrupt", r.out)


class Prune(CampaignCase):
    def age(self, jid: str, days: float) -> None:
        state = self.root / "jobs" / jid / "state.json"
        data = json.loads(state.read_text())
        data["ended"] = time.time() - days * 86400
        state.write_text(json.dumps(data))

    def test_prune_keeps_what_open_quests_and_landings_need(self) -> None:
        wt = self.started("open", files="app.py", check="unit=true")
        self.commit(wt, {"app.py": "print(2)\n"})
        tagged = self.submit(wt, "--quest", "open", "--sync", "--", "true 1")
        cited = self.submit(self.home, "--sync", "--", "true 2")  # untagged, but a journal cites it
        self.commit(wt, {".campaign/demo/journal/open.md": f"## Evidence\n\n| A1 | {cited} |\n"})
        loose = self.submit(self.home, "--sync", "--", "true 3")
        recent = self.submit(self.home, "--sync", "--", "true 4")
        kept = self.submit(self.home, "--keep", "--sync", "--", "true 5")
        for jid in (tagged, cited, loose, kept):
            self.age(jid, 30)
        dry = self.ok(self.home, "job", "prune", "--days", "14", "--dry-run").out
        self.assertIn(loose, dry)
        self.assertTrue((self.root / "jobs" / loose).exists())
        out = self.ok(self.home, "job", "prune", "--days", "14").out
        self.assertIn("2 jobs", out)
        gone = {j for j in (tagged, cited, loose, recent, kept) if not (self.root / "jobs" / j).exists()}
        self.assertEqual(gone, {loose, kept})
        self.assertNotIn(str(self.root / "jobs"), self.git(self.home, "worktree", "list"))  # kept checkouts too
        keys = self.root / "jobs" / "keys"
        self.assertTrue(
            all((keys.parent / os.readlink(k)).exists() for k in keys.iterdir()), "a key names a pruned job"
        )
        self.assertIn("reused", self.ok(self.home, "job", "submit", "--", "true 4").text)
        self.assertNotIn("reused", self.ok(self.home, "job", "submit", "--sync", "--", "true 3").text)


class Contention(CampaignCase):
    alone = True  # times builds against an exclusive benchmark

    def test_builds_and_an_exclusive_benchmark_never_overlap(self) -> None:
        """Scenario G: two builds and an exclusive benchmark compete; nobody polls."""
        log = self.root / "timeline"
        mark = f'echo "$1 $(python3 -c "import time; print(time.time())")" >> {log}'
        script = self.root / "mark.sh"
        script.write_text(mark + "\n")
        build = f"bash {script} start-$NAME; sleep 2; bash {script} end-$NAME"
        jobs = [
            self.submit(self.home, "--env", "NAME=build1", "--force", "--", build),
            self.submit(
                self.home, "--env", "NAME=bench", "--level", "exclusive", "--minutes", "5", "--force", "--", build
            ),
            self.submit(self.home, "--env", "NAME=build2", "--force", "--", build),
        ]
        self.ok(self.home, "wait", *jobs, "--timeout", "120", timeout=150)
        events = dict(line.split() for line in log.read_text().splitlines())
        times = {k: float(v) for k, v in events.items()}
        bench = (times["start-bench"], times["end-bench"])
        for name in ("build1", "build2"):
            span = (times[f"start-{name}"], times[f"end-{name}"])
            self.assertTrue(
                span[1] <= bench[0] or span[0] >= bench[1], f"{name} overlapped the benchmark: {span} {bench}"
            )
        self.assertEqual(self.job(jobs[1])["state"], "done")


if __name__ == "__main__":
    unittest.main()
