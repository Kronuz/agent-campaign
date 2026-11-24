"""Running jobs: the worker's and jobs' processes, checkouts and build-slot locks, one job's run under
quiet, its result, and runners that died."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import TextIO

from .core import (
    BEAT,
    ENTRY,
    HEAVY,
    HOST,
    KILL_GRACE,
    LIGHT_RUN,
    SKILL,
    STALE,
    TERMINAL,
    UNSTARTED_ATTEMPTS,
    CampaignError,
    Json,
    alive,
    boot_id,
    die,
    git,
    git_ok,
    jobs_dir,
    mtime,
    read_json_shared,
    same_boot,
    sh,
    stamp,
    write_json,
)
from .jobs import FAILS_WITHOUT, NO_RUN, all_jobs, attempt_dir, campaign_env, finish, load_job, slot_of
from .mirrors import mirror_env, mirror_pins
from .repo import _listed, rev
from .reuse import STRACE_ARGS, record_reads, save_reads, tracer

TREE_DEPTH = 3  # slot/ref-x/.deps/y-subbuild: how far down a slot's build trees are looked for


def drop_slot(slot: Path) -> bool:
    """Removes a build slot, a worktree of the repository it checks out, unless a runner holds it."""
    if not slot.is_dir():
        return True
    fh = slot_lock_try(slot)
    if fh is None:
        return False
    with fh:
        common = git(slot, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
        worktree_remove(str(Path(common).parent) if common else str(slot), slot)
        slot.with_name(slot.name + ".lock").unlink(missing_ok=True)  # a waiter that opened it retries
    return True


def drop_quest_slot(spec: Json) -> None:
    """After a job in its quest's build slot ends: removes the slot once that quest has landed or was
    dropped and no pending job names it (landing kept it for them)."""
    name, campaign, quest = slot_of(spec), str(spec.get("campaign") or ""), str(spec.get("quest") or "")
    state = read_json_shared(Path(campaign) / "state.json") if campaign and quest and name else None
    if state is None:
        return
    q = state.get("quests", {}).get(quest, {})
    if q.get("status") not in ("landed", "dropped") or name.partition("@")[0] != f"{state['campaign']}-{quest}":
        return
    if any(slot_of(j["spec"]) == name and j["state"].get("state") not in TERMINAL for j in all_jobs()):
        return
    drop_slot(jobs_dir() / "slots" / name)


LIGHT_BUILD_JOBS = int(os.environ.get("CAMPAIGN_LIGHT_BUILD_JOBS") or 4)  # per light job; several run at once


def worker_alive() -> bool:
    beat = mtime(jobs_dir() / ".worker" / "heartbeat")
    return beat is not None and time.time() - beat < STALE


def spawn(args: list[str], log: Path) -> subprocess.Popen[bytes]:
    with open(log, "ab") as fh:
        return subprocess.Popen(
            [sys.executable, str(ENTRY), *args],
            stdin=subprocess.DEVNULL,
            stdout=fh,
            stderr=fh,
            start_new_session=True,
            close_fds=True,
            cwd=jobs_dir(),  # never the caller's directory: a removed worktree would break every runner
        )


def ensure_worker() -> None:
    if not worker_alive() and any(j["state"].get("state") == "queued" for j in all_jobs()):
        spawn(["worker"], jobs_dir() / "worker.log")


def session_pids(sid: int) -> list[int]:
    pids: list[int] = []
    for token in sh(["ps", "-A", "-o", "pid="], check=False).split():
        pid = int(token)
        if pid == os.getpid():
            continue
        with contextlib.suppress(ProcessLookupError, PermissionError):
            if os.getsid(pid) == sid:
                pids.append(pid)
    return pids


def runs(pid: int, what: str) -> bool:
    """Whether process PID is alive and its command line names WHAT (a job runner names its directory):
    a pid alone may have been reused by an unrelated process."""
    if not alive(pid):
        return False
    return what in sh(["ps", "-o", "command=", "-p", str(pid)], check=False)


def sweep(sid: int, beat: Path | None = None) -> None:
    """Kills every process left in a job's session: helpers never outlive their job."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if beat:
            beat.touch()
        pids = session_pids(sid)
        if not pids:
            return
        for pid in pids:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, sig)
        time.sleep(2 if sig == signal.SIGTERM else 0.2)


