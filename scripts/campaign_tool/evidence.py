"""Criteria and their evidence: the quest file's criteria, journal rows, tagged jobs and checks; and
when evidence runs, the one State decision (`iterating`)."""

from __future__ import annotations

import contextlib
import fnmatch
import functools
import json
import os
import re
from collections.abc import Callable, Collection
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .ci import ci_covers, ci_gate, ci_job
from .core import CI_CHECK, CI_CITE, FAILED, CampaignError, Json, csv, die, git, jobs_dir, section, table_cells
from .jobs import FAILS_WITHOUT, NO_RUN, all_jobs, derived, load_job
from .journals import decisions, state_of
from .repo import (
    SHA,
    ancestor,
    file_at,
    has_ref,
    main_branch,
    merge_base,
    on_commits,
    read_object,
    rev,
    same_repo,
)
from .reuse import descends, docs_note, job_at, job_for_check, shape
from .state import gates

if TYPE_CHECKING:  # annotations only: they import this module, so importing them here is a cycle
    from .campaign import Campaign


ROUND_END = re.compile(r"(?:ready for review|waiting on jobs)\b", re.I)


def ready(state: str) -> bool:
    """A journal State that ends a round, the table's two: "ready for review" or "waiting on jobs",
    each with any detail after it."""
    return bool(ROUND_END.match(state))


State = tuple[int, str, str]  # a journal commit's time, its State, and the commit
_states: dict[tuple[str, ...], tuple[str, list[State], str]] = {}


def journal_states(c: Campaign, p: Json, name: str, branch: str) -> tuple[str, list[State], str]:
    """The head's code commit; the State (the journal's first State line) at each journal commit at or
    after it, newest first, with its commit time; and the State the code found. In one repository
    "after" is by ancestry, across two (a quest in another repository) by commit time. Read once per
    command and head."""
    repo, path = c.repo(p), f"{c.meta}/journal/{name}.md"
    if not (branch and has_ref(repo, branch) and has_ref(c.home, branch)):
        return "", [], ""  # a quest not started, or its branch gone: no State
    key = (str(repo), str(c.home), path, rev(repo, branch), rev(c.home, branch))
    if key not in _states:
        _states[key] = read_states(c.home, repo, path, key[3], key[4])
    return _states[key]


def read_states(home: Path, repo: Path, path: str, head: str, journal: str) -> tuple[str, list[State], str]:
    """`journal_states` at the code's HEAD and the journal's JOURNAL commit. In one repository it walks
    only the commits since the code; across two, the journal's history back to the code's time."""
    code = git(repo, "log", "-1", "--first-parent", "--format=%H %ct", head, "--", ".", ":(exclude).campaign")
    k, k_at = code.split() if code else ("", "0")
    mine = same_repo(repo, home)
    base = f"{k}^" if mine and k and has_ref(home, f"{k}^") else ""  # the code's parent bounds the walk
    after: list[State] = []
    for line in git(home, "log", "--format=%H %ct", f"{base}..{journal}" if base else journal, "--", path).splitlines():
        commit, at = line.split()
        shown = git(home, "show", f"{commit}:{path}", check=False)
        if k and (ancestor(repo, commit, k) and commit != k if mine else int(at) < int(k_at)):
            return k, after, state_of(shown)
        after.append((int(at), state_of(shown), commit))
    return k, after, state_of(git(home, "show", f"{base}:{path}", check=False) if base else "")


def set_at_head(states: list[State], found: str) -> bool:
    """Whether the State was set at the head: a commit at or after the head's code changed its value
    (`journal_states`' STATES and FOUND)."""
    return bool(states) and any(s != states[0][1] for s in [found, *(x[1] for x in states)])


READY = "ready for review"  # the journal State that ends a round: its evidence runs then


def journal_of(p: Json, pid: str, task: str = "") -> tuple[str, str]:
    """The journal and branch a quest's (or task's) evidence answers to: a live task's own, else the
    quest's (a merged or dropped task's jobs answer to the quest)."""
    t = p.get("tasks", {}).get(task) if task else None
    if t and not t.get("merged") and t.get("status") != "dropped":
        return f"{pid}--{task}", t["branch"]
    return pid, p.get("branch", "")


def committed_state(c: Campaign, pid: str, task: str = "") -> str:
    """The journal State at the head, as committed (what `iterating` reads)."""
    p = c.quests[pid]
    _, states, found = journal_states(c, p, *journal_of(p, pid, task))
    return states[0][1] if states else found


