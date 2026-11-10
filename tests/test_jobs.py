"""The job queue: identity, reuse, scheduling, timeouts, crashes, cleanup and contention."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from harness import CAMPAIGN, CampaignCase, alive, campaign_module, session_members


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

    def test_an_evidence_jobs_script_outside_the_checkout_is_part_of_its_identity(self) -> None:
        wt = self.started("fix", files="app.py")
        probe = self.home / ".scratch" / "probe.sh"
        probe.parent.mkdir()
        probe.write_text("echo one\n")
        first = self.submit(wt, "--quest", "fix", "--sync", "--", f"sh {probe}")
        r = self.ok(wt, "job", "submit", "--quest", "fix", "--", f"sh {probe}")
        self.assertEqual(r.out.split()[0], first)
        self.assertIn(f"warning: {probe} is outside this job's own checkout", r.err)
        probe.write_text("echo two\n")
        r = self.ok(wt, "job", "submit", "--quest", "fix", "--sync", "--", f"sh {probe}")
        second = r.out.split()[0]
        self.assertNotEqual(second, first, "a changed script reused the old script's result")
        self.assertIn(f"note: the bytes of {probe} are part of this evidence job's identity", r.err)
        self.assertIn("two", (self.root / "jobs" / second / "a1" / "log").read_text())
        probe.write_text("echo three\n")
        out = self.ok(self.home, "job", "rerun", second).out
        self.assertNotIn("reused", out, "a rerun kept the script digest it was submitted with")

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

    def test_a_light_job_builds_with_a_light_holds_parallelism(self) -> None:
        self.env["CMUX_SURFACE_ID"] = "surface-lead"
        show = 'echo "${CMAKE_BUILD_PARALLEL_LEVEL:-unset} ${CMUX_SURFACE_ID:-none}" > "$JOB_OUT/env"'
        light = self.submit(self.home, "--level", "light", "--", show)
        mine = self.submit(self.home, "--level", "light", "--env", "CMAKE_BUILD_PARALLEL_LEVEL=2", "--", show)
        strict = self.submit(self.home, "--level", "strict", "--", show)
        self.ok(self.home, "wait", light, mine, strict, "--timeout", "60")
        seen = {j: (self.root / "jobs" / j / "a1" / "out" / "env").read_text().split() for j in (light, mine, strict)}
        self.assertEqual(seen[light], ["4", "none"], "a job is never the lead's surface")
        self.assertEqual(seen[mine], ["2", "none"], "a job's own --env wins")
        self.assertEqual(seen[strict], ["unset", "none"], "a strict job has the machine's cores to itself")
        self.ok(
            self.home,
            "new",
            "big",
            "--title",
            "big",
            "--risk",
            "low",
            "--files",
            "app.py",
            "--check",
            "unit=cmake --build b -j8 && ctest -j3",
        )
        self.assertIn("big: light check unit asks for -j8", self.ok(self.home, "check").out)

    def test_a_rerun_chain_keeps_its_order(self) -> None:
        first = self.submit(self.home, "--", "true")
        second = self.submit(self.home, "--after", first, "--", "true")
        self.ok(self.home, "wait", second)
        (self.home / "more.py").write_text("print(2)\n")
        self.git(self.home, "add", "more.py")
        self.git(self.home, "commit", "-qm", "more")
        lines = self.ok(self.home, "job", "rerun", second, first).out.splitlines()
        new = {line.split(": ")[1].split()[0]: line.split()[0].rstrip(":") for line in lines if ": " in line}
        new_first, new_second = new[first], new[second]
        self.assertNotEqual(new_first, first)
        spec = json.loads((self.root / "jobs" / new_second / "spec.json").read_text())
        self.assertEqual(spec["after"], [new_first])

    def test_a_rerun_never_moves_a_commit_the_quests_branch_never_held(self) -> None:
        """A mutant (a commit only a scratch ref holds) is refused without --commit and kept with it; a
        commit the branch held before it was rewritten still moves to the head."""
        wt = self.started("fix", files="app.py")
        self.git(wt, "checkout", "-q", "--detach")
        mutant = self.commit(wt, {"app.py": "print('mutant')\n"}, "mutant")
        self.git(wt, "update-ref", "refs/scratch/mutant", mutant)
        self.git(wt, "checkout", "-q", "quest/demo/fix")
        jid = self.submit(wt, "--quest", "fix", "--commit", mutant, "--", 'python3 app.py > "$JOB_OUT/seen"')
        self.ok(self.home, "wait", jid)
        self.commit(wt, {"app.py": "print(2)\n"})
        r = self.fails(self.home, "job", "rerun", jid, match=f"{jid} ran at {mutant[:12]}, which was never on ")
        self.assertIn(f"Pass --commit {mutant[:12]} to keep it", r.text)
        self.assertIn(f"{jid} at {mutant[:12]}", self.ok(self.home, "job", "rerun", jid, "--commit", mutant).out)
        probe = self.submit(wt, "--quest", "fix", "--", "python3 app.py")
        self.ok(self.home, "wait", probe)
        (wt / "app.py").write_text("print(3)\n")
        self.git(wt, "commit", "-q", "--amend", "-am", "rewritten")
        out = self.ok(self.home, "job", "rerun", probe).out
        self.assertIn(f"{probe} at {self.git(wt, 'rev-parse', 'HEAD')[:12]}", out)

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

    def test_a_job_at_new_code_cancels_its_queued_older_instance(self) -> None:
        """A rerun, or a submit identical up to its commit, cancels the quest's still-queued older
        instance and says so; a running one, and one a pending job waits on, are kept."""
        wt = self.started("fix", files="app.py")
        go = self.root / "go"
        hold = f"while [ ! -e {go} ]; do sleep 0.1; done"
        gate = self.submit(wt, "--quest", "fix", "--", hold)
        self.wait_until(lambda: self.job(gate)["state"] == "running")
        old = self.submit(wt, "--quest", "fix", "--after", gate, "--", "python3 app.py")
        self.commit(wt, {"app.py": "print(2)\n"})
        out = self.ok(self.home, "job", "rerun", old, gate).out
        new = next(line.split(":")[0] for line in out.splitlines() if f": {old} at" in line)
        self.assertIn(f"superseded {old}", out)
        self.assertEqual(self.job(old)["state"], "cancelled")
        self.assertEqual(self.job(gate)["state"], "running", "a running instance was cancelled")
        self.commit(wt, {"app.py": "print(3)\n"})
        r = self.ok(wt, "job", "submit", "--quest", "fix", "--after", gate, "--", "python3 app.py")
        third = r.out.split()[0]
        self.assertIn(f"superseded {new}", r.out)
        self.assertEqual(self.job(new)["state"], "cancelled")
        waiter = self.submit(wt, "--quest", "fix", "--after", third, "--", "true waiter")
        self.commit(wt, {"app.py": "print(4)\n"})
        r = self.ok(wt, "job", "submit", "--quest", "fix", "--after", gate, "--", "python3 app.py")
        self.assertIn(f"kept {third}: {waiter} waits on it", r.out)
        self.assertEqual(self.job(third)["state"], "queued")
        go.touch()
        self.ok(self.home, "wait", "--quest", "fix", "--timeout", "60")

    def test_a_job_at_a_sibling_commit_keeps_its_queued_instance(self) -> None:
        """Mutants off one base are not newer code: only a job at a descendant supersedes one."""
        wt = self.started("fix", files="app.py")
        go = self.root / "go"
        gate = self.submit(wt, "--quest", "fix", "--", f"while [ ! -e {go} ]; do sleep 0.1; done")
        m0 = self.commit(wt, {"app.py": "print('m0')\n"})
        self.git(wt, "reset", "-q", "--hard", "HEAD~1")
        m1 = self.commit(wt, {"app.py": "print('m1')\n"})
        a = self.submit(wt, "--quest", "fix", "--commit", m0, "--after", gate, "--", "python3 app.py")
        r = self.ok(wt, "job", "submit", "--quest", "fix", "--commit", m1, "--after", gate, "--", "python3 app.py")
        self.assertNotIn("superseded", r.out)
        self.assertEqual(self.job(a)["state"], "queued", "a mutant cancelled its sibling")
        m2 = self.commit(wt, {"app.py": "print('m2')\n"})
        r2 = self.ok(wt, "job", "submit", "--quest", "fix", "--commit", m2, "--after", gate, "--", "python3 app.py")
        self.assertIn(f"superseded {r.out.split()[0]}", r2.out)
        self.assertEqual(self.job(a)["state"], "queued")
        go.touch()
        self.ok(self.home, "wait", "--quest", "fix", "--timeout", "60")

    def test_priority_orders_the_queue(self) -> None:
        self.env["CAMPAIGN_LIGHT_JOBS"] = "1"
        go = self.root / "go"  # the blocker holds the only slot until both are queued, however slow submits are
        blocker = self.submit(self.home, "--", f"while [ ! -e {go} ]; do sleep 0.1; done")
        low = self.submit(self.home, "--", "echo low")
        high = self.submit(self.home, "--priority", "5", "--", "echo high")
        go.touch()
        self.ok(self.home, "wait", blocker, low, high, "--timeout", "60")
        self.assertLess(self.job(high)["started"], self.job(low)["started"])

    def test_a_light_job_of_a_quest_in_flight_is_on_the_direct_path(self) -> None:
        """A light job of an active or in-review quest runs at 1, as gate checks do; a measurement,
        an explicit --priority, and a job of a planned quest keep theirs."""
        wt = self.started("fix")
        self.ok(self.home, "new", "later", "--title", "later", "--risk", "low", "--check", "unit=true")

        def priority(*args: str) -> int:
            jid = self.submit(wt, *args, "--force", "--", "true")
            return int(json.loads((self.root / "jobs" / jid / "spec.json").read_text())["priority"])

        self.assertEqual(priority("--quest", "fix"), 1)
        self.assertEqual(priority("--quest", "fix", "--level", "strict"), 0)
        self.assertEqual(priority("--quest", "fix", "--level", "exclusive"), 0)
        self.assertEqual(priority("--quest", "fix", "--priority", "0"), 0)
        self.assertEqual(priority("--quest", "fix", "--priority", "-1"), -1)
        self.assertEqual(priority("--quest", "later"), 0)
        self.assertEqual(priority(), 0)

    def test_deferred_work_never_takes_every_light_slot(self) -> None:
        self.env["CAMPAIGN_LIGHT_JOBS"] = "2"
        go = self.root / "go"  # the deferred jobs hold their slot until the gate check ends, however slow
        hold = f"while [ ! -e {go} ]; do sleep 0.1; done"
        late = [self.submit(self.home, "--priority", "-1", "--force", "--", hold) for _ in range(2)]
        self.wait_until(lambda: self.job(late[0])["state"] == "running")
        gate = self.submit(self.home, "--priority", "1", "--", "true")
        self.ok(self.home, "wait", gate, "--timeout", "30")
        self.assertEqual(self.job(late[1])["state"], "queued", "deferred work took every slot")
        go.touch()
        self.ok(self.home, "wait", *late, "--timeout", "30")

    def test_a_job_that_takes_no_hold_never_waits_for_a_light_slot(self) -> None:
        self.env["CAMPAIGN_LIGHT_JOBS"] = "1"
        build = self.submit(self.home, "--level", "light", "--", "sleep 6")
        self.wait_until(lambda: self.job(build)["state"] == "running")
        probe = self.submit(self.home, "--level", "none", "--", "true")
        self.ok(self.home, "wait", probe, "--timeout", "30")
        self.assertEqual(self.job(build)["state"], "running", "the probe waited for the build's slot")

    def test_the_worker_stays_while_jobs_run_and_exits_when_idle(self) -> None:
        jid = self.submit(self.home, "--", "sleep 3")
        beat = self.root / "jobs" / ".worker" / "heartbeat"
        self.wait_until(lambda: self.job(jid)["state"] == "running")
        time.sleep(1.5)
        self.assertTrue(beat.exists(), "the worker left while a job was running")
        self.ok(self.home, "wait", jid)
        self.wait_until(lambda: not (self.root / "jobs" / ".worker").exists(), timeout=10)

    def test_a_worker_whose_home_was_taken_leaves(self) -> None:
        """Two starters can both get past a fresh home; the one that no longer owns it exits."""
        jid = self.submit(self.home, "--", "sleep 60")
        self.wait_until(lambda: self.job(jid)["state"] == "running")
        pid_file = self.root / "jobs" / ".worker" / "pid"
        worker = int(pid_file.read_text())
        other = subprocess.Popen(["sleep", "60"])  # as if a second worker had taken over
        try:
            pid_file.write_text(str(other.pid))
            self.wait_until(lambda: not alive(worker), timeout=6)  # while its job still runs
        finally:
            other.kill()
            other.wait()

    def test_a_worker_restarts_between_ticks_and_keeps_its_jobs(self) -> None:
        """`worker --restart` re-executes the worker in place: same pid and home, its jobs untouched."""
        jid = self.submit(self.home, "--", "sleep 60")
        self.wait_until(lambda: self.job(jid)["state"] == "running")
        home = self.root / "jobs" / ".worker"
        worker, started = int((home / "pid").read_text()), (home / "started").read_text()
        self.assertIn("restarts on the current code", self.ok(self.home, "worker", "--restart").out)
        self.wait_until(lambda: (home / "started").read_text() != started, timeout=10)
        self.assertEqual(int((home / "pid").read_text()), worker, "the same process, re-executed")
        later = self.submit(self.home, "--force", "--", "true")  # the restarted worker still schedules
        self.ok(self.home, "wait", later, "--timeout", "30")
        self.assertEqual(self.job(jid)["state"], "running", "a restart never touches a running job")


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

    def test_runners_survive_the_removal_of_the_directory_the_worker_started_in(self) -> None:
        """A worker started from a worktree that is later removed still runs jobs: its runners never
        run in the caller's directory."""
        self.env["CMUX_SURFACE_ID"] = "surface-1"  # a lead's surface: the runner must not look for its campaign
        gone = self.home / "gone"
        gone.mkdir()
        first = self.submit(gone, "--cwd", ".", "--", "sleep 3")
        self.wait_until(lambda: self.job(first)["state"] == "running")
        gone.rmdir()
        second = self.submit(self.home, "--", "true")
        self.ok(self.home, "wait", first, second, "--timeout", "60")
        self.assertEqual(self.job(second)["state"], "done", self.job(second).get("note", ""))


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
            (d / "a1").mkdir()
            (d / "a1" / "began").write_text("0\n")  # its command ran: not requeued as unstarted
            self.ok(self.home, "job", "list", "--all")  # reaps
            self.assertEqual(self.job(d.name)["state"], "interrupted")
            self.assertIsNone(bystander.poll(), "the reaper killed a session that is not the job's")
        finally:
            bystander.kill()
            bystander.wait()

    @unittest.skipUnless(sys.platform == "darwin", "kern.boottime is macOS's")
    def test_a_drifted_macos_boottime_is_the_same_boot(self) -> None:
        """kern.boottime moves with clock adjustments: a dead runner's exit is still recorded, not a reboot."""
        d = self.root / "jobs" / "261108-000000-b00b1e"
        (d / "a1").mkdir(parents=True)
        spec: dict[str, object] = {"cmd": "true", "level": "none", "minutes": 1, "repo": "", "commit": ""}
        (d / "spec.json").write_text(json.dumps({**spec, "refs": {}, "env": {}, "cwd": str(self.root), "retries": 0}))
        sec = int(
            subprocess.check_output(["sysctl", "-n", "kern.boottime"], text=True).split("sec = ")[1].split(",")[0]
        )
        boot = f"{{ sec = {sec - 7}, usec = 205523 }}"
        (d / "state.json").write_text(
            json.dumps({"state": "running", "attempt": 1, "sid": 0, "boot": boot, "runner": 0})
        )
        (d / "a1" / "exit").write_text("0")
        (d / "heartbeat").touch()
        os.utime(d / "heartbeat", (0, 0))
        self.ok(self.home, "job", "list", "--all")  # reaps
        self.assertEqual(self.job(d.name)["state"], "done")

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
        listed = self.git(self.home, "worktree", "list").replace(str(self.root / "jobs" / "slots" / "demo-open"), "")
        self.assertNotIn(str(self.root / "jobs"), listed)  # kept checkouts too; the open quest keeps its slot
        keys = self.root / "jobs" / "keys"
        self.assertTrue(
            all((keys.parent / os.readlink(k)).exists() for k in keys.iterdir()), "a key names a pruned job"
        )
        self.assertIn("reused", self.ok(self.home, "job", "submit", "--", "true 4").text)

    def test_prune_keeps_a_landed_job_an_open_quest_reuses(self) -> None:
        wt = self.started("a", files="app.py", check="unit=python3 app.py")
        self.commit(wt, {"app.py": "print(2)\n"})
        self.verified("a")
        self.ok(self.home, "land", "a")
        (gate,) = [j["job"] for j in self.quest("a")["landed"][0]["evidence"]]
        self.started("b", files="README.md", check="unit=python3 app.py")  # main's code: a's job is b's gate
        self.assertIn("reused", self.ok(self.home, "verify", "b").out)
        self.age(gate, 30)
        self.ok(self.home, "job", "prune", "--days", "14")
        self.assertTrue((self.root / "jobs" / gate).exists(), "b's gate evidence was pruned")
        self.assertNotIn("no job", self.ok(self.home, "brief", "b").out)
        self.assertNotIn("reused", self.ok(self.home, "job", "submit", "--sync", "--", "true 3").text)

    def test_prune_slims_the_evidence_a_landing_names_and_never_reuses_it(self) -> None:
        check = "unit=python3 app.py && head -c 2000000 /dev/zero > $JOB_OUT/big.bin && echo ok > $JOB_OUT/small.txt"
        wt = self.started("a", files="app.py", check=check)
        head = self.commit(wt, {"app.py": "print(2)\n"})
        self.verified("a")
        self.ok(self.home, "land", "a")
        (gate,) = [j["job"] for j in self.quest("a")["landed"][0]["evidence"]]
        self.age(gate, 30)
        self.assertIn("slimmed 1 job", self.ok(self.home, "job", "prune", "--days", "14").out)
        out = self.root / "jobs" / gate / "a1" / "out"
        self.assertTrue((self.root / "jobs" / gate / "spec.json").exists(), "the landing's evidence was deleted")
        self.assertFalse((out / "big.bin").exists())
        self.assertTrue((out / "small.txt").exists())
        self.assertTrue(self.job(gate).get("slimmed"))
        self.assertIn(f"slimmed {gate}", (self.root / "jobs" / "prune.log").read_text())
        self.assertNotIn("reused", self.ok(self.home, "verify", "a", "--commit", head).out)

    def test_prune_keeps_the_jobs_of_a_campaign_this_machine_does_not_know(self) -> None:
        wt = self.started("open", files="app.py")
        tagged = self.submit(wt, "--quest", "open", "--sync", "--", "true 1")
        self.age(tagged, 30)
        (self.root / "campaigns" / "demo").unlink()  # moved by hand, its registry entry not renamed
        out = self.ok(self.home, "job", "prune", "--days", "14").out
        self.assertTrue((self.root / "jobs" / tagged).exists(), "prune deleted an unknown campaign's job")
        self.assertIn("no campaign on this machine is there", out)

    def test_prune_keeps_a_slot_a_pending_job_names_or_evidence_used_lately(self) -> None:
        self.started("fix", files="app.py")
        go = self.root / "go"
        blocker = self.submit(self.home, "--", f"while [ ! -e {go} ]; do sleep 0.1; done")
        slots = self.root / "jobs" / "slots"
        ran = {}
        for name in ("named", "lately", "idle"):
            quest = ("--quest", "fix") if name == "lately" else ()
            ran[name] = self.submit(self.home, *quest, "--slot", name, "--sync", "--", f"true {name}")
        self.age(ran["named"], 30)
        self.age(ran["idle"], 30)
        self.age(ran["lately"], 3)
        waiting = self.submit(self.home, "--slot", "named", "--after", blocker, "--", "true again")
        old = time.time() - 30 * 86400
        for name in ran:
            os.utime(slots / name, (old, old))  # a slot's mtime stays put while builds change what is inside
        self.ok(self.home, "job", "prune", "--days", "14")
        self.assertEqual(sorted(x.name for x in slots.iterdir() if x.is_dir()), ["lately", "named"])
        self.age(ran["lately"], 10)
        self.ok(self.home, "job", "prune", "--days", "14")
        self.assertFalse((slots / "lately").exists())
        go.touch()
        self.ok(self.home, "wait", blocker, waiting, "--timeout", "60")

    def test_the_worker_prunes_once_a_day_when_its_queue_empties(self) -> None:
        self.env["CAMPAIGN_AUTO_PRUNE"] = "1"
        self.env["CAMPAIGN_DISK_WARN_GB"] = str(10**9)
        mark = self.root / "jobs" / ".pruned"
        mark.parent.mkdir(exist_ok=True)
        mark.touch()  # pruned today: the first run leaves the store alone
        old = self.submit(self.home, "--sync", "--", "true old")  # --sync runs it here, without the worker
        self.age(old, 30)
        os.utime(mark, (time.time() - 2 * 86400,) * 2)
        self.ok(self.home, "wait", self.submit(self.home, "--", "true next"))  # the worker runs this one
        log = self.root / "jobs" / "prune.log"
        self.wait_until(lambda: not (self.root / "jobs" / old).exists())
        self.wait_until(lambda: log.exists() and f"deleted {old}" in log.read_text())  # logged after deleting
        self.wait_until(lambda: "warning: " in log.read_text())  # and under disk_warn, the disk's consumers
        again = self.submit(self.home, "--sync", "--", "true again")
        self.age(again, 30)
        self.ok(self.home, "wait", self.submit(self.home, "--", "true last"))
        time.sleep(1)  # the worker empties its queue and exits
        self.assertTrue((self.root / "jobs" / again).exists(), "the worker pruned twice in a day")


