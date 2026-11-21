"""GitHub CI: the runs and jobs at a commit, asked in the background and read once per command."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from .core import CI_CHECK, Json, git, jobs_dir, pool
from .jobs import job_summary
from .journals import checkouts
from .repo import _heads, ancestor, changed, has_ref, is_meta, main_branch, on_commits, rev, warm_status
from .reuse import CARRY_DEPTH, doc_path, from_main, job_for_check
from .state import branches, gates

if TYPE_CHECKING:  # imported when used: concurrent.futures imports logging, a sixth of a command's start
    from concurrent.futures import Future

if TYPE_CHECKING:  # annotations only: they import this module, so importing them here is a cycle
    from .campaign import Campaign


_gh: dict[str, Future[tuple[int, Any]]] = {}
_gh_lock = threading.Lock()


def gh_start(repo: Path, args: list[str], final: Callable[[Any], bool]) -> Future[tuple[int, Any]]:
    """`gh ARGS` (JSON out) in REPO, started in the background, or found started or answered:
    (returncode, data or stderr). An answer FINAL says cannot change (a run that passed) is kept on
    disk for the host; any other is asked once per command."""
    key = hashlib.sha256(json.dumps([str(repo), args]).encode()).hexdigest()[:32]
    with _gh_lock:
        if key not in _gh:
            path = jobs_dir() / ".gh" / f"{key}.json"
            hit: Json = {}
            with contextlib.suppress(OSError, ValueError):
                hit = json.loads(path.read_text())
            if hit.get("final"):
                from concurrent.futures import Future  # noqa: PLC0415

                _gh[key] = Future()
                _gh[key].set_result((0, hit["data"]))
            else:
                _gh[key] = pool().submit(gh_ask, repo, args, final, path)
        return _gh[key]


def gh_ask(repo: Path, args: list[str], final: Callable[[Any], bool], path: Path) -> tuple[int, Any]:
    p = subprocess.run(["gh", *args], cwd=repo, capture_output=True, text=True)
    if p.returncode != 0:
        return p.returncode, p.stderr.strip()
    data = json.loads(p.stdout or "null")
    if final(data):
        path.parent.mkdir(exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"final": True, "data": data}))
        tmp.replace(path)
    return 0, data


def gh_json(repo: Path, args: list[str], final: Callable[[Any], bool]) -> tuple[int, Any]:
    return gh_start(repo, args, final).result()


def ci_runs(commit: str) -> list[str]:
    """The `gh` arguments listing the CI runs at COMMIT: one query serves every question about them."""
    return ["run", "list", "--commit", commit, "--json", "databaseId,status,conclusion,name", "--limit", "50"]


def newest(runs: Any) -> list[Json]:
    """Each workflow's newest run at a commit (the highest id: a rerun or a dispatch after a red run
    speaks), in listed order."""
    out: dict[str, Json] = {}
    for run in runs if isinstance(runs, list) else []:
        name = str(run.get("name"))
        if name not in out or run.get("databaseId", 0) > out[name].get("databaseId", 0):
            out[name] = run
    return list(out.values())


def all_passed(items: Any) -> bool:
    """Finished and green: final, while a failure can still be re-run."""
    ok = ("success", "skipped", "neutral")
    return bool(items) and all(x.get("status") == "completed" and x.get("conclusion") in ok for x in items)


def ci_commit(repo: Path, commit: str) -> str:
    """The commit whose CI speaks for COMMIT: itself, or, when it has no runs, its nearest
    first-parent ancestor that differs only in campaign files or documentation, which CI workflows
    skip (`paths-ignore`). Their runs are asked for all at once."""
    asked = [(k, gh_start(repo, ci_runs(k), all_passed)) for k in ci_candidates(repo, commit)]
    for k, runs in asked:
        code, listed = runs.result()
        if code == 0 and listed:
            return k
    return commit


def ci_candidates(repo: Path, commit: str) -> list[str]:
    """COMMIT and its nearest first-parent ancestors whose code is COMMIT's: whose CI may speak for it."""
    log = on_commits(
        ("ci-line", str(repo), commit),
        lambda: git(repo, "rev-list", "--first-parent", "--max-count=20", commit, check=False).split(),
    )
    out: list[str] = []
    for k in log:
        if not all(is_meta(x) or doc_path(x, "") for x in changed(repo, k, commit)):
            break
        out.append(k)
    return out


