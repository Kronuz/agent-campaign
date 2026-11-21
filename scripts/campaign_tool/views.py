"""Quest views: the problems status reports, a quest as status shows it, and the campaign map."""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
from pathlib import Path

from style import paint, styled

from .campaign import Campaign, ended_at_head, find_campaign, park_met, unexplained_failures, writing
from .ci import ci_covers
from .core import CI_CHECK, FAILED, Json, atomic_write, git, jobs_dir, plural, section, stamp
from .evidence import criteria, criteria_evidence, evidence_table, replaced_citations, stray_rows
from .jobs import all_jobs, job_summary, load_job, quest_jobs
from .journals import (
    MAP_RANK,
    checkouts,
    journal_place,
    journal_text,
    journals_at,
    milestone_groups,
    next_step,
    state_of,
)
from .mail import answered, inbox
from .procs import worker_alive
from .repo import (
    ancestor,
    changed,
    changed_files,
    common_dir,
    frozen,
    has_ref,
    is_meta,
    lead_file,
    main_branch,
    merge_base,
    off_branch,
    on_commits,
    rev,
    same_repo,
    warm_repos,
)
from .reuse import docs_note, job_for_check
from .reviews import landing_gaps, merged_journals, task_states, verdict, waits_line
from .state import (
    abandoned,
    branches,
    campaigns,
    changes,
    clashes,
    consumed,
    cross_overlaps,
    dead_end,
    deferred,
    foreign,
    gates,
    starved,
    unstarted_waits,
)


