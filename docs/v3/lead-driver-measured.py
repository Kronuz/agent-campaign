"""Quest-hours with nothing moving them, per day, in xapiand and detent, before and after lead-driver.

The replay's `quest_hours` query (agent-campaign quest/xapiand/v2-replay, tools/replay/account.py),
applied to two windows. Each in-flight quest-hour (status active, review or blocked) takes the first
label that holds: an agent run of the quest (`campaign usage`); a job of it running; a job of it
queued or in its hold; an `after` dependency not landed; else "nothing moving it", by status.

Inputs, read-only: each home's state.json at every commit of it (`git cat-file --batch`), the job
store (~/.scratch/campaign/jobs), and `campaign usage --json` of each home (agent runs from the
Claude Code transcripts under ~/.claude/projects). Writes only under --out.

    python3 docs/v3/lead-driver-measured.py [--out DIR] [--usage-dir DIR]
"""

from __future__ import annotations

import argparse
import bisect
import itertools
import json
import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

Json = dict[str, Any]
H = 3600.0
DAY = 86400.0
HOME = Path.home()
JOBS = HOME / ".scratch" / "campaign" / "jobs"
CLAUDE = HOME / ".claude" / "projects"
HOMES = {"detent": HOME / "code/Kronuz/Detent", "xapiand": HOME / "code/Kronuz/Xapiand"}
OTHER = {"campaign-v3": HOME / "code/Kronuz/campaign-v3"}  # resolves cross-campaign `after` entries

# Installs, from the dotfiles commits that bumped the campaign skill.
LEAD_DRIVER = datetime.fromisoformat("2026-11-21T21:22:54-08:00").timestamp()  # dotfiles 08a762a
TODO_MAIL = datetime.fromisoformat("2026-11-24T03:24:03-08:00").timestamp()  # dotfiles aff9241
WINDOWS = {
    "before": (LEAD_DRIVER - 3 * DAY, LEAD_DRIVER),
    "after": (LEAD_DRIVER, TODO_MAIL),
}
SINCE, UNTIL = WINDOWS["before"][0], WINDOWS["after"][1]

FLIGHT = ("active", "review", "blocked")
LABELS = (
    "agent turn",
    "job running",
    "job waiting for the machine",
    "waiting on a dependency",
)
NOTHING = 99
VARIANTS = {
    "spans": "agent runs from campaign usage and the launch-prompt match, each its first to last line (headline)",
    "usage": "agent runs from campaign usage only, each its span (the method's literal input)",
    "turns": "agent runs from both, each its transcript's activity (line gaps up to 5 min)",
}
GAP = 300.0  # the transcript-activity variant: a gap between an agent's lines up to this is a turn


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(errors="replace"))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- state.json at every commit


