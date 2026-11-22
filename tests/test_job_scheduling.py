"""Job scheduling: drains and batches, slots and CPU use, and contention for the machine."""

from __future__ import annotations

import json
import os
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from harness import CAMPAIGN, CampaignCase, alive, alone, campaign_module


class Batches(unittest.TestCase):
    """One drain of light work serves the ready heavy jobs back to back, up to BATCH_JOBS jobs or
    BATCH_MINUTES; the worker's tick (plan) is driven here with job records and a clock."""

    def setUp(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("CAMPAIGN_")}
        for patch in (mock.patch.dict(os.environ, env, clear=True), mock.patch.object(os, "cpu_count", lambda: 16)):
            patch.start()
            self.addCleanup(patch.stop)  # 8 light slots, 4 beside a heavy job
        self.module = campaign_module()

    def job(self, n: int, level: str, state: str, submitted: float, **st: Any) -> dict[str, Any]:
        after: list[str] = []
        spec = {"level": level, "minutes": 10, "submitted": submitted, "after": after, "cmd": f"job {n}", "cwd": ""}
        return {"id": f"{n:06d}", "spec": {**spec, "campaign": "c", "repo": ""}, "state": {"state": state, **st}}

    def test_a_ready_strict_job_runs_before_light_work_is_readmitted_until_the_batch_is_full(self) -> None:
        now = 1_800_000_000.0
        ended = self.job(1, "strict", "done", now - 300, claimed=now - 200, started=now - 200, ended=now - 1)
        lights = [self.job(10 + n, "light", "queued", now - 900) for n in range(8)]
        ready = self.job(2, "strict", "queued", now - 100)
        for jobs, (since, n), first in (
            ([ended, ready, *lights], (now - 200, 1), "strict"),  # the drain holds: the batch goes on
            ([ended, ready, *lights], (now - 200, self.module.BATCH_JOBS), "light"),  # full: light first
            ([ended, ready, *lights], (now - 60 * self.module.BATCH_MINUTES, 1), "light"),  # its minutes are up
            ([ended, self.job(3, "light", "running", now - 50, claimed=now), ready, *lights], (now - 200, 1), "light"),
        ):
            jobs[0]["state"]["batch"] = {"since": since, "jobs": n}
            start, _, _ = self.module.plan(jobs, now, {})
            levels = [j["spec"]["level"] for j in start]
            self.assertEqual(levels[0], first)
            joined = levels[0] == "strict"
            got = next(j for j in start if j["id"] == ready["id"])["state"]["batch"]
            self.assertEqual(got, {"since": since, "jobs": n + 1} if joined else {"since": now, "jobs": 1})
            running = sum(j["state"]["state"] == "running" for j in jobs)  # beside a strict job, its share
            self.assertEqual(levels.count("light"), (4 if joined else 8) - running)

    def test_a_full_batch_lets_light_work_go_first_even_inside_a_drain_turn(self) -> None:
        """Inside a drain turn, once an exclusive job ends the next queued one joins its batch before light
        work; a full batch lets light work go first once, and the drains behind it wait for their turn."""
        now = 1_800_000_000.0
        ended = self.job(1, "exclusive", "done", now - 300, claimed=now - 60, started=now - 60, ended=now - 1)
        ended["state"]["batch"] = {"since": now - 60, "jobs": 1}
        light = self.job(2, "light", "queued", now - 30)
        second = self.job(3, "exclusive", "queued", now - 30)
        turn = {"until": now + 3000, "for": "drain turn", "opened": now - 600}

        def started(*jobs: dict[str, Any]) -> list[str]:
            return [j["id"] for j in self.module.plan(list(jobs), now, turn)[0]]

        self.assertEqual(started(ended, light, second), [second["id"]])
        ended["state"]["batch"] = {"since": now - 600, "jobs": self.module.BATCH_JOBS}
        self.assertEqual(started(ended, light, second), [light["id"]])

    def test_a_heavy_jobs_build_takes_a_light_slot_and_its_command_then_waits_its_turn(self) -> None:
        now = 1_800_000_000.0
        strict = self.job(1, "strict", "running", now - 900, claimed=now - 60, started=now - 60)
        built = {"started": now - 300, "ended": now - 30, "exit": 0}
        lights = [self.job(10 + n, "light", "running", now - 900, claimed=now - 60, started=now - 60) for n in range(4)]
        measure = self.job(2, "strict", "queued", now - 600)
        measure["spec"].update(build="make", commit="c0ffee", slot="s")
        light = self.job(3, "light", "queued", now - 900)
        light["spec"].update(commit="c0ffee", slot="s")
        heavy = self.module.HEAVY

        def started(*jobs: dict[str, Any]) -> dict[str, dict[str, Any]]:
            return {j["id"]: j["state"] for j in self.module.plan(list(jobs), now, {})[0]}

        # its build is light work: beside a strict job, in a light slot (4 of 8) when one is free
        self.assertEqual(started(strict, measure, *lights), {})
        self.assertEqual(started(strict, measure, *lights[1:])[measure["id"]]["phase"], "build")
        # building, it holds no heavy slot: a ready strict job starts beside it
        building = {**measure, "state": {"state": "running", "phase": "build", "claimed": now - 300, "build": built}}
        self.assertIn(strict["id"], started(building, {**strict, "state": {"state": "queued"}}))
        # built, it waits for the running strict job, keeping its slot from the light job behind it
        waiting = {**building, "state": {**building["state"], "phase": "built"}}
        self.assertEqual(started(strict, waiting, light), {})
        done = {
            **strict,
            "state": {**strict["state"], "state": "done", "ended": now - 1, "batch": {"since": now, "jobs": 1}},
        }
        admitted = started(done, waiting, light)
        self.assertEqual(set(admitted), {measure["id"]})
        self.assertEqual((admitted[measure["id"]]["phase"], admitted[measure["id"]]["state"]), ("run", "running"))
        self.assertEqual(admitted[measure["id"]]["batch"]["jobs"], 2)
        # a job it runs after failed: it is skipped before its build
        measure["spec"]["after"] = [strict["id"]]
        failed = {**strict, "state": {"state": "failed", "ended": now}}
        self.assertEqual([j["id"] for j in self.module.plan([failed, measure], now, {})[1]], [measure["id"]])
        self.assertTrue(all(lv in heavy for lv in ("strict", "exclusive")))

    def test_a_drain_waiting_for_its_build_slot_holds_no_one_up(self) -> None:
        """An overdue exclusive job whose slot a light job holds neither opens a drain turn nor keeps light
        work out; once the slot is free it is due, and light work waits for it."""
        now = 1_800_000_000.0
        holder = self.job(1, "light", "running", now - 7200, claimed=now - 60, started=now - 60)
        drain = self.job(2, "exclusive", "queued", now - 7200)
        light = self.job(3, "light", "queued", now - 600)
        for j in (holder, drain):
            j["spec"].update(commit="c0ffee", slot="s")
        start, _, turn = self.module.plan([holder, drain, light], now, {})
        self.assertEqual(([j["id"] for j in start], turn), ([light["id"]], {}))
        freed = {**holder, "state": {"state": "done", "started": now - 60, "ended": now - 1}}
        start, _, turn = self.module.plan([freed, drain, light], now, {})
        self.assertEqual(([j["id"] for j in start], turn.get("for")), ([drain["id"]], "drain turn"))

    def test_a_built_drain_is_not_held_up_by_its_own_slot(self) -> None:
        """A built exclusive job keeps its slot while it waits for its hold: the light job queued behind
        it in that slot is not light work it waits out, and its own slot never keeps it from coming due."""
        now = 1_800_000_000.0
        built = {"started": now - 300, "ended": now - 200, "exit": 0}
        drain = self.job(1, "exclusive", "running", now - 7200, phase="built", claimed=now - 300, build=built)
        drain["spec"].update(build="make", commit="c0ffee", slot="s")
        behind = self.job(2, "light", "queued", now - 600)
        behind["spec"].update(commit="c0ffee", slot="s")
        start, _, _ = self.module.plan([drain, behind], now, {})
        self.assertEqual([(j["id"], j["state"]["phase"]) for j in start], [(drain["id"], "run")])
        other = self.job(3, "light", "queued", now - 600)  # waiting for the machine: the drain is overdue
        start, _, turn = self.module.plan([drain, behind, other], now, {})
        self.assertEqual(([j["id"] for j in start], turn.get("for")), ([drain["id"]], "drain turn"))

    def test_batches_cut_the_drains_of_a_replayed_queue(self) -> None:
        """The host's queue (12 h to 2026-11-18 07:38, 719 jobs, 81 strict or exclusive), replayed with
        each job's recorded length: 5.33 drains an hour with no batches, 2.83 with them (BATCH_JOBS 6,
        BATCH_MINUTES 30); a drain is a heavy job started after light work was readmitted."""
        with mock.patch.object(self.module.worker, "BATCH_JOBS", 1):
            before = self.replay()["drains"]
        after = self.replay()["drains"]
        self.assertLess(after, 0.7 * before, f"{before:.2f} drains an hour before, {after:.2f} after")

    def test_builds_leave_the_heavy_slot_in_a_replayed_queue(self) -> None:
        """The same queue with its 18 heavy jobs' builds (94 of 391 heavy minutes) as light work: heavy
        holds shrink by exactly the builds and light work keeps its full slots meanwhile, so light jobs
        wait less (p50 70.4 to 39.3 minutes) and use more (50% to 57%), at 2.83 drains an hour, not 3.08;
        builds queue behind light work of their priority, so heavy jobs wait more (p50 3.6 to 10.3)."""
        before, after = self.replay(staged=False), self.replay()
        self.assertAlmostEqual(before["heavy"] - after["heavy"], 94, delta=1)
        self.assertLess(after["light_wait"], 0.7 * before["light_wait"])
        self.assertGreater(after["light_use"], before["light_use"])
        self.assertLessEqual(after["drains"], before["drains"])

    def replay(self, staged: bool = True, tick: float = 30.0) -> dict[str, float]:
        """The host's queue run through plan, tick by tick: drains an hour, heavy hold minutes, heavy and
        light jobs' wait p50 (minutes, every phase's), and light slot use (builds' too). STAGED builds a
        heavy job's --build as light work; else its build holds its heavy slot, as before."""
        queue = json.loads((Path(__file__).parent / "fixtures" / "queue-12h.json").read_text())
        base = 1_800_000_000.0
        pending = sorted(queue["jobs"], key=lambda j: float(j["submitted"]))
        jobs: dict[str, dict[str, Any]] = {}
        took: dict[str, float] = {}
        built: dict[str, float] = {}
        turn: dict[str, Any] = {}
        t = 0.0
        while pending or any(j["state"]["state"] in ("queued", "running") for j in jobs.values()):
            now = base + t
            while pending and pending[0]["submitted"] <= t:
                q = pending.pop(0)
                job = self.job(q["id"], q["level"], "queued", base + q["submitted"])
                job["spec"].update(minutes=q["minutes"], priority=q["priority"], campaign=str(q["campaign"]))
                job["spec"]["after"] = [f"{x:06d}" for x in q["after"]]
                jobs[job["id"]], took[job["id"]] = job, q["took"] + (0 if staged else q.get("build", 0))
                if staged and "build" in q:
                    job["spec"]["build"], built[job["id"]] = f"build {q['id']}", q["build"]
            for jid, j in jobs.items():
                st = j["state"]
                if st.get("phase") == "build" and st["build"]["started"] + built[jid] <= now:
                    st.update(phase="built", build={**st["build"], "ended": st["build"]["started"] + built[jid]})
                elif st["state"] == "running" and st.get("started") and st["started"] + took[jid] <= now:
                    j["state"] = {**st, "state": "done", "ended": st["started"] + took[jid]}
            start, skip, turn = self.module.plan(list(jobs.values()), now, turn)
            for j in skip:
                jobs[j["id"]]["state"] = {"state": "skipped", "ended": now}
            for n, j in enumerate(start):  # claims within a tick keep their order
                st = {**j["state"], "state": "running"}
                if j["state"]["state"] == "running":  # admitted to its hold once built
                    st.update(admitted=now + n / 1000, started=now)
                else:
                    st.update(
                        claimed=now + n / 1000, **({"build": {"started": now}} if "phase" in st else {"started": now})
                    )
                jobs[j["id"]] = {**j, "state": st}
            t += tick
        done = [j for j in jobs.values() if j["state"]["state"] == "done"]
        views = self.module.phases(done)
        heavy = sorted(
            (v["state"] for v in views if v["spec"]["level"] in self.module.HEAVY), key=lambda s: s["claimed"]
        )
        light = [v["state"]["claimed"] for v in views if v["spec"]["level"] == "light"]
        drains = sum(
            i == 0 or any(heavy[i - 1]["ended"] < c < st["claimed"] for c in light) for i, st in enumerate(heavy)
        )
        for st in heavy:  # every batch keeps to its bound, and a command starts once its build ended
            self.assertLessEqual(st["batch"]["jobs"], self.module.BATCH_JOBS)
            self.assertLess(st["claimed"] - st["batch"]["since"], 60 * self.module.BATCH_MINUTES)
            self.assertGreaterEqual(st["started"], st.get("build", {}).get("ended", 0))
        waited: dict[str, float] = {j["id"]: 0.0 for j in done}
        for v, a, b in self.module.waits(done, base + t):
            waited[v.get("job", v)["id"]] += (b - a) / 60
        held = {level: 0.0 for level in ("light", *self.module.HEAVY)}
        for v, a, b in self.module.spans(done, base + t):
            held[v["spec"]["level"]] += (b - a) / 60
        hours = float(queue["hours"])

        def p50(heavy: bool) -> float:
            return statistics.median(
                waited[j["id"]] for j in done if (j["spec"]["level"] in self.module.HEAVY) == heavy
            )

        return {
            "drains": drains / hours,
            "heavy": held["strict"] + held["exclusive"],
            "heavy_wait": p50(True),
            "light_wait": p50(False),
            "light_use": held["light"] / (self.module.light_slots() * t / 60),
        }


