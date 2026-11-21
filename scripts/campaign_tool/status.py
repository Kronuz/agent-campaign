"""Status and brief: the campaign as the lead and agents read it."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

from style import mark, paint, styled

from .campaign import Campaign, find_campaign, in_flight, overview, sidebar, unattended
from .ci import warm_views
from .core import FLIGHT, STATUSES, Json, git, plural
from .jobs import all_jobs
from .journals import id_ranges
from .mail import inbox
from .procs import worker_alive
from .repo import frozen, warm_repos
from .runner import reap
from .state import branches, campaigns, most_waited_on
from .views import STAGES, problems, quest_view
from .worker import cpu_lines, health_lines, queue_health


def machine_status() -> int:
    for name, o in campaigns().items():
        quests = o["state"].get("quests", {})
        counts = {s: sum(1 for q in quests.values() if q.get("status") == s) for s in STATUSES}
        flying = [q for q, x in quests.items() if x.get("status") in FLIGHT]
        print(
            f"{name}  {o['home']}: "
            + (", ".join(f"{n} {s}" for s, n in counts.items() if n) or "no quests")
            + (f"; in flight: {', '.join(flying)}" if flying else "")
        )
    return 0


JOB_MARKS = {
    "done": ("green", "✅"),
    "failed": ("yellow", "❌"),
    "timeout": ("yellow", "⏰"),
    "interrupted": ("yellow", "⚡"),
    "cancelled": ("dim", "🚫"),
    "queued": ("dim", "⏳"),
    "running": ("cyan", "🔄"),
}


def bar(done: float, width: int = 20) -> str:
    """A progress bar of CAMPAIGN_BAR's two characters (default block characters every terminal font
    has); "#-" when the output cannot encode them."""
    done = max(0.0, min(done, 1.0))
    chars = os.environ.get("CAMPAIGN_BAR", "")
    full, empty = (chars[0], chars[1]) if len(chars) == 2 else ("\u2588", "\u2591")
    try:
        (full + empty).encode(sys.stdout.encoding or "ascii")
    except (UnicodeEncodeError, LookupError):
        full, empty = "#", "-"
    n = round(done * width)
    return paint(full * n, "green") + paint(empty * (width - n), "dim") + f" {round(done * 100)}%"


def print_criteria(pid: str, detail: list[Json]) -> None:
    """Each criterion: met, pending (listed without evidence) or unrecorded, with the rows behind it."""
    width = shutil.get_terminal_size((100, 24)).columns

    def fit(text: str, indent: int) -> str:
        return text if indent + len(text) <= width else text[: max(width - indent - 3, 10)] + "..."

    print(f"  {mark('📜')}criteria:")
    pad = max(len(d["id"]) for d in detail)
    lead = "   " if styled() else ""  # under a mark
    for d in detail:
        state = "met" if any(r["met"] for r in d["rows"]) else "pending" if d["rows"] else "no row"
        style, emoji = {"met": ("green", "✅"), "pending": ("yellow", "⏳")}.get(state, ("dim", "⬜"))
        indent = len(lead) + pad + 15
        text = d["text"] if d.get("kind", "job") == "job" else f"[{d['kind']}] {d['text']}"
        print(f"    {mark(emoji)}{d['id']:<{pad}}  {paint(f'{state:<7}', style)}  {fit(text, indent)}")
        for r in d["rows"]:
            where = "journal" if r["journal"] == pid else r["journal"].split("--", 1)[-1]
            line = f"{where}: {r['evidence']}" + (f" ({'; '.join(r['why'])})" if r.get("why") else "")
            print(f"    {lead}{'':<{pad}}  {'':<7}  {fit(line, indent)}")


def task_mark(state: str) -> str:
    first = state.split(maxsplit=1)[0].rstrip(";:,.").lower() if state.strip() else ""
    marks = {
        "complete": "✅",
        "done": "✅",
        "dropped": "🚫",
        "parked": "💤",
        "waiting": "⏳",
        "blocked": "🚧",
    }
    return marks.get(first, "🔨")


def print_quest(v: Json, full: bool) -> None:
    color, emoji = STAGES.get(v["status"], ("dim", "❔"))
    print(f"\n{mark(emoji)}{paint(v['id'], 'bold')}  {paint(v['status'], color)}  {v['risk']}  {v['title']}")
    for h in v.get("weakened", []) if full else []:
        voided = f"; voided {', '.join(h['voided'])}" if h.get("voided") else ""
        print(
            f"  {mark('⚠️')}"
            + paint(f"weakened {h['at'][:16]}: {'; '.join(h['weakened'])}", "yellow")
            + f" ({h['reason']}{voided})"
        )
    g = v.get("progress")
    if g and (g["criteria"] or g["tasks"]):
        tasks = f"{g['tasks_done']} of {plural(g['tasks'], 'task')} complete" if g["tasks"] else ""
        if g["criteria"]:
            n = g["criteria"]
            met = f"{len(g['met'])} of {n} {'criterion' if n == 1 else 'criteria'} with evidence"
            met += f" ({id_ranges(g['met'])})" if g["met"] else ""
            print(f"  {bar(len(g['met']) / g['criteria'])} {met}" + (f" · {tasks}" if tasks else ""))
            if g["unlisted"]:
                print(f"  {mark('📝')}no evidence yet: {id_ranges(g['unlisted'])}")
        else:
            print(f"  {tasks}")
        if g["next"]:
            print(f"  {mark('🧭')}next: {g['next']}")
        if full and g["criteria"]:
            print_criteria(v["id"], g["detail"])
    if "branch" in v:
        print(f"  {mark('🌿')}branch {v['branch']} at {v['head']}, {plural(v['ahead'], 'commit')} ahead of main")
        if v["missing"]:
            print(f"  {mark('🛬')}" + paint("to land:", "yellow"))
            for gap in v["missing"]:
                print(f"    - {gap}")
        else:
            print(f"  {mark('✅')}" + paint(f"ready to land: `campaign land {v['id']}`", "green"))
        checks = [f"{k} {s}" for k, s in v["checks"].items()]
        if v["later"]:
            checks.append("after landing: " + ", ".join(v["later"]))
        if checks:
            print(f"  {mark('🧪')}checks: " + "; ".join(checks))
        tasks = dict(v["tasks"])
        for name, w in v["journal"].items():
            dirty = " [worktree missing]" if w["dirty"] < 0 else f" [{w['dirty']} uncommitted]" if w["dirty"] else ""
            if name == v["id"]:
                print(f"  {mark('📓')}journal: {w['state']}{dirty}")
            else:  # a task's id is QUEST--TASK
                tasks[name.split("--", 1)[1]] += dirty
        if tasks:
            print(f"  {mark('👥')}tasks:")
            for task, state in tasks.items():
                print(f"    {mark(task_mark(state))}{task}: {state}")
    elif v["launchable"]:
        print(f"  ready to start: `campaign start {v['id']}`")
    else:
        for gap in v["missing"]:
            print(f"  {gap}")
    for m in v.get("messages", []):
        if not m["answered"]:
            print(f"  {mark('📨')}" + paint(f"{m['id']} from {m['by']}", "yellow") + f" ({m['at'][:16]}): {m['text']}")
        elif full:
            print(f"  {mark('📨')}{m['id']} from {m['by']}: answered in a journal")
    # a failure is part of iterating; one no journal mentions yet is the one to look at
    bad = v["bad_at_head"]
    unread = [f for f in bad if not f["noted"]]
    counts = [f"{v['done_at_head']} passed"] if v["done_at_head"] else []
    failed = f"{len(bad)} failed"
    counts += [paint(failed, "yellow") if unread else failed] if bad else []
    counts += [f"{n} {k}" for k, n in v["jobs"].items() if n]
    if counts:
        line = f"  {mark('🔬')}jobs at this code: {', '.join(counts)}"
        if unread:
            some = "" if len(unread) == len(bad) else f"{len(unread)} "
            line += f"; {some}not in a journal yet (`campaign job log ID`):"
        elif bad:
            line += " (noted in the journals)"
        print(line)
    for f in unread:
        how = "" if f["state"] == "failed" else f"{f['state']}: "
        print(f"    {mark('👀')}{f['job']}  {how}{f['what']}" + (" (a check)" if f["check"] else ""))
    if full and v["landed"]:
        print(f"  {mark('🏁')}landed: " + ", ".join(v["landed"]))
    if v.get("deferred"):
        print(f"  {mark('⏳')}deferred checks: " + ", ".join(f"{n} {s}" for s, n in v["deferred"].items()))


def here_quest(c: Campaign, cwd: Path) -> str:
    """The quest whose worktree (or a task's) this is; "" elsewhere."""
    branch = git(cwd, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    return next((pid for pid in c.quests if any(b == branch for _, b, _, _ in branches(c, pid))), "")


def brief(c: Campaign, views: list[Json], queue: dict[str, int], scoped: bool) -> Json:
    """Status for agents: per quest, only what to act on. Scoped to one quest, its file set too."""
    out: Json = {"campaign": c.state["campaign"], "home": str(c.home), "jobs": queue}
    if not scoped:
        out["counts"] = overview(c)["counts"]
        out["messages"] = [{k: m[k] for k in ("id", "by", "at", "text")} for m in inbox(c, "")]
    quests: list[Json] = []
    out["quests"] = quests
    for v in views:
        if v["status"] in ("landed", "dropped") and not scoped:
            continue
        q: Json = {"id": v["id"], "status": v["status"], "risk": v["risk"]}
        if "branch" in v:
            q.update(branch=v["branch"], head=v["head"], ahead=v["ahead"])
        if scoped:
            q["files"] = v["files"]
        q["next"] = (
            f"campaign start {v['id']}"
            if v["launchable"]
            else f"campaign land {v['id']}"
            if "branch" in v and not v["missing"]
            else ""
        )
        q["to_land"] = v["missing"]
        q["tasks"] = v.get("tasks", {})
        q["docs_only"] = v.get("docs_only", [])
        q["messages"] = [
            {k: m[k] for k in ("id", "by", "at", "text")} for m in v.get("messages", []) if not m["answered"]
        ]
        q["failed"] = [{k: f[k] for k in ("job", "state", "what")} for f in v["bad_at_head"] if not f["noted"]]
        q["replaced"] = v["replaced"]
        q["stray"] = v["stray"]
        q["pending"] = {s: k for s, k in v["jobs"].items() if k}
        if "evidence" in v:
            q["evidence"] = v["evidence"]
        quests.append(q)
    return out


def print_brief(b: Json) -> None:
    """The brief as one line per thing to act on, no decoration."""
    if "counts" in b:
        n, jobs = b["counts"], b["jobs"]
        print(
            f"campaign {b['campaign']}: {in_flight(n)}, {n['planned']} planned, {n['landed']} landed; "
            f"jobs {jobs['queued']} queued, {jobs['running']} running"
        )
    for m in b.get("messages", []):
        print(f"- {m['id']} for the lead, from {m['by']}: {m['text']}")
    if not b["quests"] and "counts" in b:
        print("nothing in flight or planned (`campaign new ID --title T` plans a quest)")
    for q in b["quests"]:
        where = f": {q['branch']} at {q['head']}, {plural(q['ahead'], 'commit')} ahead" if "branch" in q else ""
        print(f"{q['id']} {q['status']} {q['risk']}{where}")
        if "files" in q:
            print(f"  files: {', '.join(q['files']) or '(none)'}")
        if q["next"]:
            print(f"  ready to {q['next'].split()[1]}: `{q['next']}`")
        for gap in q["to_land"]:
            print(f"  - {gap}")
        for task, state in q["tasks"].items():
            print(f"  task {task}: {state}")
        for note in q["docs_only"]:
            print(f"  evidence at other code: {note}")
        for m in q["messages"]:
            print(f"  - {m['id']} from {m['by']}: {m['text']}")
        for f in q["failed"]:
            print(f"  - {f['job']} {f['state']}, in no journal yet: {f['what']}")
        for note in q["stray"]:
            print(f"  - {note}")
        for r in q["replaced"]:
            print(
                f"  - {r['criteria']} in {r['journal']} cites {r['job']}; {r['by']} replaces it "
                f"({r['state']} at {r['commit']}): cite that one"
            )
        if q["pending"]:
            print(f"  - jobs: {', '.join(f'{k} {s}' for s, k in q['pending'].items())}")
        if len(q.get("evidence", [])) > 2:
            print("  evidence, for the journal's Evidence table:")
            print("\n".join(f"  {row}" for row in q["evidence"]))


def cmd_status(a: argparse.Namespace) -> int:
    if a.all:
        return machine_status()
    with frozen():
        return show_status(a)


def show_status(a: argparse.Namespace) -> int:
    c = find_campaign(Path.cwd())
    reap()
    if not a.id and (a.brief or not a.json):  # in a quest's worktree, that quest
        a.id = here_quest(c, Path.cwd())
    pids = [a.id] if a.id else list(c.quests)
    for pid in pids:
        c.quest(pid)
    warm_repos([c.home, *(c.repo(c.quests[x]) for x in (pids if a.brief else c.quests))])
    warm_views(c, pids, a.brief)
    if not a.brief:
        sidebar(c)
    jobs = all_jobs()
    views = [quest_view(c, pid, jobs, brief=a.brief, table=a.brief and bool(a.id)) for pid in pids]
    queue = {s: sum(1 for j in jobs if j["state"].get("state") == s) for s in ("queued", "running")}
    if a.brief:
        b = brief(c, views, queue, scoped=bool(a.id))
        if a.json:
            print(json.dumps(b, indent=2))
        else:
            print_brief(b)
        return 0
    found: list[tuple[str, str]] = [] if a.brief else problems(c)
    o = overview(c)
    if a.json:
        print(
            json.dumps(
                {
                    "campaign": c.state["campaign"],
                    "home": str(c.home),
                    "overview": o,
                    "quests": views,
                    "problems": found,
                    "jobs": queue,
                },
                indent=2,
            )
        )
        return 0
    n = o["counts"]
    print(f"{mark('🏰')}{paint('campaign ' + c.state['campaign'], 'bold')} ({c.home})")
    print(f"  quests: {n['landed']} landed, {in_flight(n)}, {n['planned']} planned")
    if n["planned"]:
        print(f"  launchable now: {', '.join(o['launchable']) or 'none'}")
    if heads := most_waited_on(c):
        print("  most waited on: " + ", ".join(f"{k} ({n} waiting)" for k, n in heads))
    if idle := unattended(c, jobs):
        print(f"  nothing moves without the lead: {', '.join(idle)} (resume, review or land each)")
    holders: dict[str, list[str]] = {}
    for k, by in o["held"].items():
        for x in by:
            holders.setdefault(x, []).append(k)
    if holders:
        print("  held by file sets: " + "; ".join(f"{x} holds {', '.join(ks)}" for x, ks in holders.items()))
    if o["hunt"] is not None:
        print(f"  {mark('🔎')}hunt list: {o['hunt']} open ({c.meta}/hunt.md)")
    live = [v for v in views if v["status"] != "dropped"]
    if live and not a.id:
        done = sum(
            1.0 if v["status"] == "landed" else len(v["progress"]["met"]) / v["progress"]["criteria"]
            for v in live
            if v["status"] == "landed" or (v.get("progress") or {}).get("criteria")
        )
        print(f"  {bar(done / len(live))} of the campaign (landed quests, plus criteria with evidence)")
    m = o["milestone"]
    if m and m["name"]:
        print(f"  {mark('🎯')}milestone {m['name']}: {m['landed']} of {plural(m['quests'], 'quest')} landed")
    up = worker_alive()
    worker = f"worker {'up' if up else 'down'}"
    print(
        f"  {mark('🔬')}jobs: {queue['queued']} queued, {queue['running']} running; {mark('⚡' if up else '💤')}{worker}"
    )
    for line in [*health_lines(queue_health(jobs, time.time())), *cpu_lines(jobs)] if not a.id else []:
        print(f"  {line}")
    for m in inbox(c, "") if not a.id else []:
        print(f"  {mark('📨')}" + paint(f"{m['id']} for the lead, from {m['by']}", "yellow") + f": {m['text']}")
    if not views:
        print("  no quests yet (`campaign new ID --title T` plans one)")
    if a.id:
        print_quest(views[0], full=True)
    else:
        for status in ("active", "review", "blocked", "planned"):
            for v in views:
                if v["status"] == status:
                    print_quest(v, full=False)
        landed = [v for v in views if v["status"] == "landed"]
        if landed:
            print(f"\n{mark('🏁')}landed: " + ", ".join(v["id"] for v in landed))
            for v in landed:
                red = {s: k for s, k in v.get("deferred", {}).items() if s != "done"}
                if red:
                    print(f"  {v['id']}: deferred checks: " + ", ".join(f"{k} {s}" for s, k in v["deferred"].items()))
    if found:
        print()
    for severity, message in found:
        print_problem(severity, message)
    return 0


def print_problem(severity: str, message: str) -> None:
    style, emoji = {"error": ("red", "❌"), "warning": ("yellow", "❗")}.get(severity, ("dim", "💬"))
    print(f"{mark(emoji)}{paint(severity, style)}: {message}")