def tracees(sid: int) -> list[int]:
    """The processes traced by a process of session SID: a job's strace follows its command's helpers."""
    found: list[int] = []
    for status in Path("/proc").glob("[0-9]*/status"):
        with contextlib.suppress(OSError, ValueError):
            tracer = re.search(r"^TracerPid:\s*(\d+)", status.read_text(), re.M)
            if tracer and int(tracer.group(1)) and os.getsid(int(tracer.group(1))) == sid:
                found.append(int(status.parent.name))
    return found


def kill_group(child: subprocess.Popen[bytes], beat: Path) -> None:
    """TERMs a job's process group, then KILLs it if it outlives KILL_GRACE. The leader is reaped as
    it goes: on Linux a zombie leader keeps its group signalable."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(child.pid, signal.SIGTERM)
    for _ in range(KILL_GRACE * 5):
        child.poll()
        beat.touch()
        try:
            os.killpg(child.pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.2)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(child.pid, signal.SIGKILL)


def worktree_add(repo: Path, path: Path, commit: str, beat: Path | None = None) -> None:
    for attempt in range(10):
        if beat:
            beat.touch()
        p = subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(path), commit],
            capture_output=True,
            text=True,
        )
        if p.returncode == 0:
            return
        if "lock" not in p.stderr or attempt == 9:
            die(f"git worktree add {path}: {p.stderr.strip()}")
        time.sleep(0.2 + secrets.randbelow(800) / 1000)


def worktree_remove(repo: str, path: Path) -> None:
    if path.exists():
        subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", str(path)], capture_output=True)
        shutil.rmtree(path, ignore_errors=True)
        subprocess.run(["git", "-C", repo, "worktree", "prune"], capture_output=True)


_slot_locks: dict[str, TextIO] = {}  # this runner's slot lock: the kernel drops it when the runner dies


def slot_lock_try(slot: Path) -> TextIO | None:
    """SLOT's lock file, open and locked, or None while another process holds it. A lock file that was
    unlinked after this opened it is retried: the lock is the file at the path now."""
    lock = slot.with_name(slot.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    while True:
        if lock.is_dir():  # an older runner's lock: wait while the runner its pid names lives
            with contextlib.suppress(FileNotFoundError, ValueError):
                if not runs(int((lock / "pid").read_text()), " _run "):
                    shutil.rmtree(lock, ignore_errors=True)
            if lock.is_dir():
                return None
        try:
            fh = open(lock, "a")  # noqa: SIM115 - held past this function: it is the lock
        except IsADirectoryError:
            continue
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            return None
        with contextlib.suppress(FileNotFoundError):
            if os.stat(lock).st_ino == os.fstat(fh.fileno()).st_ino:
                return fh
        fh.close()


def slot_idle(slot: Path) -> bool:
    """Whether no job holds SLOT's lock; it breaks no older runner's lock, so a dry run changes nothing."""
    lock = slot.with_name(slot.name + ".lock")
    if not lock.exists():
        return True
    if lock.is_dir():
        with contextlib.suppress(FileNotFoundError, ValueError):
            return not runs(int((lock / "pid").read_text()), " _run ")
        return False
    fh = slot_lock_try(slot)
    if fh is not None:
        fh.close()
    return fh is not None


def slot_lock(slot: Path, beat: Path) -> None:
    """Holds SLOT's lock until this runner releases it or dies: one job builds in a slot at a time."""
    while (fh := slot_lock_try(slot)) is None:
        beat.touch()
        time.sleep(0.5)
    _slot_locks[slot.name] = fh


