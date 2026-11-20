"""Where the time went (A2): machine time, quest wall time and agent tokens over the window, by cause.

Each figure in account.json comes from one function here, named in its docstring as `query NAME`, so
a document citing a number names the code and the command (`replay.py account`) behind it.
"""

from __future__ import annotations

import bisect
import fnmatch
import json
import os
import re
import statistics
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

import sources as src
from sources import Job, Json
from timeline import World

H = 3600.0
Interval = tuple[float, float]


def clip(a: float, b: float) -> Interval | None:
    a, b = max(a, src.SINCE), min(b, src.UNTIL)
    return (a, b) if b > a else None


def hours(x: float) -> float:
    return round(x / H, 1)


def share(part: float, whole: float) -> float:
    return round(100 * part / whole, 1) if whole else 0.0


def overlap(a: float, b: float) -> float:
    iv = clip(a, b)
    return iv[1] - iv[0] if iv else 0.0


def union(ivs: list[Interval]) -> list[Interval]:
    out: list[Interval] = []
    for a, b in sorted(ivs):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def total(ivs: list[Interval]) -> float:
    return sum(b - a for a, b in union(ivs))


def in_window(j: Job) -> bool:
    return src.SINCE <= j.submitted < src.UNTIL


# ---------------------------------------------------------------- machine time by level and state


def machine_time(jobs: list[Job]) -> Json:
    """query machine_time: run (began..ended) and staged-build hours of every job, clipped to the window."""
    rows: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    for j in jobs:
        rows[j.campaign][j.level][j.state] += overlap(j.began, j.ended) if j.began and j.ended else 0.0
        rows[j.campaign][j.level]["(staged build)"] += overlap(j.build_started, j.build_ended) if j.build_s else 0.0
    out: Json = {}
    grand = 0.0
    for c, levels in sorted(rows.items()):
        out[c] = {lv: {k: hours(v) for k, v in states.items() if v} for lv, states in sorted(levels.items())}
        sub = sum(sum(s.values()) for s in levels.values())
        out[c]["total_h"] = hours(sub)
        grand += sub
    out["total_h"] = hours(grand)
    return out


# ---------------------------------------------------------------- waits and light capacity


def waits(jobs: list[Job]) -> Json:
    """query waits: job-hours queued (submitted..claimed) and in hold (claimed..began: source
    preparation, a staged build, admission and quiet's wait), by level, for jobs submitted in the
    window; jobs that never ran count until they ended (cancelled or skipped) or the window's end."""
    q: dict[str, float] = defaultdict(float)
    hold: dict[str, float] = defaultdict(float)
    never: dict[str, float] = defaultdict(float)
    n: Counter[str] = Counter()
    per: dict[str, list[float]] = defaultdict(list)
    for j in jobs:
        if not in_window(j):
            continue
        n[j.level] += 1
        if j.ran:
            q[j.level] += overlap(j.submitted, j.claimed or j.began)
            hold[j.level] += overlap(j.claimed or j.began, j.began)
            per[j.level].append(j.began - j.submitted)
        else:
            never[j.level] += overlap(j.submitted, j.ended or src.UNTIL)
    out: Json = {}
    for lv in sorted(n):
        xs = sorted(per[lv])
        out[lv] = {
            "jobs": n[lv],
            "ran": len(xs),
            "queued_job_h": hours(q[lv]),
            "hold_job_h": hours(hold[lv]),
            "never_ran_job_h": hours(never[lv]),
            "median_wait_min": round(statistics.median(xs) / 60, 1) if xs else None,
            "p90_wait_min": round(xs[int(0.9 * (len(xs) - 1))] / 60, 1) if xs else None,
        }
    return out


class Counts:
    """How many intervals hold at time t: (#starts <= t) - (#ends <= t)."""

    def __init__(self, ivs: list[Interval]) -> None:
        self.a = sorted(a for a, _ in ivs)
        self.b = sorted(b for _, b in ivs)

    def at(self, t: float) -> int:
        return bisect.bisect_right(self.a, t) - bisect.bisect_right(self.b, t)


LIGHT_SLOTS, LIGHT_BESIDE_HEAVY = 8, 4  # the tool's light_slots() on 16 CPUs: ncpu // 2, ncpu // 4 beside heavy


