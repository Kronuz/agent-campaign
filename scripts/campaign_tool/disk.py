"""Disk: pruning the job store, sweeping merged worktrees, and warning before the disk fills."""

from __future__ import annotations

import argparse
import contextlib
import fnmatch
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from .campaign import Campaign
from .ci import ci_covers
from .core import (
    TERMINAL,
    CampaignError,
    Json,
    git,
    git_ok,
    jobs_dir,
    mtime,
    plural,
    read_json,
    size_text,
    stamp,
    write_json,
)
from .evidence import JOB_ID
from .jobs import _jobs, all_jobs, slot_of
from .journals import journal_place, journal_text, journals_at
from .mail import notify
from .repo import ancestor, common_dir, has_ref, main_branch, main_checkout, rev, same_repo
from .reuse import job_for_check
from .runner import drop_slot, reap, slot_idle, worktree_remove
from .state import branches, campaigns, gates


def readers_of(path: Path, jobs: list[Json] | None = None) -> list[str]:
    """Jobs whose command, directory or environment names a path inside PATH: inputs that live in a
    worktree outside any job's identity, which removing the worktree would lose."""
    roots = {str(path), str(path.resolve())}
    found: list[str] = []
    for j in all_jobs() if jobs is None else jobs:
        spec = j["spec"]
        text = " ".join([str(spec.get("cmd", "")), str(spec.get("cwd", "")), *map(str, spec.get("env", {}).values())])
        if any(root in text for root in roots):
            found.append(j["id"])
    return found


SLIM_BYTES = 1 << 20  # a slimmed job keeps its record, its log and its output files up to this size
BUILD_DIRS = ("build", "build-*", ".deps", "_deps")  # regenerable: prune removes them from idle worktrees
BUILD_DAYS = 3.0  # a build directory nothing changed in for this long is idle
DISK_WARN_GB = 100.0  # check and the daily prune warn under this much free disk (`config disk_warn`)
SLOT_DAYS = 7  # prune keeps a slot a quest's job ran in within this many days


def job_needs() -> Json:
    """What the campaigns on this machine need from the job store. whole: the jobs an open quest uses
    (its journals, reviews, gate evidence, confirmations). record: the jobs only landed or dropped
    quests name (landings, acceptances, journals, reviews): evidence kept as a record, their bulk not.
    open: the (campaign, quest) of each open quest; slots: their build slots; known: every campaign."""
    whole: set[str] = set()
    record: set[str] = set()
    open_quests: set[tuple[str, str]] = set()
    slots: set[str] = set()
    known: set[str] = set()
    for name, o in campaigns().items():
        c = Campaign(Path(o["home"]), name)
        known.add(c.ident)
        closed = {pid for pid, p in c.quests.items() if p["status"] in ("landed", "dropped")}
        folder = c.home / c.meta / "reviews"
        for f in sorted(folder.glob("*.md")) if folder.is_dir() else []:
            (record if f.stem.rsplit("-", 1)[0] in closed else whole).update(JOB_ID.findall(f.read_text()))
        for pid, p in c.quests.items():
            named = record if pid in closed else whole
            repo = c.repo(p)
            for landing in p.get("landed", []):
                named.update(landing.get("later", []))
                named.update(e["job"] for e in landing.get("evidence", []) if e.get("job"))
                named.update(JOB_ID.findall(" ".join(landing.get("criteria", {}).values())))
                home = same_repo(repo, c.home)
                carried = (
                    journals_at(c, repo, landing["commit"], pid)
                    if home
                    else journals_at(c, c.home, main_branch(c.home), pid)
                )
                for text in carried.values():
                    named.update(JOB_ID.findall(text))
            for acc in p.get("accepted", {}).values():
                named.update(j["job"] for f in acc.get("findings", {}).values() for j in f.get("jobs", []))
            for jname, branch, _, _ in branches(c, pid):
                named.update(JOB_ID.findall(journal_text(c, jname, branch)))
            if pid in closed:
                continue
            open_quests.add((c.ident, pid))
            slots.add(f"{name}-{pid}")
            for _, branch, _, _ in branches(c, pid):
                if has_ref(repo, branch):
                    head = rev(repo, branch)
                    whole.update(
                        j["id"]
                        for j in (
                            job_for_check(
                                repo, head, x, c.state.get("tested_docs", []), ci=ci_covers(c), files=p["files"]
                            )
                            for x in gates(p)
                        )
                        if j
                    )
    return {"whole": whole, "record": record - whole, "open": open_quests, "slots": slots, "known": known}


