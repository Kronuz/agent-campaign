"""The job commands (submit, list, show, log, rerun, cancel, prune) and messages."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shlex
import sys
from pathlib import Path

from style import mark, paint, styled

from .campaign import Campaign, find_campaign, writing
from .core import (
    HEAVY,
    LIGHT_RUN,
    LONG_RUN,
    SHORT_RUN,
    TERMINAL,
    CampaignError,
    Json,
    csv,
    die,
    git,
    git_ok,
    jobs_dir,
    plural,
    read_json,
    stamp,
    write_json,
)
from .evidence import criteria_for
from .jobs import FAILS_WITHOUT, all_jobs, attempt_dir, claim, finish, load_job, matches_inputs, resolve_job, tag
from .mail import announce, inbox, leave_message, mailbox, peers, send_message
from .repo import ancestor, changed, ever_on, frozen, has_ref, is_meta, main_branch, rev, same_repo, toplevel
from .reuse import submit, supersede
from .reviews import landing_gaps
from .runner import ensure_worker, reap, spawn
from .state import branches, campaigns
from .status import JOB_MARKS
from .wait import wait_for
from .worker import slot_cores

PARALLEL = re.compile(r"(?:\s-j\s*|--jobs[=\s]+|--parallel[=\s]+|\s-P\s*)(\d+)")
BACKGROUND = re.compile(r"(?<![&>|])&(?![&>])")  # a lone &: not &&, >&, &> or |&
LOOP_BODY = re.compile(r"\b(?:for|while|until)\b.*?\bdo\b(.*)\bdone\b", re.S)  # to the last done: loops nest
GNU_PARALLEL = re.compile(r"(?:^|[\s;&|(])parallel(?=\s)")


def copies(cmd: str) -> tuple[int, str]:
    """How many copies of its work a command visibly runs at once, and what shows it: its largest -j,
    --jobs, --parallel or -P N, its commands sent to the background, GNU parallel's one per CPU, or a
    loop that backgrounds each pass (counted as every CPU)."""
    ncpu = os.cpu_count() or 4
    if any(BACKGROUND.search(body) for body in LOOP_BODY.findall(cmd)):
        return ncpu, "a loop that sends each pass to the background"
    found = [(int(m.group(1)), m.group(0).strip()) for m in PARALLEL.finditer(cmd)]
    if GNU_PARALLEL.search(cmd) and not found:
        found.append((ncpu, "parallel, one copy per CPU"))
    if n := len(BACKGROUND.findall(cmd)):
        found.append((n, f"{plural(n, 'command')} in the background"))
    return max(found, default=(1, ""))


CTEST = re.compile(r"(?:^|[\s;&|(/])ctest(?=[\s;&|)]|$)")


def ctest_warning(cmd: str) -> str:
    """Why a command running ctest should say --stop-on-failure: ctest has no environment variable
    for it, and a suite that runs on past its first failure can run to its time cap."""
    if CTEST.search(cmd) and "--stop-on-failure" not in cmd:
        return "runs ctest without --stop-on-failure: it runs on past the first failure, up to its time cap"
    return ""


def quest_slot(c: Campaign, pid: str, repo: Path) -> str:
    """The build slot of a quest's jobs in REPO; the one in the quest's own repository is verify's."""
    name = f"{c.state['campaign']}-{pid}"
    return name if same_repo(repo, c.repo(c.quests[pid])) else f"{name}@{repo.name}"


def parse_ref(top: Path, item: str) -> tuple[str, Json]:
    name, sep, value = item.partition("=")
    if not sep or not re.fullmatch(r"[A-Za-z0-9_]+", name):
        die(f"--ref NAME=REF or NAME=PATH@REF, not {item!r}")
    where, at, ref = value.rpartition("@")
    repo = toplevel(Path(where).resolve()) if at and Path(where).is_dir() else top
    return name, {"repo": str(repo), "commit": rev(repo, ref if at and Path(where).is_dir() else value)}


PATH_TOKEN = re.compile(r"(?<![\w.~/-])~?/[^\s'\"`;|&()<>,=]+")  # an absolute or ~ path in a command


