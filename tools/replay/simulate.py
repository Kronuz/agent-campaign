"""Replays the window's job arrivals and landings under orchestration policies (A3).

The machine model is v2's on this host: 8 light slots, 4 beside a heavy job, strict one at a time
beside light work, an exclusive job alone after it drains the light holds, one job per build slot,
`after` dependencies. Durations are the recorded ones. A quest lands at its last evidence job's
simulated end plus its recorded post-evidence delay (review, CI, refusals, the lead), which a policy
may shorten. Calibration fits one parameter, the light slots v2 actually filled, to the recorded
waits, then checks landings and hourly queue lengths against the records.
"""

from __future__ import annotations

import heapq
import json
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import account
import sources as src
from sources import Json
from timeline import World

H = 3600.0
TICK = 30.0
DRAIN_WAIT = 20 * 60.0  # v2: an exclusive job waits two capped lengths for a lull
DRAIN_TURN = 3600.0  # v2: an overdue drain opens an hour of back-to-back drains
AGE = 3600.0  # v2: a job queued past an hour goes ahead in submit order
BATCH_EVERY = 4 * H  # exclusive batching: one planned drain window every four hours


@dataclass
class SimJob:
    id: str
    campaign: str
    quest: str
    level: str
    arrival: float
    run: float
    build: float
    slot: str
    after: list[str]
    gone: float  # cancelled or skipped before it ran: leaves the queue then
    shape: str
    start: float = 0.0
    end: float = 0.0
    phase: str = "queued"  # queued, building, built, draining, running, done
    built_at: float = 0.0
    deps_left: list[str] = field(default_factory=list)


@dataclass
class Policy:
    name: str
    identity: bool = False  # input-scoped identity: avoidable reruns never run
    merge_queue: bool = False  # merged result tested once: no main-moved refusal episodes
    batching: bool = False  # exclusive jobs in planned windows, light work packed around them
    driver: bool = False  # resumes, reviews and lands with no lead delay
    light_slots: int = 8


def sim_jobs(w: World, drop: set[str]) -> list[SimJob]:
    out: list[SimJob] = []
    for j in w.jobs:
        if j.id in drop:
            continue
        if not (src.SINCE <= j.submitted < src.UNTIL):
            continue
        gone = 0.0 if j.ran else (j.ended or src.UNTIL)
        out.append(
            SimJob(
                id=j.id,
                campaign=j.campaign,
                quest=j.quest,
                level=j.level,
                arrival=j.submitted,
                run=j.run_s,
                build=j.build_s,
                slot=j.slot,
                after=list(j.after),
                gone=gone,
                shape=account.shape(j),
            )
        )
    return out


