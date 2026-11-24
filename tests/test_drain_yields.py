"""A drain yields until its turn: light work runs while a claimed drain waits for its hold, a deferred
drain waits for a stretch with no work of higher priority, and a due drain still gets the machine."""

from __future__ import annotations

import os
import random
import statistics
import time
import unittest
from types import ModuleType
from typing import Any
from unittest import mock

from harness import CampaignCase, campaign_module

STARVE = 600.0  # quiet's QUIET_STARVE_MIN, in seconds
HEAVY = ("strict", "exclusive")
BASE = 1_800_000_000.0
NOW = 1_800_000_000.0


def record(q: dict[str, Any]) -> dict[str, Any]:
    """The queued job record of a queue entry (id, level, minutes, priority, campaign, submitted in
    seconds from BASE, after, and a build's seconds when it has one)."""
    spec = {
        "level": q["level"],
        "minutes": q["minutes"],
        "priority": q["priority"],
        "submitted": BASE + q["submitted"],
        "after": [f"{x:06d}" for x in q["after"]],
        "cmd": f"job {q['id']}",
        "cwd": "",
        "campaign": str(q["campaign"]),
        "repo": "",
    }
    if "build" in q:
        spec["build"] = f"build {q['id']}"
    return {"id": f"{q['id']:06d}", "spec": spec, "state": {"state": "queued"}}


def simulate(module: ModuleType, queue: list[dict[str, Any]], tick: float = 30.0) -> dict[str, Any]:
    """QUEUE through MODULE's plan on a 16-CPU host, tick by tick, each claim taking its hold as quiet
    grants it: light holds up to 8, none beside a live exclusive hold, nor beside a waiting one unless
    the light request waited STARVE; a heavy hold once no earlier heavy request or starving light one
    waits and no heavy hold runs, an exclusive one once no light hold runs. A drain claimed in phase
    "built" asks for no hold until the worker admits it. Returns the jobs at the end, the holds
    ((key, level, granted, ended)), the end, and the light slot-seconds and seconds that runnable light
    work waited beside a free light slot while no exclusive hold ran."""
    slots = 8
    env = {k: v for k, v in os.environ.items() if not k.startswith("CAMPAIGN_")}
    with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(os, "cpu_count", lambda: 16):
        pending = sorted(queue, key=lambda q: float(q["submitted"]))
        took = {f"{q['id']:06d}": float(q["took"]) for q in queue}
        built = {f"{q['id']:06d}": float(q["build"]) for q in queue if "build" in q}
        jobs: dict[str, dict[str, Any]] = {}
        asks: dict[str, tuple[str, float]] = {}  # hold requests waiting: (level, since)
        holds: dict[str, tuple[str, float]] = {}  # live holds: (level, granted)
        log: list[tuple[str, str, float, float]] = []
        idle = idle_time = 0.0
        turn: dict[str, Any] = {}
        t = 0.0
        while pending or asks or holds or any(j["state"]["state"] in ("queued", "running") for j in jobs.values()):
            now = BASE + t
            while pending and pending[0]["submitted"] <= t:
                q = pending.pop(0)
                jobs[f"{q['id']:06d}"] = record(q)
            for key, (level, granted) in list(holds.items()):  # holds end
                jid, _, phase = key.partition(":")
                length = built[jid] if phase else took[jid]
                if granted + length > now:
                    continue
                del holds[key]
                log.append((key, level, granted, granted + length))
                st = jobs[jid]["state"]
                if phase:
                    st.update(phase="built", build={**st["build"], "ended": granted + length})
                else:
                    jobs[jid]["state"] = {**st, "state": "done", "began": granted, "ended": granted + length}
            for key, (level, since) in sorted(asks.items(), key=lambda kv: kv[1][1]):  # quiet grants holds
                live = [lv for lv, _ in holds.values()]
                waiting_x = any(lv == "exclusive" for k, (lv, _) in asks.items() if k != key)
                starving = any(lv == "light" and now - s >= STARVE for k, (lv, s) in asks.items() if k != key)
                if level == "light":
                    ok = live.count("light") < slots and "exclusive" not in live
                    ok = ok and (not waiting_x or now - since >= STARVE)
                else:
                    earlier = any(lv in HEAVY and s < since for k, (lv, s) in asks.items() if k != key)
                    ok = not earlier and not starving and not any(lv in HEAVY for lv in live)
                    ok = ok and (level != "exclusive" or "light" not in live)
                if ok:
                    del asks[key]
                    holds[key] = (level, now)
            live = [lv for lv, _ in holds.values()]
            if "exclusive" not in live:
                done = {k for k, j in jobs.items() if j["state"]["state"] == "done"}
                ready = sum(
                    j["spec"]["level"] == "light"
                    and j["state"]["state"] == "queued"
                    and set(j["spec"]["after"]) <= done
                    for j in jobs.values()
                ) + sum(lv == "light" for lv, _ in asks.values())
                free = min(ready, slots - live.count("light"))
                idle += tick * free
                idle_time += tick * (free > 0)
            start, skip, turn = module.plan(list(jobs.values()), now, turn)
            for j in skip:
                jobs[j["id"]]["state"] = {"state": "skipped", "ended": now}
            for n, j in enumerate(start):  # claims within a tick keep their order
                st = {**j["state"], "state": "running"}
                at = now + n / 1000
                if j["state"]["state"] == "running":  # built, or claimed before its turn: admitted to its hold
                    st.update(admitted=at, started=at)
                    asks[j["id"]] = (j["spec"]["level"], at)
                elif st.get("phase") == "build":
                    st.update(claimed=at, build={"started": at})
                    asks[f"{j['id']}:build"] = ("light", at)
                elif st.get("phase") == "built":  # claimed: it waits for its admission
                    st.update(claimed=at)
                else:
                    st.update(claimed=at, started=at)
                    asks[j["id"]] = (j["spec"]["level"], at)
                jobs[j["id"]] = {**j, "state": st}
            t += tick
        return {
            "jobs": list(jobs.values()),
            "holds": log,
            "end": BASE + t,
            "slots": slots,
            "idle": idle,
            "idle_time": idle_time,
        }