def light_intervals(jobs: list[Job]) -> list[Interval]:
    out = [(j.began, j.ended) for j in jobs if j.level == "light" and j.began and j.ended]
    return out + [(j.build_started, j.build_ended) for j in jobs if j.build_s]


def light_capacity(jobs: list[Job], step: float = 60.0) -> Json:
    """query light_capacity: sampled each minute, the light slots (8) idle while light work was ready
    (submitted, its `after` jobs ended, not yet claimed), by what held the machine at that minute; ready
    work whose build slot another job holds is counted apart, as it could not have been claimed."""
    by_id = {j.id: j for j in jobs}
    occ = Counts(light_intervals(jobs))
    ex_run = Counts([(j.began, j.ended) for j in jobs if j.level == "exclusive" and j.began and j.ended])
    ex_drain = Counts([(j.claimed, j.began) for j in jobs if j.level == "exclusive" and j.claimed and j.began])
    st_run = Counts([(j.began, j.ended) for j in jobs if j.level == "strict" and j.began and j.ended])
    held: dict[str, list[Interval]] = defaultdict(list)  # a slot is one build directory: one job at a time
    for j in jobs:
        if j.slot and j.claimed:
            held[j.slot].append((j.claimed, j.ended or src.UNTIL))
    free: list[Interval] = []
    blocked: list[Interval] = []
    for j in jobs:
        if j.level != "light":
            continue
        deps = [by_id[x].ended for x in j.after if x in by_id and by_id[x].ended]
        if len(deps) < len(j.after):
            continue
        start, end = max([j.submitted, *deps]), j.claimed or j.began or j.ended or src.UNTIL
        if end <= start:
            continue
        busy = union(
            [
                iv
                for a, b in held.get(j.slot, [])
                if (a, b) != (j.claimed, j.ended or src.UNTIL)
                for iv in [(max(a, start), min(b, end))]
                if iv[1] > iv[0]
            ]
        )
        cur = start
        for a, b in busy:
            if a > cur:
                free.append((cur, a))
            blocked.append((a, b))
            cur = b
        if end > cur:
            free.append((cur, end))
    rq, rb = Counts(free), Counts(blocked)
    cause: Counter[str] = Counter()
    used = 0.0
    samples = 0
    t = src.SINCE
    while t < src.UNTIL:
        o, r = occ.at(t), rq.at(t)
        used += min(o, LIGHT_SLOTS)
        samples += 1
        idle = max(0, LIGHT_SLOTS - o)
        lost = min(idle, r)
        cause["its build slot held by another job of it (ready, not claimable)"] += min(idle - lost, rb.at(t))
        if lost:
            if ex_run.at(t):
                cause["an exclusive job running"] += lost
            elif ex_drain.at(t):
                cause["an exclusive job draining the light holds"] += lost
            elif st_run.at(t):
                beyond = min(lost, LIGHT_SLOTS - max(o, LIGHT_BESIDE_HEAVY))
                cause["a strict job running (light keeps 4 slots)"] += beyond
                cause["no recorded reason (measured-use charges, fair share, order, load gate)"] += lost - beyond
            else:
                cause["no recorded reason (measured-use charges, fair share, order, load gate)"] += lost
        t += step
    slot_h = step / H
    ex = [j for j in jobs if j.level == "exclusive"]
    return {
        "slot_hours": round(LIGHT_SLOTS * samples * slot_h, 1),
        "used_slot_hours": round(used * slot_h, 1),
        "utilization_pct": share(used, LIGHT_SLOTS * samples),
        "idle_slot_hours_while_light_work_ready": {k: round(v * slot_h, 1) for k, v in cause.most_common()},
        "exclusive_drain_wall_h": hours(
            total([iv for j in ex if j.claimed and j.began for iv in [clip(j.claimed, j.began)] if iv])
        ),
        "exclusive_run_wall_h": hours(
            total([iv for j in ex if j.began and j.ended for iv in [clip(j.began, j.ended)] if iv])
        ),
    }