def bulky(j: Json) -> bool:
    """Whether a job holds more than its record: a kept checkout, or an output file over SLIM_BYTES."""
    if (j["dir"] / "src").exists() or any(j["dir"].glob("ref-*")):
        return True
    return any(
        f.is_file() and not f.is_symlink() and f.stat().st_size > SLIM_BYTES
        for out in j["dir"].glob("a*/out")
        for f in out.rglob("*")
    )


def prune_plan(days: float) -> Json:
    """What `job prune` does to finished jobs older than DAYS: deletes those nothing names, slims those
    only landed or dropped quests name, and removes idle build slots. Jobs of open quests, and of
    campaigns this machine does not know ("unknown", counted per campaign), are kept whole."""
    reap()
    cutoff = time.time() - days * 86400
    needs = job_needs()
    plan: Json = {"delete": [], "slim": [], "slots": [], "unknown": {}}
    for j in all_jobs():
        st, spec = j["state"], j["spec"]
        owner = spec.get("campaign", "")
        if owner and owner not in needs["known"]:
            plan["unknown"][owner] = plan["unknown"].get(owner, 0) + 1
            continue
        ended = st.get("ended") or mtime(j["dir"] / "state.json") or time.time()
        if st.get("state") not in TERMINAL or ended > cutoff or j["id"] in needs["whole"]:
            continue
        if (owner, spec.get("quest")) in needs["open"]:
            continue
        if j["id"] in needs["record"]:
            if not st.get("slimmed") and bulky(j):
                plan["slim"].append(j)
            continue
        plan["delete"].append(j)
    root = jobs_dir() / "slots"
    used = slots_in_use()
    plan["slots"] = [
        x
        for x in (sorted(root.iterdir()) if root.is_dir() else [])
        if x.is_dir()
        and not x.name.endswith(".lock")
        and x.name.partition("@")[0] not in needs["slots"]  # NAME@REPO: an open quest's other repository
        and x.name not in used
        and slot_idle(x)
        and (mtime(x) or 0) < cutoff
    ]
    return plan


def slots_in_use() -> set[str]:
    """The slots prune keeps whatever their age: those a pending job names, and those a quest's job
    (evidence) ran in within SLOT_DAYS. A slot's own mtime moves only when its top level changes."""
    recent = time.time() - SLOT_DAYS * 86400
    used: set[str] = set()
    for j in all_jobs():
        st, name = j["state"], slot_of(j["spec"])
        ran = st.get("ended") or st.get("started") or 0
        if name and (st.get("state") not in TERMINAL or (j["spec"].get("quest") and ran > recent)):
            used.add(name)
    return used


def drop_checkouts(j: Json) -> None:
    spec = j["spec"]
    if spec.get("repo"):
        worktree_remove(spec["repo"], j["dir"] / "src")
    for ref in spec.get("refs", {}).values():
        for d in j["dir"].glob("ref-*"):
            worktree_remove(ref["repo"], d)