def metrics(run: dict[str, Any], drain_wait: float = 1200.0) -> dict[str, Any]:
    """Light slot use over the run, light slot-minutes and minutes runnable light work waited beside a free
    slot, minutes a drain held the host (admission to end), hours to empty, light jobs' wait p50 (submit
    to hold), and each drain's wait from its due (its submit plus DRAIN_WAIT) to its hold, in minutes."""
    holds, end = run["holds"], run["end"]
    first = min(float(j["spec"]["submitted"]) for j in run["jobs"])
    light = sum(b - a for _, lv, a, b in holds if lv == "light")
    spans = sorted(
        (float(j["state"]["started"]), float(j["state"]["ended"]))
        for j in run["jobs"]
        if j["spec"]["level"] == "exclusive" and j["state"].get("ended") and j["state"].get("started")
    )
    drained, reach = 0.0, first
    for a, b in spans:  # the union of the drains' spans
        drained += max(0.0, b - max(a, reach))
        reach = max(reach, b)
    granted = {key: a for key, lv, a, _ in holds if ":" not in key}
    submitted = {j["id"]: float(j["spec"]["submitted"]) for j in run["jobs"]}
    waits = [a - submitted[key] for key, lv, a, _ in holds if lv == "light" and ":" not in key]
    drains = {
        j["id"]: (granted[j["id"]] - submitted[j["id"]] - drain_wait) / 60
        for j in run["jobs"]
        if j["spec"]["level"] == "exclusive" and j["id"] in granted
    }
    return {
        "light_use": light / (run["slots"] * (end - first)),
        "idle": run["idle"] / 60,
        "idle_minutes": run["idle_time"] / 60,
        "drained": drained / 60,
        "hours": (end - first) / 3600,
        "light_wait": statistics.median(waits) / 60 if waits else 0.0,
        "drains": drains,
    }


