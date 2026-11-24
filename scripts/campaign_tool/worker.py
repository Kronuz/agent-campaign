"""The worker: its plan of which jobs run each tick, CPU metering, queue health, and its loop, which
also mails each lead its to-do (`lead_todo`)."""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import shutil
import signal
import statistics
import sys
import time
from collections import Counter
from collections.abc import Collection
from pathlib import Path
from typing import Any

from .alarm import watch_calls
from .core import ENTRY, HEAVY, LIGHT_RUN, SHORT_RUN, Json, atomic_write, jobs_dir, plural, sh, stamp
from .disk import daily_prune
from .jobs import all_jobs, claim, finish, load_job, slot_of
from .reuse import what_stem
from .runner import admit, reap, spawn, worker_alive


# a strict hold pins its own cores only where quiet can (Linux, FreeBSD's cpuset); elsewhere it drains
def expected_seconds(spec: Json, took: dict[tuple[str, str, str], list[float]]) -> float:
    """How long a job should run: the longest of its command's last three runs, at most its --minutes."""
    declared = 60.0 * int(spec.get("minutes") or 30)
    last = took.get(run_kind(spec), [])[-3:]
    return min(declared, max(last)) if last else declared


def run_kind(spec: Json) -> tuple[str, str, str]:
    """What a job runs, whatever the commit: its command, directory and repository."""
    return (str(spec.get("cmd", "")), str(spec.get("cwd", "")), str(spec.get("repo", "")))


def drains(job: Json) -> bool:
    """A job whose hold waits for every light hold and blocks new ones while it waits: an exclusive
    one. A strict hold runs beside light holds, on its own cores where the host can split them."""
    return bool(job["spec"].get("level") == "exclusive")


def window_info() -> Json:
    """The drain window's record: until, for and opened ({} when none was opened)."""
    with contextlib.suppress(OSError, ValueError):
        info = json.loads((jobs_dir() / ".window").read_text())
        return info if isinstance(info, dict) else {}
    return {}


def window_until(info: Json | None = None, now: float = 0.0) -> float:
    """When the open drain window (INFO, else the recorded one) closes: 0 when none is open at NOW."""
    with contextlib.suppress(ValueError, KeyError, TypeError):
        until = float((window_info() if info is None else info)["until"])
        return until if until > (now or time.time()) else 0.0
    return 0.0


def cmd_window(a: argparse.Namespace) -> int:
    path = jobs_dir() / ".window"
    if a.close:
        path.unlink(missing_ok=True)
        print("window closed: drains and light jobs take turns again")
        return 0
    if a.minutes:
        until = time.time() + 60 * a.minutes
        atomic_write(path, json.dumps({"until": until, "for": a.purpose, "opened": time.time()}) + "\n")
        print(f"window open until {stamp(until)}: queued drains run in batches, light work between them")
        return 0
    until = window_until()
    print(f"window open until {stamp(until)}" if until else "no window open")
    return 0


def camp(job: Json) -> str:
    """The campaign a job serves, for the worker's fair share."""
    return str(job["spec"].get("campaign") or "")


def queue_order(queued: list[Json], aged: set[str], holds: Counter[str] | None = None) -> list[Json]:
    """The order the worker tries queued jobs in: priority, the campaign holding fewer light slots, the
    AGED in submit order before the rest shortest first; ties in submit order, since a job's id holds
    only its second and a random suffix."""
    held: Counter[str] = holds if holds is not None else Counter()

    def key(j: Json) -> tuple[Any, ...]:
        spec = j["spec"]
        aging = j["id"] in aged
        minutes = 0 if aging else int(spec.get("minutes", 0) or 0)
        return (-int(spec.get("priority", 0)), held[camp(j)], not aging, minutes, float(spec.get("submitted") or 0))

    return sorted(queued, key=lambda j: (*key(j), j["id"]))