def hourly(jobs: list[Job]) -> list[Json]:
    """query hourly: light-slot use per hour (minute samples) and the queue at each hour's start."""
    occ = Counts(light_intervals(jobs))
    waiting = Counts([(j.submitted, j.began or j.ended or src.UNTIL) for j in jobs])
    wex = Counts([(j.submitted, j.began or j.ended or src.UNTIL) for j in jobs if j.level == "exclusive"])
    out = []
    t = src.SINCE - (src.SINCE % H) + H
    while t < src.UNTIL:
        used = sum(min(occ.at(t + 60 * m), LIGHT_SLOTS) for m in range(60))
        out.append(
            {
                "hour": src.iso(t),
                "light_pct": share(used, LIGHT_SLOTS * 60),
                "queued": waiting.at(t),
                "queued_exclusive": wex.at(t),
            }
        )
        t += H
    return out


# ---------------------------------------------------------------- reruns


DOC_PATHS = ("docs/*", "website/*", "*.md")
BUILD_FILES = (
    "CMakeLists*",
    "*.cmake",
    "CMakePresets*.json",
    "*toolchain*",
    "Makefile",
    "GNUmakefile",
    "*.mk",
    "meson.build",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    ".gitmodules",
)


def named_paths(cmd: str, cwd: str) -> list[str]:
    """The repository paths a command names: the tool's named_paths."""
    out: list[str] = []
    for token in re.split(r"[\s;|&()<>=\"'`]+", cmd):
        word = token[2:] if token.startswith("./") else token
        if not word or word.startswith(("-", "/", "$", "~")) or set(word) & set("*?[") or word in (".", ".."):
            continue
        out.append(os.path.normpath(os.path.join(cwd, word)).rstrip("/") if cwd else word.rstrip("/"))
    return [x for x in dict.fromkeys(out) if x and not x.startswith("..")]


def has_commit(repo: Path, sha: str) -> bool:
    res = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True)
    return res.returncode == 0


class Diffs:
    """`git diff --name-only A B` (outside .campaign/) in a job's base repository, cached in OUT/diffs.json."""

    def __init__(self, out: Path) -> None:
        self.path = out / "diffs.json"
        cached = src.read_json(self.path) if self.path.exists() else None
        self.cache: dict[str, list[str] | None] = cached if isinstance(cached, dict) else {}
        self.dirty = False

    def names(self, repo: str, a: str, b: str) -> list[str] | None:
        base = src.base_repo(repo)
        if base is None or not a or not b:
            return None
        k = f"{base}:{a}:{b}"
        if k not in self.cache:
            ok = has_commit(base, a) and has_commit(base, b)
            res = (
                src.git(base, "diff", "--name-only", "--no-renames", a, b, "--", ".", ":(exclude).campaign")
                if ok
                else ""
            )
            self.cache[k] = [x for x in res.splitlines() if x] if ok else None
            self.dirty = True
        return self.cache[k]

    def save(self) -> None:
        if self.dirty:
            self.path.write_text(json.dumps(self.cache))


def matches(path: str, globs: list[str] | tuple[str, ...]) -> bool:
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatchcase(path, g) or fnmatch.fnmatchcase(name, g) for g in globs)


COMMITISH = re.compile(r"\b[0-9a-f]{7,40}\b|\$\$")  # a commit or the shell's pid, which commands embed in paths


def shape(j: Job) -> str:
    """A job's identity without its code, host or toolchain: what makes a later one a rerun of it.
    Commits and `$$` in the command are masked: commands name output paths after their commit."""
    base = src.base_repo(j.repo)
    cmd, build = COMMITISH.sub("<c>", j.cmd), COMMITISH.sub("<c>", j.build)
    return json.dumps([str(base or j.repo), cmd, j.cwd, j.level, build, j.fails_without, sorted(j.env.items())])


