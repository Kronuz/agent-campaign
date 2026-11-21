"""The lead's to-do list: what nothing moves without, critical path first, each item with its exact
next command. `status`, `brief` and every return of `wait --commits` end with it."""

from __future__ import annotations

import os
import re

from .campaign import READY, Campaign, activity, critical_path, hunt_open, overview, park_met
from .core import Json, plural
from .journals import journal_text, state_of
from .repo import has_ref, rev
from .reviews import landing_gaps, task_gaps
from .worker import light_slots, light_weight

KINDS = ("merge", "land", "review", "finish", "answer", "resume", "start", "unpark", "slots")  # within a quest


def repeat_seconds() -> float:
    """How long an item the lead leaves undone waits before a wait fires it again."""
    return float(os.environ.get("CAMPAIGN_TODO_REPEAT", "1800"))


def item(kind: str, quest: str, text: str, cmd: str, key: str = "") -> Json:
    return {"key": key or f"{kind} {quest}", "kind": kind, "quest": quest, "text": text, "cmd": cmd}


def first_command(gaps: list[str], fallback: str) -> str:
    """The command the first gap names (`campaign ...`), else FALLBACK."""
    for gap in gaps:
        if m := re.search(r"`(campaign [^`]+)`", gap):
            return m.group(1)
    return fallback


def todo(c: Campaign, jobs: list[Json], gaps: dict[str, list[str]] | None = None) -> Json:
    """The list ("items", critical path first) and what the quests in flight are doing ("flight":
    worked on, waiting on a job, CI or a landing, needing the lead). GAPS are the landing gaps
    status already has, by quest; the others are asked here."""
    gaps = gaps or {}
    o = overview(c)
    doing = activity(c, jobs)
    items: list[Json] = [item("start", k, f"start {k}: launchable", f"campaign start {k}") for k in o["launchable"]]
    for k, p in c.quests.items():
        if met := park_met(c, p):
            back = "active" if p.get("branch") else "planned"
            items.append(item("unpark", k, f"unpark {k}: {met}", f"campaign set {k} status={back}"))
    for k, p in c.quests.items():
        repo = c.repo(p)
        if p["status"] not in ("active", "review") or not p.get("branch") or not has_ref(repo, p["branch"]):
            continue
        for task, t in sorted(p.get("tasks", {}).items()):
            name = f"{k}--{task}"
            if t.get("merged") or t.get("status") == "dropped":
                continue
            if not task_gaps(c, k, task, jobs):
                merge = f"campaign task merge {k} {task}"
                items.append(item("merge", k, f"merge {k}'s task {task}: its jobs passed", merge, f"merge {name}"))
            elif doing.get(name, ("",))[0] == "lead":
                why = doing[name][1]
                items.append(
                    item("resume", k, f"resume {k}'s task {task}: {why}", f"campaign status {k}", f"resume {name}")
                )
        missing = gaps[k] if k in gaps else landing_gaps(c, k, rev(repo, p["branch"]), final=True, ci=True, jobs=jobs)
        state = state_of(journal_text(c, k, p["branch"]))
        if not missing:
            items.append(item("land", k, f"land {k}: nothing keeps it from landing", f"campaign land {k}"))
        elif p["status"] == "active" and READY.match(state) and p["lenses"]:
            lenses = " and ".join(p["lenses"])
            text = f"review {k}: its journal says {state}; then launch its {lenses} review"
            items.append(item("review", k, text, f"campaign set {k} status=review"))
        elif doing.get(k, ("",))[0] == "lead":
            why = doing[k][1]
            if READY.match(state):
                items.append(
                    item(
                        "finish",
                        k,
                        f"finish {k}: {why}; to land: {missing[0]}",
                        first_command(missing, f"campaign status {k}"),
                    )
                )
            elif why.startswith("its journal waits on the lead"):
                items.append(item("answer", k, f"answer {k}: {why}", f"campaign status {k}"))
            else:
                items.append(item("resume", k, f"resume {k}: {why}", f"campaign status {k}"))
    if slots := idle_slots(jobs):
        free, total = slots
        if hunt := hunt_open(c):
            text = (
                f"fill {free} of {plural(total, 'light slot')} idle with {plural(hunt, 'hunt row')} open: an "
                "exploration batch (references/assignments/exploration.md), or --priority -1 work"
            )
            items.append(item("slots", "", text, "campaign new ID --title T --hunt WORDS", "slots"))
    order = critical_path(c)
    items.sort(key=lambda x: (*order(x["quest"]), KINDS.index(x["kind"])) if x["quest"] else (1 << 30, 0, 0, 0))
    flight = {
        s: sorted(k for k, (x, _) in doing.items() if x == s and "--" not in k) for s in ("working", "waiting", "lead")
    }
    return {"items": items, "flight": flight}


def idle_slots(jobs: list[Json]) -> tuple[int, int] | None:
    """(free, all) light slots when at least half idle and nothing is queued or draining the machine."""
    if any(j["state"].get("state") == "queued" for j in jobs):
        return None
    running = [j for j in jobs if j["state"].get("state") == "running"]
    if any(j["spec"].get("level") == "exclusive" for j in running):
        return None
    total = light_slots()
    used = sum(light_weight(j, {}) for j in running if j["spec"].get("level", "light") == "light")
    free = total - used
    return (free, total) if free >= max(1, total // 2) else None


def todo_lines(t: Json) -> list[str]:
    """The list as status, brief and wait print it; "wait again after this" only when it is empty."""
    f = t["flight"]
    counts = [f"{len(f['working'])} worked on", f"{len(f['waiting'])} waiting on jobs, CI or a landing"]
    counts.append(f"{len(f['lead'])} needing the lead")
    head = f"in flight: {', '.join(counts)}"
    if not t["items"]:
        return [f"{head}; nothing for the lead to do: wait again after this"]
    lines = [f"to do, critical path first ({head}):"]
    lines += [f"- {x['text']}: `{x['cmd']}`" for x in t["items"]]
    return lines