class Slots(CampaignCase):
    def test_jobs_sharing_a_slot_take_turns_without_holding_up_others(self) -> None:
        go = self.root / "go"
        first = self.submit(self.home, "--slot", "s", "--", f"while [ ! -e {go} ]; do sleep 0.1; done")
        second = self.submit(self.home, "--slot", "s", "--", "true second")
        other = self.submit(self.home, "--", "true other")
        self.ok(self.home, "wait", other, "--timeout", "30")  # the queued slot job holds no one up
        self.wait_until(lambda: self.job(first)["state"] == "running")
        time.sleep(1)  # several worker ticks
        self.assertEqual(self.job(second)["state"], "queued", "a second job entered a busy slot")
        go.touch()
        self.ok(self.home, "wait", first, second, "--timeout", "60")
        self.assertGreaterEqual(self.job(second)["started"], self.job(first)["ended"])

    def test_a_quests_light_jobs_build_in_its_slot_while_it_is_free(self) -> None:
        """A light job of a quest reuses the quest's build slot (verify's, or NAME@REPO in another
        repository) while no job of its own is pending there, else checks out afresh beside it."""
        wt = self.started("fix")
        self.ok(self.home, "new", "risky", "--title", "risky", "--risk", "critical", "--check", "unit=true")
        lib = self.repo("lib", {"lib.py": "X = 1\n"})
        go = self.root / "go"

        def slot(*args: str, cmd: str = "true") -> str:
            jid = self.submit(wt, *args, "--force", "--", cmd)
            return str(json.loads((self.root / "jobs" / jid / "spec.json").read_text())["slot"])

        self.assertEqual(slot("--quest", "fix", cmd=f"while [ ! -e {go} ]; do sleep 0.1; done"), "demo-fix")
        self.assertEqual(slot("--quest", "fix"), "", "a second job waited for a busy slot")
        go.touch()
        self.wait_until(
            lambda: (
                not any(
                    json.loads((d / "state.json").read_text())["state"] in ("queued", "running")
                    for d in (self.root / "jobs").glob("2*")
                )
            )
        )
        self.assertEqual(slot("--quest", "fix"), "demo-fix")
        self.assertEqual(slot("--quest", "fix", "--repo", str(lib), "--commit", "HEAD"), "demo-fix@lib")
        self.assertEqual(slot("--quest", "fix", "--fresh"), "")
        self.assertEqual(slot("--quest", "fix", "--level", "strict"), "")
        self.assertEqual(slot("--quest", "risky"), "")
        self.assertEqual(slot(), "")

    def test_runners_breaking_a_dead_lock_at_once_take_the_slot_one_at_a_time(self) -> None:
        slot, go = self.root / "jobs" / "slots" / "s", self.root / "go"
        stale = slot.with_name("s.lock")
        stale.mkdir(parents=True)
        dead = subprocess.Popen(["true"])
        dead.wait()
        (stale / "pid").write_text(str(dead.pid))  # a runner that died holding it
        helper = self.root / "contend.py"
        helper.write_text(
            "import importlib.util, os, sys, time\n"
            "from pathlib import Path\n"
            f"sys.path.insert(0, {str(CAMPAIGN.parent)!r})\n"
            f"spec = importlib.util.spec_from_file_location('campaign', {str(CAMPAIGN)!r})\n"
            "m = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(m)\n"
            f"while not os.path.exists({str(go)!r}):\n"
            "    time.sleep(0.01)\n"
            f"m.slot_lock(Path({str(slot)!r}), Path(sys.argv[2] + '.beat'))\n"
            "began = time.time()\n"
            "time.sleep(0.3)\n"
            "Path(sys.argv[2]).write_text(f'{began} {time.time()}')\n"
        )
        spans = [self.root / f"span{i}" for i in range(6)]
        # the command line names _run, as a runner's does: the old lock judged its holder by it
        procs = [subprocess.Popen([sys.executable, str(helper), "_run", str(x)]) for x in spans]
        time.sleep(1)  # every contender is importing or waiting at the start line
        go.touch()
        for proc in procs:
            self.assertEqual(proc.wait(timeout=60), 0)
        held = sorted(tuple(map(float, x.read_text().split())) for x in spans)
        for (_, end), (start, _) in zip(held, held[1:]):
            self.assertGreaterEqual(start, end, f"two runners held the slot at once: {held}")