def iterating(c: Campaign, pid: str, task: str = "", *, verify: bool = False) -> str:
    """The one State decision (references/host.md, "When evidence runs"): why a quest's (or task's)
    round is open, so its evidence and gate checks hold; "" when they queue: a landed quest, a tasked
    quest's `verify` once every task merged or was dropped, or a committed State that ends a round
    (`ready`) set at the head (a commit at or after the head's code changed it). The hold, the to-do,
    and the hints of `status` and `land` all ask it."""
    if os.environ.get("CAMPAIGN_EVIDENCE_HOLD") == "0":
        return ""
    p = c.quests[pid]
    tasks = p.get("tasks", {})
    if p.get("status") == "landed":
        return ""
    if verify and tasks and all(t.get("merged") or t.get("status") == "dropped" for t in tasks.values()):
        return ""
    code, states, found = journal_states(c, p, *journal_of(p, pid, task))
    state = states[0][1] if states else found
    if not ready(state):
        return f"the journal State is {state!r}" + (f": set State {READY} at {code[:12]}" if code else "")
    return (
        ""
        if set_at_head(states, found)
        else f"State {state!r} was set before the head {code[:12]}: set State at {code[:12]}"
    )


PROOF = ("job", "artifact", "decision", "manual", "ci")  # what a criterion's evidence must be; job by default


def criteria(c: Campaign, pid: str) -> dict[str, Json]:
    """A quest's acceptance criteria (A1, A2, ...) from its quest file: the first line of each and the
    kind of evidence it needs, `- A2 [artifact] ...` (job when untagged)."""
    brief = c.home / c.meta / "quests" / f"{pid}.md"
    text = section(brief.read_text() if brief.exists() else "", CRITERIA_HEADING)
    found: dict[str, Json] = {}
    tags = "|".join(PROOF)
    shape = rf"^\s*[-*]\s*\**(A\d+)\b\**[:.]?\**\s*(?:\[({tags})\]\s*)?(.*)$((?:\n[ \t]+\S.*)*)"
    for cid, kind, line, wrapped in re.findall(shape, text, re.M):
        if not line.startswith("<"):  # the template's unfilled placeholder
            whole = " ".join(f"{line}{wrapped}".split())
            found.setdefault(cid, {"text": line.strip(), "kind": kind or "job", "whole": whole})
    return found


CRITERIA_HEADING = "Acceptance criteria"


def unparsed_criteria(c: Campaign, pid: str, *, filled: bool = True) -> str:
    """Why a quest file's criteria cannot be read, or "": its `## Acceptance criteria` heading is missing
    (the commonest slip is another title, which parses as no criteria and lands unchecked), or, when
    FILLED, the section still holds only the template's placeholder. `None ...` is a deliberate answer."""
    brief = c.home / c.meta / "quests" / f"{pid}.md"
    text = brief.read_text() if brief.exists() else ""
    if not re.search(rf"^## {CRITERIA_HEADING}\s*$", text, re.M):
        other = re.findall(r"^## (.*[Cc]riteri.*?)\s*$", text, re.M)
        seen = f" (it has `## {other[0]}`)" if other else ""
        return f"{pid}'s quest file has no `## {CRITERIA_HEADING}` heading{seen}: its criteria parse as none"
    if (
        filled
        and not criteria(c, pid)
        and re.search(r"^\s*[-*]\s*\**A\d+\b[^\n]*<", section(text, CRITERIA_HEADING), re.M)
    ):
        return f"{pid}'s `## {CRITERIA_HEADING}` still holds the template's placeholder: write its criteria (or `None`)"
    return ""