def outside_roots(repo: str) -> list[Path]:
    """The job store and every checkout of REPO: where a command's paths are another job's files."""
    roots = [jobs_dir().resolve()]
    if repo:
        listing = git(Path(repo), "worktree", "list", "--porcelain", check=False)
        roots += [Path(x[len("worktree ") :]).resolve() for x in listing.splitlines() if x.startswith("worktree ")]
    return roots


def outside_inputs(command: str, repo: str) -> list[str]:
    """Paths in a command into another job's files or a checkout of its repository: inputs that are
    not part of the job's identity, so a rerun at the same code may see different files."""
    roots = outside_roots(repo)
    found: list[str] = []
    for token in PATH_TOKEN.findall(command):
        path = Path(token).expanduser().resolve()
        if any(path == r or path.is_relative_to(r) for r in roots):
            found.append(token)
    return list(dict.fromkeys(found))


def script_digests(command: str, repo: str) -> dict[str, str]:
    """The files a command names outside its checkout (in the job store, a checkout of REPO or the
    user's home), each with a digest of its bytes: what an evidence job's identity adds."""
    roots = [*outside_roots(repo), Path.home().resolve()]
    out: dict[str, str] = {}
    for token in dict.fromkeys(PATH_TOKEN.findall(command)):
        path = Path(token).expanduser().resolve()
        if path.is_file() and any(path.is_relative_to(r) for r in roots):
            with contextlib.suppress(OSError), path.open("rb") as f:
                digest = hashlib.file_digest(f, "sha256") if sys.version_info >= (3, 11) else hashlib.sha256(f.read())
                out[token] = digest.hexdigest()[:24]
    return out


SUBMIT_FLAGS = (
    "--ref",
    "--for",
    "--fails-without",
    "--quest",
    "--task",
    "--inputs",
    "--build",
    "--slot",
    "--commit",
    "--level",
    "--after",
)


def parsed_submit(spec: Json, a: argparse.Namespace, ids: list[str]) -> str:
    """What job submit understood, one line per part: its command and the options that shape the job."""
    refs = ", ".join(f"{k}={Path(r['repo']).name}@{r['commit'][:12]}" for k, r in spec.get("refs", {}).items())
    options = {
        "level": f"{spec['level']} ({spec['minutes']} min)",
        "commit": (spec.get("commit") or "")[:12],
        "quest": spec.get("quest"),
        "task": spec.get("task"),
        "inputs": ",".join(spec.get("inputs", [])),
        "for": ",".join(ids),
        "fails-without": "the command must exit nonzero" if a.fails_without else "",
        "ref": refs,
        "cwd": spec.get("cwd"),
        "slot": spec.get("slot"),
        "build": spec.get("build"),
        "cpus": spec.get("cpus"),
        "after": ",".join(spec.get("after", [])),
        "env": " ".join(f"{k}={v}" for k, v in spec["env"].items()),
    }
    shown = "  ".join(f"{k}: {v}" for k, v in options.items() if v)
    return f"command: {spec['cmd']}\noptions: {shown}"