def prepare(spec: Json, d: Path) -> Path:
    """The job's sources at its exact commits; the agent's own worktree is never used."""
    if not spec.get("commit"):
        checkout_refs(spec, d)
        return Path(spec["cwd"])
    repo = Path(spec["repo"])
    if spec.get("slot"):
        slot = jobs_dir() / "slots" / spec["slot"]
        slot_lock(slot, d / "heartbeat")
        if not (slot / ".git").exists():
            shutil.rmtree(slot, ignore_errors=True)
            worktree_add(repo, slot, spec["commit"], d / "heartbeat")
        else:
            git(slot, "checkout", "-q", "--detach", "--force", spec["commit"])
        if rev(slot, "HEAD") != spec["commit"] or git(slot, "status", "--porcelain", "--untracked-files=no"):
            die(f"slot {slot} is not clean at {spec['commit'][:12]}")
        root = slot
    else:
        root = d / "src"
        worktree_remove(str(repo), root)
        worktree_add(repo, root, spec["commit"], d / "heartbeat")
    checkout_refs(spec, d)
    return root / spec["cwd"]


def checkout_refs(spec: Json, d: Path) -> None:
    """Each --ref at its commit, from its own repository: an unversioned job's too."""
    for name, ref in spec.get("refs", {}).items():
        path = ref_path(spec, d, name)
        reset = ["checkout", "-q", "--detach", "--force", ref["commit"]]
        if slot_of(spec) and (path / ".git").exists() and git_ok(path, *reset):
            continue  # a slot's ref keeps its path and its untracked files, reset to the ref
        worktree_remove(ref["repo"], path)
        git_ok(Path(ref["repo"]), "worktree", "prune")  # a slot removed whole leaves its refs registered
        worktree_add(Path(ref["repo"]), path, ref["commit"], d / "heartbeat")


def ref_path(spec: Json, d: Path, name: str) -> Path:
    """Where a job's --ref NAME is checked out: in its slot, at one path for every job there (a build
    directory kept in the slot configured from it stays valid), else under the job's own directory."""
    slot = slot_of(spec)
    return (jobs_dir() / "slots" / slot if slot else d) / f"ref-{name}"


def cache_entries(tree: Path) -> dict[str, str]:
    """The generator, source and location CMake recorded in TREE's CMakeCache.txt (what it has of them)."""
    found: dict[str, str] = {}
    with open(tree / "CMakeCache.txt", errors="replace") as fh:
        for line in fh:
            key, _, value = line.rstrip("\n").partition("=")
            name = key.partition(":")[0]
            if name in ("CMAKE_GENERATOR", "CMAKE_HOME_DIRECTORY", "CMAKE_CACHEFILE_DIR"):
                found[name] = value
    return found


def cmake_trees(slot: Path) -> Iterator[Path]:
    """The CMake build trees in SLOT, at most TREE_DEPTH levels down (a build directory, `.deps`
    sub-builds, a ref's own): one directory listing each, no file read. A tree is not entered, its
    `-src` and `.git` directories are skipped, and a symlink is not followed."""
    todo = [(slot, 0)]
    while todo:
        here, depth = todo.pop()
        try:
            with os.scandir(here) as it:
                entries = list(it)
        except OSError:
            continue
        if depth and any(e.name == "CMakeCache.txt" and e.is_file(follow_symlinks=False) for e in entries):
            yield here
            continue
        if depth < TREE_DEPTH:
            todo += [
                (Path(e.path), depth + 1)
                for e in entries
                if e.is_dir(follow_symlinks=False) and e.name != ".git" and not e.name.endswith("-src")
            ]