def proof(c: Campaign, p: Json, commits: list[str]) -> Callable[[str, str], list[str]]:
    """What keeps an Evidence row's text from proving a criterion of a kind, at one of these commits:
    a job criterion needs a job that passed at the code; an artifact one a `path` in the commit; a
    decision one an id from PLAN.md's Decisions table; a manual one only a non-empty row. Every job a row
    names must have passed, whatever its kind, and a `ci:JOB` by the land gate's rule (`ci_named`)."""
    repo = c.repo(p)
    job = job_problem(repo, commits, p)
    recorded = decisions(c)

    def check(kind: str, text: str) -> list[str]:
        cited = list(dict.fromkeys(JOB_ID.findall(text)))
        why = [w for w in map(job, cited) if w]
        for name in dict.fromkeys(CI_CITE.findall(text)):
            state = ci_named(c, p, commits, name.strip())
            if state == "success":
                cited.append(f"ci:{name}")
            else:
                why.append(state)
        for name in dict.fromkeys(x.rstrip(".") for x in BARE_CI.findall(CODE.sub("", text))):
            why.append(f"ci:{name} is read only in backticks: cite it as `ci:{name}`")
        for path, quote in QUOTED.findall(text):
            if commits and not any(quoted(repo, k, path, quote) for k in commits):
                why.append(f'"{quote[:60]}" is not in {path} at {commits[0][:12]}')
        bare = QUOTE.sub("", text)  # what a quote says is never a path
        if kind == "job" and not cited:
            why.append(
                "names no job (a job criterion needs a job id or a `ci:JOB`; tag it [manual] or [artifact] otherwise)"
            )
        elif kind == "artifact":
            paths = re.findall(r"`([^`\s]+)`", bare)
            missing = [x for x in paths if not any(read_object(repo, f"{k}:{x}") for k in commits)]
            why += [f"{x} is not in {commits[0][:12]}" for x in missing] if commits else []
            words = [w.rstrip(".,;:") for w in re.findall(r"[\w./-]*[/.][\w./-]+", bare)]
            if not paths and not any(read_object(repo, f"{k}:{w}") for w in words for k in commits):
                why.append("names no file (an artifact criterion names a path in the repository)")
        elif kind == "decision" and not set(re.findall(r"[\w.-]+", text)) & recorded:
            why.append("names no decision in PLAN.md's Decisions table")
        return why

    return check


BARE_CI = re.compile(r"(?<![\w/])ci:([\w.+-]+)")  # a `ci:JOB` citation written without its backticks
CODE = re.compile(r"`[^`]*`")
UNMET = re.compile(r"<|(pending|queued|running|failed|missing|open|todo|none)\b", re.I)
QUOTE = re.compile(r'"[^"]+"')
QUOTED = re.compile(r'`([^`\s]+)`:?\s*"([^"]+)"')  # `path`: "text the file holds"
_quotes: dict[tuple[str, str, str], str] = {}


def quoted(repo: Path, commit: str, path: str, quote: str) -> bool:
    """Whether PATH at COMMIT holds QUOTE, whitespace aside: evidence that cites what a record says,
    not only that the record exists."""
    key = (str(repo), commit, path)
    if key not in _quotes or not SHA.fullmatch(commit):
        _quotes[key] = " ".join(file_at(repo, commit, path).split())
    return " ".join(quote.split()) in _quotes[key]


JOB_ID = re.compile(r"\b\d{6}-\d{6}-[0-9a-f]{6}\b")


def job_problem(repo: Path | None, commits: list[str], quest: Json | None = None) -> Callable[[str], str]:
    """What is wrong with a job as evidence; "" when it, or the same job rerun, passed at the code of
    one of these commits in REPO (`campaign job rerun` makes that one command)."""

    def problem(jid: str) -> str:
        d = jobs_dir() / jid
        if not (d / "spec.json").exists():
            return f"{jid} is not a job on this machine"
        job = load_job(d)
        state = job["state"].get("state")
        if state != "done":
            return f"{jid} {state or 'has no result'}"
        if not job["spec"].get("commit"):
            return f"{jid} is unversioned"
        if repo is not None and not same_repo(Path(job["spec"]["repo"]), repo):
            return f"{jid} ran in {job['spec']['repo']}, not the quest's repository"
        if job["spec"].get("fails_without"):
            held = fails_without_met(job) and mutant_holds(job, commits, quest)
            return "" if held else stale_mutant(job, commits, quest)
        for commit in commits:
            with contextlib.suppress(CampaignError, KeyError):
                twin = job_at(job["spec"], commit, merged=True)
                if twin and twin["state"].get("state") == "done":
                    return ""
        return f"{jid} ran other code"

    return problem


def ci_named(c: Campaign, p: Json, commits: list[str], name: str) -> str:
    """ "success" when CI job NAME passed at one of COMMITS by the land gate's rule (`ci_gate`: its own
    runs, else those of a commit before clean merges from main); else why not, at the first."""
    repo = c.repo(p)
    first = "no commit"
    for i, k in enumerate(commits):
        state = ci_gate(c, p, k, lambda x: ci_job(repo, x, name)).state
        if state == "success":
            return state
        first = state if i == 0 else first
    return first


def rerun_hint(_repo: Path, jids: Collection[str]) -> str:
    """The command that reruns these jobs at the quest's head, where they count. A fails-without job is
    never among them: its result holds until the code its mutation reverts changes (mutant_moved)."""
    return f"`campaign job rerun {' '.join(jids)}`"


HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+")
WHOLE = [[0, 1 << 62]]  # a file changed with no line ranges (binary, a mode): all of it


def hunks(repo: Path, a: str, b: str) -> dict[str, list[list[int]]]:
    """Each file B changes from A, with the line ranges [first, end) of A that it changes (`diff -U0`)."""

    def ask() -> dict[str, list[list[int]]]:
        out: dict[str, list[list[int]]] = {}
        path = ""
        for line in git(repo, "diff", "-U0", "--no-color", "--no-renames", "--no-ext-diff", a, b).splitlines():
            if line.startswith("diff --git a/"):
                path = line[len("diff --git a/") :].rpartition(" b/")[0]
                out[path] = []
            elif path and (m := HUNK.match(line)):
                start = int(m[1])
                out[path].append([start, start + max(int(m[2] if m[2] is not None else 1), 1)])
        return out

    found: dict[str, list[list[int]]] = on_commits(("hunks", str(repo), a, b), ask)
    return found


MUTATES = re.compile(r"\bsed\b[^|;&]*\s-i|\bperl\b[^|;&]*\s-\w*i|\bpatch\b|\bgit\s+apply\b")  # an edit in a command


def main_code(repo: Path, commit: str, head: str, quest: Json | None) -> bool:
    """Whether COMMIT is main's code, not the quest's: on main and off HEAD's history, at or before the
    quest's base, or off its own first-parent line since (merged from main). An increment leaves the
    quest's own commits its own. Without the quest, whatever main's history holds."""
    main = main_branch(repo)
    on_main = has_ref(repo, main) and ancestor(repo, commit, rev(repo, main))
    if not ancestor(repo, commit, head):
        return on_main
    base = (quest or {}).get("base", "")
    if not base:
        return on_main and ancestor(repo, commit, merge_base(repo, rev(repo, main), head))
    if ancestor(repo, commit, base):
        return True
    line = on_commits(
        ("first-parent", str(repo), base, head), lambda: git(repo, "rev-list", "--first-parent", f"{base}..{head}")
    )
    return commit not in line.split()


def differ(repo: Path, a: str, b: str) -> list[str]:
    """Paths that differ between the trees of A and B, a rename as its two paths."""
    return list(
        on_commits(
            ("differ", str(repo), a, b),
            lambda: [x for x in git(repo, "diff", "--name-only", "--no-renames", a, b).splitlines() if x],
        )
    )


def set_moved(repo: Path, since: str, head: str, quest: Json | None) -> list[str]:
    """The quest's files (its file set, else any code) that changed from SINCE to HEAD."""
    files = (quest or {}).get("files") or []
    moved = [x for x in differ(repo, since, head) if not x.startswith(".campaign/")]
    return [x for x in moved if not files or any(fnmatch.fnmatch(x, g) for g in files)]


@functools.cache
def ran_at(repo: Path, head: str, at: int) -> str:
    """The commit on HEAD's first-parent line that was the head at time AT (its newest commit by then)."""
    return git(repo, "rev-list", "-1", "--first-parent", f"--before={at}", head) if at else ""


DOTTED = re.compile(r"(?<![\w./-])[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+(?![\w/-])")  # a dotted id: test_x.Case.test_y


def module_named(path: str, cmd: str) -> bool:
    """Whether CMD names the Python file PATH by its dotted module id (`run.py test_x.Case.test_y`,
    `-m unittest tests.test_x`): the id starts with the module's dotted path or a tail of it."""
    if not path.endswith(".py"):
        return False
    parts = path[:-3].split("/")
    tails = {".".join(parts[i:]) for i in range(len(parts))}
    ids = [x for x in DOTTED.findall(cmd) if not x.endswith(".py")]  # a file name is no module id
    return any(x == t or x.startswith(f"{t}.") for x in ids for t in tails)


def own_line(repo: Path, spec: Json, quest: Json | None, head: str) -> str:
    """The head of the line a job ran on: its task's branch while it lives, the head the task merged
    at after; HEAD for a quest's job."""
    t = (quest or {}).get("tasks", {}).get(spec.get("task") or "")
    if not t:
        return head
    if t.get("branch") and has_ref(repo, t["branch"]):
        return rev(repo, t["branch"])
    return t.get("merged") or head


