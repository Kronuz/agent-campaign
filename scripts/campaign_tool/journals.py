"""Journals and the lead's Markdown records: where each journal lives, its sections and PLAN.md."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from .core import Json, git, section, shared, table_cells
from .repo import ancestor, changed, file_at, has_ref, main_branch, merge_base, names_at, rev
from .state import branches, worktree_path

if TYPE_CHECKING:  # annotations only: they import this module, so importing them here is a cycle
    from .campaign import Campaign


def journal_place(c: Campaign, name: str) -> Path | None:
    """The worktree holding NAME's journal (a quest, or QUEST--TASK): its own, or for a quest in another
    repository, its journal's worktree in the home; None when it has none."""
    pid, _, task = name.partition("--")
    p = c.quests[pid]
    record = p.get("tasks", {}).get(task, {}) if task else p
    rel = record.get("journal") or record.get("worktree", "")
    return (c.home / rel).resolve() if rel else None


def journal_text(c: Campaign, name: str, branch: str, *, committed: bool = False) -> str:
    """A quest's or task's journal: in its journal's worktree (unless only what is committed), else on
    its branch in the home, where a quest in another repository keeps a branch of the same name."""
    path = f"{c.meta}/journal/{name}.md"
    wt = journal_place(c, name)
    if wt and not committed and (wt / path).exists():
        return (wt / path).read_text()
    return file_at(c.home, branch, path) if branch else ""


def journal_worktree(c: Campaign, name: str, branch: str) -> Path:
    """A sparse worktree of the home on BRANCH holding only the campaign's directory: where a quest in
    another repository keeps its journal, so the campaign's records stay in the home and the other
    repository's history holds only its code and docs."""
    jwt = worktree_path(c.home, c.name, name)
    if has_ref(c.home, branch):
        git(c.home, "worktree", "add", "-q", "--no-checkout", str(jwt), branch)
    else:
        home_main = rev(c.home, main_branch(c.home))
        git(c.home, "worktree", "add", "-q", "--no-checkout", "-b", branch, str(jwt), home_main)
    git(jwt, "sparse-checkout", "set", "--no-cone", f"/{c.meta}/")
    git(jwt, "checkout", "-q")
    gitdir = Path(git(jwt, "rev-parse", "--path-format=absolute", "--git-dir"))
    (gitdir / "campaign-home").write_text(f"{c.home}\n{c.name}\n")
    return jwt


def landing_journals(c: Campaign, pid: str) -> dict[str, str]:
    """The committed journals a landing of a quest in another repository carries into the home."""
    found: dict[str, str] = {}
    for name, branch, _, _ in branches(c, pid):
        text = file_at(c.home, branch, f"{c.meta}/journal/{name}.md") if has_ref(c.home, branch) else ""
        if text:
            found[name] = text
    return found


def journal_gaps(c: Campaign, pid: str) -> list[str]:
    """What keeps the home journal branches of a quest in another repository from merging cleanly: a
    branch changing more than its quest's journals, or the home's main changing them meanwhile."""
    main = main_branch(c.home)
    main_head = rev(c.home, main)
    names = [pid, *(f"{pid}--{task}" for task in c.quests[pid].get("tasks", {}))]  # merged tasks' too
    own = {f"{c.meta}/journal/{name}.md" for name in names}
    gaps: list[str] = []
    for name, branch, _, _ in branches(c, pid):
        if not has_ref(c.home, branch):
            gaps.append(f"{name}'s journal branch {branch} is missing in the home")
            continue
        tip = rev(c.home, branch)
        base = merge_base(c.home, main_head, tip)
        extra = [x for x in changed(c.home, base, tip) if x not in own]
        if extra:
            gaps.append(f"{branch} in the home changes more than its journals: {', '.join(extra[:8])}")
        both = [x for x in changed(c.home, base, main_head) if x in own]
        if both:
            gaps.append(f"the home's {main} changed {', '.join(both)} since {branch} branched: merge {main} into it")
    return gaps


def land_journals(c: Campaign, pid: str) -> None:
    """Merges the journal branches of a quest in another repository into the home's main."""
    main = main_branch(c.home)
    for name, branch, _, _ in branches(c, pid):
        if has_ref(c.home, branch) and not ancestor(c.home, rev(c.home, branch), rev(c.home, main)):
            git(c.home, "merge", "-q", "--no-ff", "-m", f"campaign: journal of {name}", branch)