def stale_tree(tree: Path, generator: str, sources: Iterable[Path]) -> str:
    """Why a job that configures with GENERATOR (empty when its environment names none) from SOURCES (the
    slot's source roots) cannot reuse TREE, or "": another generator configured it, its source is gone or
    lies outside every root (a directory inside one is a source: `-S sub -B build`), or it was configured
    at another path."""
    try:
        have = cache_entries(tree)
    except OSError:
        return ""
    here = tree.resolve()
    if generator and have.get("CMAKE_GENERATOR", generator) != generator:
        return f"configured by {have['CMAKE_GENERATOR']}, this job uses {generator}"
    home = have.get("CMAKE_HOME_DIRECTORY")
    if home:
        roots = [s.resolve() for s in sources]
        if not Path(home).is_dir() or not any(Path(home).resolve().is_relative_to(r) for r in roots):
            return f"configured from {home}, outside this job's sources"
    where = have.get("CMAKE_CACHEFILE_DIR")
    if where and Path(where).resolve() != here:
        return f"configured at {where}"
    return ""


def remove_tree(slot: Path, tree: Path) -> bool:
    """Removes TREE only when it is a real directory strictly inside SLOT and SLOT is one of the
    runner's own slots; False (nothing removed) for anything else."""
    owned = (jobs_dir() / "slots").resolve()
    inside = tree.resolve().is_relative_to(slot.resolve()) and tree.resolve() != slot.resolve()
    if tree.is_symlink() or not tree.is_dir() or slot.resolve().parent != owned or not inside or tree.name == ".git":
        return False
    shutil.rmtree(tree)
    return True


def clear_stale_trees(slot: Path, generator: str, sources: Iterable[Path], say: Callable[[str], None]) -> None:
    """Before a job runs in SLOT: removes each build tree there that another generator or source
    configured (CMake fails on it) and says why, one line a tree."""
    sources = list(sources)
    for tree in cmake_trees(slot):
        why = stale_tree(tree, generator, sources)
        if why and remove_tree(slot, tree):
            say(f"campaign: removed {tree.relative_to(slot)}: {why}")


def release(spec: Json, d: Path) -> None:
    """Drops this runner's slot lock, if it holds one, and the job's own checkouts unless it keeps them
    (a slot's checkouts stay for the next job there)."""
    fh = _slot_locks.pop(spec.get("slot") or "", None)
    if fh is not None:
        fh.close()
    if spec.get("keep"):
        return
    if spec.get("commit"):
        worktree_remove(spec["repo"], d / "src")
    for name, ref in spec.get("refs", {}).items():
        if not slot_of(spec):
            worktree_remove(ref["repo"], ref_path(spec, d, name))


def quiet_argv() -> list[str]:
    found = shutil.which("quiet")
    return [found] if found else [sys.executable, str(SKILL / "scripts" / "quiet.py")]


def attend(
    argv: list[str],
    cwd: Path,
    env: dict[str, str],
    d: Path,
    *,
    began: Path | None = None,
    ended: Path | None = None,
    minutes: int = 0,
    limit: float,
) -> tuple[int, bool, bool]:
    """Runs one command of a job in its own process group, appending to its log, beating its heart,
    and killing it on cancel, past LIMIT, or MINUTES after BEGAN appears: (exit, cancelled, timed out).
    Once ENDED names the traced command's shell, the helpers it left are stopped: they keep its tracer,
    and so the job, running."""
    beat = d / "heartbeat"
    job = load_job(d)
    with open(attempt_dir(job) / "log", "ab") as out:
        child = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            preexec_fn=os.setpgrp,
        )
    st = job["state"]
    st["pgid"] = child.pid
    write_json(d / "state.json", st)
    cancelled = timed_out = False
    stopping = 0.0
    while True:
        try:
            return child.wait(timeout=BEAT if ended is None else min(BEAT, 1.0)), cancelled, timed_out
        except subprocess.TimeoutExpired:
            pass
        beat.touch()
        shell = ended.read_text().strip() if ended is not None and ended.exists() else ""
        if shell:
            stopping = stopping or time.time()
            sig = signal.SIGTERM if time.time() - stopping < KILL_GRACE else signal.SIGKILL
            for pid in tracees(os.getsid(0)):
                if str(pid) != shell:  # it exits on its own, with the command's code
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.kill(pid, sig)
        if (d / "cancel").exists() and not cancelled:
            cancelled = True
            kill_group(child, beat)
        elif not timed_out and (
            (began is not None and began.exists() and time.time() > began.stat().st_mtime + 60 * minutes)
            or time.time() > limit
        ):
            timed_out = True
            kill_group(child, beat)


