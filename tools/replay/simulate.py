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
import need
import sources as src
from sources import Json
from timeline import World

H = 3600.0
TICK = 30.0
DRAIN_WAIT = 20 * 60.0  # v2: an exclusive job waits two capped lengths for a lull
DRAIN_TURN = 3600.0  # v2: an overdue drain opens an hour of back-to-back drains
AGE = 3600.0  # v2: a job queued past an hour goes ahead in submit order
BATCH_EVERY = 4 * H
NEED_DROP = (
    need.CATEGORIES[0],
    need.CATEGORIES[1],
    need.CATEGORIES[4],
)  # never submitted under need  # exclusive batching: one planned drain window every four hours


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
    need: bool = False  # jobs only for an unanswered question; evidence by declared inputs; resubmission replaces
    sizing: bool = False  # with need: no trial past the first two of a passing row at one commit
    light_slots: int = 8
    drain_turn: float = DRAIN_TURN  # calibration: how long an overdue drain keeps draining
    slot_serial: bool = True  # calibration: a build slot runs one job at a time


def sim_jobs(w: World, drop: set[str], shift: dict[str, float], replaced: dict[str, float]) -> list[SimJob]:
    """The window's jobs less DROP, each submitted SHIFT[id] seconds earlier (a driver with no delay);
    a REPLACED job never runs and leaves the queue when its resubmission arrives."""
    out: list[SimJob] = []
    for j in w.jobs:
        if j.id in drop:
            continue
        if not (src.SINCE <= j.submitted < src.UNTIL):
            continue
        early = shift.get(j.id, 0.0)
        gone = 0.0 if j.ran else (j.ended or src.UNTIL) - early
        if j.id in replaced:
            gone = replaced[j.id] - early
        out.append(
            SimJob(
                id=j.id,
                campaign=j.campaign,
                quest=j.quest,
                level="replaced" if j.id in replaced else j.level,
                arrival=j.submitted - early,
                run=j.run_s,
                build=0.0 if j.id in replaced else j.build_s,
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
    drain_s = ex_s = 0.0

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
        busy = {j.slot for j in running if j.slot} if p.slot_serial else set()

        def ready(j: SimJob) -> bool:
            return all(by_id[x].phase == "done" or by_id[x].level == "replaced" for x in j.after if x in by_id)

        # an exclusive job waiting on a drain starts once the light holds are gone
        for j in draining:
            if lights == 0 and not ex_run:
                j.phase, j.start = "running", t
                heapq.heappush(heap_end, (t + j.run, j.id))
                ex_run = True
        # heavy admission
        ex_queue = [j for j in queue if j.level == "exclusive" and j.phase in ("queued", "built") and ready(j)]
        if p.batching:
            # a window opens every BATCH_EVERY and stays open while built exclusive work is ready
            window = t >= next_batch
            built = [j for j in ex_queue if not j.build or j.phase == "built"]
            if window and not built and not ex_run and not draining:
                next_batch = t + BATCH_EVERY
            due: list[SimJob] = built if window else []
        else:
            light_waiting = any(j.level == "light" for j in queue if j.phase == "queued")
            due = [
                j
                for j in ex_queue
                if t < turn_until or not light_waiting or t - max(j.arrival, last_drain) >= DRAIN_WAIT
            ]
            if due and t >= turn_until:
                turn_until = t + p.drain_turn
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
        # before a window, light work that would overrun it waits; builds of heavy jobs never do
        limit = next_batch if p.batching and t < next_batch else 0.0
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
            if limit and j.level == "light" and t + est(j) > limit and any(x.level == "exclusive" for x in queue):
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
        if draining and not ex_run:
            drain_s += TICK
        if ex_run:
            ex_s += TICK
        t += TICK
    return {"hourly_queue": hourly, "drain_h": round(drain_s / H, 1), "exclusive_h": round(ex_s / H, 1)}


def last_evidence(w: World) -> dict[tuple[str, str], tuple[float, float]]:
    """Each quest landed in the window: (its recorded landing, its last job's recorded end before it)."""
    out: dict[tuple[str, str], tuple[float, float]] = {}
    for c, hist in w.history.items():
        for e in hist:
            if e.verb == "land" and "increment" not in e.args and src.SINCE <= e.t < src.UNTIL:
                ends = [j.ended for j in w.jobs if j.campaign == c and j.quest == e.quest and j.ran and j.ended <= e.t]
                out.setdefault((c, e.quest), (e.t, max(ends) if ends else 0.0))
    return out


def idle_before(gaps: list[list[int]], t: float) -> float:
    return sum(max(0.0, min(b, t) - a) for a, b in gaps if a < t)


def run(w: World, out: Path, names: list[str]) -> Json:
    """query simulate: calibration of the v2 model, then each policy alone and all together."""
    rows: list[Json] = (
        [json.loads(x) for x in (out / "reruns.jsonl").read_text().splitlines()]
        if (out / "reruns.jsonl").exists()
        else []
    )
    episodes: list[Json] = src.read_json(out / "episodes.json") or []
    quests: list[Json] = src.read_json(out / "quests.json") or []
    calls: list[Json] = src.read_json(out / "calls.json") or []
    avoidable = {r["job"] for r in rows if r["verdict"] == "avoidable"}
    rerun_ids = {r["job"] for r in rows}
    gaps: dict[tuple[str, str], list[list[int]]] = {(q["campaign"], q["quest"]): q.get("idle", []) for q in quests}
    main_eps: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    for e in episodes:
        if "main moved" in e["reasons"]:
            t0 = src.stamp(e["first"].replace("Z", ":00Z"))
            main_eps[(e["campaign"], e["quest"])].append((t0, t0 + e["wall_h"] * H))
    in_episode = {
        j.id
        for j in w.jobs
        if j.id in rerun_ids and any(a <= j.submitted <= b for a, b in main_eps.get((j.campaign, j.quest), []))
    }
    shift = {j.id: idle_before(gaps.get((j.campaign, j.quest), []), j.submitted) for j in w.jobs}
    lands = last_evidence(w)
    rec_ends = {j.id: j.ended for j in w.jobs}
    rec_wait = [j.began - j.submitted for j in w.jobs if j.ran and src.SINCE <= j.submitted < src.UNTIL]
    rec_hourly = [h["queued"] for h in (src.read_json(out / "hourly.json") or [])]
    need_rows: list[Json] = (
        [json.loads(x) for x in (out / "need.jsonl").read_text().splitlines()] if (out / "need.jsonl").exists() else []
    )

    # tokens: agent runs begun in the window; the lead's from its transcript
    runs = [
        (c, r)
        for c, u in w.usage.items()
        for r in u.get("runs", [])
        if src.SINCE <= src.stamp(r.get("began", "")) < src.UNTIL
    ]
    agent_tokens = sum(int(r.get("tokens") or 0) for _, r in runs)
    merge_saved = sum(
        int(r.get("tokens") or 0)
        for c, r in runs
        if any(a <= src.stamp(r.get("began", "")) <= b for a, b in main_eps.get((c, r.get("quest", "")), []))
    )
    lead_tokens = 0
    routine_tokens = 0.0
    for c, lead in w.lead.items():
        mine = sum(v for h, v in lead.tokens.items() if src.SINCE <= src.stamp(h + ":00Z") < src.UNTIL)
        none: Json = {}
        session = max(w.usage[c].get("lead", []), key=lambda r: float(r.get("tokens") or 0), default=none)
        whole, requests = float(session.get("tokens") or 0), float(session.get("requests") or 0)
        in_window = requests * mine / whole if whole else 0.0  # the session's requests, scaled to the window
        lead_calls = sum(
            1 for x in calls if x["campaign"] == c and x["role"] == "lead" and src.SINCE <= x["t"] < src.UNTIL
        )
        lead_tokens += mine
        routine_tokens += mine * min(1.0, lead_calls / in_window) if in_window else 0.0

    def one(p: Policy, base: dict[tuple[str, str], float] | None = None) -> Json:
        drop = (avoidable if p.identity else set()) | (in_episode if p.merge_queue else set())
        replaced: dict[str, float] = {}
        if p.need:
            drop |= {r["job"] for r in need_rows if r["category"] in NEED_DROP}
            drop |= {r["job"] for r in need_rows if p.sizing and r["category"] == need.CATEGORIES[3]}
            replaced = {r["job"]: r["replaced_at"] for r in need_rows if r["category"] == need.CATEGORIES[2]}
        jobs = sim_jobs(w, drop, shift if p.driver else {}, replaced)
        mach = run_machine(jobs, p)
        waits = [j.start - j.arrival for j in jobs if j.start]
        landed = 0
        land_at: dict[tuple[str, str], float] = {}
        for (c, q), (lt, le) in lands.items():
            mine = [j for j in jobs if j.campaign == c and j.quest == q and j.end and rec_ends.get(j.id, 0) <= lt]
            e_sim = max((j.end for j in mine), default=0.0)
            delay = max(0.0, lt - le) if le else 0.0
            g = gaps.get((c, q), [])
            if p.merge_queue:
                delay -= sum(min(b, lt) - max(a, le) for a, b in main_eps.get((c, q), []) if b > le and a < lt)
            if p.driver:
                delay -= idle_before(g, lt) - idle_before(g, le) if le else 0.0
                delay = max(15 * 60.0, delay)
            delay = max(0.0, delay)
            start = e_sim if e_sim else (lt - (idle_before(g, lt) if p.driver else 0.0))
            land_at[(c, q)] = start + delay
            if start + delay < src.UNTIL:
                landed += 1
        machine_h = sum(j.run + j.build for j in jobs if j.start) / H
        common = [k for k in land_at if base and k in base]
        quest_h_saved = sum(base[k] - land_at[k] for k in common) / H if base else 0.0
        sim_h = mach["hourly_queue"][: len(rec_hourly)]
        err = sum(abs(a - b) for a, b in zip(sim_h, rec_hourly)) / max(1, sum(rec_hourly)) * 100 if rec_hourly else None
        agents = agent_tokens - (merge_saved if p.merge_queue else 0)
        lead = lead_tokens - (routine_tokens if p.driver else 0)
        return {
            "policy": p.name,
            "landings": landed,
            "landings_per_day": round(landed / 14, 2),
            "machine_h": round(machine_h, 1),
            "jobs": len(jobs),
            "median_wait_min": round(statistics.median(waits) / 60, 1) if waits else None,
            "mean_queue": round(statistics.mean(sim_h), 1) if sim_h else None,
            "queue_error_pct_vs_recorded": round(err, 1) if err is not None else None,
            "agent_tokens_M": round(agents / 1e6),
            "lead_tokens_M": round(lead / 1e6),
            "wait_job_h": round(sum(waits) / H, 1),
            "drain_wall_h": mach["drain_h"],
            "exclusive_wall_h": mach["exclusive_h"],
            "quest_h_saved_vs_v2": round(quest_h_saved, 1),
            "_land_at": land_at,
        }

    # calibration: three machine parameters v2 does not record, fitted to the recorded hourly queue
    fits: list[tuple[float, Policy, Json]] = []
    for slots in (6, 8):
        for turn in (0.0, DRAIN_TURN):
            for serial in (True, False):
                cand = Policy(
                    f"v2 model, {slots} slots, drain turn {turn / 60:.0f} min, serial slots {serial}",
                    light_slots=slots,
                    drain_turn=turn,
                    slot_serial=serial,
                )
                r = one(cand)
                fits.append((r["queue_error_pct_vs_recorded"] or 0.0, cand, r))
    fits.sort(key=lambda x: x[0])
    fit = fits[0][1]
    slots = fit.light_slots
    recorded = {
        "landings": len(lands),
        "landings_per_day": round(len(lands) / 14, 2),
        "machine_h": round(sum(j.run_s + j.build_s for j in w.jobs if src.SINCE <= j.submitted < src.UNTIL) / H, 1),
        "median_wait_min": round(statistics.median(rec_wait) / 60, 1),
        "mean_queue": round(statistics.mean(rec_hourly), 1) if rec_hourly else None,
        "agent_tokens_M": round(agent_tokens / 1e6),
        "lead_tokens_M": round(lead_tokens / 1e6),
        "lead_routine_tokens_M": round(routine_tokens / 1e6),
        "merge_episode_agent_tokens_M": round(merge_saved / 1e6),
    }
    policies = [
        Policy(
            "v2 as recorded (calibrated)", light_slots=slots, drain_turn=fit.drain_turn, slot_serial=fit.slot_serial
        ),
        Policy(
            "input-scoped identity",
            identity=True,
            light_slots=slots,
            drain_turn=fit.drain_turn,
            slot_serial=fit.slot_serial,
        ),
        Policy(
            "merge queue", merge_queue=True, light_slots=slots, drain_turn=fit.drain_turn, slot_serial=fit.slot_serial
        ),
        Policy(
            "exclusive batching",
            batching=True,
            light_slots=slots,
            drain_turn=fit.drain_turn,
            slot_serial=fit.slot_serial,
        ),
        Policy(
            "deterministic driver",
            driver=True,
            light_slots=slots,
            drain_turn=fit.drain_turn,
            slot_serial=fit.slot_serial,
        ),
        Policy(
            "need: unanswered questions only",
            need=True,
            light_slots=slots,
            drain_turn=fit.drain_turn,
            slot_serial=fit.slot_serial,
        ),
        Policy(
            "need, plus no trial past a decided row",
            need=True,
            sizing=True,
            light_slots=slots,
            drain_turn=fit.drain_turn,
            slot_serial=fit.slot_serial,
        ),
        Policy(
            "all four",
            identity=True,
            merge_queue=True,
            batching=True,
            driver=True,
            light_slots=slots,
            drain_turn=fit.drain_turn,
            slot_serial=fit.slot_serial,
        ),
        Policy(
            "all four and need",
            identity=True,
            merge_queue=True,
            batching=True,
            driver=True,
            need=True,
            light_slots=slots,
        ),
        Policy(
            "all four and need, 8 light slots filled",
            identity=True,
            merge_queue=True,
            batching=True,
            driver=True,
            need=True,
            drain_turn=fit.drain_turn,
            slot_serial=fit.slot_serial,
        ),
    ]
    if names:
        policies = [p for p in policies if p.name in names] or policies
    baseline = one(policies[0])
    results = [baseline] + [one(p, baseline["_land_at"]) for p in policies[1:]]
    for r in results + [x for _, _, x in fits]:
        r.pop("_land_at", None)
    res: Json = {
        "recorded": recorded,
        "calibration": [r for _, _, r in fits],
        "fitted": {
            "light_slots": fit.light_slots,
            "drain_turn_min": fit.drain_turn / 60,
            "slot_serial": fit.slot_serial,
        },
        "results": results,
    }
    (out / "simulation.json").write_text(json.dumps(res, indent=1))
    return res