def cmd_job_submit(a: argparse.Namespace) -> int:
    cmd = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd
    if not cmd:
        die("give the command after --")
    command = cmd[0] if len(cmd) == 1 else shlex.join(cmd)
    cap = SHORT_RUN if a.level in HEAVY else LIGHT_RUN if LONG_RUN.search(command) else 0
    minutes = a.minutes or (SHORT_RUN if a.level in HEAVY else 30)
    if cap and minutes > cap and not a.long:
        what = f"a {a.level} job" if a.level in HEAVY else "a light fuzz or soak, its build included,"
        die(f"{what} lasts at most {cap} minutes: one job per row, or --long REASON to defer it")
    if a.long and a.criteria:
        die("a --long job is deferred: it proves no criterion before landing")
    spec: Json = {
        "cmd": command,
        "level": a.level,
        "minutes": minutes,
        "priority": -1 if a.long else a.priority or 0,
        **({"long": a.long} if a.long else {}),
        "after": csv(a.after),
        "retries": a.retries,
        "keep": a.keep,
        "slot": a.slot or "",
        **({"build": a.build} if a.build else {}),
        **({"inputs": csv(a.inputs)} if a.inputs and csv(a.inputs) else {}),
    }
    spec["env"] = dict(x.split("=", 1) for x in a.env)
    cwd = Path.cwd()
    default_ref = "HEAD"
    repo = Path(a.repo).resolve() if a.repo else cwd
    c: Campaign | None = None
    p: Json = {}
    if (a.criteria or a.fails_without or a.task) and not a.quest:
        die("--for, --fails-without and --task name a quest's: add --quest ID")
    if a.fails_without and not a.criteria:
        die("--fails-without needs --for: the criteria whose test fails without the fix")
    if a.fails_without:
        spec["fails_without"] = FAILS_WITHOUT
    ids: list[str] = []
    if a.quest:
        c = find_campaign(cwd)
        p = c.quest(a.quest)
        spec.update(quest=a.quest, campaign=c.ident)
        ids = criteria_for(c, a.quest, a.criteria or "")
        repo = Path(a.repo).resolve() if a.repo else c.repo(p)
        default_ref = p.get("branch") or "HEAD"
        if a.task:
            task = p.get("tasks", {}).get(a.task)
            if task is None:
                die(f"{a.quest} has no task {a.task}")
            spec["task"], default_ref = a.task, task["branch"]
        if a.priority is None and not a.long and a.level == "light" and p["status"] in ("active", "review"):
            spec["priority"] = 1  # direct-path evidence, as verify's gate checks; measurements keep 0
    if a.unversioned or not git_ok(repo, "rev-parse", "--git-dir"):
        spec.update(repo="", commit="", refs={}, cwd=str(Path(a.cwd or cwd).resolve()))
    else:
        top = toplevel(repo)
        invoking = top
        if git_ok(cwd, "rev-parse", "--git-dir") and git(
            cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"
        ) == git(top, "rev-parse", "--path-format=absolute", "--git-common-dir"):
            invoking = toplevel(cwd)
        spec.update(repo=str(top), commit=rev(invoking if a.commit else top, a.commit or default_ref))
        spec["refs"] = dict(parse_ref(top, x) for x in a.ref)
        rel = a.cwd if a.cwd is not None else (os.path.relpath(cwd, invoking) if cwd.is_relative_to(invoking) else "")
        spec["cwd"] = "" if rel == "." else rel
        if c and not (a.slot or a.fresh or a.keep) and p["risk"] != "critical":
            spec["slot"] = quest_slot(c, a.quest, top)  # the quest's build: a busy slot is waited for
        if not a.commit and not a.quest and git(top, "status", "--porcelain", "--untracked-files=no"):
            print(
                f"note: uncommitted changes in {top} are not in the job; it runs {spec['commit'][:12]}", file=sys.stderr
            )
    if spec.get("inputs") and not (
        spec.get("commit") and matches_inputs(Path(spec["repo"]), spec["commit"], spec["inputs"])
    ):
        die(f"--inputs {','.join(spec['inputs'])} match no file at the job's commit")
    outside = outside_inputs(command, spec.get("repo", ""))
    if outside:
        print(
            f"warning: {', '.join(outside)} {'is' if len(outside) == 1 else 'are'} outside this job's own checkout, so "
            "not part of its identity: a rerun at the same code may see other files. Build it in this job, or pass "
            "another repository with --ref NAME=PATH@REF",
            file=sys.stderr,
        )
    if warning := ctest_warning(command):
        print(f"warning: this job {warning}", file=sys.stderr)
    light_work = [x for x, light in ((command, a.level == "light"), (a.build or "", bool(a.build))) if light]
    n, shown = max((copies(x) for x in light_work), default=(1, ""))
    if a.cpus:
        spec["cpus"] = a.cpus
    elif n > max(1.0, slot_cores()):  # one copy fits any slot: a host with under a core a slot warns of none
        print(
            f"warning: this light job runs {shown}, more than a light slot's {slot_cores():g} cores: --cpus N "
            "declares them, else the worker charges it the slots it is measured using",
            file=sys.stderr,
        )
    if a.quest and spec.get("commit"):
        spec["scripts"] = script_digests(command, spec["repo"])
        if spec["scripts"]:
            print(
                f"note: the bytes of {', '.join(spec['scripts'])} are part of this evidence job's identity: "
                "a changed script is another job",
                file=sys.stderr,
            )
    print(parsed_submit(spec, a, ids), file=sys.stderr)
    stray: list[str] = [x for x in cmd if x in SUBMIT_FLAGS] if len(cmd) > 1 else []
    if stray:
        print(
            f"warning: {', '.join(stray)} after -- belong{'s' if len(stray) == 1 else ''} to the command, not to "
            "job submit: put options before --",
            file=sys.stderr,
        )
    spec["cmux"] = os.environ.get("CMUX_WORKSPACE_ID", "")
    spec["what"] = a.what or command[:60] + (f" at {spec['commit'][:12]}" if spec.get("commit") else "")
    spec.setdefault("quest", "")
    if "campaign" not in spec:
        spec["campaign"] = ""
        with contextlib.suppress(CampaignError):
            spec["campaign"] = find_campaign(Path.cwd()).ident
    if ids and not spec.get("commit"):
        die("an unversioned job is never evidence: drop --for")
    jid, reused = submit(spec, force=a.force)
    if c is not None and ids:
        tag(jid, c.ident, a.quest, ids, a.fails_without)
    print(jid + (f" (reused: {resolve_job(jid)['spec'].get('what', '')})" if reused else ""))
    if not reused:
        supersede([jid])
    if a.sync:
        job = resolve_job(jid)
        if job["state"].get("state") == "queued" and claim(job):
            spawn(["_run", str(job["dir"])], job["dir"] / "runner.log").wait()
        return wait_for([jid], any_one=False, timeout=0)
    ensure_worker()
    return 0