def outcome(spec: Json, code: int, cancelled: bool = False, timed_out: bool = False) -> tuple[str, str]:
    """A command's result as the job's (state, note). A --fails-without job passes when its command
    exits nonzero; a timeout, a cancel and a command that did not run never count as failing."""
    if cancelled:
        return "cancelled", ""
    if timed_out or code == 124:
        return "timeout", ""
    if spec.get("fails_without", 0) < FAILS_WITHOUT:
        return ("done" if code == 0 else "failed"), ""
    if code in NO_RUN:
        return "failed", f"exit {code}: the command did not run, so it shows nothing about the fix"
    if code == 0:
        return "failed", "the test passed without the fix: it must fail there"
    return "done", f"the test failed without the fix, as it must (exit {code})"


def admission(d: Path, limit: float) -> str:
    """Waits for the worker to admit a built job to its hold (""), or for its cancel or LIMIT first
    ("cancelled", "timeout"). The `admitted` directory decides a race between them: who makes it wins."""
    gate, beat = d / "admitted", d / "heartbeat"
    while load_job(d)["state"].get("phase") != "run":
        made = mtime(gate)
        if made is not None and time.time() - made > 10:
            return ""  # the worker died between its two writes: it admitted the job
        why = "cancelled" if (d / "cancel").exists() else "timeout" if time.time() > limit else ""
        if why and made is None:
            with contextlib.suppress(FileExistsError):
                gate.mkdir()
                return why
        if time.time() - (mtime(beat) or 0) > BEAT:
            beat.touch()
        time.sleep(0.5)
    return ""


def admit(job: Json) -> bool:
    """Admits a built job (its runner waits in `admission`) to its hold, with the state plan gave it."""
    try:
        (job["dir"] / "admitted").mkdir()
    except FileExistsError:
        return False
    write_json(job["dir"] / "state.json", {**job["state"], "admitted": time.time()})
    return True