def run_machine(jobs: list[SimJob], p: Policy) -> Json:
    """Ticks the machine through the window; fills start/end on each job; returns hourly queue lengths."""
    by_id = {j.id: j for j in jobs}
    pending = sorted(jobs, key=lambda j: j.arrival)
    queue: list[SimJob] = []
    running: list[SimJob] = []
    expected: dict[str, list[float]] = defaultdict(list)
    turn_until = 0.0
    last_drain = 0.0
    next_batch = src.SINCE + BATCH_EVERY
    i = 0
    hourly: list[int] = []
    t = src.SINCE
    next_hour = src.SINCE - (src.SINCE % H) + H
    heap_end: list[tuple[float, str]] = []

    def est(j: SimJob) -> float:
        xs = expected.get(j.shape)
        return statistics.median(xs) if xs else max(j.run, 60.0)

    while t < src.UNTIL + 7 * 86400 and (i < len(pending) or queue or running):
        while i < len(pending) and pending[i].arrival <= t:
            queue.append(pending[i])
            i += 1
        while heap_end and heap_end[0][0] <= t:
            _, jid = heapq.heappop(heap_end)
            j = by_id[jid]
            if j.phase == "building":
                j.phase, j.built_at = "built", t
            elif j.phase == "running":
                j.phase, j.end = "done", t
                expected[j.shape].append(j.run)
                if j.level == "exclusive":
                    last_drain = t
        running = [j for j in running if j.phase in ("building", "running", "draining", "built")]
        queue = [j for j in queue if not (j.gone and j.gone <= t)]
        if t >= next_hour:
            hourly.append(sum(1 for j in queue if j.phase in ("queued", "built", "draining")))
            next_hour += H
        lights = sum(1 for j in running if j.phase == "building" or (j.phase == "running" and j.level == "light"))
        heavy_run = [j for j in running if j.phase == "running" and j.level in ("strict", "exclusive")]
        ex_run = any(j.level == "exclusive" for j in heavy_run)
        strict_run = any(j.level == "strict" for j in heavy_run)
        draining = [j for j in running if j.phase == "draining"]
        cap = 0 if ex_run or draining else (min(p.light_slots, 4) if strict_run else p.light_slots)
        busy = {j.slot for j in running if j.slot}

        def ready(j: SimJob) -> bool:
            return all(by_id[x].phase == "done" for x in j.after if x in by_id)

        # an exclusive job waiting on a drain starts once the light holds are gone
        for j in draining:
            if lights == 0 and not ex_run:
                j.phase, j.start = "running", t
                heapq.heappush(heap_end, (t + j.run, j.id))
                ex_run = True
        # heavy admission
        ex_queue = [j for j in queue if j.level == "exclusive" and j.phase in ("queued", "built") and ready(j)]
        if p.batching:
            window = t >= next_batch
            if window and not ex_queue:
                next_batch = t + BATCH_EVERY
            due: list[SimJob] = ex_queue if window else []
        else:
            light_waiting = any(j.level == "light" for j in queue if j.phase == "queued")
            due = [
                j
                for j in ex_queue
                if t < turn_until or not light_waiting or t - max(j.arrival, last_drain) >= DRAIN_WAIT
            ]
            if due and t >= turn_until:
                turn_until = t + DRAIN_TURN
        if due and not ex_run and not draining and not strict_run:
            j = sorted(due, key=lambda x: x.arrival)[0]
            if j.build and j.phase == "queued":
                pass  # builds first, below, as light work
            else:
                queue.remove(j)
                j.phase = "draining"
                running.append(j)
                draining = [j]
                cap = 0
        if not ex_run and not draining and not strict_run:
            for j in sorted(
                (x for x in queue if x.level == "strict" and x.phase in ("queued", "built") and ready(x)),
                key=lambda x: x.arrival,
            ):
                if j.build and j.phase == "queued":
                    continue
                queue.remove(j)
                j.phase, j.start = "running", t
                heapq.heappush(heap_end, (t + j.run, j.id))
                running.append(j)
                strict_run = True
                cap = min(cap, 4)
                break
        limit = next_batch if p.batching and not draining and not ex_run else 0.0  # pack light work before the window
        # light work and the builds of heavy jobs, shortest first, aged ones in submit order
        cands = [x for x in queue if x.phase == "queued" and (x.level in ("light", "none") or x.build) and ready(x)]
        cands.sort(key=lambda x: (0, x.arrival) if t - x.arrival > AGE else (1, est(x)))
        for j in cands:
            if j.level == "none":
                queue.remove(j)
                j.phase, j.start = "running", t
                heapq.heappush(heap_end, (t + j.run, j.id))
                running.append(j)
                continue
            if lights >= cap or (j.slot and j.slot in busy):
                continue
            if limit and t + est(j) > limit and any(x.level == "exclusive" for x in queue):
                continue
            busy.add(j.slot)
            lights += 1
            if j.level == "light":
                queue.remove(j)
                j.phase, j.start = "running", t
                heapq.heappush(heap_end, (t + j.run, j.id))
            else:
                j.phase = "building"
                heapq.heappush(heap_end, (t + j.build, j.id))
            running.append(j)
        # a built heavy job goes back to the heavy queue (it keeps its slot in v2; here, freed)
        for j in [x for x in running if x.phase == "built"]:
            running.remove(j)
        t += TICK
    return {"hourly_queue": hourly}


def last_evidence(w: World) -> dict[tuple[str, str], tuple[float, float]]:
    """Each quest landed in the window: (its recorded landing, its last job's recorded end before it)."""
    out: dict[tuple[str, str], tuple[float, float]] = {}
    for c, hist in w.history.items():
        for e in hist:
            if e.verb == "land" and "increment" not in e.args and src.SINCE <= e.t < src.UNTIL:
                ends = [j.ended for j in w.jobs if j.campaign == c and j.quest == e.quest and j.ran and j.ended <= e.t]
                out.setdefault((c, e.quest), (e.t, max(ends) if ends else 0.0))
    return out