def ci_warm(repo: Path, commit: str, jobs: bool) -> None:
    """Starts asking GitHub what ci_state (and with JOBS, ci_jobs) will ask about COMMIT, so the
    answers for many quests arrive together."""
    if not shutil.which("gh"):
        return

    def views(asked: Future[tuple[int, Any]]) -> None:
        with contextlib.suppress(Exception):  # ci_jobs asks again, and reports what fails
            code, runs = asked.result()
            for run in newest(runs) if code == 0 else []:
                if isinstance(run, dict) and "databaseId" in run:
                    gh_start(repo, run_view(run), jobs_passed)

    for k in ci_candidates(repo, commit):
        asked = gh_start(repo, ci_runs(k), all_passed)
        if jobs:
            asked.add_done_callback(views)


def warm_views(c: Campaign, pids: list[str], brief: bool) -> None:
    """Starts what quest_view will ask other processes about these quests: GitHub about their
    heads and, unless BRIEF, git about their worktrees."""
    for pid in pids:
        p = c.quests[pid]
        repo = c.repo(p)
        if p["status"] not in ("active", "review") or not p.get("branch") or not has_ref(repo, p["branch"]):
            continue
        for name, _, rel, _ in [] if brief else branches(c, pid):
            for place in checkouts(c, name, rel):
                if place is not None and place.exists() and _heads:
                    warm_status(place)
        jobs = any(x["cmd"].startswith(CI_CHECK) for x in gates(p))
        if jobs or c.state.get("ci") == "github":
            ci_warm(repo, rev(repo, p["branch"]), jobs)


def run_view(run: Json) -> list[str]:
    return ["run", "view", str(run["databaseId"]), "--json", "jobs"]


def jobs_passed(view: Any) -> bool:
    return all_passed((view or {}).get("jobs"))


def ci_state(repo: Path, commit: str, later: list[str]) -> str:
    if not shutil.which("gh"):
        return "gh is not installed"
    commit = ci_commit(repo, commit)
    code, listed = gh_json(repo, ci_runs(commit), all_passed)
    if code != 0:
        return f"gh failed: {listed}"
    runs = [r for r in newest(listed) if r.get("name") not in later]
    if not runs:
        return "no CI runs for this commit (push the branch)"
    pending = [r["name"] for r in runs if r.get("status") != "completed"]
    if pending:
        return "running: " + ", ".join(pending)
    bad = [
        f"{r['name']} ({r.get('conclusion')})"
        for r in runs
        if r.get("conclusion") not in ("success", "skipped", "neutral", "cancelled")
    ]
    if bad:
        return "failed: " + ", ".join(bad)
    if gone := [r["name"] for r in runs if r.get("conclusion") == "cancelled"]:
        return f"cancelled, not failed: {', '.join(gone)} (a newer push supersedes it; rerun it if none will)"
    return "success"


def ci_run_ids(repo: Path, commit: str, later: list[str]) -> list[int]:
    """The ids of the gating CI runs (`ci_state`'s) at COMMIT."""
    code, listed = gh_json(repo, ci_runs(ci_commit(repo, commit)), all_passed)
    runs = newest(listed) if code == 0 else []
    return [r["databaseId"] for r in runs if r.get("name") not in later and "databaseId" in r]


class CiGate(NamedTuple):
    state: str  # ci_state's answer
    commit: str  # the commit whose runs gave it
    carried: bool  # an ancestor's, across clean merges from main (`ci_carried`)


def clean_merge(repo: Path, commit: str) -> bool:
    """Whether COMMIT is a two-parent merge git completed alone: no conflict, even a resolved one, and
    no edit beside it (its tree is the one `git merge-tree` writes for its parents)."""

    def ask() -> bool:
        parents = git(repo, "rev-parse", f"{commit}^@").split()
        if len(parents) != 2:
            return False
        merged = subprocess.run(
            ["git", "-C", str(repo), "merge-tree", "--write-tree", *parents], capture_output=True, text=True
        )
        return merged.returncode == 0 and merged.stdout.split()[:1] == [git(repo, "rev-parse", f"{commit}^{{tree}}")]

    clean: bool = on_commits(("clean-merge", str(repo), commit), ask)
    return clean