def light_slots(heavy: bool = False) -> int:
    """Light jobs at once, as quiet admits them: half the CPUs (a quarter beside a heavy job)."""
    if os.environ.get("CAMPAIGN_LIGHT_JOBS"):
        return int(os.environ["CAMPAIGN_LIGHT_JOBS"])
    ncpu = os.cpu_count() or 4
    return max(1, ncpu // 4) if heavy else max(1, ncpu // 2)


CPU_WINDOW = float(os.environ.get("CAMPAIGN_CPU_WINDOW", "120"))  # seconds of use a light job's charge averages
CPU_SAMPLE = float(os.environ.get("CAMPAIGN_CPU_SAMPLE", "10"))  # seconds between the worker's CPU samples


def slot_cores() -> float:
    """The cores one light slot stands for: the CPUs over the light slots."""
    return (os.cpu_count() or 4) / light_slots()


def slots_for(cores: float) -> int:
    """The light slots CORES of use take: the nearest whole number, one at least, every slot at most."""
    return max(1, min(light_slots(), math.floor(cores / slot_cores() + 0.5)))


def light_weight(j: Json, charges: dict[str, int]) -> int:
    """The light slots a job takes: those its --cpus declares, or those its measured use was charged."""
    cpus = float(j["spec"].get("cpus") or 0)
    return max(slots_for(cpus) if cpus else 1, charges.get(j.get("job", j)["id"], 1))


def add_charge(d: Path, slot_seconds: float) -> None:
    """Adds SLOT_SECONDS, charged beyond a light job's one slot, to the record in its directory D."""
    with contextlib.suppress(OSError, ValueError):
        slot_seconds += float((d / "charge").read_text())
    atomic_write(d / "charge", f"{slot_seconds:.1f}\n")


def charged(j: Json, start: float, end: float) -> float:
    """The light slots a job held from START to END on average: its --cpus, or one plus the slot-seconds
    it was charged beyond it over its run, whichever is more."""
    cpus = float(j["spec"].get("cpus") or 0)
    declared = float(slots_for(cpus) if cpus else 1)
    return max(declared, 1 + float(j.get("charge") or 0) / (end - start)) if end > start else declared


def ps_seconds(text: str) -> float:
    """CPU seconds from ps's [[DD-]HH:]MM:SS[.CC]."""
    days, _, clock = text.rpartition("-")
    seconds = 0.0
    for part in clock.split(":"):
        seconds = 60 * seconds + float(part)
    return seconds + 86400 * int(days or 0)


def proc_table() -> dict[int, tuple[int, float]]:
    """Each process's parent and CPU seconds, its reaped children's included: /proc on Linux, else
    `ps -S` (FreeBSD's sums reaped children; macOS's does not, so tree_cpu asks libproc there)."""
    table: dict[int, tuple[int, float]] = {}
    if not Path("/proc/self/stat").exists():
        for line in sh(["ps", "-A", "-S", "-o", "pid=,ppid=,time="], check=False).splitlines():
            with contextlib.suppress(ValueError):
                pid, ppid, used = line.split()
                table[int(pid)] = (int(ppid), ps_seconds(used))
        return table
    tick = os.sysconf("SC_CLK_TCK")
    for name in os.listdir("/proc"):
        if name.isdigit():
            try:
                with open(f"/proc/{name}/stat") as fh:
                    text = fh.read()
            except OSError:
                continue
            f = text[text.rindex(")") + 2 :].split()  # state ppid ... utime stime cutime cstime
            table[int(name)] = (int(f[1]), sum(int(x) for x in f[11:15]) / tick)
    return table


_libproc: list[Any] = []  # macOS: libproc, a rusage_info buffer, a pid buffer and the timebase in seconds


def darwin_tree_cpu(root: int) -> float:
    """The CPU seconds of ROOT's process tree on macOS, from each member's rusage_info (its own time
    and its reaped children's), walked with proc_listchildpids: no process is started."""
    import ctypes  # noqa: PLC0415

    if not _libproc:
        lib = ctypes.CDLL("/usr/lib/libproc.dylib")

        class Timebase(ctypes.Structure):
            _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]

        tb = Timebase()
        ctypes.CDLL(None).mach_timebase_info(ctypes.byref(tb))  # rusage_info counts mach time units
        _libproc.extend([lib, (ctypes.c_uint64 * 40)(), (ctypes.c_int * 4096)(), tb.numer / tb.denom / 1e9])
    lib, info, pids, unit = _libproc
    total, todo = 0.0, [root]
    while todo:
        pid = todo.pop()
        if lib.proc_pid_rusage(pid, 2, info) == 0:  # RUSAGE_INFO_V2: user, system, then children's at 12, 13
            total += (info[2] + info[3] + info[12] + info[13]) * unit
        n = lib.proc_listchildpids(pid, pids, ctypes.sizeof(pids))
        todo += pids[: max(0, min(n, len(pids)))]
    return total


def tree_cpu(roots: Collection[int]) -> dict[int, float]:
    """The CPU seconds each root's process tree has used, its reaped descendants' included."""
    if sys.platform == "darwin":
        return {r: darwin_tree_cpu(r) for r in roots}
    table = proc_table()
    kids: dict[int, list[int]] = {}
    for pid, (ppid, _) in table.items():
        kids.setdefault(ppid, []).append(pid)
    out: dict[int, float] = {}
    for root in roots:
        total, todo = 0.0, [root]
        while todo:
            pid = todo.pop()
            total += table.get(pid, (0, 0.0))[1]
            todo += kids.get(pid, [])
        out[root] = total
    return out


def meter(jobs: list[Json], seen: dict[str, list[tuple[float, float]]], now: float) -> dict[str, Json]:
    """Samples the CPU of each running light job (its runner's process tree) into SEEN and returns,
    for each whose samples span CPU_WINDOW, the cores it used over that window and the slots they take."""
    runners = {
        j["id"]: int(j["state"]["runner"])
        for j in jobs
        if j["state"].get("state") == "running"
        and j["state"].get("runner")
        and (j["spec"].get("level") == "light" or j["state"].get("phase") == "build")
    }
    for gone in seen.keys() - runners.keys():
        del seen[gone]
    used = tree_cpu(set(runners.values())) if runners else {}
    out: dict[str, Json] = {}
    for jid, pid in runners.items():
        s = seen.setdefault(jid, [])
        cpu = max(used.get(pid, 0.0), s[-1][1] if s else 0.0)  # a zombie's or an orphan's seconds never count back
        s.append((now, cpu))
        while len(s) > 2 and now - s[1][0] >= CPU_WINDOW:
            del s[0]
        t0, c0 = s[0]
        if now - t0 >= CPU_WINDOW:
            cores = (cpu - c0) / (now - t0)
            out[jid] = {"cores": round(cores, 2), "slots": slots_for(cores)}
    return out