def cmd_job_list(a: argparse.Namespace) -> int:
    reap()
    ident = ""
    if not a.all:  # this campaign's jobs, where one is found
        with contextlib.suppress(CampaignError):
            ident = find_campaign(Path.cwd()).ident
    shown = 0
    for j in all_jobs():
        s, st = j["spec"], j["state"]
        if (a.quest and s.get("quest") != a.quest) or (a.state and st.get("state") != a.state):
            continue
        if ident and s.get("campaign") != ident:
            continue
        if not shown:
            print(
                paint(f"{'' if not styled() else '   '}{'JOB':20}  {'STATE':11} {'LEVEL':9} {'QUEST':14} WHAT", "dim")
            )
        shown += 1
        exit_code = "" if st.get("exit") is None else f" exit {st['exit']}"
        state = st.get("state", "?")
        color, emoji = JOB_MARKS.get(state, ("dim", "❔"))
        print(
            f"{mark(emoji)}{j['id']}  {paint(f'{state:11}', color)} {s.get('level', '?'):9} {s.get('quest') or '-':14} {s.get('what', '')}{exit_code}"
        )
    if not shown:
        print("no jobs" + (" of this campaign (--all lists every campaign's)" if ident else ""))
    return 0


def cmd_job_show(a: argparse.Namespace) -> int:
    j = resolve_job(a.job)
    ad = attempt_dir(j)
    print(
        json.dumps(
            {"id": j["id"], "spec": j["spec"], "state": j["state"], "log": str(ad / "log"), "out": str(ad / "out")},
            indent=2,
            default=str,
        )
    )
    return 0


def cmd_job_log(a: argparse.Namespace) -> int:
    log = attempt_dir(resolve_job(a.job)) / "log"
    lines = log.read_text(errors="replace").splitlines() if log.exists() else []
    print("\n".join(lines[-a.lines :]))
    return 0