def classify_reruns(w: World, out: Path) -> tuple[Json, list[Json]]:
    """query classify_reruns: a job submitted in the window whose shape (repository, command, directory,
    level, build, environment) ran before is a rerun of the latest such run. It is necessary when an
    input changed since, avoidable when none did. Inputs, by what is known: the read set (never
    recorded), then the paths its command and build name and the build files, then the quest's file
    set (current value); a retry is a rerun at the same code after a failure."""
    files: dict[tuple[str, str], list[str]] = {}
    for c, st in w.states.items():
        for q, rec in st.get("quests", {}).items():
            files[(c, q)] = list(rec.get("files") or [])
    diffs = Diffs(out)
    groups: dict[str, list[Job]] = defaultdict(list)
    for j in sorted(w.jobs, key=lambda j: j.submitted):
        groups[shape(j)].append(j)
    rows: list[Json] = []
    for group in groups.values():
        prev: Job | None = None
        for j in group:
            if prev is not None and in_window(j):
                rows.append(rerun_row(j, prev, diffs, files))
            if j.ran:
                prev = j
    diffs.save()
    summary: dict[str, Counter[str]] = defaultdict(Counter)
    for r in rows:
        key = f"{r['verdict']}: {r['why']}"
        summary[key]["jobs"] += 1
        summary[key]["ran"] += int(r["ran"])
        summary[key]["run_s"] += r["run_s"]
        summary[key]["wait_s"] += r["wait_s"]
    table: Json = {
        k: {"jobs": int(v["jobs"]), "ran": int(v["ran"]), "run_h": hours(v["run_s"]), "wait_job_h": hours(v["wait_s"])}
        for k, v in sorted(summary.items(), key=lambda kv: -kv[1]["run_s"])
    }
    by_level: dict[str, Counter[str]] = defaultdict(Counter)
    for r in rows:
        by_level[r["level"]][r["verdict"]] += r["run_s"]
    table["by_level_run_h"] = {lv: {k: hours(v) for k, v in c.items()} for lv, c in sorted(by_level.items())}
    table["first_runs"] = sum(1 for j in w.jobs if in_window(j)) - len(rows)
    return table, rows


def rerun_row(j: Job, p: Job, diffs: Diffs, files: dict[tuple[str, str], list[str]]) -> Json:
    row: Json = {
        "job": j.id,
        "prev": p.id,
        "campaign": j.campaign,
        "quest": j.quest,
        "level": j.level,
        "state": j.state,
        "run_s": j.run_s + j.build_s,
        "wait_s": max(0.0, (j.began or j.ended or src.UNTIL) - j.submitted),
        "ran": j.ran,
    }
    changed: list[str] | None = [] if j.commit == p.commit else diffs.names(j.repo, p.commit, j.commit)
    if changed is None:
        return {**row, "verdict": "unknown", "why": "a commit is gone from its repository"}
    if not changed:
        if j.scripts != p.scripts:
            return {**row, "verdict": "necessary", "why": "an outside script it runs changed"}
        if p.state == "done":
            return {**row, "verdict": "avoidable", "why": "same code after a pass"}
        return {**row, "verdict": "retry", "why": f"same code after {p.state}"}
    row["changed"] = len(changed)
    if all(matches(x, DOC_PATHS) for x in changed):
        return {**row, "verdict": "avoidable", "why": "only documentation changed"}
    named = named_paths(f"{j.cmd} {j.build}", j.cwd)
    if any(matches(x, BUILD_FILES) or any(x == n or x.startswith(n + "/") for n in named) for x in changed):
        return {**row, "verdict": "necessary", "why": "a path its command names or a build file changed"}
    globs = files.get((j.campaign, j.quest), [])
    if not globs:
        return {**row, "verdict": "necessary", "why": "code changed; no file set to scope it"}
    if any(matches(x, globs) for x in changed):
        return {**row, "verdict": "necessary", "why": "a file in the quest's file set changed"}
    return {**row, "verdict": "avoidable", "why": "only files outside the quest's file set changed"}


# ---------------------------------------------------------------- land refusals


REASONS = (
    ("main moved", re.compile(r"(main|master) changed \d+ files? since")),
    ("CI running", re.compile(r"CI at \w+: running")),
    ("CI failed", re.compile(r"CI at \w+: failed")),
    ("CI missing", re.compile(r"CI at \w+: no CI runs")),
    ("evidence missing", re.compile(r"criteria without evidence")),
    ("evidence at other code", re.compile(r"evidence jobs ran other code")),
    ("review not accepted", re.compile(r"review not accepted|not reviewed yet")),
    ("accepted, code changed since", re.compile(r"was accepted at \w+; changed since|changed \d+ code files under")),
    ("scope", re.compile(r"edits the lead|changes more than its journals|outside its file set")),
    ("check not green", re.compile(r"is not green")),
)
SHA12 = re.compile(r"\bat ([0-9a-f]{12})\b")
REFUSAL = re.compile(r"campaign: (\S+) cannot land yet:((?:\n  .*)+)")


def refusal_reasons(block: str) -> list[str]:
    lines = [x for x in block.splitlines() if x.startswith("  ")]
    return sorted({next((n for n, rx in REASONS if rx.search(line)), "other") for line in lines}) or ["other"]