def prune(
    days: float, dry_run: bool = False, beat: Callable[[], None] = lambda: None, build_days: float = BUILD_DAYS
) -> None:
    """Carries out prune_plan and build_plan, logging each deletion and slimming in the job store's prune.log."""
    plan = prune_plan(days)
    builds = build_plan(build_days)
    for j in plan["delete"]:
        print(f"delete {j['id']}  {j['state'].get('state')}  {j['spec'].get('what', '')}")
    for j in plan["slim"]:
        print(f"slim {j['id']}  {j['spec'].get('what', '')}")
    for x in plan["slots"]:
        print(f"slot {x.name}")
    for owner, n in sorted(plan["unknown"].items()):
        print(f"kept {plural(n, 'job')} of {owner}: no campaign on this machine is there")
    for d, n in builds:
        print(f"build {d}  {size_text(n)}")
    jobs, slim, slots = (plural(len(plan[k]), "slot" if k == "slots" else "job") for k in ("delete", "slim", "slots"))
    dirs = f"{plural(len(builds), 'build dir')} ({size_text(sum(n for _, n in builds))}, idle {build_days:g} days)"
    if dry_run:
        print(f"would delete {jobs}, slim {slim} and remove {slots} (older than {days:g} days) and {dirs}")
        return
    with open(jobs_dir() / "prune.log", "a") as log:
        for j in plan["delete"]:
            drop_checkouts(j)
            shutil.rmtree(j["dir"], ignore_errors=True)
            log.write(f"{stamp()} deleted {j['id']} {j['spec'].get('what', '')}\n")
            beat()
        for j in plan["slim"]:
            drop_checkouts(j)
            for out in j["dir"].glob("a*/out"):
                for f in list(out.rglob("*")):
                    if f.is_file() and not f.is_symlink() and f.stat().st_size > SLIM_BYTES:
                        f.unlink()
            state = read_json(j["dir"] / "state.json") or {}
            write_json(j["dir"] / "state.json", {**state, "slimmed": stamp()})
            _jobs.pop(j["id"], None)
            log.write(f"{stamp()} slimmed {j['id']} {j['spec'].get('what', '')}\n")
            beat()
        for x in plan["slots"]:
            if not drop_slot(x):
                continue  # a job took it since the plan
            log.write(f"{stamp()} removed slot {x.name}\n")
            beat()
        for d, n in builds:
            shutil.rmtree(d, ignore_errors=True)
            log.write(f"{stamp()} removed build dir {d} ({size_text(n)})\n")
            beat()
    (jobs_dir() / "disk.json").unlink(missing_ok=True)  # the sizes it cached changed
    keys = jobs_dir() / "keys"
    for link in keys.iterdir() if keys.is_dir() else []:
        if link.is_symlink() and not (jobs_dir() / os.readlink(link)).exists():
            link.unlink()
    for link in (jobs_dir() / "reads").glob("*/*"):
        if not (jobs_dir() / link.name / "spec.json").exists():
            link.unlink()
    print(f"deleted {jobs}, slimmed {slim} and removed {slots} (older than {days:g} days) and {dirs}")


def daily_prune(beat: Callable[[], None]) -> bool:
    """The worker's housekeeping once its queue empties: `job prune` with its defaults, at most once a
    day (CAMPAIGN_AUTO_PRUNE=0 turns it off). True when it ran."""
    if os.environ.get("CAMPAIGN_AUTO_PRUNE") == "0":
        return False
    mark = jobs_dir() / ".pruned"
    last = mtime(mark)
    if last is not None and time.time() - last < 86400:
        return False
    mark.touch()
    try:
        prune(14, beat=beat)
        warning = disk_warning(jobs_dir(), disk_floor())
        if warning:
            print(f"campaign: {warning}", file=sys.stderr)
            with open(jobs_dir() / "prune.log", "a") as log:
                log.write(f"{stamp()} warning: {warning}\n")
            notify("campaign: disk low", warning)
    except (CampaignError, OSError) as e:
        print(f"campaign: the daily prune failed: {e}", file=sys.stderr)
    return True


def cmd_job_prune(a: argparse.Namespace) -> int:
    prune(a.days, a.dry_run, build_days=a.build_days)
    return 0