def snapshots(home: Path, name: str) -> list[tuple[float, dict[str, Json]]]:
    """(commit time, {quest: {status, after}}) for the last commit before SINCE and every one to UNTIL."""
    rel = f".campaign/{name}/state.json"
    log = subprocess.run(
        ["git", "-C", str(home), "log", "--format=%H %ct", "--", rel],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\n")
    commits = [(sha, float(t)) for sha, t in (line.split() for line in log if line)]
    keep = [c for c in commits if SINCE <= c[1] <= UNTIL]
    keep += [c for c in commits if c[1] < SINCE][:1]  # the state in force at SINCE, if any
    keep.sort(key=lambda c: c[1])
    batch = subprocess.Popen(
        ["git", "-C", str(home), "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    assert batch.stdin and batch.stdout
    out: list[tuple[float, dict[str, Json]]] = [(0.0, {})]  # before its first commit: no quests
    for sha, t in keep:
        batch.stdin.write(f"{sha}:{rel}\n".encode())
        batch.stdin.flush()
        header = batch.stdout.readline().split()
        size = int(header[2])
        body = batch.stdout.read(size + 1)[:size]
        quests = json.loads(body).get("quests", {})
        out.append(
            (
                t,
                {
                    q: {
                        "status": v.get("status", ""),
                        "after": list(v.get("after") or []),
                    }
                    for q, v in quests.items()
                },
            )
        )
    batch.stdin.close()
    batch.wait()
    return out


def at(snaps: list[tuple[float, dict[str, Json]]], t: float) -> dict[str, Json]:
    """The snapshot in force at T (the newest commit at or before it)."""
    i = bisect.bisect_right([ts for ts, _ in snaps], t) - 1
    return snaps[max(i, 0)][1]


# ---------------------------------------------------------------- what moved each quest


def job_intervals() -> dict[tuple[str, str], list[tuple[float, float, int]]]:
    """(campaign, quest) -> [(a, b, label)]: running began..ended, waiting submitted..began (or end).
    A task's job (spec `task`) carries its quest's id, so it counts for its quest."""
    out: dict[tuple[str, str], list[tuple[float, float, int]]] = defaultdict(list)
    for d in JOBS.iterdir():
        spec = read_json(d / "spec.json") if d.is_dir() else None
        if not isinstance(spec, dict):
            continue
        st = read_json(d / "state.json") or {}
        sub = float(spec.get("submitted") or 0)
        if not sub or sub > UNTIL:
            continue
        began, ended = float(st.get("began") or 0), float(st.get("ended") or 0)
        end = ended or UNTIL
        if end < SINCE:
            continue
        key = (Path(spec.get("campaign") or "").name, spec.get("quest") or "")
        if began:
            out[key].append((began, end, 1))
        out[key].append((sub, began or end, 2))
    return out


@dataclass
class Run:
    campaign: str
    quest: str
    began: float
    ended: float
    source: str  # "usage" (campaign usage) or "prompt" (this replay's match of the launch prompt)
    log: Path | None


def read_log(path: Path) -> tuple[str, list[float]]:
    """An agent transcript's first prompt and its line times."""
    prompt, ts = "", []
    with path.open(errors="replace") as f:
        for line in f:
            i = line.find('"timestamp":"')
            if i >= 0:
                t = stamp(line[i + 13 : line.find('"', i + 13)])
                if t:
                    ts.append(t)
            if not prompt and '"type":"user"' in line:
                try:
                    body = (json.loads(line).get("message") or {}).get("content")
                except ValueError:
                    continue
                if isinstance(body, list):
                    body = " ".join(str(b.get("text", "")) for b in body if isinstance(b, dict))
                prompt = str(body or "")
    return prompt, sorted(ts)


QUEST_WORD = re.compile(r"\bquests?\W{1,3}([a-z0-9][a-z0-9-]*[a-z0-9])")


def attribute(prompt: str, ids: dict[str, set[str]]) -> tuple[str, str] | None:
    """(campaign, quest) a launch prompt names: a campaign named as a word, and the first known id
    after the word "quest", else the earliest known id as a whole word (the longer at one place)."""
    found: list[tuple[int, int, str, str]] = []
    for c, qs in ids.items():
        if not re.search(rf"\b{c}\b", prompt, re.IGNORECASE):
            continue
        for m in QUEST_WORD.finditer(prompt):
            q = m[1].split("--", 1)[0]
            if q in qs:
                found.append((m.start(), 0, c, q))
                break
        for q in qs:
            m = re.search(rf"(?<![\w-]){re.escape(q)}(?![\w-])", prompt)
            if m:
                found.append((m.start() + len(prompt), -len(q), c, q))
    return (min(found)[2], min(found)[3]) if found else None


def agent_runs(usage: dict[str, Json], ids: dict[str, set[str]]) -> tuple[list[Run], Json]:
    """Every agent run of a replayed campaign's quest touching the windows: `campaign usage`'s, then
    each Claude Code agent transcript usage leaves unattributed, matched by its launch prompt."""
    runs: list[Run] = []
    known: set[str] = set()
    for c, u in usage.items():
        for r in u.get("runs", []):
            known.add(str(r.get("agent")))
            q = str(r.get("quest") or "").split("--", 1)[0]
            a, b = stamp(r.get("began", "")), stamp(r.get("ended", ""))
            if q in ids[c] and a and b > a and b >= SINCE and a <= UNTIL:
                log = next(
                    iter(CLAUDE.glob(f"*/{r.get('session')}/subagents/agent-{r.get('agent')}.jsonl")),
                    None,
                )
                runs.append(Run(c, q, a, b, "usage", log))
    skipped = 0
    for log in CLAUDE.glob("*/*/subagents/agent-*.jsonl"):
        if log.stem.removeprefix("agent-") in known or log.stat().st_mtime < SINCE:
            continue
        prompt, ts = read_log(log)
        who = attribute(prompt, ids)
        if not ts or ts[-1] < SINCE or ts[0] > UNTIL:
            continue
        if not who:
            skipped += 1
            continue
        runs.append(Run(who[0], who[1], ts[0], ts[-1], "prompt", log))
    counts: Json = defaultdict(int)
    for r in runs:
        counts[f"{r.campaign} {r.source}"] += 1
    counts["unattributed transcripts"] = skipped
    return runs, dict(counts)


def agent_intervals(runs: list[Run], variant: str) -> dict[tuple[str, str], list[tuple[float, float, int]]]:
    """(campaign, quest) -> agent intervals: "usage" (campaign usage's runs, their span: the method's
    input), "spans" (every attributed run's span) or "turns" (every run's transcript activity, gaps
    between its lines of at most GAP)."""
    out: dict[tuple[str, str], list[tuple[float, float, int]]] = defaultdict(list)
    for r in runs:
        if variant == "usage" and r.source != "usage":
            continue
        if variant == "turns" and r.log:
            ts = [t for t in read_log(r.log)[1] if SINCE - DAY <= t <= UNTIL]
            out[(r.campaign, r.quest)] += [(a, b, 0) for a, b in itertools.pairwise(ts) if b - a <= GAP]
        else:
            out[(r.campaign, r.quest)].append((r.began, r.ended, 0))
    return out


def segments(a: float, b: float, labelled: list[tuple[float, float, int]]) -> list[tuple[float, float, int]]:
    """Splits a..b at every boundary; each piece takes the lowest label covering it (NOTHING: none)."""
    near = [x for x in labelled if x[0] < b and x[1] > a]
    cuts = sorted({a, b, *(x for s, e, _ in near for x in (s, e) if a < x < b)})
    out = []
    for s, e in itertools.pairwise(cuts):
        mid = (s + e) / 2
        out.append((s, e, min((lb for x, y, lb in near if x <= mid < y), default=NOTHING)))
    return out


# ---------------------------------------------------------------- the account


def day_of(t: float) -> str:
    """The install-aligned day holding T: B-3..B-1 before lead-driver's install, A+1.. after it."""
    n = int((t - LEAD_DRIVER) // DAY)
    return f"B{n}" if n < 0 else f"A+{n + 1}"


def account(
    snaps: dict[str, list[tuple[float, dict[str, Json]]]],
    jobs: dict[tuple[str, str], list[tuple[float, float, int]]],
    agents: dict[tuple[str, str], list[tuple[float, float, int]]],
) -> Json:
    # hours[campaign][day][label or "nothing (status)"]
    hours: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    per_quest: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    unknown_deps: set[str] = set()
    idle: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    for c in HOMES:
        times = [t for t, _ in snaps[c] if SINCE < t < UNTIL]
        cuts = sorted(
            {
                SINCE,
                UNTIL,
                *times,
                *(LEAD_DRIVER + k * DAY for k in range(-3, 4) if SINCE < LEAD_DRIVER + k * DAY < UNTIL),
            }
        )
        for a0, b0 in itertools.pairwise(cuts):
            mine = at(snaps[c], a0)
            for q, rec in mine.items():
                status = rec["status"]
                if status not in FLIGHT:
                    continue
                labelled = agents.get((c, q), []) + jobs.get((c, q), [])
                if not all(met(c, d, a0, snaps, unknown_deps) for d in rec["after"]):
                    labelled = [*labelled, (a0, b0, 3)]
                for a, b, lab in segments(a0, b0, labelled):
                    name = LABELS[lab] if lab < NOTHING else f"nothing moving it ({status})"
                    hours[c][day_of(a)][name] += (b - a) / H
                    hours[c][day_of(a)][f"in flight ({status})"] += (b - a) / H
                    if lab == NOTHING:
                        win = "before" if a < LEAD_DRIVER else "after"
                        per_quest[c][q][f"{win} {status}"] += (b - a) / H
                        if status != "blocked":
                            idle[(c, q)].append((a, b))
    return {
        "hours": {c: {d: dict(v) for d, v in sorted(days.items())} for c, days in hours.items()},
        "per_quest": {c: {q: dict(v) for q, v in qs.items()} for c, qs in per_quest.items()},
        "unknown_deps": sorted(unknown_deps),
        "stretches": stretches(idle),
    }


def stretches(idle: dict[tuple[str, str], list[tuple[float, float]]]) -> Json:
    """Each quest's contiguous nothing-moving time (active or review), clipped to each window: how
    many stretches, their hours, the hours in stretches of at least 1 h and 4 h, and the longest."""
    out: Json = {}
    for c in HOMES:
        merged: list[tuple[float, float]] = []
        for (cc, _), ivs in idle.items():
            if cc != c:
                continue
            run: list[list[float]] = []
            for a, b in sorted(ivs):
                if run and a - run[-1][1] < 1.0:
                    run[-1][1] = max(run[-1][1], b)
                else:
                    run.append([a, b])
            merged += [(a, b) for a, b in run]
        for win, (w0, w1) in WINDOWS.items():
            parts = [min(b, w1) - max(a, w0) for a, b in merged if min(b, w1) > max(a, w0)]
            days = (w1 - w0) / DAY
            out[f"{c} {win}"] = {
                "stretches": len(parts),
                "h_per_24h": sum(parts) / H / days,
                "h_in_1h_plus_per_24h": sum(x for x in parts if x >= H) / H / days,
                "h_in_4h_plus_per_24h": sum(x for x in parts if x >= 4 * H) / H / days,
                "longest_h": max(parts, default=0.0) / H,
                "median_min": sorted(parts)[len(parts) // 2] / 60 if parts else 0.0,
            }
    return out


def met(
    c: str,
    dep: str,
    t: float,
    snaps: dict[str, list[tuple[float, dict[str, Json]]]],
    unknown: set[str],
) -> bool:
    """An `after` entry holds nothing back at T: its quest landed or was dropped (v3's `met`)."""
    name, _, q = dep.rpartition(":")
    name = name or c
    if name not in snaps or q not in at(snaps[name], t):
        unknown.add(f"{c} -> {dep}")
        return False
    return at(snaps[name], t)[q]["status"] in ("landed", "dropped")


def load_usage(usage_dir: Path) -> dict[str, Json]:
    """`campaign usage --json` of each home, cached in USAGE_DIR so a rerun reads the same runs."""
    out: dict[str, Json] = {}
    for c, home in HOMES.items():
        cache = usage_dir / f"usage-{c}.json"
        if not cache.exists() or cache.stat().st_size == 0:
            res = subprocess.run(
                ["campaign", "usage", "--json"],
                cwd=home,
                capture_output=True,
                text=True,
                check=True,
            )
            usage_dir.mkdir(parents=True, exist_ok=True)
            cache.write_text(res.stdout)
        out[c] = read_json(cache) or {"runs": []}
    return out


def table(acc: Json) -> str:
    """Per campaign and status: nothing-moving hours by day, each window's total and rate per 24 h."""
    days = ["B-3", "B-2", "B-1", "A+1", "A+2", "A+3"]
    lengths = {d: DAY / H for d in days}
    lengths["A+3"] = (TODO_MAIL - (LEAD_DRIVER + 2 * DAY)) / H
    rows = [
        "| Campaign | Hours | " + " | ".join(days) + " | Before /24 h | After /24 h |",
        "|---" * 10 + "|",
    ]
    for c in HOMES:
        h = acc["hours"][c]
        for status in FLIGHT:
            for kind in (f"nothing moving it ({status})", f"in flight ({status})"):
                vals = [h.get(d, {}).get(kind, 0.0) for d in days]
                before = sum(vals[:3]) / 3
                after = sum(vals[3:]) / ((TODO_MAIL - LEAD_DRIVER) / DAY)
                label = kind.replace("nothing moving it", "nothing moving").replace("in flight", "in flight")
                rows.append(
                    f"| {c} | {label} | " + " | ".join(f"{v:.1f}" for v in vals) + f" | {before:.1f} | {after:.1f} |"
                )
    rows.append(
        f"\nA+3 is {lengths['A+3']:.2f} h long; the after rate divides by {(TODO_MAIL - LEAD_DRIVER) / DAY:.3f} days."
    )
    return "\n".join(rows)


def summary(acc: Json) -> str:
    """Per window, active and review together (what the lead moves): nothing-moving hours per 24 h,
    in-flight hours per 24 h, their share, the range over whole days, and nothing-moving counting
    a quest waiting only on an unlanded `after` as unmoved too."""
    full = {"before": ["B-3", "B-2", "B-1"], "after": ["A+1", "A+2"]}
    every = {"before": full["before"], "after": [*full["after"], "A+3"]}
    rows = [
        "| Campaign | Window | Nothing moving /24 h | In flight /24 h | Share | Whole days, min-max | With `after` waits /24 h |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in [*HOMES, "both"]:
        cs = list(HOMES) if c == "both" else [c]
        for win, (w0, w1) in WINDOWS.items():
            days = (w1 - w0) / DAY

            def get(d: str, kind: str, cs: list[str] = cs) -> float:
                return sum(
                    acc["hours"][x].get(d, {}).get(f"{kind} ({st})", 0.0) for x in cs for st in ("active", "review")
                )

            def dep(d: str, cs: list[str] = cs) -> float:
                return sum(acc["hours"][x].get(d, {}).get("waiting on a dependency", 0.0) for x in cs)

            idle = sum(get(d, "nothing moving it") for d in every[win]) / days
            flight = sum(get(d, "in flight") for d in every[win]) / days
            whole = [get(d, "nothing moving it") for d in full[win]]
            withdep = sum(get(d, "nothing moving it") + dep(d) for d in every[win]) / days
            rows.append(
                f"| {c} | {win} | {idle:.1f} | {flight:.1f} | {100 * idle / flight:.1f}% | "
                f"{min(whole):.1f}-{max(whole):.1f} | {withdep:.1f} |"
            )
    return "\n".join(rows)


def lead_hours(usage: dict[str, Json]) -> Json:
    """Each lead's active hours per install-aligned day: gaps between its transcripts' main-thread
    lines, each capped at GAP (the replay's "lead active hours", query agent_tokens)."""
    out: Json = {}
    for c, u in usage.items():
        ts: list[float] = []
        for sess in u.get("lead", []):
            if stamp(sess.get("ended", "")) < SINCE:
                continue
            for log in CLAUDE.glob(f"*/{sess.get('session')}.jsonl"):
                with log.open(errors="replace") as f:
                    for line in f:
                        i = line.find('"timestamp":"')
                        if i < 0 or '"isSidechain":true' in line:
                            continue
                        t = stamp(line[i + 13 : line.find('"', i + 13)])
                        if SINCE <= t < UNTIL:
                            ts.append(t)
        ts.sort()
        days: dict[str, float] = defaultdict(float)
        for a, b in itertools.pairwise(ts):
            days[day_of(a)] += min(b - a, GAP) / H
        out[c] = dict(sorted(days.items()))
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--out",
        type=Path,
        default=HOME / ".scratch/campaign-v3/lead-driver-measured/out",
    )
    p.add_argument(
        "--usage-dir",
        type=Path,
        default=None,
        help="where usage-<campaign>.json is cached (default --out)",
    )
    a = p.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    usage = load_usage(a.usage_dir or a.out)
    snaps = {n: snapshots(h, n) for n, h in {**HOMES, **OTHER}.items()}
    ids = {c: {q for _, s in snaps[c] for q in s} for c in HOMES}
    runs, counts = agent_runs(usage, ids)
    jobs = job_intervals()
    result: Json = {
        "windows": {k: [iso(x), iso(y)] for k, (x, y) in WINDOWS.items()},
        "runs": counts,
        "snapshots": {n: len(v) for n, v in snaps.items()},
    }
    result["lead_active_h"] = lead_hours(usage)
    for variant in VARIANTS:
        result[variant] = account(snaps, jobs, agent_intervals(runs, variant))
    (a.out / "measure.json").write_text(json.dumps(result, indent=1, sort_keys=True))
    print(
        "windows:",
        result["windows"],
        "\nagent runs:",
        counts,
        "\nsnapshots:",
        result["snapshots"],
        "\n",
    )
    print(
        "lead active hours by day:",
        {c: {d: round(h, 1) for d, h in v.items()} for c, v in result["lead_active_h"].items()},
        "\n",
    )
    for variant, what in VARIANTS.items():
        print(f"## {variant}: {what}\n\n{table(result[variant])}\n")
        print(summary(result[variant]) + "\n")
        print("unknown deps:", result[variant]["unknown_deps"], "\n")
        print("| Stretch | " + " | ".join(next(iter(result[variant]["stretches"].values()))) + " |")
        for k, v in result[variant]["stretches"].items():
            print(f"| {k} | " + " | ".join(f"{x:.1f}" if isinstance(x, float) else str(x) for x in v.values()) + " |")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