class CpuUse(unittest.TestCase):
    """A light job takes the slots it declares (--cpus) or is measured keeping busy: on a 16-CPU host,
    8 light slots of 2 cores each."""

    def setUp(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("CAMPAIGN_")}
        for patch in (mock.patch.dict(os.environ, env, clear=True), mock.patch.object(os, "cpu_count", lambda: 16)):
            patch.start()
            self.addCleanup(patch.stop)
        self.module = campaign_module()

    def job(self, n: int, state: str, submitted: float, **st: Any) -> dict[str, Any]:
        after: list[str] = []
        spec = {"level": "light", "minutes": 10, "submitted": submitted, "after": after, "cmd": f"job {n}", "cwd": ""}
        return {"id": f"{n:06d}", "spec": {**spec, "campaign": "c", "repo": ""}, "state": {"state": state, **st}}

    def test_a_light_job_takes_the_slots_it_declares_or_is_charged(self) -> None:
        now = 1_800_000_000.0
        running = [self.job(10 + n, "running", now - 900, claimed=now - 60, started=now - 60) for n in range(4)]
        queued = [self.job(20 + n, "queued", now - 100) for n in range(4)]

        def started(jobs: list[dict[str, Any]], charges: dict[str, int] | None = None) -> list[str]:
            return [j["id"] for j in self.module.plan(jobs, now, {}, charges)[0]]

        self.assertEqual(len(started(running + queued)), 4)
        # one running job keeps 7 cores busy: charged 4 slots, it leaves one of the 8 free
        self.assertEqual(len(started(running + queued, {running[0]["id"]: 4})), 1)
        # --cpus 6 takes 3 slots from the start: it and one more fill the 4 free
        wide = self.job(30, "queued", now - 200)
        wide["spec"]["cpus"] = 6
        self.assertEqual(started([*running, wide, *queued]), [wide["id"], queued[0]["id"]])
        # too wide for the free slots, it lets the light work behind it go, until it has waited an hour
        wide["spec"]["cpus"] = 16
        self.assertEqual(len(started([*running, wide, *queued])), 4)
        wide["spec"]["submitted"] = now - 7200
        self.assertEqual(started([*running, wide, *queued]), [])

    def test_queue_health_counts_the_slots_each_light_job_was_charged(self) -> None:
        """Slot-minutes used and idle slots count each light job's charge: its --cpus, or the slot-seconds
        the worker recorded beyond its one slot (add_charge), not one slot a job."""
        now = 1_800_000_000.0
        wide = self.job(1, "running", now - 3600, claimed=now - 3600, started=now - 3600)
        wide["charge"] = 3 * 3600.0  # four slots on average
        declared = self.job(2, "running", now - 3600, claimed=now - 3600, started=now - 3600)
        declared["spec"]["cpus"] = 6  # three slots
        one = self.job(3, "running", now - 3600, claimed=now - 3600, started=now - 3600)
        waiting = self.job(4, "queued", now - 3600)
        h = self.module.queue_health([wide, declared, one, waiting], now)
        self.assertEqual((h["used"], h["capacity"], h["idle"]), (480, 480, 0))
        h = self.module.queue_health([wide, declared, waiting], now)
        self.assertEqual((h["used"], h["idle"]), (420, 60))
        self.assertIn(
            "light slots 100% used (480 of 480 slot-minutes)",
            self.module.health_lines(self.module.queue_health([wide, declared, one, waiting], now))[0],
        )
        with tempfile.TemporaryDirectory(dir=os.environ.get("CAMPAIGN_TEST_TMP")) as directory:
            d = Path(directory)
            self.module.add_charge(d, 3600.0)
            self.module.add_charge(d, 7200.0)
            self.assertEqual(self.module.read_job(d)["charge"], 10800.0)

    def test_the_charge_follows_the_cores_used_over_the_window(self) -> None:
        job = self.job(1, "running", 0, runner=4242)
        seen: dict[str, list[tuple[float, float]]] = {}
        used = {4242: 0.0}
        got: list[dict[str, Any]] = []

        def tree_cpu(roots: list[int]) -> dict[int, float]:
            return {r: used[r] for r in roots}

        with (
            mock.patch.object(self.module.worker, "tree_cpu", tree_cpu),
            mock.patch.object(self.module.worker, "CPU_WINDOW", 60),
        ):
            for t in range(0, 190, 10):
                used[4242] += 70.0 if t <= 120 else 15.0  # 7 cores, then 1.5
                got.append(self.module.meter([job], seen, 1000.0 + t))
            self.assertEqual(got[5], {}, "charged before a whole window")
            self.assertEqual(got[6][job["id"]], {"cores": 7.0, "slots": 4})
            self.assertEqual(got[-1][job["id"]], {"cores": 1.5, "slots": 1})
            self.module.meter([], seen, 2000.0)
        self.assertEqual(seen, {}, "an ended job keeps its samples")

    def test_a_process_tree_counts_its_live_and_its_reaped_descendants(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ.get("CAMPAIGN_TEST_TMP")) as directory:
            mark = Path(directory) / "burned"
            burn = "import time\nt = time.process_time()\nwhile time.process_time() - t < 0.5: pass\n"
            script = f"python3 -c \"{burn}\"; python3 -c \"{burn}open('{mark}', 'w'); time.sleep(30)\" & sleep 30"
            shell = subprocess.Popen(["bash", "-c", script], start_new_session=True)
            try:
                deadline = time.time() + 60
                while not mark.exists() and time.time() < deadline:
                    time.sleep(0.05)
                used = self.module.tree_cpu([shell.pid])[shell.pid]
            finally:
                os.killpg(shell.pid, signal.SIGKILL)
                shell.wait()
        self.assertGreaterEqual(used, 0.95, "a burner's seconds are missing")
        self.assertLess(used, 5)

    def test_submit_sees_concurrent_copies(self) -> None:
        copies = self.module.copies
        self.assertEqual(copies("cmake --build b -j 8 && ctest --parallel 6"), (8, "-j 8"))
        self.assertEqual(copies("xargs -P 3 ./t"), (3, "-P 3"))
        self.assertEqual(copies("./a & ./b & wait"), (2, "2 commands in the background"))
        self.assertEqual(
            copies("for t in a b; do ./$t & done; wait")[1], "a loop that sends each pass to the background"
        )
        nested = "for i in 1 2; do (while :; do :; done) & done; sleep 9"
        self.assertEqual(copies(nested)[1], "a loop that sends each pass to the background")
        self.assertEqual(copies("parallel ./t ::: 1 2"), (16, "parallel, one copy per CPU"))
        self.assertEqual(copies("make 2>&1 && ./t &> log"), (1, ""))