def cpu_lines(jobs: list[Json]) -> list[str]:
    """The running light jobs the worker's last sample found over a slot's share, and by how much."""
    try:
        use = json.loads((jobs_dir() / ".worker" / "cpu.json").read_text())
    except (OSError, ValueError):
        return []
    running = {j["id"]: j for j in jobs if j["state"].get("state") == "running"}
    share = float(use.get("share") or slot_cores())
    over = [
        f"{jid} ({Path(camp(running[jid])).name or 'no campaign'}: {str(running[jid]['spec'].get('what', ''))[:40]}) "
        f"{u['cores']:.1f} cores, {u['cores'] / share:.1f} times a slot's {share:g}, charged {plural(u['slots'], 'slot')}"
        for jid, u in sorted(use.get("jobs", {}).items())
        if jid in running and u.get("slots", 1) > 1
    ]
    return [f"light jobs over their share: {'; '.join(over)}"] if over else []


def children() -> set[int]:
    """This process's children, exited ones included."""
    me = os.getpid()
    return {pid for pid, (ppid, _) in proc_table().items() if ppid == me}


def reap_runners(pids: set[int]) -> None:
    """Waits for the runners in PIDS that exited, so none stays a zombie: the worker spawns them and
    never waits otherwise, and a worker re-executed in place is still their parent."""
    for pid in list(pids):
        try:
            done = os.waitpid(pid, os.WNOHANG)[0]
        except ChildProcessError:  # reaped already, or not this process's child
            done = pid
        if done:
            pids.discard(pid)


STALL = 30  # minutes runnable work may wait beside idle slots before `check` warns
WASTED = ("cancelled", "skipped", "interrupted", "timeout")  # ended without a result


def spans(jobs: list[Json], now: float) -> list[tuple[Json, float, float]]:
    """Each job phase that held a slot, from its start to its end (now while it runs)."""
    out: list[tuple[Json, float, float]] = []
    for j in phases(jobs):
        st = j["state"]
        end = st.get("ended") or (now if st.get("state") == "running" else None)
        if st.get("started") and end and j["spec"].get("level") != "none":
            out.append((j, float(st["started"]), float(end)))
    return out


def waits(jobs: list[Json], now: float) -> list[tuple[Json, float, float]]:
    """Each job's runnable wait: from its submission or its last dependency's end to its start (its
    cancellation, or now while queued), per phase. A job behind a dependency that did not pass has none."""
    jobs = phases(jobs)
    by_id = {j["id"]: j for j in jobs}
    out: list[tuple[Json, float, float]] = []
    for j in jobs:
        deps = [by_id.get(x) for x in j["spec"].get("after", [])]
        if any(d is None or d["state"].get("state") != "done" for d in deps):
            continue
        ready = max([float(j["spec"].get("submitted") or 0), *(float(d["state"].get("ended") or 0) for d in deps if d)])
        st = j["state"]
        until = float(st.get("started") or st.get("ended") or (now if st.get("state") == "queued" else 0))
        if until > ready:
            out.append((j, ready, until))
    return out


def idle_for(job: Json, live: dict[str, tuple[Json, float]], slots: int) -> bool:
    """Whether the slots JOB could take were free beside the LIVE jobs (each with the light slots it was
    charged): its light slots with no drain, a heavy job's own cores, or, for a drain, the whole machine;
    never while its build slot is in use."""
    level, slot = job["spec"].get("level"), slot_of(job["spec"])
    if level == "none" or (slot and any(slot_of(x["spec"]) == slot for x, _ in live.values())):
        return False
    if drains(job):
        return not live
    if level in HEAVY:
        return not any(x["spec"].get("level") in HEAVY for x, _ in live.values())
    if any(drains(x) for x, _ in live.values()):
        return False
    taken = sum(w for x, w in live.values() if x["spec"].get("level") not in HEAVY)
    return taken + min(charged(job, 0, 0), slots) <= slots