class Disk(CampaignCase):
    def aged(self, path: Path, days: float = 10) -> None:
        then = time.time() - days * 86400
        for d, dirs, files in os.walk(path, topdown=False):
            for name in [*files, *dirs]:
                os.utime(Path(d) / name, (then, then), follow_symlinks=False)
        os.utime(path, (then, then))

    def built(self, *dirs: Path, days: float = 10) -> None:
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)
            (d / "obj.o").write_bytes(b"x" * 300_000)
            if days:
                self.aged(d, days)

    def landed(self, pid: str) -> Path:
        wt = self.started(pid, files="app.py")
        self.commit(wt, {"app.py": f"print('{pid}')\n"})
        self.verified(pid)
        self.ok(self.home, "land", pid, "--keep")
        return wt

    def test_prune_removes_idle_build_dirs_only_outside_open_quests(self) -> None:
        self.commit(self.home, {"build-tools/gen.py": "X = 1\n"})  # tracked, then ignored by the pattern
        self.commit(self.home, {".gitignore": "build/\nbuild-*/\n.deps/\n.scratch/\n"})
        done = self.landed("done")
        current = self.started("current", files="README.md")
        manual = self.root / "manual"
        self.git(self.home, "worktree", "add", "-q", "-b", "manual", str(manual))
        probe = self.home / ".scratch" / "probe-wt"  # a review's probe, kept in a .scratch
        self.git(self.home, "worktree", "add", "-q", "--detach", str(probe))
        self.built(done / "build", done / "build-tools", done / "_deps", done / ".scratch", manual / ".deps")
        self.built(probe / "build")
        self.built(current / "build", self.home / "build")  # an open quest's, the main checkout's
        self.built(done / "build-asan", days=0)  # changed today
        (done / "notes.txt").write_text("untracked source\n")
        dry = self.ok(self.home, "job", "prune", "--dry-run").out
        self.assertRegex(dry, rf"build {re.escape(str(done / 'build'))}  \d+ MiB")
        self.assertIn("2 build dirs", dry)
        self.assertTrue((done / "build").exists() and (manual / ".deps").exists(), "a dry run deleted")
        out = self.ok(self.home, "job", "prune").out
        self.assertIn("2 build dirs", out)
        self.assertFalse((done / "build").exists())
        self.assertFalse((manual / ".deps").exists())
        for kept in (
            done / "build-asan",
            done / "build-tools" / "gen.py",
            done / "build-tools" / "obj.o",
            done / "_deps",
            done / ".scratch" / "obj.o",
            done / "notes.txt",
            done / "app.py",
            current / "build",
            self.home / "build",
            probe / "build",
        ):
            self.assertTrue(kept.exists(), f"prune deleted {kept}")
        log = (self.root / "jobs" / "prune.log").read_text()
        self.assertIn(f"removed build dir {done / 'build'} (", log)
        self.assertIn(f"removed build dir {manual / '.deps'} (", log)

    def test_sweep_removes_clean_merged_worktrees_and_keeps_the_rest(self) -> None:
        self.commit(self.home, {".gitignore": ".scratch/\n"})
        clean = self.landed("clean")
        probed = self.landed("probed")
        (probed / ".scratch").mkdir()
        (probed / ".scratch" / "model.py").write_text("reusable\n")
        current = self.started("current", files="README.md")  # at main, so merged, but open
        extra = {name: self.root / name for name in ("dirty", "busy", "wip")}
        for name, path in extra.items():
            self.git(self.home, "worktree", "add", "-q", "-b", name, str(path))
        (extra["dirty"] / "notes.txt").write_text("untracked\n")
        self.commit(extra["wip"], {"wip.py": "X = 1\n"})  # not merged, no quest
        sleeper = subprocess.Popen(["sleep", "60"], cwd=extra["busy"])
        try:
            dry = self.ok(self.home, "sweep", "--dry-run").out
            self.assertIn(f"remove {clean} (quest/demo/clean, merged)", dry)
            self.assertIn(f"keep {probed} (quest/demo/probed, merged): .scratch holds 1 file (.scratch/model.py)", dry)
            self.assertIn(f"keep {extra['dirty']} (dirty, merged): uncommitted or untracked files", dry)
            self.assertIn(f"keep {extra['busy']} (busy, merged): a process runs in it", dry)
            self.assertNotIn(str(current), dry)
            self.assertNotIn(str(extra["wip"]), dry)
            self.assertTrue(clean.exists(), "a dry run removed a worktree")
            out = self.ok(self.home, "sweep").out
            self.assertIn(f"removed worktree {clean} (quest/demo/clean, merged)", out)
            self.assertFalse(clean.exists())
            self.assertFalse(self.git(self.home, "branch", "--list", "quest/demo/clean"))
            self.assertTrue((probed / ".scratch" / "model.py").exists())
            self.ok(self.home, "sweep", "--force")
            self.assertFalse(probed.exists())
            for path in (current, *extra.values()):
                self.assertTrue(path.exists(), f"sweep removed {path}")
            self.assertTrue((extra["dirty"] / "notes.txt").exists())
            self.assertIn(f"removed worktree {clean}", (self.root / "jobs" / "prune.log").read_text())
        finally:
            sleeper.kill()
            sleeper.wait()


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