def mutant_moved(spec: Json, head: str, quest: Json | None = None) -> list[str]:
    """Where a fails-without job's evidence changed by HEAD: [] while its result holds.
    - A mutant or revert commit (off HEAD's history) reverts the lines its diff from its parent
      changes; the files its command names (the test it ran) count whole, a rename as a change.
    - One whose change was the head's when it ran (the new test on its base, cut from the line it ran
      on: the quest's, or its task's, `own_line`), or a run at main's code with the quest's code from a
      ref, moves with any change to the quest's file set since it ran. One cut from another line is a
      revert.
    - A run at the quest's own commit mutates, in its command, the files it names; a mutation the
      command does not show (a script, a binary) moves with any change to the quest's file set.
    - Any other run at main's code lacks the whole fix, which the head's own runs test."""
    repo, commit, cmd, cwd = Path(spec["repo"]), spec["commit"], spec["cmd"], spec.get("cwd") or ""

    def named(paths: Collection[str]) -> list[str]:
        return [x for x in paths if x in cmd or (cwd and os.path.relpath(x, cwd) in cmd) or module_named(x, cmd)]

    if main_code(repo, commit, head, quest):
        cuts = [r["commit"] for r in spec.get("refs", {}).values() if same_repo(Path(r["repo"]), repo)]
        return [x for k in cuts for x in set_moved(repo, k, head, quest)]
    if ancestor(repo, commit, head):
        moved = set_moved(repo, commit, head, None)
        if named(moved) or MUTATES.search(cmd):
            return named(moved)
        return set_moved(repo, commit, head, quest)
    parent = git(repo, "rev-parse", f"{commit}^")
    mutation, moved = hunks(repo, parent, commit), hunks(repo, parent, head)
    then = ran_at(repo, own_line(repo, spec, quest, head), int(float(spec.get("submitted") or 0)))
    unchanged = [x for x in mutation if x not in differ(repo, commit, then or head)]
    if unchanged and (not then or ancestor(repo, parent, then)):  # the head's own change, on its base
        return set_moved(repo, then, head, quest) if then else unchanged
    lines = [
        x
        for x, ranges in mutation.items()
        if x in moved and any(a < d and c < b for a, b in ranges or WHOLE for c, d in moved[x] or WHOLE)
    ]
    return lines + [x for x in named(moved) if x not in mutation]


def mutant_holds(job: Json, commits: list[str], quest: Json | None = None) -> bool:
    """Whether a fails-without job's result holds at one of COMMITS (mutant_moved finds nothing)."""
    for k in commits:
        with contextlib.suppress(CampaignError, KeyError):
            if not mutant_moved(job["spec"], k, quest):
                return True
    return False


def stale_mutant(job: Json, commits: list[str], quest: Json | None = None) -> str:
    """Why a fails-without job is no evidence at COMMITS: it never failed without the fix, or the code
    its mutation reverts, or the test it ran, changed since."""
    if not fails_without_met(job):
        return fails_without_why(job)
    moved: list[str] = []
    with contextlib.suppress(CampaignError, KeyError):
        moved = mutant_moved(job["spec"], commits[0], quest) if commits else []
    where = f" ({', '.join(moved[:4])})" if moved else ""
    at = f" at {commits[0][:12]}" if commits else ""
    return f"{job['id']}: the code its mutation reverts, or its test, changed{where}: submit a mutant of the code{at}"


def evidence(
    journals: dict[str, str], wanted: dict[str, Json], check: Callable[[str, str], list[str]] | None = None
) -> dict[str, list[Json]]:
    """Each criterion's rows in the journals' Evidence tables (the quest's and its tasks'): which
    journal, what it cites, and whether that is evidence: a row whose other cells are not empty, a
    <placeholder>, pending or failed, and that `check` finds proves its criterion's kind."""
    rows: dict[str, list[Json]] = {x: [] for x in wanted}
    for name, text in journals.items():
        for row in section(text, "Evidence").splitlines():
            cells = table_cells(row)
            if len(cells) < 2 or cells[0].startswith("<"):
                continue
            rest = [x for x in cells[1:] if x]
            met = bool(rest) and not any(UNMET.match(x) or set(x) <= set("-: ") for x in rest)
            for cid in re.findall(r"\bA\d+\b", cells[0]):
                if cid in rows:
                    why = check(wanted[cid]["kind"], " ".join(rest)) if met and check else []
                    rows[cid].append(
                        {"journal": name, "evidence": " | ".join(rest), "met": met and not why, "why": why}
                    )
    return rows


