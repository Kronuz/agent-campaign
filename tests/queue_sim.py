#!/usr/bin/env python3
"""A simulated host for the worker's plan: a queue run tick by tick through a campaign_tool module's
plan, each claimed job taking its hold as quiet grants it (light holds up to the slots, none beside a
running or waiting exclusive hold unless the light request waited QUIET_STARVE_MIN; an exclusive hold
once no light hold runs and no starving light request waits; one heavy hold at a time).

  python3 queue_sim.py BEFORE.py AFTER.py   # today's queue shape through two modules, as a table

A job's "started" is its claim, as the runner records it; the hold may come later.
"""

from __future__ import annotations

import importlib.util
import json
import os
import random
import statistics
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import mock

STARVE = 600.0  # quiet's QUIET_STARVE_MIN, in seconds
HEAVY = ("strict", "exclusive")
BASE = 1_800_000_000.0


def load(path: Path) -> ModuleType:
    """A campaign_tool module from PATH, on a 16-CPU host (8 light slots, 4 beside a heavy job)."""
    sys.path.insert(0, str(path.parent))  # it imports report and style from its own scripts folder
    spec = importlib.util.spec_from_file_location(f"campaign_tool_{abs(hash(str(path)))}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def todays_queue(seed: int = 1119, gap: float = 10.0) -> list[dict[str, Any]]:
    """The queue of 2026-11-19 17h: 34 exclusive jobs (10 minutes declared, two in three with a build)
    and 43 light ones (10-45 minutes declared, priority 1) of three campaigns, submitted GAP seconds
    apart; each runs 40-100% of what it declares."""
    rng = random.Random(seed)
    queue: list[dict[str, Any]] = []
    for n in range(34 + 43):
        exclusive = n % 9 < 4 and sum(q["level"] == "exclusive" for q in queue) < 34
        level = "exclusive" if exclusive else "light"
        minutes = 10 if exclusive else rng.choice((10, 12, 20, 25, 30, 30, 30, 30, 40, 45))
        q: dict[str, Any] = {
            "id": n,
            "level": level,
            "minutes": minutes,
            "priority": 0 if exclusive else 1,
            "campaign": n % 3,
            "submitted": gap * n,
            "after": [],
            "took": 60 * minutes * rng.uniform(0.4, 1.0),
        }
        if exclusive and rng.random() < 2 / 3:
            q["build"] = 60 * rng.uniform(2, 10)
        queue.append(q)
    return queue


def job(q: dict[str, Any]) -> dict[str, Any]:
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
    """QUEUE through MODULE's plan: the jobs at the end, the holds ((id, level, granted, ended)) and the
    claims ((id, level, at)), with the measures `metrics` reads."""
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
        claims: list[tuple[str, str, float]] = []
        idle = 0.0  # light slot-seconds free while no exclusive hold ran and runnable light work was queued
        turn: dict[str, Any] = {}
        t = 0.0
        while pending or asks or holds or any(j["state"]["state"] in ("queued", "running") for j in jobs.values()):
            now = BASE + t
            while pending and pending[0]["submitted"] <= t:
                q = pending.pop(0)
                jobs[f"{q['id']:06d}"] = job(q)
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
                    jobs[jid]["state"] = {**st, "state": "done", "ended": granted + length}
            for key, (level, since) in sorted(asks.items(), key=lambda kv: kv[1][1]):  # quiet grants holds
                live = [lv for lv, _ in holds.values()]
                waiting_x = any(lv == "exclusive" for k, (lv, _) in asks.items() if k != key)
                starving = any(lv == "light" and now - s >= STARVE for k, (lv, s) in asks.items() if k != key)
                if level == "none":  # it takes no hold
                    ok = True
                elif level == "light":
                    ok = live.count("light") < slots and "exclusive" not in live
                    ok = ok and (not waiting_x or now - since >= STARVE)
                else:  # strict runs beside light holds (its own cores, or shared on macOS); exclusive alone
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
                )
                idle += tick * min(ready, slots - live.count("light"))
            start, skip, turn = module.plan(list(jobs.values()), now, turn)
            for j in skip:
                jobs[j["id"]]["state"] = {"state": "skipped", "ended": now}
            for n, j in enumerate(start):  # claims within a tick keep their order
                st = {**j["state"], "state": "running"}
                at = now + n / 1000
                if j["state"]["state"] == "running":  # built: admitted to its hold
                    st.update(admitted=at, started=at)
                    asks[j["id"]] = (j["spec"]["level"], at)
                elif st.get("phase") == "build":
                    st.update(claimed=at, build={"started": at})
                    asks[f"{j['id']}:build"] = ("light", at)
                else:
                    st.update(claimed=at, started=at)
                    asks[j["id"]] = (j["spec"]["level"], at)
                claims.append((j["id"], "light" if st.get("phase") == "build" else j["spec"]["level"], at))
                jobs[j["id"]] = {**j, "state": st}
            t += tick
        return {
            "jobs": list(jobs.values()),
            "holds": log,
            "claims": claims,
            "end": BASE + t,
            "slots": slots,
            "idle": idle,
        }