def disk_sizes(paths: list[Path]) -> dict[Path, int]:
    """Bytes each path takes on disk, from `du` (a hard link counts once)."""
    out: dict[Path, int] = {}
    for i in range(0, len(paths), 200):
        p = subprocess.run(["du", "-sk", *map(str, paths[i : i + 200])], capture_output=True, text=True)
        for line in p.stdout.splitlines():
            kb, _, name = line.partition("\t")
            with contextlib.suppress(ValueError):
                out[Path(name)] = int(kb) * 1024
    return out


def worktree_list(repo: Path) -> list[Json]:
    """REPO's linked worktrees, its main checkout left out: path, branch ("" when detached) and held
    (git locked it, or finds it gone)."""
    out: list[Json] = []
    for block in git(repo, "worktree", "list", "--porcelain", check=False).split("\n\n")[1:]:
        entry = {key: value for key, _, value in (line.partition(" ") for line in block.splitlines())}
        if entry.get("worktree"):
            out.append(
                {
                    "path": Path(entry["worktree"]),
                    "branch": entry.get("branch", "").removeprefix("refs/heads/"),
                    "held": "locked" in entry or "prunable" in entry,
                }
            )
    return out


def machine_worktrees() -> list[Json]:
    """Every linked worktree of the repositories this machine's campaigns use, outside the job store
    and any .scratch, with its repository's main checkout (repo) and quest: "open" when an unfinished quest of any
    campaign owns its branch or path, "landed" or "dropped" when only a finished one does, else ""."""
    repos: dict[str, Path] = {}
    owners: dict[str, str] = {}  # a branch, or a worktree's resolved path -> its quest's status
    for name, o in campaigns().items():
        c = Campaign(Path(o["home"]), name)
        for repo in {c.home, *(c.repo(p) for p in c.quests.values())}:
            if common_dir(repo):
                repos.setdefault(common_dir(repo), main_checkout(repo))
        for pid, p in c.quests.items():
            status = p["status"] if p["status"] in ("landed", "dropped") else "open"
            for qname, branch, rel, _ in branches(c, pid):
                jp = journal_place(c, qname)
                for key in (branch, *(str(x.resolve()) for x in ((c.home / rel) if rel else None, jp) if x)):
                    if owners.get(key) != "open":
                        owners[key] = status
    store = jobs_dir().resolve()
    out: list[Json] = []
    for main in repos.values():
        for wt in worktree_list(main):
            path = wt["path"].resolve()
            if wt["held"] or not path.is_dir() or path == store or store in path.parents or ".scratch" in path.parts:
                continue  # git's own hold, the job store's checkouts, a probe kept in a .scratch
            found = [owners.get(str(path), ""), owners.get(wt["branch"], "") if wt["branch"] else ""]
            quest = "open" if "open" in found else next((x for x in found if x), "")
            out.append({**wt, "path": path, "repo": main, "quest": quest})
    return out


def build_dirs(wt: Path) -> list[Path]:
    """WT's regenerable build directories: top-level directories named as BUILD_DIRS that git ignores
    whole, so none holds a tracked or an unignored file."""
    try:
        names = sorted(
            x.name
            for x in wt.iterdir()
            if any(fnmatch.fnmatchcase(x.name, g) for g in BUILD_DIRS) and x.is_dir() and not x.is_symlink()
        )
    except OSError:
        return []
    if not names:
        return []
    args = ["ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory", "--"]
    listed = git(wt, *args, *(f":(literal){n}" for n in names), check=False)
    whole = {x[:-1] for x in listed.split("\0") if x.endswith("/")}
    return [wt / n for n in names if n in whole]


def changed_within(path: Path, days: float) -> bool:
    """Whether anything in PATH changed within DAYS; a part `find` cannot read counts as changed."""
    if days <= 0:
        return False
    minutes = f"-{max(1, round(days * 1440))}"
    p = subprocess.run(["find", str(path), "-mmin", minutes, "-print", "-quit"], capture_output=True, text=True)
    return p.returncode != 0 or bool(p.stdout.strip())