def cited_in_flight(journals: dict[str, str], wanted: Collection[str]) -> dict[str, Json]:
    """For each criterion whose Evidence rows cite a job still queued or running, a row that keeps it
    unmet: its proof is not in yet, whatever else proves it."""
    out: dict[str, Json] = {}
    for name, text in journals.items():
        for row in section(text, "Evidence").splitlines():
            cells = table_cells(row)
            if len(cells) < 2:
                continue
            for jid in dict.fromkeys(JOB_ID.findall(" ".join(cells[1:]))):
                d = jobs_dir() / jid
                state = load_job(d)["state"].get("state") if (d / "spec.json").exists() else None
                if state not in ("queued", "running"):
                    continue
                for cid in re.findall(r"\bA\d+\b", cells[0]):
                    if cid in wanted:
                        flight = out.setdefault(cid, {"journal": name, "evidence": "", "met": False, "why": []})
                        flight["why"].append(f"{jid} {state}: land waits for its result")
    return out


def stray_rows(journals: dict[str, str], wanted: Collection[str]) -> list[str]:
    """Criterion rows (a table row whose first cell names a criterion) under another heading than
    Evidence, where landing never reads them."""
    found: list[str] = []
    for name, text in journals.items():
        heading = ""
        for line in text.splitlines():
            if line.startswith("## "):
                heading = line[3:].strip()
            elif heading != "Evidence" and line.lstrip().startswith("|"):
                ids = [x for x in re.findall(r"\bA\d+\b", table_cells(line)[0]) if x in wanted]
                if ids:
                    where = f"under `## {heading}`" if heading else "above every heading"
                    found.append(
                        f"{name}'s journal has a row for {', '.join(ids)} {where}: landing reads only `## Evidence`"
                    )
    return found


def evidence_shape(spec: Json) -> str:
    """A job up to its code: what a tag names, so each run of the same job at later code counts."""
    refs = sorted((name, r.get("repo")) for name, r in spec.get("refs", {}).items())
    fields = [spec.get(k) for k in ("repo", "cmd", "cwd", "level", "inputs")]
    build = [spec["build"]] if spec.get("build") else []
    return json.dumps([*fields, refs, sorted(spec.get("env", {}).items()), *build])


def tagged(c: Campaign, pid: str, jobs: list[Json]) -> dict[str, Json]:
    """Each criterion the quest's jobs are tagged for (`job submit --for`): the newest job of each
    tagged command ("runs"), and the jobs tagged as failing without the fix ("fails")."""

    def index(jobs: list[Json]) -> dict[tuple[Any, Any], list[tuple[Json, Json]]]:
        tags: dict[tuple[Any, Any], list[tuple[Json, Json]]] = {}
        for j in sorted(jobs, key=lambda j: j["spec"].get("submitted", 0)):
            for t in j.get("tags", []):
                tags.setdefault((t.get("campaign"), t.get("quest")), []).append((j, t))
        return tags

    out: dict[str, Json] = {}
    for j, t in derived(jobs, "tags", index).get((c.ident, pid), []):
        for cid in t.get("for", []):
            entry = out.setdefault(cid, {"runs": {}, "fails": []})
            if t.get("fails_without"):
                entry["fails"].append(j)
            else:
                entry["runs"][evidence_shape(j["spec"])] = j
    return out


NAMED_CHECK = re.compile(r"`([\w.:-]+)`\s+check\b")  # "the `unit` check" in a criterion's text


def named_check(c: Campaign, p: Json, text: str, commits: list[str]) -> Json | None:
    """The evidence of the one gate check a criterion's text names ("the `unit` check"): its job or CI
    job passed at one of COMMITS' code. None when the text names no check."""
    names = list(dict.fromkeys(NAMED_CHECK.findall(text)))
    if not names:
        return None
    checks = {x["name"]: x for x in gates(p)}
    if len(names) > 1 or names[0] not in checks:
        why = f"names {', '.join(names)}: a criterion names one gate check of its quest ({', '.join(checks)})"
        return {"journal": "checks", "evidence": "", "met": False, "why": [why]}
    check, repo = checks[names[0]], c.repo(p)
    for k in commits:
        if check["cmd"].startswith(CI_CHECK):
            if ci_named(c, p, [k], check["cmd"][len(CI_CHECK) :].strip()) == "success":
                return {"journal": "checks", "evidence": check["cmd"], "met": True, "why": []}
            continue
        job = job_for_check(repo, k, check, c.state.get("tested_docs", []), ci=ci_covers(c), files=p["files"])
        if job and job["state"].get("state") == "done":
            return {"journal": "checks", "evidence": f"{job['id']} (the {names[0]} check)", "met": True, "why": []}
    at = f" at {commits[0][:12]}" if commits else ""
    return {"journal": "checks", "evidence": "", "met": False, "why": [f"the {names[0]} check has not passed{at}"]}