def problems(c: Campaign) -> list[tuple[str, str]]:
    """(severity, message) for everything git, the worktrees and the queue contradict."""
    out: list[tuple[str, str]] = []
    if c.state.get("landing"):
        out.append(
            (
                "warning",
                f"the landing of {c.state['landing'].get('id')} was interrupted; the next state-writing command completes or clears it",
            )
        )
    for pid, p in c.quests.items():
        for d in dict.fromkeys(p["needs"] + p["after"]) if p["status"] not in ("landed", "dropped") else ():
            if why := dead_end(c, d):
                out.append(("error", f"{pid} waits on {why}: rewire it (`campaign set {pid} after=...`)"))
            elif why := abandoned(c, d):
                out.append(
                    (
                        "warning",
                        f"{pid} waits on {d}: {why}, so it no longer blocks; confirm {pid} needs none of its "
                        f"work and remove it (`campaign set {pid} after=...`)",
                    )
                )
        if p["status"] in ("active", "review") and (first := starved(c, pid)):
            out.append(
                (
                    "error",
                    f"{pid} holds files of {', '.join(first)}, which it lands after, so they cannot start and "
                    f"{pid} cannot land: narrow {pid}'s files (`campaign set {pid} files=...`) to what it changes",
                )
            )
        reviewed = p["status"] == "review" and set(p["lenses"]) <= set(p.get("accepted", {}))
        if reviewed and (late := [d for d in p["after"] if not c.met(d) and not dead_end(c, d)]):
            out.append(
                (
                    "warning",
                    f"{pid} is reviewed and waits to land after {', '.join(late)}: if landing first breaks "
                    f"nothing, drop that order (`campaign set {pid} after=...`)",
                )
            )
    for name, o in campaigns().items():
        for qid, q in o["state"].get("quests", {}).items() if name != c.name else ():
            for d in [*q.get("needs", []), *q.get("after", [])] if q.get("status") not in ("landed", "dropped") else ():
                mine = d.partition(":")[2] if d.partition(":")[0] == c.name else ""
                if mine and (why := dead_end(c, mine)):
                    out.append(("error", f"{name}:{qid} waits on our {why}: tell {name}'s lead which quest it means"))
    touched: dict[str, list[tuple[str, str]]] = {}
    warm_repos([c.repo(p) for p in c.quests.values()])
    landed: dict[Path, list[str]] = {}
    for p in c.quests.values():
        landed.setdefault(c.repo(p), []).extend(x["commit"] for x in p.get("landed", []))
    stray: dict[Path, set[str]] = {}
    for pid, p in c.quests.items():
        repo = c.repo(p)
        if not common_dir(repo):
            out.append(("error", f"{pid}: repository {repo} is missing"))
            continue
        main = main_branch(repo)
        if repo not in stray:
            stray[repo] = off_branch(repo, landed[repo], main)
        for entry in p.get("landed", []):
            if entry["commit"] in stray[repo]:
                out.append(("error", f"{pid}: landed commit {entry['commit'][:12]} is not on {main}"))
        if p["status"] not in ("active", "review"):
            continue
        unmet = [d for d in p["needs"] if not c.met(d)]
        if unmet:
            out.append(("warning", f"{pid}: active before its needs landed: {', '.join(unmet)}"))
        for name, branch, rel, _ in branches(c, pid):
            if not has_ref(repo, branch):
                out.append(("error", f"{name}: branch {branch} is gone; drop the quest or restart it"))
                continue
            wt = (c.home / rel).resolve() if rel else None
            if wt is None or not wt.exists():
                out.append(("warning", f"{name}: worktree {rel or '(none)'} is missing"))
            if not same_repo(repo, c.home):
                if not has_ref(c.home, branch):
                    out.append(("error", f"{name}: its journal branch {branch} is missing in the home"))
                jp = journal_place(c, name)
                if jp is None or jp == wt or not jp.exists():
                    out.append(("warning", f"{name}: its journal's worktree in the home is missing"))
            head = rev(repo, branch)
            if name == pid and ancestor(repo, head, main) and head != rev(repo, p["base"]):
                out.append(
                    (
                        "warning",
                        f"{pid}: {branch} is merged into {main} but not recorded (`campaign land {pid} --record {head[:12]}`)",
                    )
                )
            against = rev(repo, main) if name == pid else rev(repo, p["branch"])
            paths = changed(repo, merge_base(repo, against, head), head)
            owned = [x for x in paths if lead_file(x)]
            if owned:
                out.append(("warning", f"{name}: edits the lead's files {', '.join(owned[:5])}"))
            code = [x for x in paths if not is_meta(x)]
            scope = p["files"] if name == pid else p.get("tasks", {}).get(name.partition("--")[2], {}).get("files", [])
            if scope:
                outside = [x for x in code if not any(fnmatch.fnmatch(x, g) for g in scope)]
                if outside:
                    out.append(("warning", f"{name}: changes files outside its scope: {', '.join(outside[:5])}"))
            for x in code:
                touched.setdefault(f"{p['repo']}:{x}", []).append((pid, name))
    overlaps: dict[tuple[str, ...], list[str]] = {}
    for path, who in touched.items():
        pids = tuple(sorted({w[0] for w in who}))
        if len(pids) > 1:
            overlaps.setdefault(pids, []).append(path.split(":", 1)[1])
    for pids, paths in sorted(overlaps.items()):
        out.append(("warning", f"one writer per file: {' and '.join(pids)} both change {', '.join(paths[:5])}"))
    for pid, p in c.quests.items():
        if met := park_met(c, p):
            back = "active" if p.get("branch") else "planned"
            out.append(("error", f"{pid}: parked {met}: `campaign set {pid} status={back}`"))
        live = p["status"] in ("planned", "active", "review")
        for dep in [*p["needs"], *p["after"]]:
            if ":" in dep and live and foreign(dep) is None:
                out.append(("warning", f"{pid}: depends on {dep}, which no campaign on this machine has"))
        if not live:
            continue
        # one warning per peer quest, naming every overlap: files, and contracts either side changes
        peer: dict[str, Json] = {}
        if p["status"] in ("active", "review"):
            for other in cross_overlaps(c, pid):
                peer.setdefault(other, {"files": False, "theirs": [], "mine": []})["files"] = True
        for name, o in campaigns().items():
            if name == c.name:
                continue
            # only a started quest changes a contract
            for qid, q in o["state"].get("quests", {}).items():
                if q.get("status") not in ("planned", "active", "review"):
                    continue
                theirs = consumed(p["consumes"], changes(Path(o["home"]), q)) if q.get("status") != "planned" else []
                mine = consumed(q.get("consumes", []), changes(c.home, p)) if p["status"] != "planned" else []
                if theirs or mine:
                    entry = peer.setdefault(f"{name}:{qid}", {"files": False, "theirs": [], "mine": []})
                    entry.update(theirs=theirs, mine=mine)
        for other, e in peer.items():
            parts = [f"files may overlap: order them (--after {other}) or narrow files"] if e["files"] else []
            if e["theirs"]:
                parts.append(
                    f"contract{'s' if len(e['theirs']) > 1 else ''} {', '.join(e['theirs'])} changing in {other}: build against its brief"
                )
            if e["mine"]:
                parts.append(
                    f"{other} consumes contract{'s' if len(e['mine']) > 1 else ''} {', '.join(e['mine'])}, which {pid} changes: tell its lead"
                )
            out.append(("warning", f"{pid} and {other}: " + "; ".join(parts)))
    providers: dict[str, list[str]] = {}
    for pid, p in c.quests.items():
        if p["status"] != "dropped":
            for contract in p["provides"]:
                providers.setdefault(contract, []).append(pid)
    for contract, pids in sorted(providers.items()):
        if len(pids) > 1:
            out.append(
                ("warning", f"contract {contract} is provided by {', '.join(pids)}: one provider, or a system review")
            )
        changing = [x for x in pids if c.quests[x]["status"] in ("active", "review")]
        consumers = [
            x
            for x, q in c.quests.items()
            if contract in q["consumes"] and q["status"] in ("planned", "active", "review") and x not in pids
        ]
        if changing and consumers:
            out.append(
                (
                    "info",
                    f"contract {contract} is changing in {', '.join(changing)}; consumers {', '.join(consumers)} build against its brief",
                )
            )
    jobs = all_jobs()
    for pid, p in c.quests.items():
        for record in p["landed"]:
            for jid in record.get("later", []):
                job = load_job(jobs_dir() / jid)
                st = job["state"].get("state", "missing")
                if st == "missing":
                    out.append(("warning", f"{pid}: deferred check {jid}'s job is gone; its result is unknown"))
                elif st in ("failed", "timeout", "interrupted", "cancelled", "skipped"):
                    at = str(job["spec"].get("commit", record["commit"]))[:12]
                    out.append(
                        (
                            "error",
                            f"{pid}: deferred check {jid} is {st} at {at}: fix forward or revert, then `campaign verify {pid} --deferred`",
                        )
                    )
    if any(j["state"].get("state") == "queued" for j in jobs) and not worker_alive():
        out.append(
            ("warning", "jobs are queued and no worker runs (`campaign worker`, or any submit or wait starts one)")
        )
    for j in jobs:
        if j["state"].get("state") == "interrupted" and j["spec"].get("campaign") == c.ident:
            out.append(("warning", f"job {j['id']} ({j['spec'].get('what')}) was interrupted and not retried"))
    return out


