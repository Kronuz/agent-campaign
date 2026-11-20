"""Markdown tables from the replay's JSON outputs, so each table in docs/v3 is reproducible:
`replay.py report NAME` prints the tables a document quotes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import sources as src
from sources import Json


def table(head: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(str(x) for x in r) + " |" for r in rows]
    return "\n".join(out)


def load(out: Path, name: str) -> Any:
    return src.read_json(out / name) or {}


def inventory(out: Path) -> str:
    inv: Json = load(out, "inventory.json")
    j = inv.get("jobs", {})
    parts = [
        f"Window {inv.get('window')}; timeline {inv.get('timeline', {}).get('events')} events by source {inv.get('timeline', {}).get('by_source')}."
    ]
    parts.append(
        table(
            [
                "jobs",
                "span",
                "count",
                "by state",
                "by level",
                "with rkey",
                "tagged",
                "with build",
                "reads.json",
                "inputs",
            ],
            [
                [
                    j.get("path"),
                    j.get("span"),
                    j.get("count"),
                    j.get("by_state"),
                    j.get("by_level"),
                    j.get("with_rkey"),
                    j.get("with_tags"),
                    j.get("with_build"),
                    j.get("with_reads"),
                    j.get("with_inputs"),
                ]
            ],
        )
    )
    parts.append(
        table(
            [
                "campaign",
                "state.json quests",
                "messages",
                "history commits",
                "history span",
                "journals",
                "journal commits",
                "usage runs",
                "usage span",
                "lead calls kept",
                "land refusals",
                "lead tokens in window",
                "prompts",
                "notices",
            ],
            [
                [
                    n,
                    s["quests"],
                    s["messages"],
                    s["history_commits"],
                    s["span"],
                    inv["journals"][n]["quests"],
                    inv["journals"][n]["commits"],
                    inv["usage"][n]["runs"],
                    inv["usage"][n]["span"],
                    inv["lead"][n]["commands"],
                    inv["lead"][n]["land_refusals"],
                    f"{inv['lead'][n]['tokens_in_window'] / 1e6:.0f}M",
                    inv["lead"][n]["prompts"],
                    inv["lead"][n]["notices"],
                ]
                for n, s in inv.get("state", {}).items()
            ],
        )
    )
    r = inv.get("repos", {})
    parts.append(
        f"Repositories with commits on main in the window: {r.get('count')}, {r.get('commits')} first-parent commits."
    )
    parts.append(
        table(["repo", "commits", "merges"], [[x["repo"], x["commits"], x["merges"]] for x in r.get("rows", [])[:15]])
    )
    return "\n\n".join(parts)


def account(out: Path) -> str:
    a: Json = load(out, "account.json")
    parts = []
    mt = a["machine_time"]
    rows = []
    for c, levels in mt.items():
        if not isinstance(levels, dict):
            continue
        for lv, states in levels.items():
            if isinstance(states, dict):
                rows.append(
                    [
                        c,
                        lv,
                        states.get("done", 0),
                        sum(v for k, v in states.items() if k in ("failed", "timeout", "interrupted")),
                        states.get("cancelled", 0),
                        states.get("(staged build)", 0),
                        round(sum(states.values()), 1),
                    ]
                )
    parts.append(
        "machine_time (hours)\n\n"
        + table(["campaign", "level", "done", "failed/timeout", "cancelled", "staged build", "total"], rows)
        + f"\n\nTotal {mt['total_h']} h."
    )
    parts.append(
        "waits (job-hours)\n\n"
        + table(
            ["level", "jobs", "ran", "queued", "in hold", "never ran", "median wait min", "p90 wait min"],
            [
                [
                    lv,
                    w["jobs"],
                    w["ran"],
                    w["queued_job_h"],
                    w["hold_job_h"],
                    w["never_ran_job_h"],
                    w["median_wait_min"],
                    w["p90_wait_min"],
                ]
                for lv, w in a["waits"].items()
            ],
        )
    )
    lc = a["light_capacity"]
    parts.append(
        f"light_capacity: {lc['slot_hours']} slot-hours, {lc['used_slot_hours']} used ({lc['utilization_pct']}%); exclusive drains {lc['exclusive_drain_wall_h']} h wall, exclusive runs {lc['exclusive_run_wall_h']} h wall.\n\n"
        + table(
            ["idle light slots while light work was ready, by what held the machine", "slot-hours"],
            [[k, v] for k, v in lc["idle_slot_hours_while_light_work_ready"].items()],
        )
    )
    rr = a["reruns"]
    parts.append(
        f"classify_reruns: {rr.get('first_runs')} first runs.\n\n"
        + table(
            ["verdict: why", "jobs", "ran", "run h", "wait job-h"],
            [
                [k, v["jobs"], v["ran"], v["run_h"], v["wait_job_h"]]
                for k, v in rr.items()
                if isinstance(v, dict) and "jobs" in v
            ],
        )
        + "\n\n"
        + table(["level", "run h by verdict"], [[lv, v] for lv, v in rr.get("by_level_run_h", {}).items()])
    )
    lr = a["land_refusals"]
    parts.append(
        f"land_refusals by reason: {lr['refusals_by_reason']}\n\n"
        + table(
            [
                "episodes",
                "n",
                "refusals",
                "landed",
                "h first refusal to land",
                "median h",
                "jobs",
                "job run h",
                "accepts",
                "commits refused",
            ],
            [
                [
                    k,
                    v["episodes"],
                    v["refusals"],
                    v["landed"],
                    v["wall_h_first_refusal_to_land"],
                    v["median_h_first_refusal_to_land"],
                    v["jobs_in_episodes"],
                    v["job_run_h_in_episodes"],
                    v["accepts_in_episodes"],
                    v["distinct_commits_refused"],
                ]
                for k, v in lr.items()
                if isinstance(v, dict) and "episodes" in v
            ],
        )
    )
    parts.append(
        "quest_hours\n\n"
        + table(
            ["what moved an in-flight quest", "h", "%"], [[k, v["h"], v["pct"]] for k, v in a["quest_hours"].items()]
        )
    )
    at = a["agent_tokens"]
    keys = list(next(iter(at.values())).keys()) if at else []
    parts.append("agent_tokens\n\n" + table(["measure", *at.keys()], [[k, *[at[c][k] for c in at]] for k in keys]))
    return "\n\n".join(parts)


def run(out: Path, name: str) -> str:
    return {"inventory": inventory, "account": account}[name](out)
