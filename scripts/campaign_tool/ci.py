"""GitHub CI: the runs and jobs at a commit, asked in the background and read once per command."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .core import CI_CHECK, Json, git, jobs_dir, pool
from .jobs import job_summary
from .journals import checkouts
from .repo import _heads, changed, has_ref, is_meta, on_commits, rev, warm_status
from .reuse import doc_path, job_for_check
from .state import branches, gates

if TYPE_CHECKING:  # imported when used: concurrent.futures imports logging, a sixth of a command's start
    from concurrent.futures import Future

if TYPE_CHECKING:  # annotations only: they import this module, so importing them here is a cycle
    from .campaign import Campaign


_gh: dict[str, Future[tuple[int, Any]]] = {}
_until: dict[str, float] = {}  # when each answer in _gh stops counting
_gh_lock = threading.Lock()
_pruned = [False]
GH_TIMEOUT = 30  # seconds a gh call may take: past it, a failed answer
LOCK_WAIT = 10  # seconds a process waits for another asking the same: then it asks too


def ci_seconds() -> float:
    """How long an answer GitHub may still change (a run queued or running, or none yet) is kept for
    every process on the host; a failed call half as long, a completed red run list five times, a
    green one fifteen (a newer run may still come)."""
    return float(os.environ.get("CAMPAIGN_CI_SECONDS", "120"))


FOREVER = 1e18
NO_RUNS = "no CI runs for this commit (push the branch)"


def runs_kept(runs: Any) -> float:
    """Seconds a commit's run list is kept: a newer run can follow even a completed set."""
    done = bool(runs) and all(isinstance(x, dict) and x.get("status") == "completed" for x in runs)
    return (15 if all_passed(runs) else 5) * ci_seconds() if done else ci_seconds()


def ancestor_kept(runs: Any) -> float:
    """Seconds an ancestor candidate's run list is kept: one with none had its runs long ago or
    never will (CI skipped it), as final as a completed set."""
    return runs_kept(runs) if runs else 5 * ci_seconds()


def view_kept(view: Any) -> float:
    """Seconds a run's jobs are kept: for good once they all passed; a red run can be re-run."""
    jobs = (view or {}).get("jobs") if isinstance(view, dict) else None
    if all_passed(jobs):
        return FOREVER
    return 5 * ci_seconds() if jobs and all(x.get("status") == "completed" for x in jobs) else ci_seconds()


def gh_cached(path: Path) -> Json:
    """The answer on disk at PATH while it counts ({"until", "code", "data"}), else {}."""
    hit: Any = None
    with contextlib.suppress(OSError, ValueError):
        hit = json.loads(path.read_text())
    if not isinstance(hit, dict) or "data" not in hit:
        return {}
    with contextlib.suppress(TypeError, ValueError):
        if time.time() < float(hit.get("until", 0)):
            return hit
    return {}


def gh_start(repo: Path, args: list[str], kept: Callable[[Any], float]) -> Future[tuple[int, Any]]:
    """`gh ARGS` (JSON out) in REPO, started in the background, or found started or answered:
    (returncode, data or stderr). An answer is kept on disk for every process on the host for the
    seconds KEPT gives it, a failed one for half of ci_seconds()."""
    key = hashlib.sha256(json.dumps([str(repo), args]).encode()).hexdigest()[:32]
    with _gh_lock:
        if key not in _gh:
            path = jobs_dir() / ".gh" / f"{key}.json"
            if hit := gh_cached(path):
                from concurrent.futures import Future  # noqa: PLC0415

                _gh[key] = Future()
                _gh[key].set_result((int(hit.get("code", 0)), hit["data"]))
                _until[key] = float(hit["until"])
            else:
                _gh[key] = pool().submit(gh_ask, repo, args, kept, path, key)
        return _gh[key]


def gh_ask(repo: Path, args: list[str], kept: Callable[[Any], float], path: Path, key: str) -> tuple[int, Any]:
    """Asks GitHub unless another process answered while this one waited for the key's lock, at
    most LOCK_WAIT seconds: a hung call elsewhere holds no one past that."""
    path.parent.mkdir(exist_ok=True)
    prune_answers(path.parent)
    with open(path.with_suffix(".lock"), "w") as lock:
        for _ in range(LOCK_WAIT * 10):
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                time.sleep(0.1)
        if hit := gh_cached(path):
            _until[key] = float(hit["until"])
            return int(hit.get("code", 0)), hit["data"]
        try:
            p = subprocess.run(["gh", *args], cwd=repo, capture_output=True, text=True, timeout=GH_TIMEOUT)
            code, data = p.returncode, (json.loads(p.stdout or "null") if p.returncode == 0 else p.stderr.strip())
        except subprocess.TimeoutExpired:
            code, data = 124, f"gh took over {GH_TIMEOUT} s"
        except ValueError as e:
            code, data = 1, f"gh answered no JSON: {e}"
        _until[key] = time.time() + (kept(data) if code == 0 else ci_seconds() / 2)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"until": _until[key], "code": code, "data": data}))
        tmp.replace(path)
        return code, data


def prune_answers(folder: Path) -> None:
    """Once a process: removes the answers and locks untouched for a day."""
    if _pruned[0]:
        return
    _pruned[0] = True
    for x in folder.iterdir() if folder.is_dir() else []:
        with contextlib.suppress(OSError):
            if time.time() - x.stat().st_mtime > 86400:
                x.unlink()


def gh_json(repo: Path, args: list[str], kept: Callable[[Any], float]) -> tuple[int, Any]:
    return gh_start(repo, args, kept).result()