def land_refusals(w: World) -> tuple[Json, list[Json]]:
    """query land_refusals: each "cannot land yet" answer to a `campaign land` the lead ran (its
    transcript, background outputs included); an episode runs from a quest's first refusal to its
    next landing in state history, and counts the quest's jobs submitted and accepts recorded in it."""
    episodes: list[Json] = []
    reasons: Counter[str] = Counter()
    for c, lead in w.lead.items():
        lands: dict[str, list[float]] = defaultdict(list)
        accepts: dict[str, list[float]] = defaultdict(list)
        for e in w.history.get(c, []):
            if e.verb == "land" and "increment" not in e.args:
                lands[e.quest].append(e.t)
            elif e.verb == "accept":
                accepts[e.quest].append(e.t)
        refused: dict[str, list[Json]] = defaultdict(list)
        seen: set[tuple[str, int]] = set()
        for cmd in lead.commands:
            if cmd.verb != "land" or not (src.SINCE <= cmd.t < src.UNTIL):
                continue
            for m in REFUSAL.finditer(cmd.output):
                key = (m[1], round(cmd.t))
                if key in seen:
                    continue
                seen.add(key)
                rs = refusal_reasons(m[0])
                reasons.update(rs)
                refused[m[1]].append({"t": cmd.t, "reasons": rs, "commits": SHA12.findall(m[0])})
        for q, rs in refused.items():
            rs.sort(key=lambda r: r["t"])
            ep: list[Json] = []
            for r in rs:
                if ep and any(ep[0]["t"] <= t < r["t"] for t in lands[q]):
                    episodes.append(episode(c, q, ep, lands=lands[q], accepts=accepts[q], jobs=w.jobs))
                    ep = []
                ep.append(r)
            if ep:
                episodes.append(episode(c, q, ep, lands=lands[q], accepts=accepts[q], jobs=w.jobs))

    def tally(eps: list[Json]) -> Json:
        return {
            "episodes": len(eps),
            "refusals": sum(e["refusals"] for e in eps),
            "landed": sum(1 for e in eps if e["landed"]),
            "wall_h_first_refusal_to_land": round(sum(e["wall_h"] for e in eps if e["landed"]), 1),
            "median_h_first_refusal_to_land": round(statistics.median([e["wall_h"] for e in eps if e["landed"]]), 2)
            if any(e["landed"] for e in eps)
            else None,
            "jobs_in_episodes": sum(e["jobs"] for e in eps),
            "job_run_h_in_episodes": round(sum(e["job_run_h"] for e in eps), 1),
            "accepts_in_episodes": sum(e["accepts"] for e in eps),
            "distinct_commits_refused": sum(e["commits"] for e in eps),
        }

    return {
        "refusals_by_reason": dict(reasons.most_common()),
        "all": tally(episodes),
        "with_main_moved": tally([e for e in episodes if "main moved" in e["reasons"]]),
        "refused_only_because_main_moved": tally([e for e in episodes if e["only_main_moved"]]),
    }, episodes


def episode(c: str, q: str, ep: list[Json], *, lands: list[float], accepts: list[float], jobs: list[Job]) -> Json:
    t0 = ep[0]["t"]
    landed = next((t for t in sorted(lands) if t >= t0), 0.0)
    t1 = landed or src.UNTIL
    js = [j for j in jobs if j.campaign == c and j.quest == q and t0 <= j.submitted <= t1]
    return {
        "campaign": c,
        "quest": q,
        "first": src.iso(t0),
        "refusals": len(ep),
        "reasons": sorted({x for r in ep for x in r["reasons"]}),
        "only_main_moved": any(r["reasons"] == ["main moved"] for r in ep),
        "landed": bool(landed),
        "wall_h": round((t1 - t0) / H, 2),
        "jobs": len(js),
        "job_run_h": round(sum(j.run_s + j.build_s for j in js) / H, 2),
        "accepts": sum(1 for t in accepts if t0 <= t <= t1),
        "commits": len({x for r in ep for x in r["commits"]}),
    }


# ---------------------------------------------------------------- quests: what each in-flight hour was


FLIGHT = ("active", "review", "blocked")
STATUS = re.compile(r"\bstatus=(\w+)")