def build_plan(days: float) -> list[tuple[Path, int]]:
    """The build directories prune removes, with their sizes: those of worktrees no open quest owns
    (main checkouts and the job store are not worktrees here) that nothing changed in for DAYS."""
    found = [
        d
        for wt in machine_worktrees()
        if wt["quest"] != "open"
        for d in build_dirs(wt["path"])
        if not changed_within(d, days)
    ]
    sizes = disk_sizes(found)
    return [(d, sizes.get(d, 0)) for d in found]


def disk_floor(c: Campaign | None = None) -> float:
    """GiB of free disk under which check and the daily prune warn: $CAMPAIGN_DISK_WARN_GB, else the
    campaign's disk_warn, else (the worker) the largest of the machine's campaigns, else DISK_WARN_GB."""
    if os.environ.get("CAMPAIGN_DISK_WARN_GB"):
        return float(os.environ["CAMPAIGN_DISK_WARN_GB"])
    states = [c.state] if c else [o["state"] for o in campaigns().values()]
    return max((float(x.get("disk_warn", DISK_WARN_GB)) for x in states), default=DISK_WARN_GB)


def disk_consumers() -> list[tuple[str, int, str]]:
    """What the campaigns know fills the disk, largest first: (what, bytes, the command that reclaims
    it). Measured with `du` at most once an hour (a prune or sweep measures again)."""
    cache = jobs_dir() / "disk.json"
    with contextlib.suppress(CampaignError):
        cached = read_json(cache)
        if cached and time.time() - float(cached.get("at", 0)) < 3600:
            return [(str(w), int(n), str(cmd)) for w, n, cmd in cached["consumers"]]
    groups: dict[tuple[str, str], list[Path]] = {}
    store = jobs_dir()
    if store.is_dir():
        groups["the job store's jobs", "campaign job prune"] = [x for x in store.iterdir() if x.name != "slots"]
    if (store / "slots").is_dir():
        groups["build slots", "campaign job prune"] = [store / "slots"]
    for wt in machine_worktrees():
        if wt["quest"] != "open":
            key = (f"build dirs in {wt['repo'].name}'s worktrees", "campaign job prune --build-days 0")
            groups.setdefault(key, []).extend(build_dirs(wt["path"]))
    ccache = Path(os.environ.get("CCACHE_DIR") or Path.home() / ".ccache")
    if ccache.is_dir():
        groups["ccache", "ccache --evict-older-than 7d"] = [ccache]
    sizes = disk_sizes([x for paths in groups.values() for x in paths])
    rows = sorted(
        ((what, sum(sizes.get(x, 0) for x in paths), cmd) for (what, cmd), paths in groups.items()),
        key=lambda row: -row[1],
    )
    write_json(cache, {"at": time.time(), "consumers": rows})
    return rows


def disk_warning(where: Path, floor: float) -> str:
    """A warning when WHERE's disk has under FLOOR GiB free, naming the five largest consumers the
    campaigns know and what reclaims each; "" otherwise."""
    disk = shutil.disk_usage(where)
    if disk.free >= floor * 2**30:
        return ""
    top = ", ".join(f"{what} {size_text(n)} (`{cmd}`)" for what, n, cmd in disk_consumers()[:5] if n)
    return (
        f"{disk.free / 2**30:.0f} GiB free ({disk.free / disk.total:.0%}) on {where}'s disk, under {floor:g} GiB "
        f"(`campaign config disk_warn=N`); largest: {top or 'none the campaigns know'}; "
        "`campaign sweep` removes merged worktrees"
    )