def message_target(text: str) -> tuple[str, str]:
    """(campaign name, quest id) from ID, NAME:ID, NAME: (its lead) or : (this campaign's)."""
    name, sep, pid = text.rpartition(":")
    return (name, pid) if sep else ("", text)


def cmd_message(a: argparse.Namespace) -> int:
    """Sends a message to a quest or a lead, from anywhere; or (the lead) closes one."""
    name, pid = message_target(a.quest)
    whom = pid or "the lead"
    if a.close:
        if not a.reason:
            die("closing a message needs --reason R")
        with writing() as c:
            if name and name != c.name:
                die(f"close {name}'s messages from its home ({name}'s main checkout, or -C)")
            record = c.quest(pid) if pid else c.state
            message = next((m for m in inbox(c, pid) if m["id"] == a.close), None)
            if message is None:
                die(f"{whom} has no open message {a.close}")
            record.setdefault("messages", []).append({**message, "closed": a.reason})
            if pid:
                record["updated"] = stamp()
            c.save(f"campaign: close {a.close} for {whom}")
            (mailbox(c, pid) / f"{a.close}.json").unlink()
        print(f"{whom}: {a.close} closed")
        return 0
    if not a.text:
        die("give the message's text")
    here: Campaign | None = None
    lost = ""
    try:
        here = find_campaign(Path.cwd())
    except CampaignError as e:
        lost = str(e)
    if name and name not in campaigns() and here is not None and name in peers(here):
        uid = send_message(here, name, pid, a.text, a.by)
        print(f"{uid} for {name}:{pid or ''} queued in {here.name}'s outbox (`campaign wait` there fetches it)")
        return 0
    if name and here is not None and name == here.name:
        c = here
    elif name:
        other = campaigns().get(name)
        if other is None:
            die(
                f"no campaign {name} on this machine or among the peers (`campaign status --all`, `campaign config peers`)"
            )
        c = Campaign(Path(other["home"]), name)
    elif here is not None:
        c = here
    else:
        die(f"{lost}; or address the message as NAME:ID or NAME:")
    if pid:
        p = c.quest(pid)
        if p["status"] in ("landed", "dropped"):
            die(
                f"{pid} is {p['status']}: a finding about it is a new quest; tell the lead (`campaign message {c.name}: ...`)"
            )
    nid = leave_message(c, pid, a.text, a.by, wake=False)
    then = "its journal answers it by that id, or the lead closes it" if pid else "the lead closes it"
    print(f"{nid} for {whom}: {then}")
    message = read_json(mailbox(c, pid) / f"{nid}.json") or {}
    why = announce(c, mailbox(c, pid) / f"{nid}.json", message, whom) if message else "the message is gone"
    if why and name and name != (here.name if here else ""):
        print(f"campaign: {nid} waits in {c.name}'s mailbox unannounced ({why}); it is retried", file=sys.stderr)
    return 0


def job_ok_or_pending(jid: str) -> bool:
    """Whether job JID passed or has not ended yet: a job ordered after it can still run."""
    with contextlib.suppress(CampaignError):
        return resolve_job(jid)["state"].get("state") not in set(TERMINAL) - {"done"}
    return False


LANDING_PRIORITY = 3  # a landing quest's reruns at a merge from main: above gate checks (2), so it never chases main
VOIDED = ("criteria without evidence at ", "evidence jobs ran other code than ", "check ")  # what a merge voids


def own_head(repo: Path, head: str, main: str) -> str:
    """The quest's own last commit under HEAD's merges from MAIN and campaign-record commits; "" when
    HEAD holds no merge from main above it."""
    own, merged = head, False
    while not ancestor(repo, own, main):  # on main: the walk left the quest's own commits
        parents = git(repo, "rev-list", "--parents", "-n1", own).split()[1:]
        if len(parents) == 2 and ancestor(repo, parents[1], main):
            own, merged = parents[0], True
        elif len(parents) == 1 and all(is_meta(x) for x in changed(repo, parents[0], own)):
            own = parents[0]
        else:
            return own if merged else ""
    return ""