def status_intervals(w: World) -> dict[tuple[str, str], list[tuple[float, float, str]]]:
    """query status_intervals: each quest's status over time, from its state.json history subjects
    (new: planned, start: active, set status=S: S, land: landed); unknown before its first event."""
    out: dict[tuple[str, str], list[tuple[float, float, str]]] = {}
    for c, hist in w.history.items():
        cur: dict[str, tuple[float, str]] = {}
        for e in hist:
            new = ""
            if e.verb == "new":
                new = "planned"
            elif e.verb == "start":
                new = "active"
            elif e.verb == "set" and not e.args.startswith("task "):
                m = STATUS.search(e.args)
                new = m[1] if m else ""
            elif e.verb == "land" and "increment" not in e.args:
                new = "landed"
            if not new or not e.quest:
                continue
            if e.quest in cur:
                t0, s0 = cur[e.quest]
                out.setdefault((c, e.quest), []).append((t0, e.t, s0))
            cur[e.quest] = (e.t, new)
        for q, (t0, s0) in cur.items():
            out.setdefault((c, q), []).append((t0, src.UNTIL, s0))
    return out


LABELS = ("agent turn", "job running", "job waiting for the machine", "waiting on a dependency")


def quest_hours(w: World) -> tuple[Json, list[Json]]:
    """query quest_hours: every in-flight quest-hour (status active, review or blocked) labelled by the
    first that holds: an agent run of the quest (`campaign usage`); a job of it running; a job of it
    queued or in its hold; an `after` dependency (current value) not landed; else nothing moving it.
    Tasks (QUEST--TASK) are left out: their quest's hours cover them."""
    landed_at: dict[tuple[str, str], float] = {}
    for c, hist in w.history.items():
        for e in hist:
            if e.verb == "land" and "increment" not in e.args:
                landed_at.setdefault((c, e.quest), e.t)
    jobs_of: dict[tuple[str, str], list[Job]] = defaultdict(list)
    for j in w.jobs:
        jobs_of[(j.campaign, j.quest)].append(j)
    runs_of: dict[tuple[str, str], list[Interval]] = defaultdict(list)
    for c, u in w.usage.items():
        for r in u.get("runs", []):
            a, b = src.stamp(r.get("began", "")), src.stamp(r.get("ended", ""))
            if a and b > a:
                runs_of[(c, r.get("quest", ""))].append((a, b))
    totals: dict[str, float] = defaultdict(float)
    per: list[Json] = []
    for (c, q), ivs in sorted(status_intervals(w).items()):
        if "--" in q:
            continue  # a task (QUEST--TASK): its quest's hours cover it
        rec = w.states.get(c, {}).get("quests", {}).get(q, {})
        dep_until = max((landed_at.get((c, d), src.UNTIL) for d in rec.get("after", [])), default=0.0)
        labelled: list[tuple[float, float, int]] = [(a, b, 0) for a, b in runs_of[(c, q)]]
        for j in jobs_of[(c, q)]:
            end = j.ended or src.UNTIL
            if j.began:
                labelled.append((j.began, end, 1))
            labelled.append((j.submitted, j.began or end, 2))
        mine: dict[str, float] = defaultdict(float)
        for a0, b0, status in ivs:
            iv = clip(a0, b0) if status in FLIGHT else None
            if not iv:
                continue
            extra = [(iv[0], dep_until, 3)] if dep_until > iv[0] else []
            for a, b, lab in segments(iv, labelled + extra):
                name = LABELS[lab] if lab < len(LABELS) else f"nothing moving it ({status})"
                mine[name] += b - a
        if mine:
            for k, v in mine.items():
                totals[k] += v
            per.append(
                {
                    "campaign": c,
                    "quest": q,
                    "status_now": rec.get("status", ""),
                    **{k: hours(v) for k, v in mine.items()},
                }
            )
    whole = sum(totals.values())
    table: Json = {k: {"h": hours(v), "pct": share(v, whole)} for k, v in sorted(totals.items(), key=lambda kv: -kv[1])}
    table["total"] = {"h": hours(whole), "pct": 100.0, "quests": len(per)}
    return table, per