def fails_without_met(j: Json) -> bool:
    """Whether a job tagged --fails-without shows the test failing where the fix is missing, by the
    rule of its own record: a current job's command failed (not as 124, 126 or 127); an older job's
    self-inverted command exited 0."""
    st = j["state"]
    if st.get("state") != "done":
        return False
    return j["spec"].get("fails_without", 0) < FAILS_WITHOUT or st.get("exit") not in (0, 124, *NO_RUN)


def fails_without_why(j: Json) -> str:
    st, v = j["state"], j["spec"].get("fails_without", 0)
    state = st.get("state")
    if state == "failed" and v < FAILS_WITHOUT:
        return f"{j['id']} exited {st.get('exit')}: its command had to exit 0 where the fix is missing"
    if state in ("done", "failed") and v >= FAILS_WITHOUT:
        return f"{j['id']} {state}: {st.get('note') or 'it did not fail without the fix'}"
    return f"{j['id']} {state}: no result yet" if state in ("queued", "running") else f"{j['id']} {state}"


def tagged_row(
    repo: Path, commits: list[str], entry: Json, named: Json | None = None, quest: Json | None = None
) -> Json:
    """A tagged criterion's evidence: a run of one of its tagged jobs, or the check its text names,
    that passed at one of COMMITS' code (across merges from main), and, when any is tagged, a
    fails-without job that exited 0."""
    passed: list[str] = [named["evidence"]] if named and named["met"] else []
    why: list[str] = []
    for j in entry["runs"].values():
        mine = same_repo(Path(j["spec"].get("repo") or "/"), repo)
        twin = None
        for k in commits if mine else []:
            with contextlib.suppress(CampaignError, KeyError):
                found = job_at(j["spec"], k, merged=True)
                twin = twin or (found if found and found["state"].get("state") == "done" else None)
        state = j["state"].get("state")
        if twin is not None:
            passed.append(twin["id"] + (f" ({docs_note(twin)})" if docs_note(twin) else ""))
        elif not mine:
            why.append(f"{j['id']} ran in {j['spec'].get('repo')}, not the quest's repository")
        else:
            why.append(f"{j['id']} ran other code" if state == "done" else f"{j['id']} {state}")
    fails = [j["id"] for j in entry["fails"] if fails_without_met(j) and mutant_holds(j, commits, quest)]
    if entry["fails"] and not fails:
        why += [stale_mutant(j, commits, quest) for j in entry["fails"]]
    text = ", ".join(passed) + (f"; fails without: {', '.join(fails)}" if fails else "")
    met = bool(passed) and (bool(fails) or not entry["fails"])
    return {"journal": "jobs", "evidence": text or "none passed", "met": met, "why": [] if met else why}


def ci_row(c: Campaign, p: Json, commits: list[str], ci: bool) -> Json:
    """A [ci] criterion's evidence: the land gate's CI check (`ci_gate`), green at one of COMMITS, or
    waived by `land --no-ci` (CI false)."""
    if not ci:
        return {"journal": "ci", "evidence": "CI waived by --no-ci", "met": True, "why": []}
    if not ci_covers(c):
        return {"journal": "ci", "evidence": "", "met": False, "why": ["the campaign runs no CI (`config ci=github`)"]}
    why = ""
    for k in commits:
        why, at, carried = ci_gate(c, p, k)
        if why == "success":
            via = f" (carried to {k[:12]} across clean merges)" if carried else ""
            return {"journal": "ci", "evidence": f"CI green at {at[:12]}{via}", "met": True, "why": []}
    return {
        "journal": "ci",
        "evidence": "",
        "met": False,
        "why": [f"CI at {commits[0][:12]}: {why}" if commits else "no commit"],
    }