def ci_carried(c: Campaign, p: Json, commit: str) -> list[str]:
    """The quest's own commits before merges from main whose CI speaks for COMMIT, nearest first: its
    first-parent ancestors across clean merges from main (`clean_merge`) and documentation-only
    commits, once at least one such merge is crossed."""
    repo = c.repo(p)
    main_head = rev(repo, main_branch(repo))
    line = on_commits(
        ("first-parents", str(repo), commit),
        lambda: git(repo, "rev-list", "--first-parent", f"--max-count={CARRY_DEPTH + 1}", commit).split(),
    )
    out: list[str] = []
    merged = False
    for newer, older in zip(line, line[1:]):
        if clean_merge(repo, newer) and from_main(repo, newer, main_head):
            merged = True
        elif not all(is_meta(x) or doc_path(x, "") for x in changed(repo, older, newer)):
            break
        if merged:
            out.append(older)
    return out


def decided(state: str) -> bool:
    """Whether a `ci_state` or `ci_job` answer is final: green, or failed."""
    return state == "success" or state.startswith("failed:") or state.endswith(" failed")


def ci_gate(c: Campaign, p: Json, target: str, ask: Callable[[str], str] | None = None) -> CiGate:
    """CI's verdict on landing TARGET (ASK's, default `ci_state`): its own runs once finished, else the
    first finished runs at a commit `ci_carried` finds; the landed main runs CI after
    (`main_ci_problems`). Undecided, the nearest runs say why."""
    repo = c.repo(p)
    later = c.state.get("ci_later", [])
    question = ask or (lambda k: ci_state(repo, k, later))
    own = CiGate(question(target), ci_commit(repo, target), False)
    if decided(own.state):
        return own
    waiting = own
    for k in ci_carried(c, p, target):
        gate = CiGate(question(k), ci_commit(repo, k), True)
        if decided(gate.state):
            return gate
        if waiting.state.startswith("no CI"):
            waiting = gate
    return waiting


def ci_landing(c: Campaign, p: Json, target: str) -> Json:
    """The CI runs a landing of TARGET relies on, for its record."""
    gate = ci_gate(c, p, target)
    runs = ci_run_ids(c.repo(p), gate.commit, c.state.get("ci_later", []))
    return {"commit": gate.commit, "runs": runs, **({"carried": True} if gate.carried else {})}


def main_ci_problems(c: Campaign) -> list[tuple[str, str]]:
    """An error for each landing that relied on CI carried across a clean merge from main (`ci_gate`) whose
    landed commit's CI then failed, naming both sides of the merge (it, and the quests that landed on
    main after its green head) and its fix-forward command, until CI is green at main's head or at a
    later such landing. `check` and `status` list them with `problems`."""
    if c.state.get("ci") != "github" or not shutil.which("gh"):
        return []
    later = c.state.get("ci_later", [])
    carried: dict[Path, list[tuple[str, Json]]] = {}
    for pid, p in c.quests.items():
        for record in p.get("landed", []):
            if record.get("ci", {}).get("carried"):
                carried.setdefault(c.repo(p), []).append((pid, record))
    out: list[tuple[str, str]] = []
    for repo, landings in carried.items():
        main = main_branch(repo)
        head = rev(repo, main)
        if ci_state(repo, head, later) == "success":
            continue
        depth = {r["commit"]: int(git(repo, "rev-list", "--count", r["commit"])) for _, r in landings}
        for pid, record in sorted(landings, key=lambda x: depth[x[1]["commit"]], reverse=True):
            landed = record["commit"]
            if not ancestor(repo, landed, head):
                continue
            state = ci_state(repo, landed, later)
            if state == "success":
                break  # main was green after the older ones
            if state.startswith("failed"):
                files = ",".join(c.quests[pid]["files"])
                green = record["ci"]["commit"]
                theirs = sorted(
                    {
                        qid
                        for qid, q in c.quests.items()
                        if qid != pid and c.repo(q) == repo
                        for r in q.get("landed", [])
                        if ancestor(repo, r["commit"], landed) and not ancestor(repo, r["commit"], green)
                    }
                )
                side = f"main's side brought {', '.join(theirs)}" if theirs else "main's side landed no quest"
                out.append(
                    (
                        "error",
                        f"{pid}: CI on {main} at {landed[:12]}, where it landed on CI carried from "
                        f"{green[:12]} across a merge from {main} ({side}), {state}: fix {main} forward "
                        f"(`campaign new {pid}-fix --title \"Fix {main}'s CI after {pid}\" --files '{files}'`)",
                    )
                )
    return out


