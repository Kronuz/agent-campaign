"""The campaign map: goals, milestones and quests with their intent, result and criteria (MAP.md)."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from style import paint, styled

from .campaign import Campaign, find_campaign, unexplained_failures, writing
from .core import Json, atomic_write, git, plural, section, stamp
from .evidence import criteria, criteria_evidence
from .jobs import all_jobs, job_summary
from .journals import MAP_RANK, journal_text, journals_at, milestone_groups, next_step
from .repo import frozen, has_ref, main_branch, on_commits, rev, warm_repos
from .reviews import verdict
from .state import branches


def acceptance(acc: Json, now: str) -> str:
    """How an acceptance reads: at its commit, or void when it names no review content (an acceptance
    by an earlier tool, which `land` refuses), with the review's verdict now."""
    if "review" not in acc:
        return f"{now}; its acceptance names no review content, so it is void: accept again"
    return f"accepted at {acc['commit'][:12]}"


def first_paragraph(text: str, limit: int = 400) -> str:
    """A section's first paragraph on one line, without the template's <placeholders>."""
    for para in re.split(r"\n\s*\n", text.strip()):
        line = " ".join(para.split())
        if line and not line.startswith("<"):
            return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"
    return ""


def quest_map(c: Campaign, pid: str, jobs: list[Json]) -> Json:
    """A quest as the map shows it: its intent (the quest file's Outcome), its result (the journal's
    Result), the documents its landing added to its repository, its criteria and reviews, and the jobs
    that failed at its current code with no journal naming them."""
    p = c.quests[pid]
    repo = c.repo(p)
    wanted = criteria(c, pid)
    final = [x for x in p["landed"] if x.get("final")]
    journals: dict[str, str] = {}
    main = main_branch(repo)
    if final:
        journals = (
            journals_at(c, repo, main, pid)
            or journals_at(c, c.home, main_branch(c.home), pid)
            or journals_at(c, repo, final[-1]["commit"], pid)
        )
    for name, branch, _, _ in branches(c, pid):
        journals.setdefault(name, journal_text(c, name, branch))
    journal = journals.get(pid, "")
    rows: dict[str, list[Json]] = {}
    if final and not final[-1].get("unverified") and not final[-1].get("earlier"):
        met = set(wanted)  # landing required every criterion
    elif final:  # recorded after the fact, or before the campaign: what its journal's evidence proves
        rows = criteria_evidence(c, pid, [final[-1]["commit"], rev(repo, main)], journals, jobs)
        met = {x for x, r in rows.items() if any(e["met"] for e in r)}
    elif p["status"] in ("active", "review") and p.get("branch") and has_ref(repo, p["branch"]):
        tips = [rev(repo, b) for _, b, _, _ in branches(c, pid) if has_ref(repo, b)]
        rows = criteria_evidence(c, pid, tips, journals, jobs)
        met = {x for x, r in rows.items() if any(e["met"] for e in r)}
    else:
        met = set()
    docs: list[str] = []
    if final:
        start = final[-1].get("from") or p.get("base", "")
        if start:
            end = final[-1]["commit"]
            changed = on_commits(
                ("docs-added", str(repo), start, end),
                lambda: git(repo, "diff", "--name-only", "--diff-filter=AM", start, end, "--", "docs", check=False),
            )
            prefix = "" if repo == c.home.resolve() else f"{repo.name}:"
            docs = [prefix + x for x in changed.splitlines()]
    reviews = {}
    for lens in p["lenses"]:
        acc = p.get("accepted", {}).get(lens)
        reviews[lens] = acceptance(acc, verdict(c, pid, lens)) if acc else verdict(c, pid, lens)
    blocked = first_paragraph(section(journal, "Blocked on"))
    return {
        "id": pid,
        "title": p["title"],
        "risk": p["risk"],
        "status": p["status"],
        "reason": p.get("reason", ""),
        "milestone": p.get("milestone", ""),
        "needs": [d for d in p["needs"] if not c.met(d)],
        "landed": {
            "at": final[-1]["at"],
            "commit": final[-1]["commit"],
            "repo": repo.name,
            "how": "landed before the campaign"
            if final[-1].get("earlier")
            else "recorded as landed"
            if final[-1].get("recorded")
            else "landed",
        }
        if final
        else None,
        "outcome": first_paragraph(section(c.quest_file(pid), "Outcome")),
        "result": "\n".join(x for x in section(journal, "Result").strip().splitlines() if not x.startswith("<")),
        "docs": docs,
        "criteria": [
            {"id": x, "text": w["text"], "met": x in met, "listed": bool(rows.get(x))} for x, w in wanted.items()
        ],
        "failing": [
            {"job": j["id"], "state": j["state"]["state"], "what": job_summary(j)["what"]}
            for j in unexplained_failures(c, pid, jobs, journals.values())
        ],
        "reviews": reviews,
        "next": "" if final else next_step(journal),
        "blocked": "" if final or re.match(r"nothing\b", blocked, re.I) else blocked,
    }