class Scheduling(CampaignCase):
    def test_jobs_submitted_in_one_second_run_in_submit_order(self) -> None:
        """A job id holds its second and a random suffix, so ids alone misorder jobs of one second."""
        order = campaign_module().queue_order

        def job(jid: str, submitted: float, level: str = "exclusive") -> dict[str, Any]:
            return {"id": jid, "spec": {"submitted": submitted, "level": level, "minutes": 1, "campaign": "c"}}

        first, second = job("261118-010101-ffffff", 100.25), job("261118-010101-000000", 100.75)
        self.assertEqual([j["id"] for j in order([second, first], set())], [first["id"], second["id"]])

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

    def test_a_passed_job_counts_across_docs_only_commits_whatever_it_ran_after(self) -> None:
        self.env["CAMPAIGN_STRACE"] = ""  # whole-tree identity on every platform: no read sets
        build = self.submit(self.home, "--", "true build")
        run = self.submit(self.home, "--after", build, "--", "true run")
        self.ok(self.home, "wait", run, "--timeout", "60")
        self.commit(self.home, {"README.md": "app, documented\n", "website/index.html": "<p>app</p>\n"})
        self.assertEqual(self.submit(self.home, "--", "true build"), build)
        self.assertEqual(self.submit(self.home, "--after", build, "--", "true run"), run)
        for code in ({"tests/README.md": "a fixture\n"}, {"bench/notes.md": "rows\n"}, {"docs/CMakeLists.txt": "x\n"}):
            self.commit(self.home, code)
            new = self.submit(self.home, "--", "true build")
            self.assertNotEqual(new, build, code)
            self.ok(self.home, "wait", new, "--timeout", "60")
            build = new

    def test_a_pending_job_is_reused_only_when_it_runs_after_the_same_jobs(self) -> None:
        slow = self.submit(self.home, "--", "sleep 3")
        queued = self.submit(self.home, "--after", slow, "--", "true")
        other = self.submit(self.home, "--", "exit 1")
        self.assertNotEqual(self.submit(self.home, "--after", other, "--", "true"), queued)
        self.assertEqual(self.submit(self.home, "--after", slow, "--", "true"), queued)
        self.run_cli(self.home, "wait", slow, queued, other, "--timeout", "60")
        self.assertEqual(self.job(queued)["state"], "done")

    def test_a_job_at_new_code_cancels_its_queued_older_instance(self) -> None:
        """A rerun, or a submit identical up to its commit, cancels the quest's queued older instance
        and says so; one a pending job waits on is kept."""
        wt = self.started("fix", files="app.py")
        go = self.root / "go"
        hold = f"while [ ! -e {go} ]; do sleep 0.1; done"
        gate = self.submit(wt, "--quest", "fix", "--", hold)
        self.wait_until(lambda: self.job(gate)["state"] == "running")
        old = self.submit(wt, "--quest", "fix", "--after", gate, "--", "python3 app.py")
        self.commit(wt, {"app.py": "print(2)\n"})
        out = self.ok(self.home, "job", "rerun", old).out
        new = next(line.split(":")[0] for line in out.splitlines() if f": {old} at" in line)
        self.assertIn(f"superseded {old}", out)
        self.assertEqual(self.job(old)["state"], "cancelled")
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

    def test_a_rerun_drops_a_predecessor_that_ended_unpassed(self) -> None:
        """A job skipped because the job it ran after failed runs when rerun alone."""
        wt = self.started("fix", files="app.py")
        bad = self.submit(wt, "--quest", "fix", "--", "exit 3")
        after = self.submit(wt, "--quest", "fix", "--after", bad, "--", "python3 app.py")
        self.run_cli(self.home, "wait", bad, after, "--timeout", "60")
        self.assertEqual(self.job(after)["state"], "skipped")
        self.commit(wt, {"app.py": "print(2)\n"})
        out = self.ok(self.home, "job", "rerun", after).out
        self.assertIn(f"no longer after {bad}, which ended failed", out)
        new = out.splitlines()[-1].split(":")[0].split()[0]
        self.ok(self.home, "wait", new, "--timeout", "60")
        self.assertEqual(self.job(new)["state"], "done")

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

    def test_the_worker_leaves_no_zombie_runners(self) -> None:
        """The worker reaps each runner it spawned, those of the code it re-executed over included."""
        go = self.root / "go"
        blocker = self.submit(self.home, "--", f"while [ ! -e {go} ]; do sleep 0.1; done")
        self.wait_until(lambda: self.job(blocker)["state"] == "running")
        worker = int((self.root / "jobs" / ".worker" / "pid").read_text())
        for turn in range(2):
            quick = [self.submit(self.home, "--force", "--", f"echo {turn} {n}") for n in range(6)]
            self.ok(self.home, "wait", *quick, "--timeout", "60")
            if not turn:
                self.ok(self.home, "worker", "--restart")
        time.sleep(1)  # a few ticks
        listed = subprocess.run(["ps", "-A", "-o", "ppid=,stat="], capture_output=True, text=True).stdout
        zombies = [x for x in listed.splitlines() if x.split()[0] == str(worker) and x.split()[1].startswith("Z")]
        self.assertEqual(zombies, [], "finished runners left as zombies")
        go.touch()
        self.ok(self.home, "wait", blocker, "--timeout", "30")

    def test_a_restarted_worker_reaps_children_its_old_code_never_recorded(self) -> None:
        """An older worker's image leaves exited children nobody recorded; the code it re-executes
        into finds them and reaps them."""
        go = self.root / "go"
        blocker = self.submit(self.home, "--", f"while [ ! -e {go} ]; do sleep 0.1; done")
        self.wait_until(lambda: self.job(blocker)["state"] == "running")
        home = self.root / "jobs" / ".worker"
        old = (
            "import os, subprocess, sys, time\n"
            "subprocess.Popen(['true'])\n"  # exits and is never waited for
            "time.sleep(0.5)\n"
            f"open({str(home / 'pid.new')!r}, 'w').write(str(os.getpid()))\n"
            f"os.replace({str(home / 'pid.new')!r}, {str(home / 'pid')!r})\n"  # holds the home, as a restart does
            f"os.execv(sys.executable, [sys.executable, {str(CAMPAIGN)!r}, 'worker'])\n"
        )
        worker = subprocess.Popen(
            [sys.executable, "-c", old],
            env=self.env,
            cwd=self.root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.wait_until(lambda: (home / "pid").read_text() == str(worker.pid))
        beat = (home / "heartbeat").stat().st_mtime
        self.wait_until(lambda: (home / "heartbeat").stat().st_mtime > beat + 0.5)  # a few of its ticks
        listed = subprocess.run(["ps", "-A", "-o", "ppid=,stat="], capture_output=True, text=True).stdout
        zombies = [x for x in listed.splitlines() if x.split()[0] == str(worker.pid) and x.split()[1].startswith("Z")]
        self.assertEqual(zombies, [], "a child the old image never recorded stayed a zombie")
        go.touch()
        self.ok(self.home, "wait", blocker, "--timeout", "30")
        worker.wait(timeout=30)

    def test_submit_records_cpus_and_warns_of_undeclared_copies(self) -> None:
        self.env["CAMPAIGN_LIGHT_JOBS"] = str(os.cpu_count() or 4)  # a core a slot
        r = self.ok(self.home, "job", "submit", "--", "make -j 8")
        self.assertIn("this light job runs -j 8, more than a light slot's 1 cores: --cpus N declares them", r.err)
        r = self.ok(self.home, "job", "submit", "--cpus", "8", "--force", "--", "make -j 8")
        self.assertNotIn("more than a light slot", r.err)
        self.assertIn("cpus: 8", r.err)
        self.assertEqual(json.loads((self.root / "jobs" / r.out.split()[0] / "spec.json").read_text())["cpus"], 8)
        self.env["CAMPAIGN_LIGHT_JOBS"] = str(4 * (os.cpu_count() or 4))  # a quarter core a slot
        self.assertNotIn("more than a light slot", self.ok(self.home, "job", "submit", "--", "python3 app.py").err)

    @alone
    def test_a_light_job_over_its_share_is_charged_and_shown(self) -> None:
        # a sixteenth of a core a slot: charged on a loaded host too, where nice 19 gets little CPU
        slots = str(16 * (os.cpu_count() or 4))
        self.env.update(CAMPAIGN_LIGHT_JOBS=slots, CAMPAIGN_CPU_WINDOW="1", CAMPAIGN_CPU_SAMPLE="0.2")
        busy = "for i in 1 2 3 4 5 6 7 8; do (while :; do :; done) & done; sleep 60"
        r = self.ok(self.home, "job", "submit", "--", busy)
        self.assertIn("a loop that sends each pass to the background", r.err)
        jid = r.out.split()[0]
        use = self.root / "jobs" / ".worker" / "cpu.json"

        def charged() -> int:
            try:
                return int(json.loads(use.read_text())["jobs"].get(jid, {}).get("slots", 0))
            except (OSError, ValueError):
                return 0

        self.wait_until(lambda: charged() >= 2, timeout=60)
        self.assertIn(f"light jobs over their share: {jid} (demo: ", self.ok(self.home, "status").out)
        self.ok(self.home, "job", "cancel", jid)

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

    def test_a_slots_refs_keep_one_path_reset_to_the_ref_per_job(self) -> None:
        base = self.git(self.home, "rev-parse", "HEAD")
        self.commit(self.home, {"README.md": "second\n"})
        second = self.git(self.home, "rev-parse", "HEAD")
        show = 'echo "$JOB_REF_BASE" > "$JOB_OUT/path"; cat "$JOB_REF_BASE/README.md" > "$JOB_OUT/seen"; touch "$JOB_REF_BASE/built"'
        one = self.submit(self.home, "--slot", "s", "--ref", f"base={base}", "--", show)
        two = self.submit(self.home, "--slot", "s", "--ref", f"base={second}", "--", show)
        self.ok(self.home, "wait", one, two, "--timeout", "60")
        out = [self.root / "jobs" / x / "a1" / "out" for x in (one, two)]
        stable = self.root / "jobs" / "slots" / "s" / "ref-base"
        self.assertEqual([(o / "path").read_text().strip() for o in out], [str(stable)] * 2)
        self.assertEqual([(o / "seen").read_text() for o in out], ["app\n", "second\n"])
        self.assertTrue((stable / "built").exists(), "the slot's ref was recreated: a build dir in it is lost")
        self.assertEqual(self.git(stable, "rev-parse", "HEAD"), second)
        alone = self.submit(self.home, "--force", "--ref", f"base={base}", "--", show)  # no slot: its own directory
        self.ok(self.home, "wait", alone, "--timeout", "60")
        own = self.root / "jobs" / alone
        self.assertEqual((own / "a1" / "out" / "path").read_text().strip(), str(own / "ref-base"))

    def test_submit_prints_back_what_it_parsed(self) -> None:
        base = self.git(self.home, "rev-parse", "HEAD")
        shown = self.ok(self.home, "job", "submit", "--ref", f"base={base}", "--level", "none", "--", "true").err
        self.assertIn("command: true", shown)
        self.assertIn(f"ref: base={self.home.name}@{base[:12]}", shown)
        misplaced = self.ok(self.home, "job", "submit", "--level", "none", "--", "true", "--ref", f"base={base}").err
        self.assertIn("command: true --ref", misplaced)
        self.assertIn("--ref after -- belongs to the command", misplaced)

    def test_a_quests_jobs_wait_for_its_slot_and_run_there(self) -> None:
        """A job of a quest runs in the quest's build slot (verify's, or NAME@REPO in another repository)
        whatever its level: one submitted while another holds the slot waits, then runs there, on the
        first one's build. --fresh checks out afresh; critical quests and jobs of no quest have no slot."""
        wt = self.started("fix")
        self.ok(self.home, "new", "risky", "--title", "risky", "--risk", "critical", "--check", "unit=true")
        lib = self.repo("lib", {"lib.py": "X = 1\n"})
        go, slot = self.root / "go", self.root / "jobs" / "slots" / "demo-fix"

        def spec(jid: str) -> dict[str, Any]:
            return dict(json.loads((self.root / "jobs" / jid / "spec.json").read_text()))

        def seen(jid: str) -> list[str]:
            log = (self.root / "jobs" / jid / "a1" / "log").read_text().split("\n")
            return [x for x in log if x and not x.startswith("quiet")]

        build = f"mkdir -p build && echo built > build/out; while [ ! -e {go} ]; do sleep 0.1; done"
        first = self.submit(wt, "--quest", "fix", "--", build)
        self.wait_until(lambda: self.job(first)["state"] == "running")
        second = self.submit(wt, "--quest", "fix", "--level", "strict", "--", "cat build/out && pwd")
        time.sleep(1)  # several worker ticks
        self.assertEqual(self.job(second)["state"], "queued", "a second job entered a busy slot")
        go.touch()
        self.ok(self.home, "wait", first, second, "--timeout", "60")
        self.assertEqual([spec(x)["slot"] for x in (first, second)], ["demo-fix", "demo-fix"])
        self.assertGreaterEqual(self.job(second)["started"], self.job(first)["ended"])
        out = seen(second)
        self.assertEqual((out[0], Path(out[1]).resolve()), ("built", slot.resolve()))
        fresh = self.submit(wt, "--quest", "fix", "--fresh", "--", "test ! -e build/out && pwd")
        self.ok(self.home, "wait", fresh, "--timeout", "60")
        self.assertEqual(spec(fresh)["slot"], "")
        self.assertEqual(Path(seen(fresh)[0]).resolve(), (self.root / "jobs" / fresh / "src").resolve())
        other = self.submit(wt, "--quest", "fix", "--repo", str(lib), "--commit", "HEAD", "--", "true")
        self.assertEqual(spec(other)["slot"], "demo-fix@lib")
        self.assertEqual(spec(self.submit(wt, "--quest", "risky", "--", "true"))["slot"], "")
        self.assertEqual(spec(self.submit(wt, "--", "true"))["slot"], "")

    def test_a_measurement_runs_in_the_checkout_its_build_made(self) -> None:
        """--build builds under light, then runs the command at the job's level in the same checkout:
        the quest's build slot for a --quest job, whatever its level."""
        wt = self.started("fix")
        build = "mkdir -p build && echo built > build/out && echo $QUIET_HELD > build/held"
        run = 'cat build/out && cat build/held && echo "$QUIET_HELD" && pwd'
        jid = self.submit(wt, "--quest", "fix", "--level", "strict", "--build", build, "--", run)
        self.ok(self.home, "wait", jid, "--timeout", "60")
        spec = json.loads((self.root / "jobs" / jid / "spec.json").read_text())
        self.assertEqual(spec["slot"], "demo-fix")
        out = [
            x for x in (self.root / "jobs" / jid / "a1" / "log").read_text().split("\n") if not x.startswith("quiet")
        ]
        self.assertEqual(out[0], "built")
        self.assertTrue(out[1].startswith(f"job-{jid}-build light"), out)  # the build ran under light
        self.assertTrue(out[2].startswith(f"job-{jid} "), out)  # the run under its own strict hold
        self.assertEqual(Path(out[3]).resolve(), (self.root / "jobs" / "slots" / "demo-fix").resolve())
        st = self.job(jid)  # its build's times, then its hold's, admitted by the worker once built
        self.assertEqual((st["phase"], st["build"]["exit"]), ("run", 0))
        self.assertLessEqual(st["build"]["started"], st["build"]["ended"])
        self.assertLessEqual(st["build"]["ended"], st["admitted"])
        self.assertLessEqual(st["admitted"], st["started"])
        again = self.submit(wt, "--quest", "fix", "--level", "strict", "--build", "exit 3", "--", run)
        self.run_cli(self.home, "wait", again, "--timeout", "60")
        self.assertEqual((self.job(again)["state"], self.job(again)["note"]), ("failed", "its build failed (exit 3)"))
        self.assertEqual(self.job(again)["build"]["exit"], 3)
        self.assertNotIn("started", self.job(again))  # it never asked for its strict hold
        self.assertFalse((self.root / "jobs" / again / "admitted").exists())
        self.assertNotIn(f"quiet: job-{again} ", (self.root / "jobs" / again / "a1" / "log").read_text())
        self.assertNotEqual(again, jid)  # another build is another job

    def test_a_built_job_cancelled_before_its_hold_never_takes_it(self) -> None:
        d = self.root / "jobs" / "built"
        d.mkdir(parents=True)
        (d / "spec.json").write_text("{}")
        (d / "state.json").write_text(json.dumps({"state": "running", "phase": "built"}))
        (d / "cancel").touch()
        module = campaign_module()
        self.assertEqual(module.admission(d, time.time() + 60), "cancelled")
        self.assertFalse(module.admit(module.load_job(d)), "the worker admits it after its cancel")
        (d / "cancel").unlink()
        shutil.rmtree(d / "admitted")
        self.assertTrue(module.admit({**module.load_job(d), "state": {"state": "running", "phase": "run"}}))
        self.assertEqual(module.admission(d, time.time()), "")  # admitted wins over its limit

    def test_runners_breaking_a_dead_lock_at_once_take_the_slot_one_at_a_time(self) -> None:
        slot, go = self.root / "jobs" / "slots" / "s", self.root / "go"
        stale = slot.with_name("s.lock")
        stale.mkdir(parents=True)
        dead = subprocess.Popen(["true"])
        dead.wait()
        (stale / "pid").write_text(str(dead.pid))  # a runner that died holding it
        helper = self.root / "contend.py"
        helper.write_text(
            "import os, sys, time\n"
            "from pathlib import Path\n"
            f"sys.path.insert(0, {str(CAMPAIGN.parent)!r})\n"
            "from campaign_tool import runner as m\n"
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
