"""Landing: version pins, task merges, merging main, landing a quest, and cleaning up after it."""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import subprocess
import sys
from pathlib import Path

from .campaign import Campaign, blockers, close_hunt, find_campaign, landing_record, writing
from .checks import landing_reruns, submit_checks
from .ci import check_evidence, ci_covers
from .core import (
    EXCLUDE_META,
    TERMINAL,
    CampaignError,
    Json,
    die,
    git,
    git_ok,
    jobs_dir,
    plural,
    section,
    stamp,
    write_json,
)
from .disk import readers_of
from .evidence import criteria_evidence, unparsed_criteria
from .jobcmds import quest_slot
from .jobs import all_jobs, claim, resolve_job, slot_of
from .journals import journals_at, land_journals, landing_journals
from .leads import unlocks
from .mail import inbox, mailbox, notify
from .map import refresh_map
from .repo import (
    ancestor,
    changed,
    code_tree,
    frozen,
    has_ref,
    is_meta,
    main_branch,
    main_checkout,
    merge_base,
    rev,
    same_repo,
)
from .reuse import submit
from .reviews import landing_gaps, main_moved, repo_lock, review_gaps, task_gaps
from .runner import drop_slot, spawn
from .state import branches, deferred
from .wait import wait_for

VERSION = re.compile(r"(?<![\w.])(?:[0-9a-f]{7,40}|v?\d+(?:\.\d+)+(?:[-+][0-9A-Za-z]+)*)(?!\w|\.\d)")


def pin_problems(repo: Path, commit: str) -> list[str]:
    """Why COMMIT is not a version-pin bump: one changing nothing but version strings (a tag, a
    release number, a commit id) in lines of files it neither adds nor removes."""
    parent = f"{commit}^"
    if not git_ok(repo, "rev-parse", "--verify", "--quiet", parent):
        return [f"{commit[:12]} has no parent"]
    scope = ("--", ".", *EXCLUDE_META)
    status = [x.split("\t") for x in git(repo, "diff", "--name-status", parent, commit, *scope).splitlines()]
    out = [f"{x[-1]} is {x[0]}, not modified" for x in status if x[0] != "M"]
    lines: dict[str, tuple[list[str], list[str]]] = {}
    path, hunk = "", False
    for line in git(repo, "diff", "-U0", "--no-color", "--no-ext-diff", parent, commit, *scope).splitlines():
        if line.startswith("diff --git "):
            path, hunk = line.rsplit(" b/", 1)[-1], False
        elif line.startswith("@@"):
            hunk = True
        elif hunk and line[:1] in "-+":
            lines.setdefault(path, ([], []))["-+".index(line[0])].append(VERSION.sub("<version>", line[1:]))
    for x in status:
        removed, added = lines.get(x[-1], ([], []))
        if x[0] == "M" and (not removed or sorted(removed) != sorted(added)):
            out.append(f"{x[-1]} changes more than version strings")
    return out if status else [f"{commit[:12]} changes nothing outside the campaign's files"]