def run(w: World, out: Path, names: list[str]) -> Json:
    rows: list[Json] = (
        [json.loads(x) for x in (out / "reruns.jsonl").read_text().splitlines()]
        if (out / "reruns.jsonl").exists()
        else []
    )
    episodes = src.read_json(out / "episodes.json") or []
    quests = src.read_json(out / "quests.json") or []
    avoidable = {r["job"] for r in rows if r["verdict"] == "avoidable"}
    idle: dict[tuple[str, str], float] = {}
    for q in quests:
        idle[(q["campaign"], q["quest"])] = sum(v for k, v in q.items() if k.startswith("nothing moving")) * H
    main_eps: dict[tuple[str, str], float] = defaultdict(float)
    for e in episodes:
        if "main moved" in e["reasons"] and e["landed"]:
            main_eps[(e["campaign"], e["quest"])] += e["wall_h"] * H
    lands = last_evidence(w)
    rec_ends = {j.id: j.ended for j in w.jobs}
    rec_wait = [j.began - j.submitted for j in w.jobs if j.ran and src.SINCE <= j.submitted < src.UNTIL]
    rec_hourly = [h["queued"] for h in (src.read_json(out / "hourly.json") or [])]

    def one(p: Policy) -> Json:
        drop = avoidable if p.identity else set()
        jobs = sim_jobs(w, drop)
        mach = run_machine(jobs, p)
        end_of = {j.id: j.end for j in jobs}
        waits = [j.start - j.arrival for j in jobs if j.start]
        landed = 0
        land_days: list[float] = []
        for (c, q), (lt, le) in lands.items():
            mine = [j for j in jobs if j.campaign == c and j.quest == q and j.end and rec_ends.get(j.id, 0) <= lt]
            e_sim = max((j.end for j in mine), default=le)
            delay = max(0.0, lt - le) if le else 0.0
            if p.merge_queue:
                delay = max(0.0, delay - main_eps.get((c, q), 0.0))
            if p.driver:
                delay = max(15 * 60.0, delay - idle.get((c, q), 0.0))
            when = (e_sim if le else lt) + delay
            if when < src.UNTIL:
                landed += 1
                land_days.append((when - src.SINCE) / 86400)
        del end_of
        machine_h = sum(j.run + j.build for j in jobs if j.start or j.phase != "queued") / H
        sim_h = mach["hourly_queue"][: len(rec_hourly)]
        err = sum(abs(a - b) for a, b in zip(sim_h, rec_hourly)) / max(1, sum(rec_hourly)) * 100 if rec_hourly else None
        return {
            "policy": p.name,
            "landings": landed,
            "landings_per_day": round(landed / 14, 2),
            "machine_h": round(machine_h, 1),
            "jobs": len(jobs),
            "median_wait_min": round(statistics.median(waits) / 60, 1) if waits else None,
            "mean_queue": round(statistics.mean(mach["hourly_queue"]), 1) if mach["hourly_queue"] else None,
            "queue_error_pct_vs_recorded": round(err, 1) if err is not None else None,
        }

    # calibration: the light slots v2 effectively filled, fitted to the recorded median wait
    fits = []
    for slots in (2, 3, 4, 5, 6, 8):
        r = one(Policy(f"v2 model, {slots} light slots", light_slots=slots))
        fits.append((abs((r["median_wait_min"] or 0) - statistics.median(rec_wait) / 60), slots, r))
    fits.sort(key=lambda x: x[0])
    slots = fits[0][1]
    recorded = {
        "landings": len(lands),
        "landings_per_day": round(len(lands) / 14, 2),
        "machine_h": round(sum(j.run_s + j.build_s for j in w.jobs if src.SINCE <= j.submitted < src.UNTIL) / H, 1),
        "median_wait_min": round(statistics.median(rec_wait) / 60, 1),
        "mean_queue": round(statistics.mean(rec_hourly), 1) if rec_hourly else None,
    }
    policies = [
        Policy("v2 as recorded (calibrated)", light_slots=slots),
        Policy("input-scoped identity", identity=True, light_slots=slots),
        Policy("merge queue", merge_queue=True, light_slots=slots),
        Policy("exclusive batching", batching=True, light_slots=slots),
        Policy("deterministic driver", driver=True, light_slots=slots),
        Policy("all four", identity=True, merge_queue=True, batching=True, driver=True, light_slots=slots),
        Policy("all four, 8 light slots used", identity=True, merge_queue=True, batching=True, driver=True),
    ]
    if names:
        policies = [p for p in policies if p.name in names] or policies
    res: Json = {
        "recorded": recorded,
        "calibration": [{"light_slots": s, **r} for _, s, r in fits],
        "fitted_light_slots": slots,
        "results": [one(p) for p in policies],
    }
    (out / "simulation.json").write_text(json.dumps(res, indent=1))
    return res