def quest_view(c: Campaign, pid: str, jobs: list[Json], brief: bool = False, table: bool = False) -> Json:
    """A quest as status shows it: what it still needs ("missing"), its branch, checks, reviews and
    journals, and the jobs that failed at its current code, each noted or not in a journal. Brief
    keeps only what an agent acts on."""
    p = c.quests[pid]
    view: Json = {"id": pid, "status": p["status"], "risk": p["risk"], "title": p["title"], "missing": []}
    view["weakened"] = [h for h in p.get("history", []) if h.get("weakened")]
    view["files"] = p["files"]
    unmet = unstarted_waits(c, p)
    clash: list[str] = clashes(c, pid) if p["status"] == "planned" and not unmet else []
    view["launchable"] = p["status"] == "planned" and not unmet and not clash
    if p["status"] == "planned" and unmet:
        view["missing"] = [waits_line(c, unmet)]
    elif clash:
        view["missing"] = [f"files may overlap {', '.join(clash)}: start once it lands, or narrow files"]
    elif p["status"] == "blocked":
        view["missing"] = [f"blocked until {', '.join(p.get('until', []))}: {p.get('reason', '')}"]
    repo = c.repo(p)
    mine = quest_jobs(jobs, c.ident, pid)
    view["jobs"] = {s: sum(1 for j in mine if j["state"].get("state") == s) for s in ("queued", "running")}
    view["landed"] = [
        x["commit"][:12]
        + ("" if x.get("final") else " (increment)")
        + (" (before the campaign)" if x.get("earlier") else "")
        + (f" (unverified: {'; '.join(x['unverified'])})" if x.get("unverified") else "")
        + (f" (CI skipped: {x['ci_skipped']})" if x.get("ci_skipped") else "")
        for x in p["landed"]
    ]
    later = [load_job(jobs_dir() / j)["state"].get("state", "missing") for x in p["landed"] for j in x.get("later", [])]
    if later:
        view["deferred"] = {s: later.count(s) for s in sorted(set(later))}
    checked: set[str] = set()
    texts: dict[str, str] = {}
    if p["status"] in ("active", "review") and p.get("branch") and has_ref(repo, p["branch"]):
        main = main_branch(repo)
        head = rev(repo, p["branch"])
        base = rev(repo, main)
        ahead = on_commits(
            ("ahead", str(repo), base, head), lambda: git(repo, "rev-list", "--count", f"{base}..{head}")
        )
        view.update(branch=p["branch"], head=head[:12], ahead=int(ahead))
        view["missing"] = landing_gaps(c, pid, head, final=True, ci=True, jobs=jobs)
        view["docs_only"] = [
            f"{x['name']}: {j['id']} {docs_note(j)}"
            for x in gates(p)
            if not x["cmd"].startswith(CI_CHECK)
            and (j := job_for_check(repo, head, x, c.state.get("tested_docs", []), ci=ci_covers(c), files=p["files"]))
            and docs_note(j)
        ]
        view["tasks"] = task_states(c, pid, jobs)
        if brief:
            for name, branch, _, _ in branches(c, pid):
                texts[name] = journal_text(c, name, branch)
            texts.update(merged_journals(c, pid))
        if table:
            tips = [rev(repo, b) for _, b, _, _ in branches(c, pid) if has_ref(repo, b)]
            view["evidence"] = evidence_table(criteria_evidence(c, pid, tips, texts, jobs))
    if not brief and "branch" in view:
        head = rev(repo, p["branch"])
        checks: dict[str, str] = {}
        view["checks"] = checks
        view["later"] = [x["name"] for x in deferred(p)]
        for check in gates(p):
            job = job_for_check(repo, head, check, c.state.get("tested_docs", []), ci=ci_covers(c), files=p["files"])
            note = f" ({docs_note(job)})" if job and docs_note(job) else ""
            checks[check["name"]] = (job["state"].get("state", "?") + note) if job else "not run at head"
            if job:
                checked.add(job["id"])
        view["reviews"] = {lens: verdict(c, pid, lens) for lens in p["lenses"]}
        for lens, acc in p.get("accepted", {}).items():
            if lens in view["reviews"]:
                view["reviews"][lens] = acceptance(acc, verdict(c, pid, lens))
        work: dict[str, Json] = {}
        view["journal"] = work
        for name, branch, rel, _ in branches(c, pid):
            dirty = 0  # the code's and the journal's worktrees; -1 when one is missing
            for place in checkouts(c, name, rel):
                if place is None or not place.exists():
                    dirty = -1
                    break
                dirty += len(changed_files(place))
            texts[name] = journal_text(c, name, branch)
            task = p.get("tasks", {}).get(name.partition("--")[2], {})
            state = f"dropped: {task.get('reason', '')}" if task.get("status") == "dropped" else state_of(texts[name])
            work[name] = {"state": state, "dirty": dirty, "worktree": rel}
        texts.update(merged_journals(c, pid))
        journal = texts.get(pid) or journal_text(c, pid, p["branch"])
        wanted = criteria(c, pid)
        tasks = [x for x in view["tasks"].values() if not x.startswith("dropped: ")]
        tips = [rev(repo, b) for _, b, _, _ in branches(c, pid) if has_ref(repo, b)]
        journals = {pid: journal, **{n: t for n, t in texts.items() if n != pid}}
        rows = criteria_evidence(c, pid, tips, journals, jobs)
        view["progress"] = {
            "criteria": len(wanted),
            "met": [x for x, r in rows.items() if any(e["met"] for e in r)],
            "unlisted": [x for x, r in rows.items() if not r],
            "detail": [{"id": x, **wanted[x], "rows": r} for x, r in rows.items()],
            "tasks": len(tasks),
            "tasks_done": sum(1 for t in tasks if re.match(r"(complete|done|merged)\b", t, re.I)),
            "next": next_step(journal),
        }
    view["replaced"] = replaced_citations(texts, mine)
    view["stray"] = stray_rows(texts, criteria(c, pid)) if brief and texts else []
    ended = ended_at_head(c, pid, mine)
    view["done_at_head"] = sum(1 for j in ended if j["state"].get("state") == "done")
    view["messages"] = [{**m, "answered": answered(m, texts.values())} for m in inbox(c, pid)]
    noted = "\n".join(texts.values())
    view["bad_at_head"] = [
        {**job_summary(j), "check": j["id"] in checked, "noted": j["id"] in noted}
        for j in ended
        if j["state"].get("state") in FAILED
    ]
    return view


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