def process_dirs() -> list[Path] | None:
    """The working directories of this user's processes, or None where the host shows none."""
    if Path("/proc/self/cwd").exists():
        found: list[Path] = []
        for d in Path("/proc").iterdir():
            if d.name.isdigit():
                with contextlib.suppress(OSError):
                    found.append(Path(os.readlink(d / "cwd")))
        return found
    if not shutil.which("lsof"):
        return None
    p = subprocess.run(["lsof", "-w", "-a", "-d", "cwd", "-u", str(os.getuid()), "-Fn"], capture_output=True, text=True)
    return [Path(line[1:]) for line in p.stdout.splitlines() if line.startswith("n/")]


def sweep_plan() -> list[Json]:
    """The worktrees `campaign sweep` removes or keeps: of the known repositories, on a branch merged
    into main or of a landed or dropped quest, and no open quest's. Each says why it stays (why), and
    lists the files its .scratch holds."""
    cwds = process_dirs()
    jobs = all_jobs()
    out: list[Json] = []
    for wt in machine_worktrees():
        path, repo, branch = wt["path"], wt["repo"], wt["branch"]
        if wt["quest"] == "open" or not branch:
            continue
        try:
            merged = ancestor(repo, branch, main_branch(repo))
        except CampaignError:  # no main branch
            merged = False
        if not merged and wt["quest"] not in ("landed", "dropped"):
            continue
        dirty = git(path, "status", "--porcelain", check=False)
        busy = cwds is not None and any(x == path or path in x.parents for x in cwds)
        readers = readers_of(path, jobs)
        why = (
            "uncommitted or untracked files"
            if dirty
            else "this host shows no process's directory (no /proc, no lsof)"
            if cwds is None
            else "a process runs in it"
            if busy
            else f"job {', '.join(readers)} reads from it"
            if readers
            else ""
        )
        scratch = path / ".scratch"
        files: list[str] = []
        if scratch.is_dir():
            files = sorted(str(f.relative_to(path)) for f in scratch.rglob("*") if f.is_file())
        out.append({**wt, "merged": merged, "why": why, "scratch": files})
    return out


def cmd_sweep(a: argparse.Namespace) -> int:
    """Removes clean worktrees of merged branches and finished quests, and their merged branches."""
    plan = sweep_plan()
    going = [w for w in plan if not w["why"] and (a.force or not w["scratch"])]
    sizes = disk_sizes([w["path"] for w in going])
    removed = 0
    for w in plan:
        path, repo, branch = w["path"], w["repo"], w["branch"]
        label = f"{path} ({branch}, {'merged' if w['merged'] else w['quest']})"
        shown = ", ".join(w["scratch"][:5]) + (", ..." if len(w["scratch"]) > 5 else "")
        if w["why"]:
            print(f"keep {label}: {w['why']}")
            continue
        if w not in going:
            print(
                f"keep {label}: .scratch holds {plural(len(w['scratch']), 'file')} ({shown}); commit what is "
                "reusable, then --force"
            )
            continue
        size = size_text(sizes.get(path, 0))
        if a.dry_run:
            print(f"remove {label}  {size}")
            continue
        r = subprocess.run(["git", "-C", str(repo), "worktree", "remove", str(path)], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"keep {label}: git refused: {(r.stderr or r.stdout).strip()}")
            continue
        removed += 1
        line = f"removed worktree {label} ({size})"
        if w["merged"]:
            gone = git_ok(repo, "branch", "-q", "-d", branch)
            line += f" and branch {branch}" if gone else f"; kept branch {branch}: `git branch -d` refused"
        print(line)
        with open(jobs_dir() / "prune.log", "a") as log:
            log.write(f"{stamp()} {line}\n")
    total = size_text(sum(sizes.get(w["path"], 0) for w in going))
    if a.dry_run:
        print(f"would remove {plural(len(going), 'worktree')} ({total}) and keep {len(plan) - len(going)}")
    else:
        (jobs_dir() / "disk.json").unlink(missing_ok=True)
        print(f"removed {plural(removed, 'worktree')} and kept {len(plan) - removed}")
    return 0
