"""A job's life and end: heartbeats, limits, crashes, pruning and disk."""

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

from harness import CampaignCase, alive, campaign_module, session_members


class Races(unittest.TestCase):
    def test_a_heartbeat_removed_between_checks_reads_as_no_worker(self) -> None:
        module = campaign_module()
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"CAMPAIGN_JOBS": directory}):
            (Path(directory) / ".worker").mkdir()
            # the worker removes its heartbeat right after an existence check says it is there
            with mock.patch.object(Path, "exists", return_value=True):
                self.assertFalse(module.worker_alive())


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

    def test_measurements_and_fuzzing_last_ten_minutes_unless_deferred_as_long(self) -> None:
        self.fails(
            self.home, "job", "submit", "--level", "strict", "--minutes", "11", "--", "true", match="at most 10 minutes"
        )
        self.fails(self.home, "job", "submit", "--minutes", "31", "--", "./fuzz-reader", match="at most 30 minutes")
        self.submit(self.home, "--minutes", "25", "--", "./fuzz-reader")  # a light fuzz shares a light slot
        short = self.submit(self.home, "--level", "exclusive", "--", "true")
        self.assertEqual(json.loads((self.root / "jobs" / short / "spec.json").read_text())["minutes"], 10)
        self.fails(self.home, "job", "submit", "--level", "strict", "--minutes", "45", "--long", "x", "--", "true")
        self.fails(self.home, "job", "submit", "--long", " ", "--", "true", match="--long needs a reason")
        soak = self.submit(self.home, "--minutes", "60", "--long", "the hour-long soak", "--", "./soak-cache")
        spec = json.loads((self.root / "jobs" / soak / "spec.json").read_text())
        self.assertEqual((spec["minutes"], spec["priority"], spec["long"]), (60, -1, "the hour-long soak"))
        self.fails(self.home, "new", "x", "--title", "x", "--check", "bench:strict:20=true", match="at most 10")
        self.ok(self.home, "new", "y", "--title", "y", "--check", "bench:strict=true")
        self.assertEqual(self.ok(self.home, "get", "y", "check").out, "bench:strict:10=true\n")

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
