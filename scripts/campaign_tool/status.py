"""Status and brief: the problems status reports, a quest as status shows it, and both commands' output."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

from style import mark, paint, styled

from .campaign import Campaign, ended_at_head, find_campaign, in_flight, overview, park_met, sidebar, unattended
from .ci import ci_covers, main_ci_problems, warm_views
from .core import CI_CHECK, FAILED, FLIGHT, STATUSES, Json, git, jobs_dir, plural
from .evidence import criteria, criteria_evidence, evidence_table, replaced_citations, stray_rows
from .jobs import all_jobs, job_summary, load_job, quest_jobs
from .journals import checkouts, id_ranges, journal_place, journal_text, next_step, state_of
from .mail import answered, inbox
from .map import STAGES, acceptance
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
from .runner import reap, worker_alive
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
    most_waited_on,
    starved,
    unstarted_waits,
)
from .title import keep_title
from .todo import todo, todo_lines
from .worker import cpu_lines, health_lines, queue_health

GUARD_HOURS = 24  # the window the treadmill guard reads
GUARD_NEVER = 3  # rounds cancelled before any of their jobs started, in the window
GUARD_UNPROVEN = 4  # rounds that ran in the window and met no new criterion


def parent(repo: str, commit: str) -> str:
    return on_commits(
        ("parent", repo, commit), lambda: git(Path(repo), "rev-parse", "-q", "--verify", f"{commit}^", check=False)
    )


def rounds(jobs: list[Json]) -> dict[str, list[Json]]:
    """Jobs grouped by the quest head they ran against: a round. A fails-without job counts with the
    head its mutant was cut from (its parent), else with the round submitted just before it."""
    out: dict[str, list[Json]] = {}
    order = sorted(jobs, key=lambda j: float(j["spec"].get("submitted") or 0))
    for j in order:
        if not j["spec"].get("fails_without"):
            out.setdefault(j["spec"].get("commit") or "unversioned", []).append(j)
    last = ""
    for j in order:
        spec = j["spec"]
        if not spec.get("fails_without"):
            last = spec.get("commit") or "unversioned"
            continue
        commit = spec.get("commit") or ""
        up = parent(spec["repo"], commit) if commit and spec.get("repo") and commit not in out else commit
        out.setdefault(up if up in out else last or commit, []).append(j)
    return out


def treadmill(c: Campaign, pid: str, jobs: list[Json], now: float) -> str:
    """Why a quest's last GUARD_HOURS of evidence look queued at every commit, counted in rounds (one
    per head): at least GUARD_NEVER cancelled before any job started, or GUARD_UNPROVEN that ran and
    met no new criterion (first proven by a job tagged for it); "" otherwise. Names the counts, the
    job-hours waited, and the lead's step."""
    since = now - GUARD_HOURS * 3600
    mine = quest_jobs(jobs, c.ident, pid)
    window = [j for j in mine if float(j["spec"].get("submitted") or 0) >= since]
    first: dict[str, tuple[float, str]] = {}
    for j in mine:
        ended = float(j["state"].get("ended") or 0)
        for t in j.get("tags", []) if j["state"].get("state") == "done" else ():
            for cid in t.get("for", []) if (t.get("campaign"), t.get("quest")) == (c.ident, pid) else ():
                first[cid] = min(first.get(cid, (ended, j["id"])), (ended, j["id"]))
    proving = {jid for ended, jid in first.values() if ended >= since}
    never = barren = 0
    for group in rounds(window).values():
        if not any(j["state"].get("started") for j in group):
            never += any(j["state"].get("state") == "cancelled" for j in group)
        elif not any(j["id"] in proving for j in group):
            barren += 1
    if never < GUARD_NEVER and barren < GUARD_UNPROVEN:
        return ""
    waited = sum(
        max(0.0, float(j["state"].get("started") or j["state"].get("ended") or now) - float(j["spec"]["submitted"]))
        for j in window
    )
    heads = len(rounds(window))
    return (
        f"{pid} queued evidence at {heads} heads in the last {GUARD_HOURS} h: {never} "
        f"{'round was' if never == 1 else 'rounds were'} cancelled before any job started, {barren} ran and met "
        f"no new criterion, {len(proving)} newly met; {waited / 3600:.0f} job-hours waiting. Lead: tell its "
        "contributor to iterate in its worktree under `quiet light` and submit one round per State ready for review"
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
    now = time.time()
    for pid, p in c.quests.items():
        if p["status"] in ("active", "review") and (text := treadmill(c, pid, jobs, now)):
            out.append(("warning", text))
    if any(j["state"].get("state") == "queued" for j in jobs) and not worker_alive():
        out.append(
            ("warning", "jobs are queued and no worker runs (`campaign worker`, or any submit or wait starts one)")
        )
    for j in jobs:
        if j["state"].get("state") == "interrupted" and j["spec"].get("campaign") == c.ident:
            out.append(("warning", f"job {j['id']} ({j['spec'].get('what')}) was interrupted and not retried"))
    out += main_ci_problems(c)
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
    view["treadmill"] = treadmill(c, pid, jobs, time.time()) if p["status"] in ("active", "review") else ""
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
        q["treadmill"] = v["treadmill"]
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
        if q["treadmill"]:
            print(f"  - {q['treadmill']}")
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
    if not a.json and (line := keep_title(c)):
        print(line)
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
    listed = None if a.id else todo(c, jobs, {v["id"]: v["missing"] for v in views if "branch" in v})
    if a.brief:
        b = brief(c, views, queue, scoped=bool(a.id))
        if listed is not None:
            b["todo"] = listed
        if a.json:
            print(json.dumps(b, indent=2))
        else:
            print_brief(b)
            for line in todo_lines(listed) if listed is not None else []:
                print(line)
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
                    **({"todo": listed} if listed is not None else {}),
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
    if listed is not None:
        print()
        for line in todo_lines(listed, None):
            print(line)
    return 0


def print_problem(severity: str, message: str) -> None:
    style, emoji = {"error": ("red", "❌"), "warning": ("yellow", "❗")}.get(severity, ("dim", "💬"))
    print(f"{mark(emoji)}{paint(severity, style)}: {message}")
