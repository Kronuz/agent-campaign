"""check and verify: the campaign's consistency, housekeeping, and a quest's gate checks as jobs."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from style import mark, paint

from .campaign import Campaign, find_campaign, migrate_parks, writing
from .ci import ci_covers
from .core import CI_CHECK, HEAVY, TERMINAL, CampaignError, Json, die, jobs_dir, plural, read_json, stamp
from .disk import disk_floor, disk_warning, prune_plan
from .evidence import criteria_for
from .jobcmds import LANDING_PRIORITY, PARALLEL, at_landing, ctest_warning, quest_slot, rerun_jobs
from .jobs import all_jobs, load_job, matches_inputs, quest_jobs, resolve_job, tag
from .journals import journal_place, journal_state
from .leads import stuck_warnings
from .repo import ancestor, frozen, has_ref, main_branch, rev, same_repo
from .reuse import build_test, check_spec, docs_note, job_at, shape, submit
from .runner import LIGHT_BUILD_JOBS, ensure_worker, reap
from .state import branches, deferred, gates, locate, select, validate
from .status import print_problem, problems
from .worker import long_timeouts, stalls


def cmd_check(a: argparse.Namespace) -> int:
    home, pinned, _ = locate(Path.cwd())
    path = home / select(home, os.environ.get("CAMPAIGN") or pinned)[1] / "state.json"
    data = read_json(path)
    if data is None:
        die(f"{path} does not exist")
    migrate_parks(data)  # the next writer records it
    errors = validate(data)
    for e in errors:
        print_problem("error", e)
    if errors:
        return 1
    with frozen():  # check only reads
        reap()
        c = Campaign(home)
        found = problems(c)
        notes = [*check_commands(c), *housekeeping(c), *(("warning", text) for text, _ in stuck_warnings())]
        notes += stalls(all_jobs(), time.time()) + long_timeouts(all_jobs(), c.ident, time.time())
    for severity, message in [*found, *notes]:
        print_problem(severity, message)
    if not any(s != "note" for s, _ in [*found, *notes]):
        print(f"{mark('✅')}{paint('ok', 'green')}")
    return 1 if any(s == "error" for s, _ in found) else 0


def check_commands(c: Campaign) -> list[tuple[str, str]]:
    """Checks of live quests whose commands waste a light slot: asking for more build jobs than a light
    hold allows (several light jobs run at once, so each keeps to LIGHT_BUILD_JOBS), or running ctest
    on past its first failure."""
    out: list[tuple[str, str]] = []
    for pid, p in c.quests.items():
        if p["status"] in ("landed", "dropped"):
            continue
        for kind in ("checks", "later"):
            for check in p.get(kind, []):
                if warning := ctest_warning(check.get("cmd", "")):
                    out.append(("warning", f"{pid}: check {check['name']} {warning}"))
                asked = [int(n) for n in PARALLEL.findall(check.get("cmd", ""))]
                if check.get("level") not in HEAVY and asked and max(asked) > LIGHT_BUILD_JOBS:
                    out.append(
                        (
                            "warning",
                            f"{pid}: light check {check['name']} asks for -j{max(asked)}; a light job keeps to "
                            f"-j{LIGHT_BUILD_JOBS} (drop the -j: CMAKE_BUILD_PARALLEL_LEVEL is set)",
                        )
                    )
    return out


def housekeeping(c: Campaign) -> list[tuple[str, str]]:
    """What the campaign and the job store leave behind: landed or dropped quests with worktrees or
    branches, what `job prune` would do to jobs, jobs of campaigns this machine does not know, and free
    disk under the campaign's disk_warn."""
    out: list[tuple[str, str]] = []
    left = [
        pid
        for pid, p in c.quests.items()
        if p["status"] in ("landed", "dropped")
        and any(
            (rel and (c.home / rel).exists())
            or has_ref(c.repo(p), b)
            or (not same_repo(c.repo(p), c.home) and has_ref(c.home, b))
            or ((jp := journal_place(c, name)) is not None and jp.exists())
            for name, b, rel, _ in branches(c, pid)
        )
    ]
    if left:
        out.append(("note", f"still have worktrees or branches: {', '.join(left)} (`campaign clean ID`)"))
    plan = prune_plan(14)
    if plan["delete"] or plan["slim"] or plan["slots"]:
        jobs, slim, slots = (
            plural(len(plan[k]), "slot" if k == "slots" else "job") for k in ("delete", "slim", "slots")
        )
        out.append(("note", f"`campaign job prune` would delete {jobs}, slim {slim} and remove {slots}"))
    for owner, n in sorted(plan["unknown"].items()):
        out.append(("note", f"{plural(n, 'job')} of {owner}, a campaign this machine does not know: prune keeps them"))
    warning = disk_warning(c.home, disk_floor(c))
    if warning:
        out.append(("warning", warning))
    return out


GATE_PRIORITY = 2  # above light evidence (1), measurements (0) and deferred checks (-1); below LANDING_PRIORITY
SLOW_MINUTES = 20  # a gate check declared this long runs once per round (`verify` holds it while iterating)
RESULTS = set(TERMINAL) - {"cancelled", "skipped"}  # the ends that say whether a check passes