def at_landing(c: Campaign, pid: str) -> list[str] | None:
    """The gaps at the quest's branch head when they are only evidence a merge from main voided (its
    criteria and checks were met at its own commit below the merge); None when anything else is
    missing, or no merge from main is in the way. Its reruns run at LANDING_PRIORITY."""
    p = c.quests.get(pid)
    if p is None or p.get("status") not in ("active", "review") or not p.get("branch"):
        return None
    with contextlib.suppress(CampaignError):
        repo = c.repo(p)
        main = main_branch(repo)
        head = rev(repo, p["branch"])
        own = own_head(repo, head, main)
        if not own:
            return None
        with frozen():
            before = landing_gaps(c, pid, own, final=True, ci=False)
            now = landing_gaps(c, pid, head, final=True, ci=False)
        if not any(g.startswith(VOIDED) for g in before) and all(
            g.startswith((f"{main} changed ", *VOIDED)) for g in now
        ):
            return now
    return None


def cmd_job_rerun(a: argparse.Namespace) -> int:
    """The same jobs (command, level, directory, environment, other repositories) at another commit:
    by default the head of their quest's branch, in whichever of the job's repositories holds it
    (its own or a `--ref`'s), else of their repository."""
    rerun_jobs(a.jobs, a.commit, a.minutes)
    return 0


