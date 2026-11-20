"""Runs no question needed (A2, A3): beyond identical reruns, the runs whose question was already
superseded or moot (quest landed or dropped), the trials past what decided their row, and the landing-time reruns whose
declared inputs had not changed.

Each job gets the first category that applies, so no job counts twice; `replay.py need` writes
need.json (the totals) and need.jsonl (one row per job). v2 records no declared inputs, so the
inputs are a stated proxy: the paths a job's command and build name, the quest's file set
(current value), the top-level build files, and build files under those paths.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

import account
import sources as src
from sources import Job, Json
from timeline import World

H = 3600.0
TRIAL = re.compile(r"(?i)\b(?:trial|round)[ _-]?\d+\b|(?<=[-_])t\d+(?=[-_\s'\"/]|$)")

CATEGORIES = (
    "identical rerun",  # account.classify_reruns: avoidable
    "quest landed or dropped before it began",
    "superseded: a newer submission for the same question before it began",
    "trial past the first two of a passing row at one commit",
    "landing-time rerun, declared inputs unchanged",
)


def question(j: Job) -> str:
    """What a job answers: its row, the shape with any trial index masked. A criterion is many rows
    and v2 records no verdict per criterion over time, so "criterion already evidenced" is not
    determinable; the row is the finest question the records name."""
    return TRIAL.sub("<t>", account.shape(j))


def trial_group(j: Job) -> str:
    return json.dumps([j.campaign, j.quest, j.commit, TRIAL.sub("<t>", account.shape(j))])


def ends_of(w: World) -> dict[tuple[str, str], float]:
    """Each quest's first landing or drop in history."""
    out: dict[tuple[str, str], float] = {}
    for c, hist in w.history.items():
        for e in hist:
            done = (e.verb == "land" and "increment" not in e.args) or (e.verb == "set" and "status=dropped" in e.args)
            if done and e.quest:
                out.setdefault((c, e.quest), e.t)
    return out


def landing_starts(w: World, episodes: list[Json]) -> dict[tuple[str, str], float]:
    """When each quest entered its landing: its first accept or first land refusal."""
    out: dict[tuple[str, str], float] = {}
    for c, hist in w.history.items():
        for e in hist:
            if e.verb == "accept" and e.quest:
                out[(c, e.quest)] = min(out.get((c, e.quest), e.t), e.t)
    for ep in episodes:
        k = (ep["campaign"], ep["quest"])
        t = src.stamp(ep["first"].replace("Z", ":00Z"))
        out[k] = min(out.get(k, t), t)
    return out


def declared(j: Job, files: list[str], changed: list[str]) -> bool:
    """Whether a changed path is among the job's inputs, by the proxy in the module docstring."""
    named = account.named_paths(f"{j.cmd} {j.build}", j.cwd)

    def under(x: str) -> bool:
        return any(x == n or x.startswith(n + "/") for n in named) or account.matches(x, files)

    for x in changed:
        if under(x):
            return True
        if account.matches(x, account.BUILD_FILES) and ("/" not in x or under(x.rsplit("/", 1)[0])):
            return True
    return False


def classify(w: World, out: Path) -> tuple[Json, list[Json]]:
    """query need: every job submitted in the window, by the first CATEGORIES entry that applies."""
    reruns = {r["job"]: r for r in map(json.loads, (out / "reruns.jsonl").read_text().splitlines())}
    episodes: list[Json] = src.read_json(out / "episodes.json") or []
    by_id = {j.id: j for j in w.jobs}
    files = {(c, q): list(r.get("files") or []) for c, st in w.states.items() for q, r in st.get("quests", {}).items()}
    ended = ends_of(w)
    landing = landing_starts(w, episodes)
    diffs = account.Diffs(out)
    jobs = sorted(w.jobs, key=lambda j: j.submitted)
    by_q: dict[str, list[Job]] = defaultdict(list)
    for j in jobs:
        by_q[question(j)].append(j)
    newer: dict[str, float] = {}
    for group in by_q.values():
        for a, b in zip(group, group[1:]):
            if b.commit != a.commit:
                newer[a.id] = b.submitted
    trials: dict[str, list[Job]] = defaultdict(list)
    rows: list[Json] = []
    for j in jobs:
        tg = trial_group(j)
        before = list(trials[tg])
        trials[tg].append(j)
        if not account.in_window(j):
            continue
        start = j.began or j.ended or src.UNTIL
        k = (j.campaign, j.quest)
        cat = ""
        r = reruns.get(j.id)
        if r and r["verdict"] == "avoidable":
            cat = CATEGORIES[0]
        elif k in ended and ended[k] <= start:
            cat = CATEGORIES[1]
        elif j.id in newer and newer[j.id] <= start:
            cat = CATEGORIES[2]
        elif TRIAL.search(account.shape(j)) and len(before) >= 2 and all(x.state == "done" for x in before):
            cat = CATEGORIES[3]
        elif r and r["verdict"] == "necessary" and k in landing and j.submitted >= landing[k]:
            p = by_id.get(r["prev"])
            changed = diffs.names(j.repo, p.commit, j.commit) if p else None
            same_scripts = p is not None and j.scripts == p.scripts
            if changed is not None and same_scripts and not declared(j, files.get(k, []), changed):
                cat = CATEGORIES[4]
        if cat:
            rows.append(
                {
                    "job": j.id,
                    "campaign": j.campaign,
                    "quest": j.quest,
                    "level": j.level,
                    "category": cat,
                    "ran": j.ran,
                    "run_s": j.run_s + j.build_s,
                    "wait_s": max(0.0, start - j.submitted),
                    "replaced_at": newer.get(j.id, 0.0),
                }
            )
    diffs.save()
    tally: dict[str, Counter[str]] = defaultdict(Counter)
    for x in rows:
        for key in (x["category"], f"{x['category']} | {x['level']}"):
            tally[key]["jobs"] += 1
            tally[key]["ran"] += int(x["ran"])
            tally[key]["run_s"] += x["run_s"]
            tally[key]["wait_s"] += x["wait_s"]
    summary: Json = {
        k: {
            "jobs": int(v["jobs"]),
            "ran": int(v["ran"]),
            "run_h": round(v["run_s"] / H, 1),
            "wait_job_h": round(v["wait_s"] / H, 1),
        }
        for k, v in sorted(tally.items())
    }
    summary["all"] = {
        "jobs": len(rows),
        "ran": sum(1 for x in rows if x["ran"]),
        "run_h": round(sum(x["run_s"] for x in rows) / H, 1),
        "wait_job_h": round(sum(x["wait_s"] for x in rows) / H, 1),
        "of_machine_h": round(sum(j.run_s + j.build_s for j in w.jobs if account.in_window(j)) / H, 1),
    }
    return summary, rows


def run(w: World, out: Path) -> Json:
    summary, rows = classify(w, out)
    (out / "need.json").write_text(json.dumps(summary, indent=1))
    (out / "need.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return summary