def journals_at(c: Campaign, repo: Path, commit: str, pid: str) -> dict[str, str]:
    """The quest's and its tasks' journals in a commit: what a landing would carry."""
    folder = f"{c.meta}/journal"
    names = names_at(repo, commit, folder)
    mine = sorted(n for n in names if n == f"{pid}.md" or (n.startswith(f"{pid}--") and n.endswith(".md")))
    return {n[:-3]: file_at(repo, commit, f"{folder}/{n}") for n in mine}


def journal_state(c: Campaign, name: str, branch: str) -> str:
    return state_of(journal_text(c, name, branch, committed=True))


def state_of(journal: str) -> str:
    m = re.search(r"^State:\s*(.+)$", journal, re.M)
    return m.group(1).strip() if m else "(no State line)" if journal.strip() else "(no journal)"


def decisions(c: Campaign) -> set[str]:
    """The ids in PLAN.md's Decisions table: what a [decision] criterion's evidence names."""
    found: set[str] = shared(c.home / c.meta / "PLAN.md", plan_decisions) or set()
    return found


def plan_decisions(plan: Path) -> set[str]:
    rows = section(plan.read_text(), "Decisions").splitlines()
    ids = (table_cells(r)[0] for r in rows if r.startswith("|"))
    return {x for x in ids if x and x.lower() != "id" and not set(x) <= set("-: ")}


def id_ranges(ids: list[str]) -> str:
    """A1, A2, A3, A3b, A5 as "A1-A3, A3b, A5": consecutive plain ids as a range, a suffixed one alone."""

    def key(x: str) -> tuple[int, str]:
        m = re.fullmatch(r"A(\d+)([a-z]?)", x)
        return (int(m.group(1)), m.group(2)) if m else (1 << 30, x)

    runs: list[list[str]] = []
    for x in sorted(ids, key=key):
        n, suffix = key(x)
        last = key(runs[-1][-1]) if runs else (-2, "-")
        if not suffix and not last[1] and n == last[0] + 1:
            runs[-1].append(x)
        else:
            runs.append([x])
    return ", ".join(r[0] if len(r) == 1 else f"{r[0]}-{r[-1]}" for r in runs)


def next_step(journal: str) -> str:
    """The first step in a journal's Next list."""
    m = re.search(r"^\s*(?:\d+\.|[-*])\s+(.+)$", section(journal, "Next"), re.M)
    step = m.group(1).strip() if m else ""
    return "" if step.startswith("<") else step


def checkouts(c: Campaign, name: str, rel: str) -> list[Path | None]:
    """A quest's or task's code and journal worktrees, None for one it has not."""
    return list(dict.fromkeys([(c.home / rel).resolve() if rel else None, journal_place(c, name)]))


def plan_milestones(c: Campaign) -> list[Json]:
    """PLAN.md's Milestones table in order: each milestone's name, quests and validation."""
    plan = c.home / c.meta / "PLAN.md"
    rows: list[Json] = []
    for line in section(plan.read_text() if plan.exists() else "", "Milestones").splitlines():
        cells = table_cells(line)
        if len(cells) < 2 or not cells[0] or cells[0].startswith("<") or set(cells[0]) <= set("-: "):
            continue
        if cells[0].lower() == "milestone":
            continue
        ids = [x for x in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", cells[1]) if x in c.quests]
        rows.append({"name": cells[0], "quests": ids, "validation": cells[2] if len(cells) > 2 else ""})
    return rows


MAP_RANK = {"landed": 0, "review": 1, "active": 2, "blocked": 3, "planned": 4}


def milestone_groups(c: Campaign, statuses: dict[str, str]) -> list[tuple[str, str, list[str]]]:
    """(name, validation, quest ids) per milestone among the quests in STATUSES: PLAN.md's rows in
    order, then the other milestones quests name; a row's quests first, then the quests naming it, by
    status. Dropped quests are in none."""
    named = plan_milestones(c)
    rows = {m["name"]: m for m in named}
    names = [m["name"] for m in named] + sorted({c.quests[x].get("milestone", "") for x in statuses} - set(rows) - {""})
    out: list[tuple[str, str, list[str]]] = []
    for name in names:
        row = rows.get(name, {"quests": [], "validation": ""})
        ids = [x for x in row["quests"] if x in statuses] + sorted(
            (x for x in statuses if c.quests[x].get("milestone", "") == name and x not in row["quests"]),
            key=lambda x: (MAP_RANK.get(statuses[x], 5), x),
        )
        out.append((name, row["validation"], [x for x in dict.fromkeys(ids) if statuses[x] != "dropped"]))
    return out