def rerun_jobs(
    jobs: list[str], at: str | None = None, minutes: int | None = None, c: Campaign | None = None
) -> list[str]:
    """`job rerun`: returns the new (or reused) job ids. A quest's light evidence rerun at its head
    while it is `at_landing` runs at LANDING_PRIORITY; every other job keeps its priority."""
    specs = {j["id"]: dict(j["spec"]) for j in map(resolve_job, jobs)}
    if not at and (old := [jid for jid, s in specs.items() if s.get("fails_without")]):
        die(
            f"{', '.join(old)} must fail at the code without the fix: at the quest's head they run the fix; "
            "name that code with --commit"
        )
    if minutes is not None and minutes <= 0:
        die("--minutes is a positive number")
    minutes = minutes or 0  # 0: each job keeps its own
    for jid, spec in specs.items() if minutes else ():  # a job that timed out, given the time it needs
        heavy = spec["level"] in HEAVY
        cap = SHORT_RUN if heavy else LIGHT_RUN if LONG_RUN.search(spec["cmd"]) else 0
        if cap and minutes > cap and not spec.get("long"):
            die(f"{jid}: a {spec['level']} job lasts at most {cap} minutes: one job per row, or submit it --long")
        spec["minutes"] = minutes
    order: list[str] = []

    def visit(jid: str) -> None:  # a job after its prerequisites, so a rerun chain keeps its order
        if jid not in order:
            for x in specs[jid].get("after", []):
                if x in specs:
                    visit(x)
            order.append(jid)

    for jid in specs:
        visit(jid)
    if c is None:
        with contextlib.suppress(CampaignError):
            c = find_campaign(Path.cwd())

    def quest_code(spec: Json) -> tuple[str, Path | None, list[str]]:
        """The quest's branch, its repository and every branch its code is on (its tasks', main)."""
        if at or not spec.get("quest") or c is None:
            return "", None, []
        with contextlib.suppress(CampaignError):
            quest = c.quest(spec["quest"])
            home = c.repo(quest)
            task = quest.get("tasks", {}).get(spec.get("task", ""), {})
            branch = task["branch"] if task.get("branch") and has_ref(home, task["branch"]) else quest.get("branch", "")
            return branch, home, [b for _, b, _, _ in branches(c, spec["quest"])] + [main_branch(home)]
        return "", None, []

    for jid in order:  # every job is checked before any is queued
        spec = specs[jid]
        if not spec.get("commit"):
            die(f"{jid} is unversioned: submit it again")
        branch, home, lines = quest_code(spec)
        if not branch or home is None:
            continue
        mine = same_repo(Path(spec["repo"]), home)
        moving = (
            [spec["commit"]]
            if mine
            else [r["commit"] for r in spec["refs"].values() if same_repo(Path(r["repo"]), home)]
        )
        for x in moving:
            if not ever_on(home, x, lines):
                die(
                    f"{jid} ran at {x[:12]}, which was never on {', '.join(lines)} (a mutant or scratch commit?): "
                    f"moving it to {branch} would drop that change. Pass --commit {x[:12]} to keep it, or "
                    f"--commit {branch} to move it"
                )
    landing = {
        q for q in {s.get("quest") for s in specs.values()} if not at and c and q and at_landing(c, q) is not None
    }
    renamed: dict[str, str] = {}
    queued: list[str] = []
    ids: list[str] = []
    for jid in order:
        spec = specs[jid]
        branch, home, _ = quest_code(spec)
        commit = ""
        if branch and home is not None and not same_repo(Path(spec["repo"]), home):
            refs = {k: dict(r) for k, r in spec.get("refs", {}).items()}
            for r in refs.values():
                if same_repo(Path(r["repo"]), home):  # the quest's code is a ref: move it, keep the job's commit
                    r["commit"], commit = rev(Path(r["repo"]), branch), spec["commit"]
            spec["refs"] = refs
            branch = ""  # the job's own repository does not hold the quest's branch
        commit = commit or rev(Path(spec["repo"]), at or branch or "HEAD")
        if spec.get("quest"):  # an evidence job's outside scripts as they are now
            spec["scripts"] = script_digests(spec["cmd"], spec["repo"])
        what, old = spec.get("what", ""), f" at {spec['commit'][:12]}"
        ended = [x for x in spec.get("after", []) if x not in renamed and not job_ok_or_pending(x)]
        for x in ended:  # a predecessor that ended unpassed would skip the rerun again
            print(f"{jid}: no longer after {x}, which ended {resolve_job(x)['state'].get('state')}")
        spec.update(
            commit=commit,
            after=[renamed.get(x, x) for x in spec.get("after", []) if x not in ended],
            cmux=os.environ.get("CMUX_WORKSPACE_ID", ""),
            what=(what[: -len(old)] if what.endswith(old) else what) + f" at {commit[:12]}",
        )
        lifted = (
            c is not None
            and spec.get("quest") in landing
            and spec.get("campaign") == c.ident
            and spec["level"] not in HEAVY
            and int(spec.get("priority", 0)) >= 1
        )  # the quest's own direct-path evidence and gate checks; measurements and deferred work keep theirs
        if lifted:
            spec["priority"] = max(int(spec["priority"]), LANDING_PRIORITY)
        new, reused = submit(spec)
        if lifted and reused:  # a rerun queued before the quest reached landing moves up with it
            lift_queued(new, int(spec["priority"]))
        for t in resolve_job(jid).get("tags", []):  # the rerun proves what the job proved
            tag(new, t["campaign"], t["quest"], t["for"], t["fails_without"])
        renamed[jid] = new
        moved = "".join(
            f", {k} at {r['commit'][:12]}"
            for k, r in spec.get("refs", {}).items()
            if home and same_repo(Path(r["repo"]), home)
        )
        print(f"{new}" + (" (reused)" if reused else "") + f": {jid} at {commit[:12]}{moved}")
        if not reused:
            queued.append(new)
        ids.append(new)
    supersede(queued)
    ensure_worker()
    return ids


def lift_queued(jid: str, priority: int) -> None:
    """Raises a still-queued job's priority to PRIORITY."""
    d = jobs_dir() / jid
    spec, state = read_json(d / "spec.json"), read_json(d / "state.json")
    if spec and state and state.get("state") == "queued" and int(spec.get("priority", 0)) < priority:
        write_json(d / "spec.json", {**spec, "priority": priority})


def cmd_job_cancel(a: argparse.Namespace) -> int:
    j = resolve_job(a.job)
    state = j["state"].get("state")
    if state not in ("queued", "running"):
        die(f"{a.job} is {state}")
    if state == "queued" and claim(j):
        finish(load_job(j["dir"]), -1, "cancelled")
    else:
        (j["dir"] / "cancel").touch()
    print(f"{a.job}: cancelling")
    return 0