def map_data(c: Campaign, only: str = "") -> Json:
    """The campaign as a map: its goals, its milestones in PLAN.md's order with their quests, the
    quests in no milestone, and the dropped ones."""
    if only:
        c.quest(only)
    jobs = all_jobs()
    with frozen():
        warm_repos([c.home, *(c.repo(p) for x, p in c.quests.items() if not only or x == only)])
        quests = {pid: quest_map(c, pid, jobs) for pid in c.quests if not only or pid == only}
    plan = c.home / c.meta / "PLAN.md"
    goals: list[str] = []
    for line in section(plan.read_text() if plan.exists() else "", "Goals").splitlines():
        text = line.strip()
        if text.startswith(("- ", "* ")):
            goals.append(text[2:])
        elif text and line[:1].isspace() and goals:  # a wrapped goal's next line
            goals[-1] += " " + text
    goals = [g for g in goals if not g.startswith("<")]
    placed: set[str] = set()
    groups: list[Json] = []
    for name, validation, ids in milestone_groups(c, {x: q["status"] for x, q in quests.items()}):
        placed.update(ids)
        if ids or not only:
            groups.append({"name": name, "validation": validation, "quests": [quests[x] for x in ids]})
    rest = sorted(
        (x for x, q in quests.items() if x not in placed and q["status"] != "dropped"),
        key=lambda x: (MAP_RANK.get(quests[x]["status"], 5), x),
    )
    live = [q for q in quests.values() if q["status"] != "dropped"]
    return {
        "campaign": c.state["campaign"],
        "goals": goals,
        "landed": sum(1 for q in live if q["status"] == "landed"),
        "quests": len(live),
        "criteria": sum(len(q["criteria"]) for q in live),
        "met": sum(1 for q in live for x in q["criteria"] if x["met"]),
        "milestones": groups,
        "other": [quests[x] for x in rest],
        "dropped": [q for q in quests.values() if q["status"] == "dropped"],
    }


def quest_mark(q: Json) -> tuple[str, str]:
    """A quest's emoji and color on the map: landed (with criteria left open or not), failing at its
    current code with no journal naming it, or its status."""
    if q["status"] == "landed":
        return ("✅", "green") if all(x["met"] for x in q["criteria"]) else ("⚠️", "yellow")
    if q["failing"]:
        return "🔥", "red"
    tint, emoji = STAGES.get(q["status"], ("dim", "❔"))
    return emoji, tint


MAP_LEGEND = (
    "✅ landed · ⚠️ landed, criteria left open · 🔥 failing · 🔍 in review · 🔨 active · 🚧 blocked · 📋 planned"
)


def milestone_mark(quests: list[Json]) -> tuple[str, str]:
    """A milestone's emoji and color: done, failing, under way, waiting only on blocked quests, or
    planned."""
    statuses = {q["status"] for q in quests}
    if not statuses:
        return "⬜", "dim"
    if any(q["failing"] for q in quests):
        return "🔥", "red"
    if statuses == {"landed"}:
        return "✅", "green"
    if statuses & {"landed", "active", "review"}:
        return "🔨", "cyan"
    return ("🚧", "red") if statuses == {"blocked"} else ("📋", "dim")