def idle_minutes(
    jobs: list[Json], now: float, since: float, only: Collection[str] | None = None
) -> tuple[dict[str, int], int, int]:
    """Minute by minute since SINCE: per job (of ONLY), the minutes it waited runnable beside a free slot
    it could take; the minutes any job did; and the minutes a drain held the machine."""
    held = [x for x in spans(jobs, now) if x[2] > since]
    waiting = [x for x in waits(jobs, now) if x[2] > since and (only is None or x[0].get("job", x[0])["id"] in only)]
    if only is not None:  # sample only from the first wait
        since = max(since, min((a for _, a, _ in waiting), default=now))
    slots = light_slots()
    weights = {j["id"]: charged(j, a, b) for j, a, b in held}
    per_job: Counter[str] = Counter()
    idle = drained = 0
    for t in range(int(since) + 30, int(now), 60):
        live = {j["id"]: (j, weights[j["id"]]) for j, a, b in held if a <= t < b}
        drained += any(drains(j) for j, _ in live.values())
        stuck = [j.get("job", j)["id"] for j, a, b in waiting if a <= t < b and idle_for(j, live, slots)]
        per_job.update(stuck)
        idle += bool(stuck)
    return dict(per_job), idle, drained


def queue_health(jobs: list[Json], now: float) -> Json:
    """The host's last hour from the job records: light slot-minutes used (the slots each job was
    charged) against the slots, minutes a drain held it, minutes runnable work waited beside idle
    slots, and the jobs that ended, by result."""
    since = now - 3600
    held = [x for x in spans(jobs, now) if x[2] > since]
    used = (
        sum((min(b, now) - max(a, since)) * charged(j, a, b) for j, a, b in held if j["spec"].get("level") not in HEAVY)
        / 60
    )
    _, idle, drained = idle_minutes(jobs, now, since)
    ended = [j for j in jobs if float(j["state"].get("ended") or 0) > since]
    return {
        "used": round(used),
        "capacity": 60 * light_slots(),
        "drained": drained,
        "idle": idle,
        "ended": len(ended),
        "failed": sum(j["state"].get("state") == "failed" for j in ended),
        "wasted": dict(Counter(j["state"]["state"] for j in ended if j["state"].get("state") in WASTED)),
        "filler": sum(j["state"].get("state") == "queued" and int(j["spec"].get("priority", 0)) < 0 for j in jobs),
    }


def health_lines(h: Json) -> list[str]:
    """The two lines a lead's report quotes: host use and wasted jobs, over the last hour."""
    share = round(100 * h["used"] / h["capacity"]) if h["capacity"] else 0
    kinds = ", ".join(f"{n} {k}" for k, n in sorted(h["wasted"].items()))
    return [
        f"host, last hour: light slots {share}% used ({h['used']} of {h['capacity']} slot-minutes), "
        f"drains {h['drained']} min, runnable work beside idle slots {h['idle']} min",
        f"jobs ended, last hour: {h['ended']}, {h['failed']} failed, {sum(h['wasted'].values())} produced nothing"
        + (f" ({kinds})" if kinds else ""),
    ] + (
        [f"light slots {100 - share}% idle and no --priority -1 work queued: queue fuzz, soaks or benchmarks"]
        if share < 75 and not h.get("filler")
        else []
    )


def long_timeouts(jobs: list[Json], ident: str, now: float) -> list[tuple[str, str]]:
    """A note per kind of run (its `what` stem) of this campaign in the last day whose --minutes is over
    twice three times the median of its passed runs (three at least): a hang runs that long for nothing."""
    runs: dict[tuple[str, str], list[float]] = {}
    last: dict[tuple[str, str], Json] = {}
    for j in sorted(jobs, key=lambda j: float(j["spec"].get("submitted") or 0)):
        if j["spec"].get("campaign") != ident or not what_stem(j):
            continue
        kind, st = (str(j["spec"].get("repo", "")), what_stem(j)), j["state"]
        if st.get("state") == "done" and st.get("started") and st.get("ended"):
            runs.setdefault(kind, []).append(float(st["ended"]) - float(st["started"]))
        if now - float(j["spec"].get("submitted") or 0) < 86400:
            last[kind] = j
    out: list[tuple[str, str]] = []
    for kind, j in last.items():
        took = runs.get(kind, [])
        enough = math.ceil(3 * statistics.median(took) / 60) if len(took) >= 3 else 0
        if enough and 2 * enough < int(j["spec"].get("minutes") or 0):
            out.append(
                (
                    "note",
                    f"{kind[1]}: --minutes {j['spec']['minutes']}, but its last {len(took)} passed runs took "
                    f"{statistics.median(took) / 60:.1f} min at the median: --minutes {enough} stops a hang sooner",
                )
            )
    return out


def stalls(jobs: list[Json], now: float) -> list[tuple[str, str]]:
    """A warning when a queued job waited over STALL minutes beside idle slots: the lead's to fix."""
    queued = {j["id"] for j in jobs if j["state"].get("state") == "queued" or j["state"].get("phase") == "built"}
    per_job: dict[str, int] = idle_minutes(jobs, now, now - 86400, queued)[0] if queued else {}
    stuck = {k: m for k, m in per_job.items() if m > STALL}
    if not stuck:
        return []
    worst = max(stuck, key=lambda k: stuck[k])
    down = "" if worker_alive() else "; the worker is down (a submit or wait starts it)"
    text = f"{plural(len(stuck), 'queued job')} waited over {STALL} minutes beside idle slots (longest {worst}, "
    return [("warning", text + f"{stuck[worst]} min){down}: `campaign job show {worst}`")]