def criteria_evidence(
    c: Campaign,
    pid: str,
    commits: list[str],
    journals: dict[str, str],
    jobs: list[Json] | None = None,
    *,
    ci: bool = True,
) -> dict[str, list[Json]]:
    """Each criterion's evidence at one of COMMITS: the jobs tagged for it or the gate check its text
    names, CI for a [ci] one (waived when not CI), else its rows in the journals' Evidence tables
    (journals written before tags, and the other kinds). A row citing a job still queued or running
    keeps its criterion unmet (`cited_in_flight`)."""
    wanted = criteria(c, pid)
    p = c.quests[pid]
    repo = c.repo(p)
    tags = {
        x: t
        for x, t in tagged(c, pid, all_jobs() if jobs is None else jobs).items()
        if wanted.get(x, {}).get("kind") == "job"
    }
    rows = evidence(
        journals, {x: w for x, w in wanted.items() if x not in tags and w["kind"] != "ci"}, proof(c, p, commits)
    )
    named = {x: named_check(c, p, w["whole"], commits) for x, w in wanted.items() if w["kind"] == "job"}
    ran = ci_row(c, p, commits, ci) if any(w["kind"] == "ci" for w in wanted.values()) else {}
    found = {
        x: [tagged_row(repo, commits, tags[x], named.get(x), p)]
        if x in tags
        else [ran]
        if w["kind"] == "ci"
        else [n for n in [named.get(x)] if n] + rows[x]
        for x, w in wanted.items()
    }
    for x, flight in cited_in_flight(journals, wanted).items():
        found[x] = [flight, *({**r, "met": False} for r in found[x])]
    return found


def unmet_why(rows: dict[str, list[Json]], unproven: Collection[str]) -> str:
    """What keeps each unproven criterion unmet, for the landing gap that lists them: the jobs and
    paths its rows name and why, or that nothing it cites proves it."""
    out = []
    for cid in unproven:
        why = list(dict.fromkeys(w for r in rows.get(cid, []) for w in r["why"])) or ["nothing cited proves it"]
        out.append(f"{cid}: {', '.join(why)}")
    return "; ".join(out)


def evidence_table(rows: dict[str, list[Json]]) -> list[str]:
    """The journal's Evidence table, generated: what proves each criterion, or what it lacks."""
    out = ["| Criterion | Evidence |", "|---|---|"]
    for cid, found in rows.items():
        met = [r["evidence"] for r in found if r["met"]]
        lacks = "; ".join(w for r in found for w in r["why"]) or "no evidence yet"
        out.append(f"| {cid} | {'; '.join(met) if met else f'<{lacks}>'} |")
    return out


def criteria_for(c: Campaign, pid: str, given: str) -> list[str]:
    """--for IDS, checked against the quest file: a job proves only job criteria."""
    wanted = criteria(c, pid)
    ids = csv(given)
    if ids and not wanted and (why := unparsed_criteria(c, pid)):
        die(f"--for {given}: {why}")
    for cid in ids:
        if cid not in wanted:
            die(f"--for {cid}: {pid}'s quest file has no criterion {cid} (it has {', '.join(wanted) or 'none'})")
        if wanted[cid]["kind"] == "ci":
            die(f"--for {cid}: a [ci] criterion is met by CI at the landed commit, not by a job")
        if wanted[cid]["kind"] != "job":
            die(f"--for {cid}: an [{wanted[cid]['kind']}] criterion is proven in the journal, not by a job")
    return ids


def replaced_citations(journals: dict[str, str], jobs: list[Json]) -> list[Json]:
    """Evidence rows citing a job a newer one of its shape replaced: at its code or a descendant,
    submitted later, ended, and cited by no journal of the quest. The row should name that result."""
    by_id = {j["id"]: j for j in jobs}
    shapes = {j["id"]: shape(j["spec"]) for j in jobs}
    cited = {x for text in journals.values() for x in JOB_ID.findall(section(text, "Evidence"))}
    out: list[Json] = []
    for name, text in journals.items():
        for row in section(text, "Evidence").splitlines():
            cells = table_cells(row)
            if len(cells) < 2 or cells[0].startswith("<"):
                continue
            for jid in dict.fromkeys(JOB_ID.findall(" ".join(cells[1:]))):
                old = by_id.get(jid)
                if old is None:
                    continue
                newer = [
                    j
                    for j in jobs
                    if shapes[j["id"]] == shapes[jid]
                    and j["spec"].get("submitted", 0) > old["spec"].get("submitted", 0)
                    and j["state"].get("state") in ("done", *FAILED)
                    and descends(old["spec"], j["spec"])
                ]
                if newer and not cited & {j["id"] for j in newer}:
                    n = max(newer, key=lambda j: j["spec"].get("submitted", 0))
                    out.append(
                        {
                            "journal": name,
                            "criteria": cells[0],
                            "job": jid,
                            "by": n["id"],
                            "state": n["state"].get("state"),
                            "commit": str(n["spec"].get("commit", ""))[:12],
                        }
                    )
    return out