class Plan(unittest.TestCase):
    """The worker's tick (plan) with job records and a clock: 8 light slots, 4 beside a heavy job."""

    def setUp(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("CAMPAIGN_")}
        for patch in (mock.patch.dict(os.environ, env, clear=True), mock.patch.object(os, "cpu_count", lambda: 16)):
            patch.start()
            self.addCleanup(patch.stop)
        self.module = campaign_module()

    def job(self, n: int, level: str, state: str, submitted: float, priority: int = 0, **st: Any) -> dict[str, Any]:
        after: list[str] = []
        spec = {"level": level, "minutes": 10, "priority": priority, "submitted": submitted, "after": after}
        spec.update(cmd=f"job {n}", cwd="", campaign="c", repo="")
        return {"id": f"{n:06d}", "spec": spec, "state": {"state": state, **st}}

    def started(self, *jobs: dict[str, Any], turn: dict[str, Any] | None = None) -> list[str]:
        return [j["id"] for j in self.module.plan(list(jobs), NOW, turn or {})[0]]

    def test_light_work_runs_while_a_claimed_drain_waits(self) -> None:
        """A drain claimed in a lull (built, or claimed before its turn) holds no one up before it is due:
        a light job of higher priority that ends before then starts, and the drain is admitted only once
        no light job runs."""
        build = {"started": NOW - 300, "ended": NOW - 200, "exit": 0}
        built = self.job(1, "exclusive", "running", NOW - 300, phase="built", claimed=NOW - 300, build=build)
        built["spec"]["build"] = "make"
        waiting = self.job(2, "exclusive", "running", NOW - 300, phase="built", claimed=NOW - 300)
        for drain in (built, waiting):
            light = self.job(3, "light", "queued", NOW - 10, priority=1)
            self.assertEqual(self.started(drain, light), [light["id"]])
            self.assertEqual(self.started(drain), [drain["id"]])

    def test_a_deferred_drain_past_its_wait_holds_no_one_up_while_higher_work_waits(self) -> None:
        """The 2026-11-24 06:10 shape: a built priority -1 drain an hour past its submit, light work of
        priority 1 queued. The light work starts; the drain waits and opens no turn."""
        build = {"started": NOW - 3500, "ended": NOW - 3400, "exit": 0}
        drain = self.job(1, "exclusive", "running", NOW - 3600, -1, phase="built", claimed=NOW - 3500, build=build)
        drain["spec"]["build"] = "make"
        light = self.job(2, "light", "queued", NOW - 600, priority=1)
        start, _, turn = self.module.plan([drain, light], NOW, {})
        self.assertEqual(([j["id"] for j in start], turn), ([light["id"]], {}))

    def test_a_deferred_drain_waits_for_a_stretch_with_no_higher_work(self) -> None:
        """A priority -1 drain is not claimed while work of higher priority is queued, light or heavy, even
        when the running light work ends soon; it is claimed once nothing of higher priority has waited
        for DRAIN_WAIT."""
        drain = self.job(1, "exclusive", "queued", NOW - 7200, -1)
        lights = [
            self.job(10 + n, "light", "running", NOW - 900, 1, claimed=NOW - 100, started=NOW - 100) for n in range(8)
        ]
        for higher in (self.job(2, "light", "queued", NOW - 600, 1), self.job(3, "strict", "queued", NOW - 600)):
            blocked = [*lights[:7], self.job(4, "strict", "running", NOW - 900, claimed=NOW - 60, started=NOW - 60)]
            for running in (lights, blocked):
                start, _, turn = self.module.plan([drain, higher, *running], NOW, {})
                self.assertNotIn(drain["id"], [j["id"] for j in start])
                self.assertEqual(turn, {})
        wait = 2 * self.module.SHORT_RUN * 60
        for ago, claimed in ((wait - 60, False), (wait + 60, True)):
            gone = self.job(2, "light", "done", NOW - 7000, 1, claimed=NOW - ago, started=NOW - ago, ended=NOW - 30)
            self.assertEqual(drain["id"] in self.started(drain, gone), claimed)

    def test_an_overdue_drain_behind_a_strict_job_lets_in_light_work_that_ends_before_it(self) -> None:
        """The heavy slot is the strict job's for 8 more minutes: a light job expected to take 5 starts,
        one declaring 10 waits."""
        drain = self.job(1, "exclusive", "queued", NOW - 7200)
        strict = self.job(2, "strict", "running", NOW - 600, claimed=NOW - 120, started=NOW - 120)
        short, long = (self.job(n, "light", "queued", NOW - 60, 1) for n in (3, 4))
        short["spec"]["minutes"] = 5
        self.assertEqual(
            self.started(drain, strict, short, long, turn={"until": NOW + 600, "opened": NOW}), [short["id"]]
        )

    def test_a_deferred_drain_waiting_aside_holds_no_other_drain_back(self) -> None:
        """A priority -1 drain claimed in its stretch, then passed by work of higher priority, waits aside:
        a due drain of priority 0 is claimed beside it."""
        aside = self.job(1, "exclusive", "running", NOW - 7200, -1, phase="built", claimed=NOW - 600)
        light = self.job(2, "light", "running", NOW - 300, 1, claimed=NOW - 300, started=NOW - 300)
        higher = self.job(3, "light", "queued", NOW - 60, 1)
        due = self.job(4, "exclusive", "queued", NOW - 7200)
        start = {j["id"]: j["state"] for j in self.module.plan([aside, light, higher, due], NOW, {})[0]}
        self.assertEqual(start[due["id"]]["phase"], "built")
        self.assertNotIn(aside["id"], start)

    def test_one_drain_is_claimed_at_a_time(self) -> None:
        """Two drains with builds in a lull: the first's build is claimed, the second stays queued until the
        first ends."""
        drains = [self.job(n, "exclusive", "queued", NOW - 60) for n in (1, 2)]
        for d in drains:
            d["spec"]["build"] = "make"
        self.assertEqual(self.started(*drains), [drains[0]["id"]])
        claimed = {
            **drains[0],
            "state": {"state": "running", "phase": "build", "claimed": NOW, "build": {"started": NOW}},
        }
        self.assertEqual(self.started(claimed, drains[1]), [])
        ended = {**drains[0], "state": {"state": "done", "claimed": NOW - 60, "started": NOW - 30, "ended": NOW - 1}}
        self.assertEqual(self.started(ended, drains[1]), [drains[1]["id"]])

    def test_a_due_drain_gets_the_machine_under_a_stream_of_light_work(self) -> None:
        """A3: light jobs of priority 1 (10-30 minutes declared, 60-100% of it run) arrive every 90 seconds
        for four hours from three campaigns, more than 8 slots hold; drains of priority 0, with and
        without a build, arrive among them. Each drain takes its hold within DRAIN_TURN of its due."""
        rng = random.Random(1124)
        queue: list[dict[str, Any]] = []
        for n in range(160):
            minutes = rng.choice((10, 15, 20, 25, 30))
            light = {"level": "light", "minutes": minutes, "priority": 1, "campaign": n % 3}
            queue.append(
                {**light, "id": n, "submitted": 90.0 * n, "after": [], "took": 60 * minutes * rng.uniform(0.6, 1)}
            )
        for k, at in enumerate((1800, 6000, 10200)):
            for build in (False, True):
                n = len(queue)
                drain: dict[str, Any] = {"id": n, "level": "exclusive", "minutes": 10, "priority": 0}
                drain.update(campaign=k % 3, submitted=float(at + 60 * build), after=[], took=300.0)
                queue.append({**drain, **({"build": 240.0} if build else {})})
        m = metrics(simulate(self.module, queue))
        turn = 3600 / 60
        self.assertEqual(len(m["drains"]), 6)
        late = {k: round(w) for k, w in m["drains"].items() if w > turn}
        self.assertFalse(late, f"drains past DRAIN_TURN after their due, in minutes: {late}")