# a batch of heavy jobs run back to back behind one drain of light work, bounded so light work still
# gets the machine: the queue history's batches (12 h, 2026-11-18) ran a median of 2.7 minutes per job
BATCH_JOBS = int(os.environ.get("CAMPAIGN_BATCH_JOBS") or 6)
BATCH_MINUTES = float(os.environ.get("CAMPAIGN_BATCH_MINUTES") or 30)


def batch_of(jobs: list[Json]) -> Json:
    """The newest heavy job's batch ({"since", "jobs"}) while one drain still serves it: the job runs,
    or no light job started after it ended. {} once light work was readmitted, or with no heavy job."""
    heavy = [j for j in jobs if j["spec"].get("level") in HEAVY and j["state"].get("batch")]
    if not heavy:
        return {}
    last = max(heavy, key=lambda j: float(j["state"].get("claimed") or 0))
    ended = float(last["state"].get("ended") or 0)
    if last["state"].get("state") == "running" or not ended:
        return dict(last["state"]["batch"])
    readmitted = any(j["spec"].get("level") == "light" and float(j["state"].get("claimed") or 0) > ended for j in jobs)
    return {} if readmitted else dict(last["state"]["batch"])


def phases(jobs: list[Json]) -> list[Json]:
    """The jobs as the holds they take. A heavy job the worker builds in two phases (queued, or its
    state names a phase) is two: its build under light, then its command at its level after it. Each
    phase keeps the job it is of under "job"; a built job waiting for its hold keeps its checkout, as
    does a drain claimed without a build (phase "built" from its claim)."""
    out: list[Json] = []
    for j in jobs:
        spec, st = j["spec"], j["state"]
        if st.get("phase") == "built" and st.get("state") == "running" and not spec.get("build"):
            out.append({**j, "job": j, "state": {**st, "state": "queued", "claimed": st.get("admitted")}})
            continue
        if not (spec.get("build") and spec.get("level") in HEAVY and (st.get("phase") or st.get("state") == "queued")):
            out.append(j)
            continue
        b = st.get("build") or {}
        build = {
            **j,
            "id": f"{j['id']}:build",
            "job": j,
            "spec": {**spec, "level": "light", "cmd": spec["build"], "minutes": LIGHT_RUN},
            "state": {
                "state": "done" if st.get("phase") in ("built", "run") else st["state"],
                "claimed": st.get("claimed"),
                "started": b.get("started"),
                "ended": b.get("ended"),
            },
        }
        waiting = st["state"] == "running" and st.get("phase") != "run"
        run = {**st, "state": "queued" if waiting else st["state"], "claimed": st.get("admitted")}
        out += [build, {**j, "job": j, "spec": {**spec, "after": [build["id"]]}, "state": run}]
    return out