def ci_runs(commit: str) -> list[str]:
    """The `gh` arguments listing the CI runs at COMMIT: one query serves every question about them."""
    return ["run", "list", "--commit", commit, "--json", "databaseId,status,conclusion,name", "--limit", "50"]


def all_passed(items: Any) -> bool:
    """Finished and green."""
    ok = ("success", "skipped", "neutral")
    return bool(items) and all(x.get("status") == "completed" and x.get("conclusion") in ok for x in items)


def ci_commit(repo: Path, commit: str) -> str:
    """The commit whose CI speaks for COMMIT: itself, or, when it has no runs, its nearest
    first-parent ancestor that differs only in campaign files or documentation, which CI workflows
    skip (`paths-ignore`). COMMIT and its pushed ancestors are asked, all at once."""
    asked = [
        (k, gh_start(repo, ci_runs(k), runs_kept if k == commit else ancestor_kept))
        for k in ci_candidates(repo, commit)
    ]
    for k, runs in asked:
        code, listed = runs.result()
        if code == 0 and listed:
            return k
    return commit


def ci_candidates(repo: Path, commit: str) -> list[str]:
    """COMMIT and its nearest first-parent ancestors whose code is COMMIT's, those a remote-tracking
    ref holds: whose CI may speak for it."""
    log = on_commits(
        ("ci-line", str(repo), commit),
        lambda: git(repo, "rev-list", "--first-parent", "--max-count=20", commit, check=False).split(),
    )
    out: list[str] = []
    for k in log:
        if not all(is_meta(x) or doc_path(x, "") for x in changed(repo, k, commit)):
            break
        out.append(k)
    gone = unpushed(repo, out[1:])  # COMMIT itself is always asked: it may be pushed from elsewhere
    return [k for k in out if k not in gone]


def unpushed(repo: Path, commits: list[str]) -> set[str]:
    """COMMITS no remote-tracking ref contains: GitHub never saw them, so they have no CI. None when
    REPO has no remote-tracking ref at all (nothing tells)."""
    if not commits or not git(repo, "for-each-ref", "--count=1", "--format=%(refname)", "refs/remotes", check=False):
        return set()
    return set(commits) & set(git(repo, "rev-list", *commits, "--not", "--remotes", check=False).split())


def ci_warm(repo: Path, commit: str, jobs: bool) -> None:
    """Starts asking GitHub what ci_state (and with JOBS, ci_jobs) will ask about COMMIT, so the
    answers for many quests arrive together."""
    if not shutil.which("gh"):
        return

    def views(asked: Future[tuple[int, Any]]) -> None:
        with contextlib.suppress(Exception):  # ci_jobs asks again, and reports what fails
            code, runs = asked.result()
            for run in runs if code == 0 and isinstance(runs, list) else []:
                if isinstance(run, dict) and "databaseId" in run:
                    gh_start(repo, run_view(run), view_kept)

    for k in ci_candidates(repo, commit):
        asked = gh_start(repo, ci_runs(k), runs_kept if k == commit else ancestor_kept)
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


def ci_state(repo: Path, commit: str, later: list[str]) -> str:
    if not shutil.which("gh"):
        return "gh is not installed"
    commit = ci_commit(repo, commit)
    code, listed = gh_json(repo, ci_runs(commit), runs_kept)
    if code != 0:
        return f"gh failed: {listed}"
    runs = [r for r in listed or [] if r.get("name") not in later]
    if not runs:
        return NO_RUNS
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


def ci_jobs(repo: Path, commit: str) -> list[Json] | str:
    """The GitHub CI jobs of every run at COMMIT (name, status, conclusion), or why there are none."""
    if not shutil.which("gh"):
        return "gh is not installed"
    commit = ci_commit(repo, commit)
    code, runs = gh_json(repo, ci_runs(commit), runs_kept)
    if code != 0:
        return f"gh failed: {runs}"
    found: list[Json] = []
    for code, view in [x.result() for x in [gh_start(repo, run_view(run), view_kept) for run in runs or []]]:
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


def ci_waiting(c: Campaign, p: Json) -> bool:
    """Whether CI at the quest's head is queued or running, the landing's or a `ci:` check's: a live
    waiter, which moves the quest without the lead."""
    repo = c.repo(p)
    if not shutil.which("gh") or not p.get("branch") or not has_ref(repo, p["branch"]):
        return False
    head = rev(repo, p["branch"])
    if c.state.get("ci") == "github" and ci_state(repo, head, c.state.get("ci_later", [])).startswith("running: "):
        return True
    names = [x["cmd"][len(CI_CHECK) :].strip() for x in gates(p) if x["cmd"].startswith(CI_CHECK)]
    return any(ci_job(repo, head, name).endswith(" is running") for name in names)


def forget_ci() -> None:
    """Drops this process's answers that stopped counting, so a long wait asks again."""
    with _gh_lock:
        now = time.time()
        for key in [k for k, f in _gh.items() if f.done() and _until.get(k, 0) <= now]:
            del _gh[key]
            _until.pop(key, None)


def ci_covers(c: Campaign) -> bool:
    """Whether a gate check's job at an ancestor counts across merges from main, the one rule every
    command shares (`verify`, `job rerun`, `brief`, `status`, `land`, `land --record`): landing needs
    CI green at the exact commit, and CI tests the merge with main. The build-test (a gate check
    without inputs) otherwise needs the exact code. `land --no-ci` waives the CI gate, not this."""
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
            state = ci_job(repo, target, check["cmd"][len(CI_CHECK) :].strip())
            if state == "success":
                evidence.append({"check": check["name"], "ci": check["cmd"], "commit": target, "state": "done"})
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
