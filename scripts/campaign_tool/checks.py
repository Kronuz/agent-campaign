"""check and verify: the campaign's consistency, housekeeping, and a quest's gate checks as jobs."""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

from style import mark, paint

from .campaign import Campaign, find_campaign, migrate_parks, writing
from .ci import ci_covers
from .core import CI_CHECK, HEAVY, CampaignError, Json, die, jobs_dir, plural, read_json, stamp
from .disk import disk_floor, disk_warning, prune_plan
from .evidence import criteria_for
from .jobcmds import (
    LANDING_PRIORITY,
    PARALLEL,
    at_landing,
    ctest_warning,
    evidence_hold,
    held,
    quest_slot,
    rerun_jobs,
)
from .jobs import all_jobs, load_job, matches_inputs, resolve_job, tag
from .journals import journal_place
from .leads import stuck_warnings
from .repo import frozen, has_ref, main_branch, rev, same_repo
from .reuse import build_test, check_spec, docs_note, job_at, submit
from .reviews import Gap
from .runner import LIGHT_BUILD_JOBS, ensure_worker, reap
from .state import branches, ci_owned, deferred, gates, locate, select, validate
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
    hold allows (several light jobs run at once, so each keeps to LIGHT_BUILD_JOBS), running ctest
    on past its first failure, or building a sanitizer locally that CI builds."""
    out: list[tuple[str, str]] = []
    for pid, p in c.quests.items():
        if p["status"] in ("landed", "dropped"):
            continue
        out += [("warning", f"{pid}: {text}") for text in ci_owned(gates(p), c.repo(p))]
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
    """Queues CHECKS at COMMIT; with HOLD (the lead's `verify`), each passes `evidence_hold` first, so
    while the quest iterates only those an earlier job answers come back, and nothing new is queued."""
    p = c.quests[qid]
    repo = c.repo(p)
    ids: list[str] = []
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
        kind, why = evidence_hold(c, spec, [(c, qid, True)] if hold else [], exact=exact)
        if kind == "held":
            print(f"{check['name']}: {held(why)}")
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


def landing_reruns(c: Campaign, pid: str) -> tuple[str, set[str], set[str]]:
    """Queues what a merge from main left a landing quest to rerun (`at_landing`): its voided evidence
    jobs and gate checks at its head, at LANDING_PRIORITY, past the hold (row "land"). Returns what it
    queued, for the refusal, with the jobs it reran and the checks it queued."""
    gaps = at_landing(c, pid)
    if gaps is None:
        return "", set(), set()
    p = c.quests[pid]
    stale = [jid for g in gaps for jid in g.jobs if not resolve_job(jid)["spec"].get("fails_without")]  # no mutant here
    since = time.time()
    try:
        reruns = rerun_jobs(stale, c=c, hold=False) if stale else []
        checks = submit_checks(c, pid, gates(p), rev(c.repo(p), p["branch"]), priority=LANDING_PRIORITY)
    except CampaignError as e:
        return f"\n  its landing jobs were not queued: {e}", set(), set()
    fresh = [x for x in dict.fromkeys(checks) if float(resolve_job(x)["spec"].get("submitted") or 0) >= since]
    said = [f"reruns {' '.join(dict.fromkeys(reruns))}"] if reruns else []
    said += [f"landing checks {' '.join(fresh)}"] if fresh else []  # never passed at this code
    names = {x["name"] for x in gates(p) if not x["cmd"].startswith(CI_CHECK)}
    return f"\n  queued at landing priority {LANDING_PRIORITY}: {'; '.join(said)}" if said else "", set(stale), names


HINT = re.compile(r":? \(?`campaign [^`]*`\)?")  # a gap's command, which `land` ran itself
HELD = re.compile(r"(?::| \() ?(?:reruns|verify) held: .*$", re.S)  # a held gap's why, which `land` ran past


def landing_refusal(c: Campaign, pid: str, gaps: list[Gap], *, merged: bool) -> str:
    """What `land` refuses with: GAPS, each with its hint; after its merge from main, a gap the reruns
    and landing checks it queues (`landing_reruns`) answer is named without its hint, as queued."""
    queued, reran, checks = landing_reruns(c, pid) if merged else ("", set(), set())

    def answered(g: Gap) -> bool:
        if g.kind == "stale":
            return bool(g.jobs) and set(g.jobs) <= reran
        return g.kind == "check" and (m := re.match(r"check (\S+) is not green", g.text)) is not None and m[1] in checks

    shown = [HELD.sub("", HINT.sub("", g.text)) + " (queued below)" if answered(g) else g.text for g in gaps]
    return f"{pid} cannot land yet:\n  " + "\n  ".join(shown) + queued


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
    queued = submit_checks(
        c, a.id, checks, commit, priority=GATE_PRIORITY, fresh=a.fresh, force=a.force, hold=not a.force
    )
    for jid in queued:
        if ids:
            tag(jid, c.ident, a.id, ids, False)
    if queued:
        print(f"wait for them with `campaign wait --quest {a.id}`")
    return 0