def cmd_pin(a: argparse.Namespace) -> int:
    """Records a version-pin bump the lead committed on main: only version strings changed, and the
    repository's own pin check (`config pin_check`) passed at it. CI gates it after the push."""
    c = find_campaign(Path.cwd())
    repo = (c.home / a.repo).resolve() if a.repo else c.home
    main = main_branch(repo)
    if a.commit:
        commit = rev(repo, a.commit)
    else:  # main's newest commit outside the campaign's files
        line = git(repo, "rev-list", "--first-parent", "--max-count=20", main).split()
        code = (k for k in line if git_ok(repo, "rev-parse", "--verify", "--quiet", f"{k}^"))
        commit = next((k for k in code if any(not is_meta(x) for x in changed(repo, f"{k}^", k))), line[0])
    if not ancestor(repo, commit, rev(repo, main)):
        die(f"{commit[:12]} is not on {main}: commit the pin there")
    problems = pin_problems(repo, commit)
    if problems:
        die(f"{commit[:12]} is not a pin bump ({'; '.join(problems)}): land it as a quick fix or a quest")
    jid = ""
    if c.state.get("pin_check"):
        spec: Json = {
            "repo": str(repo),
            "commit": commit,
            "refs": {},
            "cwd": "",
            "cmd": c.state["pin_check"],
            "level": "light",
            "minutes": 30,
            "env": {},
            "campaign": c.ident,
            "what": f"pin check at {commit[:12]}",
        }
        jid, _ = submit(spec)
        job = resolve_job(jid)
        if job["state"].get("state") == "queued" and claim(job):
            spawn(["_run", str(job["dir"])], job["dir"] / "runner.log").wait()
        if wait_for([jid], any_one=False, timeout=0) != 0:
            die(f"the pin check {jid} did not pass at {commit[:12]}: `campaign job log {jid}`")
    with writing() as c:
        rel = os.path.relpath(repo, c.home)
        c.state.setdefault("pins", []).append({"repo": rel, "commit": commit, "check": jid, "at": stamp()})
        c.save(f"campaign: pin bump {commit[:12]}" + (f" in {repo.name}" if rel != "." else ""))
    if a.push:
        for where in dict.fromkeys([main_checkout(repo), c.home]):
            if refused := push_main(where):
                die(f"recorded, but the push from {where} failed: {refused}")
    print(
        f"pin bump {commit[:12]}: recorded"
        + (f", pin check {jid} passed" if jid else "")
        + ("; pushed" if a.push else "")
    )
    return 0


def sync_main(c: Campaign, where: Path) -> None:
    """Brings origin's main into WHERE's main before a landing pushes it, when the commits it lacks
    change only campaign files (never this campaign's state, which only this checkout writes);
    refuses otherwise, before anything lands."""
    main = main_branch(where)
    tracking = f"refs/remotes/origin/{main}"
    fetched = subprocess.run(
        ["git", "-C", str(where), "fetch", "-q", "origin", f"+refs/heads/{main}:{tracking}"],
        capture_output=True,
        text=True,
    )
    if fetched.returncode != 0:
        listed = subprocess.run(
            ["git", "-C", str(where), "ls-remote", "--exit-code", "origin", f"refs/heads/{main}"], capture_output=True
        )
        if listed.returncode == 2:  # origin has no main yet: the push creates it
            return
        die(f"cannot fetch origin's {main} into {where}: {fetched.stderr.strip()}; nothing landed")
    remote, local = rev(where, tracking), rev(where, main)
    if ancestor(where, remote, local):
        return
    theirs = changed(where, merge_base(where, local, remote), remote)
    state = f"{c.meta}/state.json" if same_repo(where, c.home) else None
    code = [x for x in theirs if not is_meta(x) or x == state]
    if code:
        die(
            f"origin's {main} has commits {where} lacks that change {plural(len(code), 'file')} outside the "
            f"campaign's files or its state (e.g. {code[0]}); nothing landed: merge origin's {main} into {main} "
            "and land again"
        )
    merged = subprocess.run(
        ["git", "-C", str(where), "merge", "-q", "--no-edit", "-m", f"campaign: merge origin's {main}", tracking],
        capture_output=True,
        text=True,
    )
    if merged.returncode != 0:
        git(where, "merge", "--abort", check=False)
        die(f"merging origin's {main} into {where} failed: {merged.stderr.strip()}; nothing landed")


def push_main(where: Path) -> str:
    """Pushes WHERE's main to origin: git's error when the push fails, else ""."""
    pushed = subprocess.run(
        ["git", "-C", str(where), "push", "-q", "origin", main_branch(where)], capture_output=True, text=True
    )
    if pushed.returncode == 0:
        return ""
    return (pushed.stderr or pushed.stdout).strip() or f"git push exited {pushed.returncode}"