def map_text(c: Campaign, only: str = "", color: bool = False) -> str:
    """The map as Markdown: a checkbox per quest, with its intent, result, documents and criteria. Its
    emoji are part of the document (MAP.md shows them); color only with `color`, for a terminal."""
    m = map_data(c, only)

    def p(text: str, *styles: str) -> str:
        return paint(text, *styles) if color else text

    out = [p(f"# {m['campaign']}: map", "bold"), ""]

    def criteria_count(n: int) -> str:
        return f"{n} criterion" if n == 1 else f"{n} criteria"

    if not only:
        out += [f"- 🎯 {g}" for g in m["goals"]] + ([""] if m["goals"] else [])
        out += [
            f"{m['landed']} of {plural(m['quests'], 'quest')} landed, {m['met']} of {criteria_count(m['criteria'])} "
            f"met. Generated by `campaign map` on {stamp()[:10]}; edit the quest files and journals, not this.",
            "",
            p(MAP_LEGEND, "dim"),
            "",
        ]

    def quest(q: Json, full: bool) -> list[str]:
        done = q["status"] == "landed"
        met = sum(1 for x in q["criteria"] if x["met"])
        emoji, tint = quest_mark(q)
        name = p("**" + q["id"] + "**", "bold", tint)
        head = f"- {emoji} {name}: {q['title']} "
        if done:
            left = [x["id"] for x in q["criteria"] if not x["met"]]
            landed = q["landed"]
            head += p(
                f"({q['risk']} risk, {landed['how']} {landed['at'][:10]} at {landed['repo']}:{landed['commit'][:12]}",
                "dim",
            )
            head += (p("; ", "dim") + p(f"left open: {', '.join(left)}", "yellow") if left else "") + p(")", "dim")
        else:
            head += p(f"({q['risk']} risk, {q['status']}, {met} of {criteria_count(len(q['criteria']))} met)", "dim")
        lines = [head]

        def note(label: str, text: str, style: str = "dim") -> None:
            lines.append(f"  - {p(label + ':', style)} {text}")

        if q["outcome"]:
            note("Outcome", q["outcome"])
        if done or q["result"]:
            result = q["result"] if full else first_paragraph(q["result"])
            note("Result", result.replace("\n", "\n    ") if result else "not written")
        if q["docs"]:
            note("Docs", ", ".join(q["docs"]))
        # a landing before the campaign was reviewed by its own process, which its Result names
        shown = {
            k: v for k, v in q["reviews"].items() if not (done and q["landed"]["how"] != "landed" and v == "none yet")
        }
        if shown:
            note("Reviews", ", ".join(f"{lens} {v}" for lens, v in shown.items()))
        if q["needs"]:
            note("Needs", ", ".join(q["needs"]))
        if q["blocked"]:
            note("Blocked on", q["blocked"], "red")
        if q["next"]:
            note("Next", q["next"], "cyan")
        if q["status"] == "blocked" and q["reason"]:
            note("Blocked", q["reason"], "red")
        if q["failing"]:
            note("Failing", "; ".join(f"{f['job']} {f['state']}: {f['what']}" for f in q["failing"]), "red")
        if full or q["status"] in ("active", "review"):
            for x in q["criteria"]:
                text = x["text"] if full else x["text"].split(": ", 1)[0][:100]
                sign = "✅" if x["met"] else "⏳" if x["listed"] else "⬜"
                lines.append(f"  - {sign} {x['id']} {text}")
        return lines

    for g in m["milestones"]:
        n = len(g["quests"])
        k = sum(1 for q in g["quests"] if q["status"] == "landed")
        state = "no quests yet" if not n else "done" if k == n else f"{k} of {n} landed"
        emoji, tint = milestone_mark(g["quests"])
        out += [p(f"## {emoji} {g['name']}: ", "bold") + p(state, "bold", tint), ""]
        if g["validation"] and not g["validation"].startswith("<"):
            out += [f"{p('Validation on main:', 'dim')} {g['validation']}", ""]
        for q in g["quests"]:
            out += quest(q, bool(only))
        out.append("")
    if m["other"]:
        out += (
            ([p("## Not in a milestone", "bold"), ""] if m["milestones"] else [])
            + [line for q in m["other"] for line in quest(q, bool(only))]
            + [""]
        )
    if m["dropped"]:
        out += [p("## Dropped", "bold"), ""]
        out += [f"- 🚫 {p('~~' + q['id'] + '~~', 'dim')}: {q['title']} ({q['reason']})" for q in m["dropped"]] + [""]
    return "\n".join(out).rstrip() + "\n"


def refresh_map(c: Campaign) -> tuple[str, ...]:
    """Rewrites MAP.md when the campaign keeps one; the paths to commit with the state."""
    path = c.home / c.meta / "MAP.md"
    if not path.exists():
        return ()
    atomic_write(path, map_text(c))
    return (f"{c.meta}/MAP.md",)


def cmd_map(a: argparse.Namespace) -> int:
    if a.write:
        with writing() as c:
            path = c.home / c.meta / "MAP.md"
            atomic_write(path, map_text(c))
            c.save("campaign: map", extra=(f"{c.meta}/MAP.md",))
        print(f"wrote {path}; `land` keeps it current")
        return 0
    c = find_campaign(Path.cwd())
    if a.json:
        print(json.dumps(map_data(c, a.id or ""), indent=2))
    else:
        print(map_text(c, a.id or "", color=styled()), end="")
    return 0


STAGES = {  # color and mark per quest status
    "planned": ("dim", "📋"),
    "active": ("cyan", "🔨"),
    "review": ("magenta", "🔍"),
    "blocked": ("red", "🚧"),
    "landed": ("green", "✅"),
    "dropped": ("dim", "🚫"),
}