def metrics(run: dict[str, Any]) -> dict[str, float]:
    """Light slot use (light holds, builds' too, over the slots and the whole run), minutes a drain held
    the host (an exclusive job's claim to its end, as `campaign status` counts them), heavy hold
    minutes, drains (exclusive holds that light work came before), light slot-minutes left idle beside
    runnable light work while no exclusive hold ran, the hours to empty, and light jobs' wait p50
    (submit to hold, in minutes)."""
    holds, end = run["holds"], run["end"]
    first = min(float(j["spec"]["submitted"]) for j in run["jobs"])
    light = sum(b - a for _, lv, a, b in holds if lv == "light")
    spans = sorted(
        (float(j["state"]["started"]), float(j["state"]["ended"]))
        for j in run["jobs"]
        if j["spec"]["level"] == "exclusive" and j["state"].get("ended")
    )
    drained, reach = 0.0, first
    for a, b in spans:  # the union of the drains' spans
        drained += max(0.0, b - max(a, reach))
        reach = max(reach, b)
    exclusive = sorted((a, b) for _, lv, a, b in holds if lv == "exclusive")
    lights = sorted(a for _, lv, a, _ in holds if lv == "light")
    drains = sum(i == 0 or any(exclusive[i - 1][1] <= c < a for c in lights) for i, (a, _) in enumerate(exclusive))
    submitted = {j["id"]: float(j["spec"]["submitted"]) for j in run["jobs"]}
    waits = [a - submitted[key] for key, lv, a, _ in holds if lv == "light" and ":" not in key]
    return {
        "light_use": light / (run["slots"] * (end - first)),
        "drained": drained / 60,
        "heavy": sum(b - a for _, lv, a, b in holds if lv in HEAVY) / 60,
        "drains": float(drains),
        "idle": run["idle"] / 60,
        "hours": (end - first) / 3600,
        "light_wait": statistics.median(waits) / 60 if waits else 0.0,
    }


def recorded_queue(staged: bool = True) -> list[dict[str, Any]]:
    """The host's queue of 12 h to 2026-11-18 07:38 (tests/fixtures/queue-12h.json): 719 jobs, 67 strict
    and 14 exclusive, each with its recorded length. Unless STAGED, a heavy job builds inside its hold."""
    queue: list[dict[str, Any]] = json.loads((Path(__file__).parent / "fixtures" / "queue-12h.json").read_text())[
        "jobs"
    ]
    if staged:
        return queue
    return [{**q, "took": q["took"] + q["build"]} if "build" in q else q for q in queue]


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    modules = {name: load(Path(p)) for name, p in zip(("before", "after"), sys.argv[1:])}
    seeds = range(1, 6)
    shapes = {  # each a list of queues: today's shape over five seeds, queued at once or arriving
        "today, queued at once": [todays_queue(seed, 10.0) for seed in seeds],
        "today, arriving over 2 h": [todays_queue(seed, 94.0) for seed in seeds],
        "recorded 12 h of 11-18": [recorded_queue()],
    }
    print(f"before {sys.argv[1]}, after {sys.argv[2]}; today's shape is the mean of {len(seeds)} seeds\n")
    print(
        "| queue | code | light slot use | idle slot-min beside runnable light work | drain min | drains |"
        " hours to empty | light wait p50 |"
    )
    print("|---|---|---|---|---|---|---|---|")
    for shape, queues in shapes.items():
        for name, module in modules.items():
            runs = [metrics(simulate(module, q)) for q in queues]
            m = {k: statistics.mean(r[k] for r in runs) for k in runs[0]}
            print(
                f"| {shape} | {name} | {100 * m['light_use']:.1f}% | {m['idle']:.0f} | {m['drained']:.0f} | "
                f"{m['drains']:.1f} | {m['hours']:.2f} | {m['light_wait']:.0f} min |",
                flush=True,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