def plan(
    jobs: list[Json], now: float, turn: Json, charges: dict[str, int] | None = None
) -> tuple[list[Json], list[Json], Json]:
    """One worker tick, decided from the job records and CHARGES (the light slots measured use takes, by
    job): the queued jobs to start, in order (a heavy one's state carries its batch), the jobs to skip
    (one they run after did not succeed), and the drain turn's record ({} for none). A heavy job with a
    build starts it as light work (state phase "build"); once built, it waits in the heavy queue's order
    for its hold (phase "run": the worker admits it). One drain is claimed at a time; it holds no one up
    until it is due, and takes its hold once no light job runs (a drain claimed earlier waits in phase
    "built"). A deferred drain (priority below 0) is due only a DRAIN_WAIT after work of higher priority
    last waited."""
    claimed = [j for j in jobs if drains(j) and j["state"].get("state") == "running"]
    pending = [  # the drains not yet admitted to their hold
        j
        for j in jobs
        if drains(j)
        and (
            j["state"].get("state") == "queued"
            or (j["state"].get("state") == "running" and j["state"].get("phase") in ("build", "built"))
        )
    ]
    jobs = phases(jobs)
    charges = charges or {}
    # a drain waits two of its capped lengths for a lull: light work keeps most of the machine
    drain_wait = float(os.environ.get("CAMPAIGN_DRAIN_WAIT", str(2 * SHORT_RUN * 60)))
    drain_slack = float(os.environ.get("CAMPAIGN_DRAIN_SLACK", "600"))
    drain_turn = float(os.environ.get("CAMPAIGN_DRAIN_TURN", "3600"))
    # light work started while a drain is queued ends by its due plus this: soaks wait, builds and suites do not
    drain_hold = float(os.environ.get("CAMPAIGN_DRAIN_HOLD", "14400"))
    age = float(os.environ.get("CAMPAIGN_AGE", "3600"))  # seconds before a queued job outranks shorter ones
    states = {j["id"]: j["state"].get("state") for j in jobs}
    queued = [j for j in jobs if states[j["id"]] == "queued"]
    running = [j for j in jobs if states[j["id"]] == "running"]
    # a slot is one build directory: one job at a time, a built one keeping it while it waits for its hold
    built = [j for j in queued if j["state"].get("phase") == "built"]
    busy = {slot_of(j["spec"]) for j in running + built} - {""}
    heavy = any(j["spec"].get("level") in HEAVY for j in running)
    # a "none" job takes no hold: it never waits for a light slot, and has its own as many
    # a light job takes the slots it declares or is measured using
    lights = [j for j in running if j["spec"].get("level") not in (*HEAVY, "none")]
    light = sum(light_weight(j, charges) for j in lights)
    bare = sum(1 for j in running if j["spec"].get("level") == "none")
    late = sum(
        light_weight(j, charges) if j in lights else 1
        for j in running
        if j["spec"].get("level") not in HEAVY and int(j["spec"].get("priority", 0)) < 0
    )
    # fair share: campaigns with light work split the slots; within one, the shortest job first
    holds: Counter[str] = Counter()
    for j in lights:
        holds[camp(j)] += light_weight(j, charges)

    def held(j: Json) -> bool:
        """Whether J's build slot is another job's: a built job keeps its own while it waits for its hold."""
        return slot_of(j["spec"]) in busy and j["state"].get("phase") != "built"

    # how long each command took, oldest run first
    took: dict[tuple[str, str, str], list[float]] = {}
    for j in sorted(jobs, key=lambda j: j["id"]):
        st = j["state"]
        if st.get("state") == "done" and st.get("started") and st.get("ended"):
            took.setdefault(run_kind(j["spec"]), []).append(float(st["ended"]) - float(st["started"]))
    # a drain's wait counts from the last drain that ran: one cancelled or skipped while queued
    # never held the machine, so it must not restart the wait
    last_drain = max(
        (float(j["state"].get("ended") or 0) for j in jobs if drains(j) and j["state"].get("started")),
        default=0.0,
    )

    # deferred drains wait for work of higher priority: the priorities waiting now, and when each last did
    def prio(j: Json) -> int:
        return int(j["spec"].get("priority", 0))

    def ready(j: Json) -> bool:
        return all(states.get(x) == "done" for x in j["spec"].get("after", []))

    top = max((prio(j) for j in queued if j["spec"].get("level") != "none" and ready(j)), default=None)
    waited: dict[int, float] = {}
    for j in jobs:
        if j["spec"].get("level") != "none":
            stop = float(j["state"].get("claimed") or j["state"].get("ended") or 0)
            waited[prio(j)] = max(waited.get(prio(j), 0.0), stop)

    def eligible(j: Json) -> bool:
        """Whether drain J may be claimed: a deferred one only while no work of higher priority waits."""
        return prio(j) >= 0 or top is None or top <= prio(j)

    def due_at(j: Json) -> float:
        since = max(float(j["spec"].get("submitted") or 0), last_drain)
        if prio(j) < 0:  # a stretch with no work of higher priority waiting
            since = max([since, *(t for p, t in waited.items() if p > prio(j))])
        return since + drain_wait

    # one drain at a time, from its claim (its build, or its wait for admission) to its end; a deferred
    # one claimed before work of higher priority came waits aside, so it holds no other drain back
    drain_busy = any(eligible(j) or j not in pending for j in claimed)
    # the drains that may take the machine: not waiting for their slot (a claimed one holds its own)
    ready_drains = [
        j
        for j in pending
        if eligible(j) and ready(j) and (j["state"].get("state") == "running" or slot_of(j["spec"]) not in busy)
    ]
    # when the first drain falls due, or now while one holds the machine: light work running far past
    # it would hold it for hours
    due = min([*(due_at(j) for j in ready_drains), *(now for j in claimed if j not in pending)], default=0.0)

    def outlasts(j: Json) -> bool:
        """Whether light job J would run past the queued drain's due by more than DRAIN_HOLD."""
        return bool(due) and now + expected_seconds(j["spec"], took) > max(due, now) + drain_hold

    def mine(j: Json) -> bool:
        """Whether view J is a drain's: its build, or its hold."""
        return drains(j.get("job", j))

    # light work its slot or a queued drain holds back (a built drain's, a soak's) is not waiting for the
    # machine, nor is a drain's own build
    wanting = {
        camp(j)
        for j in queued
        if j["spec"].get("level") not in (*HEAVY, "none") and not mine(j) and not held(j) and not outlasts(j)
    }
    sharing = max(1, len(wanting | {c for c, n in holds.items() if n}))
    slots = light_slots(heavy)
    share = math.ceil(slots / sharing)  # no slot idles while every campaign has its share
    # shortest first, except that a job queued past `age` goes ahead in submit order, so a
    # stream of short jobs never starves a long one
    aged = {j["id"] for j in queued if now - float(j["spec"].get("submitted") or now) > age}
    queued = queue_order(queued, aged, holds)

    # a draining hold blocks new light holds until the running ones end: start one only when
    # they all end soon; past its wait, only light work that ends before them fills the lull
    def ends(r: Json) -> float:
        return float(r["state"].get("started") or now) + expected_seconds(r["spec"], took)

    lights_end = max((ends(r) for r in lights), default=now)
    # an overdue drain also waits for a running strict job, which light work may share
    free_at = max([lights_end, *(ends(r) for r in running if r["spec"].get("level") == "strict")])
    drain_ok = lights_end - now <= drain_slack
    starved = [
        j
        for j in queued
        if j["spec"].get("level") not in (*HEAVY, "none")
        and not mine(j)
        and int(j["spec"].get("priority", 0)) >= 1
        and now - float(j["spec"].get("submitted") or now) > drain_turn
        and all(states.get(x) == "done" for x in j["spec"].get("after", []))
    ]
    if (
        starved
        and turn.get("for") == "drain turn"
        and not any(drains(j) for j in running)
        and last_drain >= float(turn.get("opened") or now)
    ):
        # direct-path work that waited a whole turn ends it once its running drain ends: light
        # work has DRAIN_WAIT from that drain's end, so a long drain queue cannot hold it for hours
        turn = {}
    window = window_until(turn, now) > 0  # an open window: every ready drain is due, back to back
    # a drain waiting for its build slot, or a deferred one behind work of higher priority, holds no one up
    overdue = {j["id"] for j in ready_drains if window or now >= due_at(j)}
    if overdue and not window:
        # an overdue drain opens a turn: the queued drains run back to back for DRAIN_TURN, then
        # light work has DRAIN_WAIT again, so a queue of drains takes turns, not one per hour
        turn = {"until": now + drain_turn, "for": "drain turn", "opened": now}
        window = True
    # while the last heavy job's drain holds and its batch has room, the next heavy job goes first, so
    # light work is not readmitted between them; a full batch lets light work go first once, even past
    # an overdue drain, and the drains behind it wait for their turn
    batch = batch_of(jobs)
    room = bool(batch) and batch["jobs"] < BATCH_JOBS and now - batch["since"] < 60 * BATCH_MINUTES
    if batch:
        queued.sort(key=lambda j: (j["spec"].get("level") in HEAVY) != room)
    full = bool(batch) and not room and not heavy and not lights  # its drain holds: nothing of ours runs
    if not full:  # a due drain's turn: its build takes the first free light slot
        queued.sort(key=lambda j: not (mine(j) and j.get("job", j)["id"] in overdue))
    yielded = False  # a full batch's light work started this tick
    start: list[Json] = []
    skip: list[Json] = []
    reserved = False  # an aged light job too wide for the free slots: the light work behind it waits
    for j in queued:
        deps = [states.get(x, "missing") for x in j["spec"].get("after", [])]
        if any(s not in ("queued", "running", "done") for s in deps):
            skip.append(j.get("job", j))
            continue
        is_heavy = j["spec"].get("level") in HEAVY
        is_bare = j["spec"].get("level") == "none"
        is_late = int(j["spec"].get("priority", 0)) < 0
        weight = 0 if is_heavy or is_bare else min(light_weight(j, charges), slots)
        waiting = any(s != "done" for s in deps)
        if weight and light + weight > slots and not waiting:
            reserved = reserved or j["id"] in aged
            continue
        if waiting or (is_heavy and heavy) or (is_bare and bare >= slots) or (weight and reserved):
            continue
        of = j.get("job", j)  # a phase starts the job it is of: its build, or its command's hold
        if mine(j):
            if yielded or (drain_busy and of["state"].get("state") == "queued"):
                continue  # light work goes first after a full batch; one drain is claimed at a time
            # it is due, or a lull in light work (a deferred drain has its stretch instead)
            lull = prio(of) >= 0 and not wanting and (drain_ok or of["state"].get("state") == "running")
            if of["id"] not in overdue and not lull:
                continue
        if (
            overdue
            and (weight or is_heavy)
            and not full
            and not mine(j)
            and now + expected_seconds(j["spec"], took) > free_at
        ):
            continue  # an overdue drain waits: only work that ends before the running work starts
        if weight and outlasts(j):
            continue  # it would run past a queued drain's due by more than DRAIN_HOLD: it waits for the drains
        if is_late and not is_heavy and late >= max(1, slots // 2):
            continue  # deferred work never takes every slot from the direct path
        if not is_heavy and not is_bare and not mine(j) and len(wanting) > 1 and holds[camp(j)] >= share:
            continue  # its campaign has its share while another campaign waits
        if held(j):
            continue  # its slot's job runs: it waits its turn, and the jobs behind it go on
        if is_heavy and drains(j) and light:
            if of["state"].get("state") == "queued":  # claimed now, it holds nothing until it is admitted
                start.append({**of, "state": {**of["state"], "phase": "built"}})
                busy |= {slot_of(j["spec"])} - {""}
                drain_busy = True
            continue  # its hold waits for the running work: quiet would keep new light work out meanwhile
        yielded = yielded or (full and bool(weight))
        drain_busy = drain_busy or mine(j)
        if is_heavy:
            batch = {"since": batch["since"], "jobs": batch["jobs"] + 1} if room else {"since": now, "jobs": 1}
            slots = light_slots(True)  # light work beside it keeps to a heavy job's share from now on
            share = math.ceil(slots / sharing)
        state = {**of["state"], **({"batch": batch} if is_heavy else {})}
        start.append({**of, "state": {**state, "phase": "run" if is_heavy else "build"} if of is not j else state})
        busy |= {slot_of(j["spec"])} - {""}
        heavy = heavy or is_heavy
        light += weight
        bare += 1 if is_bare else 0
        holds[camp(j)] += weight
        late += (weight or 1) if is_late and not is_heavy else 0
    return start, skip, turn


def cmd_worker(a: argparse.Namespace) -> int:
    jd = jobs_dir()
    home = jd / ".worker"
    me = str(os.getpid())
    if a.restart:
        try:
            pid = int((home / "pid").read_text())
        except (FileNotFoundError, ValueError):
            pid = 0
        if not pid or not worker_alive():
            print("no worker is running; the next submit or wait starts one on the current code")
            return 0
        os.kill(pid, signal.SIGHUP)
        print(f"worker {pid}: restarts on the current code after its tick; running jobs keep running")
        return 0
    asked: list[str] = []  # "restart" or "stop", acted on between ticks, never mid-claim
    signal.signal(signal.SIGHUP, lambda *_: asked.append("restart"))
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: asked.append("stop"))

    def take() -> bool:
        """Puts a home holding our pid and a fresh heartbeat in place at once: no starter sees it half made."""
        new = jd / f".worker.new.{me}"
        new.mkdir()
        (new / "pid").write_text(me)
        (new / "heartbeat").touch()
        try:
            new.rename(home)  # refused while another worker's home (never empty) is there
            return True
        except OSError:
            shutil.rmtree(new, ignore_errors=True)
            return False

    try:
        ours = (home / "pid").read_text() == me  # re-executed by a restart: the home is already ours
    except FileNotFoundError:
        ours = False
    if ours or take():
        pass
    elif worker_alive():
        return 0
    else:
        stale = jd / f".worker.stale.{me}"
        with contextlib.suppress(OSError):
            home.rename(stale)
        shutil.rmtree(stale, ignore_errors=True)
        if not take():
            return 0
    (home / "started").write_text(f"{time.time()}\n")  # when this code began scheduling
    tick = float(os.environ.get("CAMPAIGN_WORKER_TICK", "2"))
    # its runners: a restart's execv keeps the old image's children, recorded or not; each job's CPU
    # samples and its charge
    runners: set[int] = children() if ours else set()
    seen: dict[str, list[tuple[float, float]]] = {}
    use: dict[str, Json] = {}
    with contextlib.suppress(OSError, ValueError, AttributeError):
        use = dict(json.loads((home / "cpu.json").read_text()).get("jobs", {}))
    sampled = 0.0
    try:
        while True:
            try:
                if (home / "pid").read_text() != me:
                    return 0  # another worker took the home: one worker at a time
            except FileNotFoundError:
                return 0
            (home / "heartbeat").touch()
            reap_runners(runners)
            reap()
            watch_calls()
            jobs = all_jobs()
            from .todo import lead_todo  # noqa: PLC0415  # todo reads this module's slots: a cycle at import

            mailing = lead_todo(jobs, time.time())  # the leads' to-do, as mail, while one lives
            if not any(j["state"].get("state") in ("queued", "running") for j in jobs):
                if daily_prune((home / "heartbeat").touch):
                    continue  # whatever arrived while it pruned
                if not mailing:
                    return 0
            if time.time() - sampled >= CPU_SAMPLE:
                since, sampled = sampled, time.time()
                running = {j["id"] for j in jobs if j["state"].get("state") == "running"}
                measured = meter(jobs, seen, sampled)
                use = {k: v for k, v in use.items() if k in running} | measured
                atomic_write(home / "cpu.json", json.dumps({"share": slot_cores(), "jobs": use}) + "\n")
                for jid, u in measured.items():  # what queue health counts after the job ends
                    if u["slots"] > 1 and since:
                        add_charge(jd / jid, (u["slots"] - 1) * (sampled - since))
            turn = window_info()
            start, skip, window = plan(jobs, time.time(), turn, {k: int(v["slots"]) for k, v in use.items()})
            if window != turn:
                if window:
                    atomic_write(jobs_dir() / ".window", json.dumps(window) + "\n")
                else:
                    (jobs_dir() / ".window").unlink(missing_ok=True)
            for j in skip:
                if claim(j):
                    finish(load_job(j["dir"]), -1, "skipped", "a job it runs after did not succeed")
            for j in start:
                if j["state"].get("state") == "running":
                    admit(j)  # built: its runner takes its hold
                elif claim(j):
                    runners.add(spawn(["_run", str(j["dir"])], j["dir"] / "runner.log").pid)
            if "stop" in asked:
                return 0
            if asked:  # a restart keeps the pid and the home: no starter sees the worker gone
                sys.stdout.flush()
                os.execv(sys.executable, [sys.executable, str(ENTRY), "worker"])
            time.sleep(tick)
    finally:
        with contextlib.suppress(FileNotFoundError, ValueError):
            if int((home / "pid").read_text()) == os.getpid():
                shutil.rmtree(home, ignore_errors=True)