def passed_before(jobs: list[Json], spec: Json, commit: str) -> str:
    """The earlier commit of the quest at which SPEC's check passed in its last run, among the quest's
    JOBS; "" when that run failed or none ran."""
    runs = [
        j
        for j in jobs
        if j["state"].get("state") in RESULTS
        and shape(j["spec"]) == shape(spec)
        and j["spec"].get("commit") not in (None, commit)
        and ancestor(Path(spec["repo"]), j["spec"]["commit"], commit)
    ]
    last = max(runs, key=lambda j: j["spec"].get("submitted", 0), default=None)
    return last["spec"]["commit"] if last is not None and last["state"]["state"] == "done" else ""


def submit_checks(
    c: Campaign,
    qid: str,
    checks: list[Json],
    commit: str,
    *,
    priority: int,
    fresh: bool = False,
    force: bool = False,
    hold: bool = False,
) -> list[str]:
    """Queues CHECKS at COMMIT; with HOLD, not a slow gate check that passed at an earlier commit."""
    p = c.quests[qid]
    repo = c.repo(p)
    ids: list[str] = []
    jobs: list[Json] | None = None
    for check in checks:
        if check["cmd"].startswith(CI_CHECK):
            continue  # GitHub runs it when the branch is pushed
        spec: Json = {
            **check_spec(repo, commit, check),
            "minutes": check["minutes"],
            "quest": qid,
            "campaign": c.ident,
            "what": f"{qid} {'deferred ' if check.get('later') else ''}check {check['name']} at {commit[:12]}",
            "slot": "" if fresh or p["risk"] == "critical" else quest_slot(c, qid, repo),
            "cmux": os.environ.get("CMUX_WORKSPACE_ID", ""),
            "priority": priority,
            "retries": 1,
        }
        exact = build_test(check)
        earlier = None if force else job_at(spec, commit, exact=exact, merged=ci_covers(c) or not exact)
        if earlier is not None and docs_note(earlier) and not earlier["state"].get("slimmed"):
            print(f"{check['name']}: {earlier['id']} (reused: {docs_note(earlier)})")
            ids.append(earlier["id"])
            continue
        if hold and not check.get("later") and check["minutes"] >= SLOW_MINUTES:
            jobs = quest_jobs(all_jobs(), c.ident, qid) if jobs is None else jobs
            if at := passed_before(jobs, spec, commit):
                end = "it reruns at the round's end (State ready for review, or land)"
                print(f"{check['name']}: held, slow and passed at {at[:12]}: {end}")
                continue
        if spec.get("inputs") and not matches_inputs(repo, commit, spec["inputs"]):
            print(
                f"campaign: {check['name']}'s inputs {','.join(spec['inputs'])} match no file at {commit[:12]}",
                file=sys.stderr,
            )
        jid, reused = submit(spec, force=force, exact=exact)
        at = load_job(jobs_dir() / jid)["spec"]["commit"] if reused and spec.get("inputs") else commit
        note = f" (reused: its inputs are unchanged since {at[:12]})" if at != commit else " (reused)" if reused else ""
        print(f"{check['name']}: {jid}{note}")
        ids.append(jid)
    ensure_worker()
    return ids


def landing_reruns(c: Campaign, pid: str) -> str:
    """Queues what a merge from main left a landing quest to rerun (`at_landing`): its voided evidence
    jobs and gate checks at its head, at LANDING_PRIORITY. Returns what it queued, for the refusal."""
    gaps = at_landing(c, pid)
    if gaps is None:
        return ""
    p = c.quests[pid]
    stale = [jid for g in gaps for jid in g.jobs if not resolve_job(jid)["spec"].get("fails_without")]  # no mutant here
    try:
        ids = rerun_jobs(stale, c=c) if stale else []
        ids += submit_checks(c, pid, gates(p), rev(c.repo(p), p["branch"]), priority=LANDING_PRIORITY)
    except CampaignError as e:
        return f"\n  its reruns were not queued: {e}"
    return f"\n  queued its reruns at landing priority {LANDING_PRIORITY}: {' '.join(dict.fromkeys(ids))}"


def verify_deferred(a: argparse.Namespace) -> int:
    with writing() as c:
        p = c.quest(a.id)
        if not deferred(p):
            die(f"{a.id} has no deferred checks")
        if not p["landed"] or not p["landed"][-1].get("final"):
            die(f"{a.id} has not landed: `land` queues its deferred checks, `verify --all` runs them on the branch")
        repo = c.repo(p)
        commit = rev(repo, a.commit or main_branch(repo))
        p["landed"][-1]["later"] = submit_checks(c, a.id, deferred(p), commit, priority=-1, force=a.force)
        p["updated"] = stamp()
        c.save(f"campaign: rerun {a.id}'s deferred checks at {commit[:12]}")
    return 0


def cmd_verify(a: argparse.Namespace) -> int:
    if a.deferred:
        return verify_deferred(a)
    c = find_campaign(Path.cwd())
    p = c.quest(a.id)
    checks = p["checks"] if a.all else gates(p)
    if not checks:
        die(f"{a.id} has no {'' if a.all else 'gate '}checks (`campaign set {a.id} --check NAME=CMD`)")
    if not p.get("branch") and not a.commit:
        die(f"{a.id} has no branch")
    commit = rev(c.repo(p), a.commit or p["branch"])
    ids = criteria_for(c, a.id, a.criteria or "")
    iterating = not journal_state(c, a.id, p.get("branch", "")).startswith("ready for review")
    queued = submit_checks(
        c, a.id, checks, commit, priority=GATE_PRIORITY, fresh=a.fresh, force=a.force, hold=iterating and not a.force
    )
    for jid in queued:
        if ids:
            tag(jid, c.ident, a.id, ids, False)
    print(f"wait for them with `campaign wait --quest {a.id}`")
    return 0