class Yielding(CampaignCase):
    """The worker and quiet for real: a claimed drain asks quiet for nothing until it is admitted."""

    def test_light_work_of_higher_priority_takes_its_hold_while_a_claimed_drain_waits(self) -> None:
        """A1: a drain claimed in a lull waits for the running light job; a light job of higher priority
        submitted meanwhile, ending well before the drain is due, gets its quiet hold at once, and the
        drain runs once both end."""
        self.env.update(CAMPAIGN_DRAIN_WAIT="3600", CAMPAIGN_DRAIN_SLACK="100000")
        go = self.root / "go"
        ours = self.submit(self.home, "--level", "light", "--force", "--", f"while [ ! -e {go} ]; do sleep 0.1; done")
        try:
            self.wait_until((self.root / "jobs" / ours / "a1" / "began").exists)
            ab = self.submit(self.home, "--level", "exclusive", "--minutes", "1", "--", "true")
            self.wait_until(lambda: self.job(ab).get("claimed"))
            theirs = self.submit(
                self.home, "--level", "light", "--priority", "2", "--minutes", "1", "--force", "--", "true"
            )
            began = self.root / "jobs" / theirs / "a1" / "began"
            deadline = time.time() + 15  # quiet keeps a light request behind a waiting exclusive one 10 min
            while not began.exists() and time.time() < deadline:
                time.sleep(0.2)
            self.assertTrue(began.exists(), "the light job waited behind the claimed drain")
        finally:
            go.touch()
        self.ok(self.home, "wait", ours, ab, theirs, "--timeout", "60", timeout=90)
        self.assertEqual([self.job(j)["state"] for j in (ours, ab, theirs)], ["done"] * 3)
        self.assertGreaterEqual(float(self.job(ab)["began"]), float(self.job(theirs)["ended"]) - 1)


if __name__ == "__main__":
    unittest.main()