def segments(iv: Interval, labelled: list[tuple[float, float, int]]) -> list[tuple[float, float, int]]:
    """Splits IV at every boundary and labels each piece with the lowest label covering it (99: none)."""
    a, b = iv
    near = [x for x in labelled if x[0] < b and x[1] > a]
    cuts = sorted({a, b, *(x for s, e, _ in near for x in (s, e) if a < x < b)})
    out = []
    for s, e in zip(cuts, cuts[1:]):
        mid = (s + e) / 2
        out.append((s, e, min((lb for x, y, lb in near if x <= mid < y), default=99)))
    return out


# ---------------------------------------------------------------- agent tokens per landed quest


def active(times: list[float], gap: float = 300.0) -> float:
    """Seconds between consecutive transcript lines in the window, each gap capped at GAP: time working."""
    ts = [t for t in times if src.SINCE <= t < src.UNTIL]
    return sum(min(b - a, gap) for a, b in zip(ts, ts[1:]))


def agent_tokens(w: World) -> Json:
    """query agent_tokens: `campaign usage --json` runs begun in the window, per quest, for quests that
    landed in the window and for the rest; and the lead's tokens and active hours (its transcript)."""
    landed: set[tuple[str, str]] = set()
    for c, hist in w.history.items():
        for e in hist:
            if e.verb == "land" and "increment" not in e.args and src.SINCE <= e.t < src.UNTIL:
                landed.add((c, e.quest))
    per: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for c, u in w.usage.items():
        for r in u.get("runs", []):
            if not (src.SINCE <= src.stamp(r.get("began", "")) < src.UNTIL):
                continue
            k = (c, r.get("quest", "") or "-")
            per[k]["runs"] += 1
            per[k]["tokens"] += int(r.get("tokens") or 0)
            per[k]["output"] += int(r.get("output_tokens") or 0)
            per[k]["seconds"] += float(r.get("seconds") or 0)
    out: Json = {}
    for c in w.homes:
        mine = {q: v for (cc, q), v in per.items() if cc == c}
        lq = [v for q, v in mine.items() if (c, q) in landed]
        rest = [v for q, v in mine.items() if (c, q) not in landed]
        lead_tokens = sum(v for h, v in w.lead[c].tokens.items() if src.SINCE <= src.stamp(h + ":00Z") < src.UNTIL)

        def med(key: str, scale: float, vs: list[dict[str, float]] = lq) -> float | None:
            return round(statistics.median([v[key] for v in vs]) / scale, 2) if vs else None

        out[c] = {
            "quests_landed_in_window": sum(1 for cc, _ in landed if cc == c),
            "landed_with_agent_runs": len(lq),
            "runs_per_landed_median": med("runs", 1),
            "tokens_per_landed_median_M": med("tokens", 1e6),
            "tokens_per_landed_mean_M": round(statistics.mean([v["tokens"] for v in lq]) / 1e6, 2) if lq else None,
            "output_tokens_per_landed_median_k": med("output", 1e3),
            "agent_h_per_landed_median": med("seconds", H),
            "agent_tokens_on_landed_M": round(sum(v["tokens"] for v in lq) / 1e6, 1),
            "agent_tokens_on_not_landed_M": round(sum(v["tokens"] for v in rest) / 1e6, 1),
            "agent_runs": sum(v["runs"] for v in mine.values()),
            "agent_h": round(sum(v["seconds"] for v in mine.values()) / H, 1),
            "lead_tokens_M": round(lead_tokens / 1e6, 1),
            "lead_active_h": round(active(w.lead[c].activity) / H, 1),
        }
    return out


# ---------------------------------------------------------------- run


def run(w: World, out: Path) -> Json:
    out.mkdir(parents=True, exist_ok=True)
    reruns, rows = classify_reruns(w, out)
    refusals, episodes = land_refusals(w)
    qh, per = quest_hours(w)
    res: Json = {
        "window": [src.iso(src.SINCE), src.iso(src.UNTIL)],
        "machine_time": machine_time(w.jobs),
        "waits": waits(w.jobs),
        "light_capacity": light_capacity(w.jobs),
        "reruns": reruns,
        "land_refusals": refusals,
        "quest_hours": qh,
        "agent_tokens": agent_tokens(w),
    }
    (out / "account.json").write_text(json.dumps(res, indent=1))
    (out / "reruns.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (out / "episodes.json").write_text(json.dumps(episodes, indent=1))
    (out / "quests.json").write_text(json.dumps(per, indent=1))
    (out / "hourly.json").write_text(json.dumps(hourly(w.jobs), indent=1))
    return res
