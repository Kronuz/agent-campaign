"""What v2 leaves behind (A6): disk by owner and age, zombie jobs and quests, unreaped processes,
and duplicate work, each with what it costs. Read-only: `du`, `ps`, `git worktree list`, the records.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections import Counter, defaultdict
from pathlib import Path

import sources as src
from sources import Json
from timeline import World

GIB = 1024**3
DAY = 86400.0


def du(path: Path, depth: int) -> dict[str, int]:
    """`du -k -d DEPTH PATH`: bytes per entry (one walk; -x stays on its filesystem)."""
    res = subprocess.run(
        ["du", "-x", "-k", "-d", str(depth), str(path)], capture_output=True, text=True, errors="replace"
    )
    out: dict[str, int] = {}
    for line in res.stdout.splitlines():
        kb, _, p = line.partition("\t")
        if kb.isdigit():
            out[p] = int(kb) * 1024
    return out


def age_days(p: Path, now: float) -> float:
    try:
        return round((now - p.stat().st_mtime) / DAY, 1)
    except OSError:
        return -1.0


def status_of(w: World) -> dict[tuple[str, str], str]:
    return {(c, q): r.get("status", "") for c, st in w.states.items() for q, r in st.get("quests", {}).items()}


def scratch(w: World, now: float, sizes: dict[str, int]) -> Json:
    """query scratch: ~/.scratch by owner (its first level) and task (second), size, age, .purpose;
    SIZES is one `du -d 4` walk of ~/.scratch, shared with job_store."""
    root = src.HOME / ".scratch"
    owners: Counter[str] = Counter()
    tasks: list[Json] = []
    for p, b in sizes.items():
        rel = Path(p).relative_to(root) if p != str(root) else Path()
        if len(rel.parts) == 1:
            owners[rel.parts[0]] += b
        elif len(rel.parts) == 2:
            pp = Path(p)
            purpose = pp / ".purpose"
            tasks.append(
                {
                    "path": str(rel),
                    "gib": round(b / GIB, 2),
                    "age_days": age_days(pp, now),
                    "purpose": purpose.read_text(errors="replace").strip()[:160] if purpose.is_file() else "",
                }
            )
    tasks.sort(key=lambda t: -t["gib"])
    no_purpose = [t for t in tasks if not t["purpose"]]
    return {
        "total_gib": round(sizes.get(str(root), 0) / GIB, 1),
        "by_owner_gib": {k: round(v / GIB, 1) for k, v in owners.most_common()},
        "largest_tasks": tasks[:25],
        "tasks": len(tasks),
        "tasks_without_purpose": len(no_purpose),
        "gib_without_purpose": round(sum(t["gib"] for t in no_purpose), 1),
        "gib_idle_over_14_days": round(sum(t["gib"] for t in tasks if t["age_days"] > 14), 1),
    }


def job_store(w: World, now: float, sizes: dict[str, int]) -> Json:
    """query job_store: the store's job directories by job state, quest status and age, and its build
    slots by whether any quest that used them is still in flight."""
    root = src.JOBS
    top = {p: b for p, b in sizes.items() if p == str(root) or Path(p).parent == root}
    slots = {p: b for p, b in sizes.items() if Path(p).parent == root / "slots"}
    st = status_of(w)
    by_job = {j.id: j for j in w.jobs}
    jobs_by: Counter[str] = Counter()
    for p, b in top.items():
        name = Path(p).name
        j = by_job.get(name)
        if j is None:
            continue
        status = st.get((j.campaign, j.quest), "no quest" if not j.quest else "quest gone")
        age = (now - (j.ended or j.submitted)) / DAY
        band = "<1d" if age < 1 else "1-3d" if age < 3 else "3-7d" if age < 7 else ">7d"
        jobs_by[f"{j.state} | quest {status} | {band}"] += b
    users: dict[str, list[src.Job]] = defaultdict(list)
    for j in w.jobs:
        if j.slot:
            users[j.slot].append(j)
    slot_rows: list[Json] = []
    for p, b in slots.items():
        name = Path(p).name
        if p == str(root / "slots"):
            continue
        js = users.get(name, [])
        statuses = sorted({st.get((j.campaign, j.quest), "-") for j in js})
        last = max((j.ended or j.submitted for j in js), default=0.0)
        live = any(j.state in ("queued", "running") for j in js)
        flight = any(s in ("active", "review", "blocked", "planned") for s in statuses)
        verdict = (
            "in use" if live else "quest in flight" if flight else "no job ever named it" if not js else "leftover"
        )
        slot_rows.append(
            {
                "slot": name,
                "gib": round(b / GIB, 2),
                "idle_days": round((now - last) / DAY, 1) if last else None,
                "quests": sorted({j.quest for j in js})[:4],
                "statuses": statuses,
                "verdict": verdict,
            }
        )
    slot_rows.sort(key=lambda r: -r["gib"])
    by_verdict: Counter[str] = Counter()
    for r in slot_rows:
        by_verdict[r["verdict"]] += r["gib"]
    named = {Path(p).name: b for p, b in top.items()}
    return {
        "total_gib": round(top.get(str(root), 0) / GIB, 1),
        "parts_gib": {k: round(named.get(k, 0) / GIB, 1) for k in ("slots", "git-mirrors", "keys", "reads")},
        "job_dirs_gib": round(sum(b for p, b in top.items() if Path(p).name in by_job) / GIB, 1),
        "job_dirs_by_state_quest_age_gib": {k: round(v / GIB, 2) for k, v in jobs_by.most_common(20)},
        "slots": len(slot_rows),
        "slots_by_verdict_gib": {k: round(v, 1) for k, v in by_verdict.most_common()},
        "largest_slots": slot_rows[:20],
    }


WT = re.compile(r"^worktree (.+)$", re.M)


def worktrees(w: World, now: float, walk: dict[str, int]) -> Json:
    """query worktrees: every `git worktree` of every repository under ~/code/Kronuz, by the status of
    the quest its path names (<repo>-wt/<campaign>/<quest>), with size for the leftover ones."""
    st = status_of(w)
    rows: list[Json] = []
    for d in sorted(src.CODE.iterdir()):
        if d.name.endswith("-wt") or d.is_symlink() or not (d / ".git").is_dir():
            continue
        for path in WT.findall(src.git(d, "worktree", "list", "--porcelain")):
            p = Path(path)
            if p == d:
                continue
            parts = p.parts
            campaign, quest = (parts[-2], parts[-1]) if len(parts) >= 2 else ("", "")
            status = st.get((campaign, quest), "")
            rows.append(
                {
                    "repo": d.name,
                    "path": path,
                    "campaign": campaign,
                    "quest": quest,
                    "status": status,
                    "exists": p.is_dir(),
                }
            )
    leftover = [r for r in rows if r["exists"] and r["status"] in ("landed", "dropped")]
    unknown = [r for r in rows if r["exists"] and not r["status"]]
    sizes: dict[str, int] = dict(walk)  # build-slot worktrees are in the ~/.scratch walk already
    scratch_root = str(src.HOME / ".scratch") + "/"
    for root in {str(Path(r["path"]).parents[1]) for r in leftover + unknown if not r["path"].startswith(scratch_root)}:
        sizes.update(du(Path(root), 2))
    for r in leftover + unknown:
        r["gib"] = round(sizes.get(r["path"], 0) / GIB, 2)
        r["age_days"] = age_days(Path(r["path"]), now)
    by: Counter[str] = Counter(r["status"] or "no quest" for r in rows if r["exists"])
    return {
        "worktrees": len(rows),
        "missing_on_disk": sum(1 for r in rows if not r["exists"]),
        "by_quest_status": dict(by.most_common()),
        "leftover_landed_or_dropped": len(leftover),
        "leftover_gib": round(sum(r["gib"] for r in leftover), 1),
        "no_quest": len(unknown),
        "no_quest_gib": round(sum(r["gib"] for r in unknown), 1),
        "largest_leftovers": sorted(leftover, key=lambda r: -r["gib"])[:15],
        "largest_no_quest": sorted(unknown, key=lambda r: -r["gib"])[:15],
    }


def zombies(w: World, now: float) -> Json:
    """query zombies: jobs in no terminal state that nothing will advance (running with a stale
    heartbeat; queued for a landed or dropped quest; queued after a job that failed), and quests in
    flight whose branch and worktree are gone or that nothing touched in three days."""
    st = status_of(w)
    by_id = {j.id: j for j in w.jobs}
    rows: list[Json] = []
    for j in w.jobs:
        if j.state == "running":
            beat = src.JOBS / j.id / "heartbeat"
            stale = now - (beat.stat().st_mtime if beat.exists() else 0)
            if stale > 120:
                rows.append({"job": j.id, "why": f"running, heartbeat {stale / 3600:.1f} h old", "quest": j.quest})
        elif j.state == "queued":
            s = st.get((j.campaign, j.quest), "")
            if s in ("landed", "dropped"):
                rows.append(
                    {
                        "job": j.id,
                        "why": f"queued for a {s} quest",
                        "quest": j.quest,
                        "level": j.level,
                        "minutes": j.minutes,
                    }
                )
            bad = [x for x in j.after if x in by_id and by_id[x].state in src.TERMINAL and by_id[x].state != "done"]
            if bad:
                rows.append({"job": j.id, "why": "queued after a job that did not succeed", "quest": j.quest})
    last: dict[tuple[str, str], float] = defaultdict(float)
    for j in w.jobs:
        last[(j.campaign, j.quest)] = max(last[(j.campaign, j.quest)], j.ended or j.submitted)
    for c, u in w.usage.items():
        for r in u.get("runs", []):
            k = (c, r.get("quest", ""))
            last[k] = max(last[k], src.stamp(r.get("ended", "")))
    for c, hist in w.history.items():
        for e in hist:
            last[(c, e.quest)] = max(last[(c, e.quest)], e.t)
    quests: list[Json] = []
    for c, stt in w.states.items():
        for q, r in stt.get("quests", {}).items():
            if r.get("status") not in ("active", "review"):
                continue
            idle = (now - last[(c, q)]) / DAY if last[(c, q)] else None
            wt = r.get("worktree", "")
            gone = bool(wt) and not Path(wt).is_dir()
            if gone or idle is None or idle > 3:
                quests.append(
                    {
                        "campaign": c,
                        "quest": q,
                        "status": r.get("status"),
                        "idle_days": round(idle, 1) if idle else None,
                        "worktree_gone": gone,
                    }
                )
    return {"jobs": rows, "jobs_count": len(rows), "quests": quests, "quests_count": len(quests)}


def processes() -> Json:
    """query processes: `ps` rows that are zombies (Z) or campaign runners and holds whose job is terminal."""
    res = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,stat=,etime=,time=,command="], capture_output=True, text=True, errors="replace"
    )
    z: list[Json] = []
    orphans: list[Json] = []
    jid = re.compile(r"\b(\d{6}-\d{6}-[0-9a-f]{6})\b")
    terminal: set[str] = set()
    for d in src.JOBS.iterdir():
        if jid.fullmatch(d.name):
            s = src.read_json(d / "state.json") or {}
            if s.get("state") in src.TERMINAL:
                terminal.add(d.name)
    for line in res.stdout.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        pid, ppid, stat, etime, cpu, cmd = parts
        if stat.startswith("Z"):
            z.append({"pid": pid, "ppid": ppid, "etime": etime, "cmd": cmd[:120]})
        m = jid.search(cmd)
        if m and m[1] in terminal and ("_run" in cmd or "quiet" in cmd or "JOB_" in cmd or "jobs/" in cmd):
            orphans.append({"pid": pid, "ppid": ppid, "job": m[1], "etime": etime, "cpu": cpu, "cmd": cmd[:160]})
    return {"zombies": z, "zombie_count": len(z), "children_of_finished_jobs": orphans, "orphan_count": len(orphans)}


DUP = re.compile(r"redundant|superseded|already landed|duplicate|landed the fix|redid", re.I)


def duplicates(w: World, out: Path) -> Json:
    """query duplicates: jobs whose run repeated a passed one at code differing only outside its inputs
    (classify_reruns' avoidable rows), and quests dropped as redundant or superseded, with their jobs'
    machine time and their agents' tokens."""
    rows: list[Json] = (
        [json.loads(x) for x in (out / "reruns.jsonl").read_text().splitlines()]
        if (out / "reruns.jsonl").exists()
        else []
    )
    avoid = [r for r in rows if r["verdict"] == "avoidable" and r["ran"]]
    by_why: Counter[str] = Counter()
    for r in avoid:
        by_why[r["why"]] += r["run_s"]
    dropped: list[Json] = []
    for c, hist in w.history.items():
        for e in hist:
            if e.verb == "set" and "status=dropped" in e.args and DUP.search(e.args):
                js = [j for j in w.jobs if j.campaign == c and j.quest == e.quest]
                toks = sum(int(r.get("tokens") or 0) for r in w.usage[c].get("runs", []) if r.get("quest") == e.quest)
                dropped.append(
                    {
                        "campaign": c,
                        "quest": e.quest,
                        "at": src.iso(e.t),
                        "reason": e.args[:160],
                        "jobs": len(js),
                        "cpu_min": round(sum(j.run_s + j.build_s for j in js) / 60),
                        "tokens_M": round(toks / 1e6, 1),
                    }
                )
    return {
        "avoidable_reruns": len(avoid),
        "avoidable_run_h": round(sum(r["run_s"] for r in avoid) / 3600, 1),
        "avoidable_by_why_h": {k: round(v / 3600, 1) for k, v in by_why.most_common()},
        "dropped_as_redundant": dropped,
    }


def run(w: World, out: Path) -> Json:
    now = time.time()
    walk = du(src.HOME / ".scratch", 4)  # one walk: owners, tasks, job directories and build slots
    res: Json = {
        "at": src.iso(now),
        "scratch": scratch(w, now, walk),
        "job_store": job_store(w, now, walk),
        "worktrees": worktrees(w, now, walk),
        "zombies": zombies(w, now),
        "processes": processes(),
        "duplicates": duplicates(w, out),
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "leftovers.json").write_text(json.dumps(res, indent=1))
    return res