def cmd_run(a: argparse.Namespace) -> int:
    """One attempt of one job, supervised by this process, the leader of its own session."""
    d = Path(a.jobdir)
    job = load_job(d)
    spec, st = job["spec"], job["state"]
    me = os.getpid()
    # a staged job's build is light work: its hold, and so its start, comes once the worker admits it
    staged = st.get("phase") == "build"
    began = {"build": {"started": time.time()}} if staged else {"started": time.time()}
    st.update(state="running", runner=me, sid=os.getsid(0), boot=boot_id(), host=HOST, **began)
    write_json(d / "state.json", st)
    job["state"] = st
    beat = d / "heartbeat"
    beat.touch()
    ad = attempt_dir(job)
    shutil.rmtree(ad, ignore_errors=True)
    (ad / "out").mkdir(parents=True)
    if (d / "cancel").exists():
        finish(job, -1, "cancelled", fence=me)
        return 0
    try:
        cwd = prepare(spec, d)
    except CampaignError as e:
        release(spec, d)
        (ad / "exit").write_text("125")
        finish(job, 125, "failed", f"could not prepare sources: {e}", fence=me)
        return 0
    env = dict(os.environ)
    env.pop("CMUX_WORKSPACE_ID", None)  # the worker may have started in another workspace
    env.pop("CMUX_SURFACE_ID", None)  # nor is a job the lead whose surface `note_lead` records
    if spec.get("level") not in HEAVY:  # a light hold's build and test parallelism, where a command names none
        env.setdefault("CMAKE_BUILD_PARALLEL_LEVEL", str(LIGHT_BUILD_JOBS))
        env.setdefault("CTEST_PARALLEL_LEVEL", str(LIGHT_BUILD_JOBS))
    env.setdefault("CTEST_OUTPUT_ON_FAILURE", "1")
    if spec.get("cmux"):
        env["CMUX_WORKSPACE_ID"] = spec["cmux"]  # quiet's pill and notice go to the submitter's
    if spec.get("path"):
        env["PATH"] = spec["path"]  # the toolchain its fingerprint was taken with, whoever started the worker
    env.update(campaign_env(spec))  # over whatever the worker inherited; the job's own --env wins
    env.update(spec["env"])
    env.update(
        JOB_ID=d.name,
        JOB_DIR=str(ad),
        JOB_OUT=str(ad / "out"),
        JOB_SRC=str(cwd),
        JOB_COMMIT=spec.get("commit") or "",
        QUIET_KILL="1",
        QUIET_WHO=f"campaign job {d.name}",
        QUIET_BESIDE=str(ad / "beside"),
    )
    if spec.get("fails_without", 0) >= FAILS_WITHOUT:
        env["QUIET_EXPECT"] = "fail"  # its failing is its passing: no failure notice
    for name in spec.get("refs", {}):
        env[f"JOB_REF_{name.upper()}"] = str(ref_path(spec, d, name))
    env.update(mirror_env(env))
    if spec.get("commit") and env.get("CAMPAIGN_GIT"):
        try:
            mirror_pins(env["CAMPAIGN_GIT"], jobs_dir() / "slots" / spec["slot"] if spec.get("slot") else d / "src")
        except (OSError, CampaignError) as e:  # the job still runs: its own git reaches GitHub
            print(f"campaign: filling git mirrors failed: {e}", file=sys.stderr)
    if slot_of(spec):  # a shared slot's build tree another generator configured fails the configure
        slot = jobs_dir() / "slots" / spec["slot"]
        sources = [slot, *(ref_path(spec, d, name) for name in spec["refs"])]
        try:
            with open(ad / "log", "a") as note:
                clear_stale_trees(slot, env.get("CMAKE_GENERATOR", ""), sources, lambda x: print(x, file=note))
        except OSError as e:  # the job still runs, and fails on its own if the tree was the cause
            print(f"campaign: clearing stale build trees failed: {e}", file=sys.stderr)
    if spec.get("build"):  # built under light in this checkout, then run at the job's level
        what = f"build for {spec.get('what') or spec['cmd'][:60]}"
        built = [*quiet_argv(), "light", f"job-{d.name}-build", str(LIGHT_RUN), what, "--", "bash", "-c", spec["build"]]
        light_env = {"CMAKE_BUILD_PARALLEL_LEVEL": str(LIGHT_BUILD_JOBS), **env}
        wait_max = float(os.environ.get("CAMPAIGN_WAIT_MAX", str(24 * 3600)))
        since = float(st.get("build", {}).get("started") or time.time())
        code, cancelled, timed_out = attend(built, cwd, light_env, d, limit=time.time() + 60 * LIGHT_RUN + wait_max)
        sweep(os.getsid(0), beat)
        st["build"] = {"started": since, "ended": time.time(), "exit": code}
        if code == 0 and not cancelled and not timed_out and staged:
            st["phase"] = "built"
            write_json(d / "state.json", st)
            why = admission(d, time.time() + wait_max) or ("cancelled" if (d / "cancel").exists() else "")
            st = job["state"] = {**load_job(d)["state"], **({} if why else {"phase": "run", "started": time.time()})}
            write_json(d / "state.json", st)
            cancelled, timed_out = why == "cancelled", why == "timeout"
        if code != 0 or cancelled or timed_out:
            (ad / "exit").write_text(str(code))
            release(spec, d)
            state = "cancelled" if cancelled else "timeout" if timed_out or code == 124 else "failed"
            finish(job, code, state, f"its build failed (exit {code})" if state == "failed" else "", fence=me)
            return 0
    argv = ["bash", "-c", 'date +%s > "$JOB_DIR/began"\n' + spec["cmd"]]
    traced = tracer() if spec.get("commit") and spec["level"] in ("none", "light") and not spec.get("build") else ""
    if traced:  # what it reads decides where its result counts (record_reads)
        # strace waits for every helper the command leaves: the shell says when the command ended
        script = f'date +%s > "$JOB_DIR/began"\n(\n{spec["cmd"]}\n)\ns=$?\necho $$ > "$JOB_DIR/ended"\nexit $s'
        argv = [traced, *STRACE_ARGS, "-o", str(ad / "strace"), "--", "bash", "-c", script]
    if spec["level"] != "none":
        argv = [
            *quiet_argv(),
            spec["level"],
            f"job-{d.name}",
            str(spec["minutes"]),
            spec.get("what") or spec["cmd"][:60],
            "--",
            *argv,
        ]
    limit = time.time() + 60 * spec["minutes"] + float(os.environ.get("CAMPAIGN_WAIT_MAX", str(24 * 3600)))
    began = ad / "began" if spec["level"] == "none" else None
    ended = ad / "ended" if traced else None
    code, cancelled, timed_out = attend(
        argv, cwd, env, d, began=began, ended=ended, minutes=spec["minutes"], limit=limit
    )
    sweep(os.getsid(0), beat)
    (ad / "exit").write_text(str(code))
    state, note = outcome(spec, code, cancelled, timed_out)
    reads: Json | None = None
    if traced and state == "done":
        root = jobs_dir() / "slots" / spec["slot"] if spec.get("slot") else d / "src"
        try:
            reads = record_reads(spec, root, cwd, ad / "strace")
        except Exception as e:  # recorded: the job keeps whole-tree identity
            reads = {"complete": False, "why": [f"recording its reads failed: {e}"], "reads": {}}
    (ad / "strace").unlink(missing_ok=True)
    release(spec, d)
    if finish(job, code, state, note, fence=me) and reads is not None:
        save_reads(job, reads)
    with contextlib.suppress(CampaignError, OSError):
        drop_quest_slot(spec)
    return 0