def ci_jobs(repo: Path, commit: str) -> list[Json] | str:
    """The GitHub CI jobs of every run at COMMIT (name, status, conclusion), or why there are none."""
    if not shutil.which("gh"):
        return "gh is not installed"
    commit = ci_commit(repo, commit)
    code, runs = gh_json(repo, ci_runs(commit), all_passed)
    if code != 0:
        return f"gh failed: {runs}"
    found: list[Json] = []
    for code, view in [x.result() for x in [gh_start(repo, run_view(run), jobs_passed) for run in newest(runs)]]:
        if code != 0:
            return f"gh failed: {view}"
        found += (view or {}).get("jobs", [])
    return found


def ci_job(repo: Path, commit: str, name: str) -> str:
    """ "success" when every CI job at COMMIT whose name contains NAME passed; else why not."""
    jobs = ci_jobs(repo, commit)
    if isinstance(jobs, str):
        return jobs
    mine = [j for j in jobs if name.lower() in str(j.get("name", "")).lower()]
    if not mine:
        return f"no CI job named {name!r} at {commit[:12]} (push the branch)"
    if any(j.get("status") != "completed" for j in mine):
        return f"CI job {name!r} is running"
    failed = [
        str(j.get("name")) for j in mine if j.get("conclusion") not in ("success", "skipped", "neutral", "cancelled")
    ]
    gone = [str(j.get("name")) for j in mine if j.get("conclusion") == "cancelled"]
    if failed:
        return f"CI job {', '.join(failed)} failed"
    if gone:
        return f"CI job {', '.join(gone)} was cancelled, not failed (a newer push supersedes it; rerun it if none will)"
    return "success"


def ci_covers(c: Campaign) -> bool:
    """Whether a gate check's job at an ancestor counts across merges from main, the one rule every
    command shares (`verify`, `job rerun`, `brief`, `status`, `land`, `land --record`): landing needs
    CI green at the commit or across clean merges from main (`ci_gate`), and CI tests main's landed
    code after (`main_ci_problems`). The build-test (a gate check without inputs) otherwise needs the
    exact code. `land --no-ci` waives the CI gate, not this."""
    return c.state.get("ci") == "github"


def check_evidence(c: Campaign, p: Json, target: str, ci: bool | None = None) -> tuple[list[str], list[Json]]:
    """Each gate check's job at TARGET's code, or why there is none, by `ci_covers`' rule. CI is
    ignored: callers that still pass it get that rule."""
    repo = c.repo(p)
    missing: list[str] = []
    evidence: list[Json] = []
    if gates(p) and all(x.get("inputs") for x in gates(p)):
        missing.append("build-test: every gate check declares inputs; one without them must pass at the exact code")
    for check in gates(p):
        if check["cmd"].startswith(CI_CHECK):
            name = check["cmd"][len(CI_CHECK) :].strip()
            state, at, _ = ci_gate(c, p, target, lambda k, name=name: ci_job(repo, k, name))
            if state == "success":
                evidence.append({"check": check["name"], "ci": check["cmd"], "commit": at, "state": "done"})
            else:
                missing.append(f"{check['name']}: {state}")
            continue
        job = job_for_check(repo, target, check, c.state.get("tested_docs", []), ci=ci_covers(c), files=p["files"])
        if job is None:
            missing.append(f"{check['name']}: no job")
        elif job["state"].get("state") != "done":
            missing.append(f"{check['name']}: {job['state'].get('state')} ({job['id']})")
        else:
            evidence.append(job_summary(job))
    return missing, evidence