def cmd_task_merge(a: argparse.Namespace) -> int:
    """Merges a task's branch into its quest's branch once its own jobs passed at its head, then
    removes the task's worktree and branch (and, for a quest in another repository, its journal's)."""
    with writing() as c:
        p = c.quest(a.id)
        t = p.get("tasks", {}).get(a.task)
        if t is None:
            die(f"{a.id} has no task {a.task}")
        if t.get("merged"):
            die(f"task {a.task} merged into {a.id} at {t['merged'][:12]} already")
        if t.get("status") == "dropped":
            die(f"task {a.task} is dropped (`campaign set {a.id} --task {a.task} status=active` undoes it)")
        repo = c.repo(p)
        places = [(repo, "worktree"), *([(c.home, "journal")] if not same_repo(repo, c.home) else [])]
        merged = all(ancestor(owner, t["branch"], p["branch"]) for owner, _ in places)  # nothing left to merge
        if not merged and (gaps := task_gaps(c, a.id, a.task, all_jobs())):
            die(f"task {a.task} cannot merge yet:\n  " + "\n  ".join(gaps))
        head = rev(repo, t["branch"])
        for _, field in places:
            mine = (c.home / t[field]).resolve() if t.get(field) else None
            if mine and mine.exists():
                if git(mine, "status", "--porcelain"):
                    die(f"{mine} has uncommitted or untracked files: commit or remove them first")
                if field == "worktree" and (readers := readers_of(mine)):
                    die(f"job {', '.join(readers)} reads from {mine}: merge once it ends")
            into = (c.home / p[field]).resolve() if p.get(field) else None
            if into is None or not into.exists():
                die(f"{a.id}'s {field} is missing: `campaign start {a.id}` recreates it")
            if git(into, "symbolic-ref", "--quiet", "--short", "HEAD", check=False) != p["branch"]:
                die(f"{into} is not on {p['branch']}")
            if git(into, "status", "--porcelain", "--untracked-files=no"):
                die(f"{into} has uncommitted changes: commit them first")
        for owner, field in places:
            into = (c.home / p[field]).resolve()
            if ancestor(owner, t["branch"], p["branch"]):
                continue
            message = f"campaign: merge task {a.task} into {a.id}"
            done = subprocess.run(
                ["git", "-C", str(into), "merge", "-q", "--no-ff", "-m", message, t["branch"]],
                capture_output=True,
                text=True,
            )
            if done.returncode != 0:
                conflicts = git(into, "diff", "--name-only", "--diff-filter=U", check=False).split()
                git(into, "merge", "--abort", check=False)
                die(
                    f"merging {t['branch']} into {p['branch']} conflicts in "
                    f"{', '.join(conflicts) or done.stderr.strip()}: resolve it in a task; nothing merged here"
                )
        for owner, field in places:
            mine = (c.home / t[field]).resolve() if t.get(field) else None
            if mine and mine.exists():
                git(owner, "worktree", "remove", str(mine))
            if field in t:
                t[field] = ""
            if has_ref(owner, t["branch"]):
                git(owner, "branch", "-q", "-D", t["branch"])  # merged into the quest's branch, not main
        t["merged"] = head
        p["updated"] = stamp()
        c.save(f"campaign: merge task {a.task} into {a.id} at {head[:12]}")
    print(f"{a.id}: merged task {a.task} at {head[:12]} into {p['branch']}; its worktree and branch are removed")
    return 0