def reap() -> None:
    """Finds jobs whose runner died (crash, kill, reboot): records, requeues or interrupts them."""
    for job in all_jobs():
        d, st, spec = job["dir"], job["state"], job["spec"]
        if st.get("state") != "running":
            continue
        beat = d / "heartbeat"
        last = mtime(beat) or 0.0
        if time.time() - last < STALE:
            continue
        lock = d / "reaping"
        try:
            lock.mkdir()
        except FileExistsError:
            held = mtime(lock)
            if held is None or time.time() - held < 60:
                continue  # another reaper holds it, or just finished
        _listed.clear()  # the job may change
        try:
            if load_job(d)["state"] != st:
                continue
            booted_here = same_boot(st.get("boot"))
            sid = int(st.get("sid") or 0)
            # the runner leads its session; a live leader that is not this job's runner reused its id
            if sid and booted_here and (not alive(sid) or runs(sid, f"_run {d}")):
                sweep(sid)
            exit_file = attempt_dir(job) / "exit"
            if exit_file.exists() and booted_here:
                code = int(exit_file.read_text().strip() or "1")
                state, note = outcome(spec, code)
                finish(job, code, state, "; ".join(filter(None, [note, "recorded after its runner died"])))
                continue
            release(spec, d)
            attempt = int(st.get("attempt", 1))
            unstarted = not (attempt_dir(job) / "began").exists()  # its command never ran: safe to repeat
            if attempt <= int(spec.get("retries", 0)) or (unstarted and attempt < UNSTARTED_ATTEMPTS):
                shutil.rmtree(d / "claim", ignore_errors=True)
                shutil.rmtree(d / "admitted", ignore_errors=True)
                why = "lost its runner before its command started" if unstarted else "lost its runner"
                write_json(
                    d / "state.json",
                    {
                        "state": "queued",
                        "attempt": attempt + 1,
                        "note": f"requeued at {stamp()}: attempt {attempt} {why}",
                    },
                )
            else:
                finish(
                    job,
                    -1,
                    "interrupted",
                    f"attempt {attempt} lost its runner"
                    + (" before its command started" if unstarted else "" if booted_here else " (the host rebooted)")
                    + "; not retried",
                )
        finally:
            shutil.rmtree(lock, ignore_errors=True)
