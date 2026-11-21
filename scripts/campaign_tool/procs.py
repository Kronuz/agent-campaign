"""Processes and checkouts: the worker's process, job sessions, worktrees and build-slot locks."""

from __future__ import annotations

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
from pathlib import Path
from typing import TextIO

from .core import ENTRY, KILL_GRACE, STALE, TERMINAL, Json, alive, die, git, jobs_dir, read_json_shared, sh
from .jobs import all_jobs, slot_of


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


def mtime(path: Path) -> float | None:
    """A file's modification time, or None when it is gone (its owner may remove it at any moment)."""
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return None


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
