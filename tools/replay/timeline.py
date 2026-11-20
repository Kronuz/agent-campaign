"""All of v2's records as one event timeline, and the inventory of its sources."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sources as src
from sources import Json


@dataclass
class World:
    """Everything the replay loads, once."""

    homes: dict[str, Path]
    jobs: list[src.Job]
    states: dict[str, Json]
    history: dict[str, list[src.StateEvent]]
    journals: dict[str, list[src.JournalLog]]
    usage: dict[str, Json]
    lead: dict[str, list[src.LeadCommand]]
    activity: dict[str, list[float]]
    efficiency: dict[str, Json]
    repos: list[src.RepoLog]
    quiet: Json


def load(out: Path = src.OUT, refresh: bool = False, repos: bool = True) -> World:
    homes = src.campaign_homes()
    lead: dict[str, list[src.LeadCommand]] = {}
    activity: dict[str, list[float]] = {}
    usage: dict[str, Json] = {}
    for name, home in homes.items():
        usage[name] = src.usage(home, name, out, refresh)
        sessions = sorted(usage[name].get("lead", []), key=lambda r: -float(r.get("tokens") or 0))
        log = src.lead_transcript(sessions[0]["session"]) if sessions else None
        lead[name], activity[name] = src.lead_commands(log, name, out, refresh) if log else ([], [])
    return World(
        homes=homes,
        jobs=src.load_jobs(),
        states={n: src.load_state(h, n) for n, h in homes.items()},
        history={n: src.state_history(h, n) for n, h in homes.items()},
        journals={n: src.journal_logs(h, n) for n, h in homes.items()},
        usage=usage,
        lead=lead,
        activity=activity,
        efficiency={n: src.efficiency_notes(h / ".campaign" / n / "efficiency.md") for n, h in homes.items()},
        repos=src.repo_logs() if repos else [],
        quiet=src.quiet_holds(),
    )


def events(w: World) -> list[Json]:
    """One time-ordered list: (t, campaign, quest, source, kind, detail)."""
    ev: list[Json] = []

    def add(t: float, campaign: str, quest: str, source: str, kind: str, **detail: Any) -> None:
        if t:
            ev.append(
                {"t": round(t, 3), "campaign": campaign, "quest": quest, "source": source, "kind": kind, **detail}
            )

    for j in w.jobs:
        base = {"job": j.id, "level": j.level}
        add(j.submitted, j.campaign, j.quest, "job", "submit", **base, what=j.what[:80])
        add(j.claimed, j.campaign, j.quest, "job", "claim", **base)
        add(j.build_started, j.campaign, j.quest, "job", "build", **base)
        add(j.began, j.campaign, j.quest, "job", "begin", **base)
        add(j.ended, j.campaign, j.quest, "job", "end", **base, state=j.state, exit=j.exit)
    for name, hist in w.history.items():
        for e in hist:
            add(e.t, name, e.quest, "state", e.verb, sha=e.sha[:12], args=e.args[:120])
    for name, logs in w.journals.items():
        for jl in logs:
            for t in jl.commits:
                add(t, name, jl.quest, "journal", "commit")
    for name, u in w.usage.items():
        for r in u.get("runs", []):
            q = r.get("quest", "")
            add(
                src.stamp(r.get("began", "")),
                name,
                q,
                "agent",
                "run-begin",
                role=r.get("role", ""),
                agent=r.get("agent", ""),
            )
            add(src.stamp(r.get("ended", "")), name, q, "agent", "run-end", tokens=r.get("tokens", 0))
    for name, cmds in w.lead.items():
        for c in cmds:
            add(c.t, name, c.quest, "lead", c.verb, refused="cannot land yet" in c.output)
    ev.sort(key=lambda e: (e["t"], e["source"], e["kind"]))
    return ev


NEVER = {
    "jobs": [
        "why the job was submitted (which criterion, retry, rerun of what): only `what` text, `for.json` tags for some",
        "read set: reads.json is absent from every job (no strace on macOS)",
        "declared inputs: no spec carries `inputs`",
        "the submitter (agent or lead, which turn)",
        "split of claimed-to-began between source preparation, build and quiet's wait (except staged builds)",
        "CPU time actually used (only wall clock; `cpus` on few specs)",
        "pruned jobs: older than 14 days are deleted (prune.log names them)",
    ],
    "state": [
        "land refusals (a refused land writes nothing)",
        "status at a time: only commit subjects, not the fields that changed (the files diff, not parsed here)",
        "dependency edges over time: `after`/`needs` are current values only",
        "who issued each command",
    ],
    "journals": ["agent turns: a journal commit is a proxy, not a turn record"],
    "usage": [
        "per-turn outcome (what the turn achieved); only totals per run",
        "lead tokens per quest: the lead is one long session",
        "cost: harnesses report none (cost_available false)",
    ],
    "lead": [
        "commands typed in other sessions or by the owner",
        "the lead's reason for each action and its delay cause",
    ],
    "quiet": ["any history: only live holds and pills; waits are inferred from job states"],
    "efficiency": ["free-text ledger; hourly queue notes only from 2026-11-17"],
    "git": ["CI runs and their durations (GitHub, not recorded locally)"],
}


def inventory(w: World) -> Json:
    def span(ts: list[float]) -> list[str]:
        ts = [t for t in ts if t]
        return [src.iso(min(ts)), src.iso(max(ts))] if ts else ["-", "-"]

    by_state = Counter(j.state for j in w.jobs)
    by_level = Counter(j.level for j in w.jobs)
    by_campaign = Counter(j.campaign for j in w.jobs)
    inv: Json = {
        "window": [src.iso(src.SINCE), src.iso(src.UNTIL)],
        "jobs": {
            "path": str(src.JOBS),
            "span": span([j.submitted for j in w.jobs]),
            "count": len(w.jobs),
            "by_state": dict(by_state),
            "by_level": dict(by_level),
            "by_campaign": dict(by_campaign),
            "with_rkey": sum(1 for j in w.jobs if j.rkey),
            "with_tags": sum(1 for j in w.jobs if j.tags),
            "with_build": sum(1 for j in w.jobs if j.build),
            "with_reads": sum(1 for j in w.jobs if j.reads),
            "with_inputs": sum(1 for j in w.jobs if j.inputs),
            "fails_without": sum(1 for j in w.jobs if j.fails_without),
        },
        "state": {
            n: {
                "path": str(w.homes[n] / ".campaign" / n / "state.json"),
                "quests": len(w.states[n].get("quests", {})),
                "messages": len(w.states[n].get("messages", [])),
                "history_commits": len(w.history[n]),
                "span": span([e.t for e in w.history[n]]),
                "verbs": dict(Counter(e.verb for e in w.history[n])),
            }
            for n in w.homes
        },
        "journals": {
            n: {"quests": len(w.journals[n]), "commits": sum(len(j.commits) for j in w.journals[n])} for n in w.homes
        },
        "usage": {
            n: {
                "runs": len(w.usage[n].get("runs", [])),
                "span": span([src.stamp(r.get("began", "")) for r in w.usage[n].get("runs", [])]),
                "harnesses": dict(Counter(r.get("harness", "") for r in w.usage[n].get("runs", []))),
                "lead_sessions": len(w.usage[n].get("lead", [])),
            }
            for n in w.homes
        },
        "lead": {
            n: {
                "commands": len(w.lead[n]),
                "span": span([c.t for c in w.lead[n]]),
                "verbs": dict(Counter(c.verb for c in w.lead[n])),
                "land_refusals": sum(1 for c in w.lead[n] if c.verb == "land" and "cannot land yet" in c.output),
                "activity_lines": len(w.activity[n]),
            }
            for n in w.homes
        },
        "efficiency": {n: {k: v for k, v in w.efficiency[n].items() if k != "queues"} for n in w.homes},
        "repos": {
            "count": len(w.repos),
            "commits": sum(r.commits for r in w.repos),
            "rows": [r.__dict__ for r in sorted(w.repos, key=lambda r: -r.commits)],
        },
        "quiet": w.quiet,
        "never_recorded": NEVER,
    }
    return inv


def write(out: Path, w: World) -> Json:
    out.mkdir(parents=True, exist_ok=True)
    ev = events(w)
    with (out / "timeline.jsonl").open("w") as f:
        for e in ev:
            f.write(json.dumps(e) + "\n")
    inv = inventory(w)
    inv["timeline"] = {"path": str(out / "timeline.jsonl"), "events": len(ev), "by_source": {}}
    inv["timeline"]["by_source"] = dict(Counter(e["source"] for e in ev))
    (out / "inventory.json").write_text(json.dumps(inv, indent=1, default=str))
    return inv