def merge_main_into(c: Campaign, pid: str) -> str:
    """Merges main into the quest's branch in its worktree (a merge, never a rebase), and pushes the
    branch to origin when there is one, so CI runs at the merge. A conflict refuses, leaving the
    branch as it was. Returns what happened, "" when a plain land would not ask for the merge: main
    is in the branch, or moved only in its campaign records."""
    p = c.quests[pid]
    repo = c.repo(p)
    main, branch = main_branch(repo), p["branch"]
    main_head = rev(repo, main)
    if ancestor(repo, main_head, rev(repo, branch)) or not main_moved(
        repo, main_head, merge_base(repo, main_head, rev(repo, branch))
    ):
        return ""
    wt = (c.home / p.get("worktree", "")).resolve() if p.get("worktree") else None
    if wt is None or not wt.exists():
        die(f"{pid} has no worktree to merge {main} in: `campaign start {pid}` recreates it")
    if git(wt, "symbolic-ref", "--quiet", "--short", "HEAD", check=False) != branch:
        die(f"{wt} is not on {branch}")
    if git(wt, "status", "--porcelain", "--untracked-files=no"):
        die(f"{wt} has uncommitted changes: commit them, then merge {main}")
    done = subprocess.run(
        ["git", "-C", str(wt), "merge", "-q", "--no-edit", "-m", f"Merge {main} into {branch}", main],
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        conflicts = git(wt, "diff", "--name-only", "--diff-filter=U", check=False).split()
        git(wt, "merge", "--abort", check=False)
        die(
            f"merging {main} into {branch} conflicts in {', '.join(conflicts) or done.stderr.strip()}: "
            "resolve it in the quest's worktree; nothing landed"
        )
    head = rev(wt, "HEAD")
    said = f"merged {main} into {branch} at {head[:12]}"
    if git_ok(wt, "remote", "get-url", "origin"):
        pushed = subprocess.run(
            ["git", "-C", str(wt), "push", "-q", "origin", f"HEAD:refs/heads/{branch}"], capture_output=True, text=True
        )
        said += ", pushed" if pushed.returncode == 0 else f" (its push failed: {pushed.stderr.strip()})"
    return said + "; "


def cmd_land(a: argparse.Namespace) -> int:
    with writing() as c, contextlib.ExitStack() as held:
        p = c.quest(a.id)
        repo = c.repo(p)
        held.enter_context(repo_lock(c, repo))
        waited = blockers(c)
        main = main_branch(repo)
        checkout = main_checkout(repo)
        if a.record:
            target = rev(repo, a.record)
            if not ancestor(repo, target, main):
                die(f"{target[:12]} is not on {main}")
            if not a.reason:
                die("--record needs --reason: how it landed without `campaign land`")
            final = rev(repo, p["branch"]) == target if p.get("branch") else True
            # the reviews as they stood against the code the branch started from
            reviewed_from = p.get("base") or target
            unverified: list[str] = []  # with --earlier: verified by the process of its time, as the reason says
            if not a.earlier:
                unverified = check_evidence(c, p, target)[0] + (
                    review_gaps(c, a.id, target, reviewed_from) if final else []
                )
            p["landed"].append(
                {
                    "commit": target,
                    "tested": target,
                    "at": stamp(float(git(repo, "log", "-1", "--format=%ct", target))),
                    "recorded": stamp(),
                    "final": final,
                    "reason": f"{'landed before the campaign' if a.earlier else 'recorded after the fact'}: {a.reason}",
                    **({"earlier": True} if a.earlier else {}),
                    "evidence": check_evidence(c, p, target)[1],
                    "unverified": unverified,
                }
            )
            if final:
                p["status"] = "landed"
            if final and not same_repo(repo, c.home):
                land_journals(c, a.id)
            kept = clean_quest(c, a.id, keep=True) if final and not a.keep else []
            p["updated"] = stamp()
            opened = unlocks(c, waited, f"{a.id} landed") if final else ""
            c.save(
                f"campaign: record {a.id} landed at {target[:12]}" + (" (unverified)" if unverified else ""),
                extra=refresh_map(c),
            )
            print(
                f"{a.id}: recorded {target[:12]}" + (f"; never verified: {'; '.join(unverified)}" if unverified else "")
            )
            for item in kept:
                print(f"kept {item} (`campaign clean {a.id}` once resolved)")
            if opened:
                print(opened)
            return 0
        if a.earlier:
            die("--earlier describes a landing --record records")
        if p["status"] not in ("active", "review"):
            die(f"{a.id} is {p['status']}")
        if why := unparsed_criteria(c, a.id):
            die(why)
        final = a.upto is None
        if not final and not a.reason:
            die("an increment needs --reason (why this prefix is safe to land alone)")
        if a.merge_main and not final:
            die("--merge-main lands the whole quest: drop --upto")
        head = rev(repo, p["branch"])
        target = rev(repo, a.upto) if a.upto else head
        if not ancestor(repo, target, head):
            die(f"{a.upto} is not on {p['branch']}")
        if not final:
            if re.match(r"none\b", section(c.quest_file(a.id), "May land early").strip(), re.I):
                die(f"{a.id}'s quest file says None under 'May land early': it all waits for the review")
            if code_tree(repo, target) == code_tree(repo, head):
                die(f"{a.upto} has the whole quest's code: land it without --upto")
        if git(checkout, "symbolic-ref", "--quiet", "--short", "HEAD", check=False) != main:
            die(f"{checkout} is not on {main}")
        if git(checkout, "status", "--porcelain", "--untracked-files=no"):
            die(f"{checkout} has uncommitted changes")
        for where in dict.fromkeys([checkout, c.home]) if a.push else ():
            sync_main(c, where)
        merged = merge_main_into(c, a.id) if a.merge_main else ""
        if merged:
            target = rev(repo, p["branch"])
        with frozen():  # the gaps only read
            gaps = landing_gaps(c, a.id, target, final=final, ci=a.no_ci is None)
        if gaps:
            queued = landing_reruns(c, a.id) if merged else ""  # what the merge voided, ahead of the queue
            die(merged + f"{a.id} cannot land yet:\n  " + "\n  ".join(gaps) + queued)
        if merged:
            print(merged.rstrip("; "))
        evidence = check_evidence(c, p, target, ci=a.no_ci is None and ci_covers(c))[1]
        carried = landing_journals(c, a.id) if not same_repo(repo, c.home) else journals_at(c, repo, target, a.id)
        proven: dict[str, list[Json]] = (
            criteria_evidence(c, a.id, [target], carried, ci=a.no_ci is None) if final else {}
        )
        met = {x: "; ".join(r["evidence"] for r in rows if r["met"]) for x, rows in proven.items()}
        main_head = rev(repo, main)
        base = merge_base(repo, main_head, target)
        message = f"campaign: land {a.id}" + ("" if final else f" increment ({a.reason})")
        mode = "ff" if base == main_head else "merge"
        c.state["landing"] = {
            "id": a.id,
            "target": target,
            "final": final,
            "reason": a.reason or "",
            "evidence": evidence,
            **({"criteria": met} if met else {}),
            "mode": mode,
            "from": main_head,
            "at": stamp(),
            **({"ci_skipped": a.no_ci} if a.no_ci and c.state.get("ci") else {}),
        }
        write_json(c.path, c.state)
        before = rev(checkout, "HEAD")
        try:
            if mode == "ff":
                git(checkout, "merge", "-q", "--ff-only", target)
            else:
                merged = subprocess.run(
                    ["git", "-C", str(checkout), "merge", "-q", "--no-ff", "-m", message, target],
                    capture_output=True,
                    text=True,
                )
                if merged.returncode != 0:
                    git(checkout, "merge", "--abort", check=False)
                    die(f"merge failed: {merged.stderr.strip()}")
                if not git_ok(checkout, "diff", "--quiet", target, "HEAD", "--", ".", *EXCLUDE_META):
                    git(checkout, "reset", "-q", "--keep", before)
                    die("the merge changed code outside the campaign's files; nothing landed")
            if a.push and (refused := push_main(checkout)):  # the code first: a refused push records nothing
                git(checkout, "reset", "-q", "--keep", before)
                die(
                    f"the push of {main} to origin failed, so {a.id} did not land and {main} is back at "
                    f"{before[:12]}: {refused}\n  `campaign land {a.id} --push` again merges origin's {main} "
                    "first when it changed only campaign files"
                )
        except CampaignError:
            c.state.pop("landing", None)
            write_json(c.path, c.state)
            raise
        record = landing_record(repo, main, c.state.pop("landing"))
        if record is None:
            die(f"{main} does not contain {target[:12]} after landing it")
        if not same_repo(repo, c.home):
            land_journals(c, a.id)
        p["landed"].append(record)
        landed = record["commit"]
        filed: list[Json] = inbox(c, a.id) if final else []
        if final:
            p["status"] = "landed"
            p.setdefault("messages", []).extend({**m, "answered": landed[:12]} for m in filed)
            if deferred(p):
                try:
                    record["later"] = submit_checks(c, a.id, deferred(p), landed, priority=-1)
                except CampaignError as e:
                    print(f"campaign: landed, but the deferred checks were not queued: {e}", file=sys.stderr)
        kept = clean_quest(c, a.id, keep=True) if final and not a.keep else []
        p["updated"] = stamp()
        opened = unlocks(c, waited, f"{a.id} landed") if final else ""
        hunted = close_hunt(c, a.id, repo, landed) if final else ()
        c.save(message + f" at {landed[:12]}", extra=(*refresh_map(c), *hunted))
        for m in filed:
            (mailbox(c, a.id) / f"{m['id']}.json").unlink(missing_ok=True)
        notify(f"Landed {a.id}" if final else f"Landed part of {a.id}", f"{p['title']} at {landed[:12]}")
        if a.push and (refused := push_main(c.home)):
            die(
                f"{a.id} landed and origin has it, but the push of its record from {c.home} failed: {refused}"
                f"\n  push {main_branch(c.home)} from {c.home} once origin's changes are merged"
            )
    via = "" if landed == target else f" (merge {landed[:12]})"
    print(f"{a.id}: landed {target[:12]} on {main}{via}" + ("" if final else " (increment)"))
    for item in kept:
        print(f"kept {item} (`campaign clean {a.id}` once resolved)")
    if opened:
        print(opened)
    return 0


def clean_quest(c: Campaign, pid: str, *, force: bool = False, keep: bool = False) -> list[str]:
    """Removes a landed or dropped quest's worktrees, merged branches and build slot, and returns what
    it kept and why. A worktree with uncommitted or untracked files, or that a job reads from, stops it,
    or with keep is kept; so is an unmerged branch, and a slot its pending jobs use."""
    p = c.quests[pid]
    repo = c.repo(p)
    kept: list[str] = []
    busy = [
        j["id"]
        for j in all_jobs()
        if j["spec"].get("quest") == pid
        and j["spec"].get("campaign", c.ident) in (c.ident, "")
        and j["state"].get("state") not in TERMINAL
    ]
    if busy and not keep:
        die(f"jobs still pending for {pid}: {', '.join(busy)}")
    apart = not same_repo(repo, c.home)  # its journal's worktree and branch are in the home
    for name, branch, _, _ in branches(c, pid):
        task = name.partition("--")[2]
        record = p["tasks"][task] if task else p
        places = [(repo, "worktree"), *([(c.home, "journal")] if apart else [])]
        held = False
        for owner, field in places:
            rel = record.get(field, "")
            wt = (c.home / rel).resolve() if rel else None
            if wt and wt.exists():
                dirty = git(wt, "status", "--porcelain")
                readers = readers_of(c.home / rel) if field == "worktree" else []
                why = (
                    "uncommitted or untracked files"
                    if dirty
                    else f"job {', '.join(readers)} reads from it"
                    if readers
                    else ""
                )
                if why and not force:
                    if not keep:
                        die(
                            f"{wt} has {why} (commit, move or remove them, or --force)"
                            + (f":\n{dirty}" if dirty else "")
                        )
                    kept.append(f"{wt}: {why}")
                    held = True
                    break
                git(owner, "worktree", "remove", *(["--force"] if force else []), str(wt))
            if field in record:
                record[field] = ""
        if held:
            continue
        for owner, _ in places:
            if has_ref(owner, branch):
                if force or ancestor(owner, branch, main_branch(owner)):
                    git(owner, "branch", "-q", "-D" if force else "-d", branch)
                else:
                    kept.append(f"{branch}{' (its journal, in the home)' if owner != repo else ''}: not merged")
    own = quest_slot(c, pid, repo)
    pending = {slot_of(j["spec"]): j["id"] for j in all_jobs() if j["state"].get("state") not in TERMINAL}
    for slot in [jobs_dir() / "slots" / own, *(jobs_dir() / "slots").glob(f"{own}@*")]:  # @: other repositories'
        if slot.name in pending or not drop_slot(slot):
            user = f"job {pending[slot.name]}" if slot.name in pending else "a runner"
            kept.append(f"its build slot {slot.name}: {user} uses it; the last job there removes it")
    return kept


def cmd_clean(a: argparse.Namespace) -> int:
    with writing() as c:
        p = c.quest(a.id)
        if p["status"] not in ("landed", "dropped"):
            die(f"{a.id} is {p['status']}: clean only landed or dropped quests")
        for item in clean_quest(c, a.id, force=a.force):
            print(f"kept {item} (--force removes it)")
        p["updated"] = stamp()
        c.save(f"campaign: clean {a.id}")
    print(f"{a.id}: cleaned")
    return 0
