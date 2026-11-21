"""Read sets: the files a traced job read, so its result counts at other code that leaves them alone."""

from __future__ import annotations

import codecs
import contextlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .core import Json, jobs_dir, read_json, write_json
from .jobs import job_key, load_job
from .repo import on_commits, tree_map

STRACE_ARGS = ("-f", "-q", "-y", "-s", "65535", "-e", "trace=%file,%process,fchdir")
FORKS = ("clone(", "clone3(", "fork(", "vfork(")
_tracers: dict[str, bool] = {}


def tracer() -> str:
    """The strace that records a job's reads: $CAMPAIGN_STRACE ("" turns it off), else strace on
    Linux, if it runs here (a container may forbid ptrace)."""
    found = os.environ.get("CAMPAIGN_STRACE")
    if found is None:
        found = (shutil.which("strace") or "") if sys.platform.startswith("linux") else ""
    if found and found not in _tracers:
        probe = [found, *STRACE_ARGS, "-o", os.devnull, "--", "true"]
        _tracers[found] = subprocess.run(probe, capture_output=True, check=False).returncode == 0
    return found if found and _tracers[found] else ""


def strace_paths(log: Path, cwd: str) -> tuple[list[str], str]:
    """The absolute paths a strace log shows the job's processes touching, or why it is not a whole trace."""
    if not log.exists() or not log.stat().st_size:
        return [], "the trace is missing"
    cwds: dict[str, str] = {}
    pending: dict[str, str] = {}
    out: list[str] = []
    running: set[str] = set()
    early: set[str] = set()  # children seen before their clone returns
    shared: set[str] = set()  # processes that share a directory (CLONE_FS: threads)
    top, unknown, moved = "", False, False
    for line in log.read_text(errors="replace").splitlines():
        pid, _, rest = line.partition(" ")
        rest = rest.strip()
        top = top or pid
        if pid not in cwds:  # a child seen before its parent's clone returns starts where the parent is
            parents = {cwds.get(p, cwd) for p, call in pending.items() if call.startswith(FORKS)}
            unknown = unknown or (pid != top and len(parents) != 1)
            cwds[pid] = cwd if pid == top or len(parents) != 1 else parents.pop()
            fs = [p for p, call in pending.items() if call.startswith(FORKS) and "CLONE_FS" in call]
            shared.update([pid, *fs] if fs else [])
            early.update([pid] if pid != top else [])
        running.add(pid)
        if rest.startswith("+++"):
            if rest.startswith("+++ exited with"):
                running.discard(pid)
            cwds.pop(pid, None)  # a reused pid is a new process
            pending.pop(pid, None)
            continue
        if rest.endswith("<unfinished ...>"):
            pending[pid] = rest[: -len("<unfinished ...>")]
            continue
        resumed = re.match(r"<\.\.\. \w+ resumed>(.*)", rest)
        if resumed:
            rest = pending.pop(pid, "") + resumed.group(1)
        m = re.match(r"(\w+)\((.*)\)\s+=\s+(-?\d+|\?)", rest)
        if not m:
            continue
        call, args, ret = m.groups()
        if call + "(" in FORKS and ret.isdigit():
            if ret not in early:  # a child seen early already has its directory, and may have exited
                cwds[ret] = cwds[pid]
                running.add(ret)
            early.discard(ret)
            shared.update([pid, ret] if "CLONE_FS" in args else [])
        moved = moved or (call in ("chdir", "fchdir") and ret == "0" and pid in shared)
        dirfd = re.match(r"(\d+)<([^>]*)>, ", args)
        if call == "fchdir" and ret == "0" and (fd := re.match(r"\d+<([^>]*)>", args)):
            cwds[pid] = fd.group(1)
        strings = re.findall(r'"((?:[^"\\]|\\.)*)"', args)
        if not strings:
            continue
        name = codecs.escape_decode(strings[0].encode())[0].decode(errors="surrogateescape")
        path = os.path.normpath(os.path.join(dirfd.group(2) if dirfd else cwds[pid], name))
        if call == "chdir" and ret == "0":
            cwds[pid] = path
        out.append(path)
    if any(part.endswith(".git") for x in out for part in Path(x).parts):
        return out, "a process read git's object store, which a read set does not key"
    if unknown or moved:
        return out, "a process's directory is one the trace cannot tell"
    return out, "the trace ends before every process exits" if running else ""


def record_reads(spec: Json, root: Path, cwd: Path, log: Path) -> Json:
    """A traced job's read set: each tracked file or submodule its processes touched (a symlink's
    target too) and "**"; complete only when the trace ended cleanly."""
    objects = tree_map(Path(spec["repo"]), spec["commit"])
    found, why = strace_paths(log, str(cwd))
    keys = {"**"}
    for full in {x for path in found for x in (path, os.path.realpath(path))}:
        for top in dict.fromkeys([str(root), os.path.realpath(root)]):
            rel = full[len(top) + 1 :] if full.startswith(top + "/") else ""
            while rel and rel not in objects:  # inside a submodule, its commit
                rel = rel.rpartition("/")[0]
            keys.add(rel or "**")
    return {"complete": not why, "why": [why] if why else [], "reads": {k: objects[k] for k in sorted(keys)}}


def save_reads(job: Json, reads: Json) -> None:
    """Keeps a passed job's read set and files it under its reuse key, so read_twin finds it."""
    d = job["dir"]
    write_json(d / "reads.json", reads)
    rkey = job["spec"].get("rkey")
    if reads["complete"] and rkey:
        folder = jobs_dir() / "reads" / rkey
        folder.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileExistsError):
            (folder / d.name).symlink_to(f"../../{d.name}")


def reads_hold(repo: Path, commit: str, d: Path) -> bool:
    """Whether every file in a job's complete read set holds the same mode and object at COMMIT."""
    reads = read_json(d / "reads.json") if (d / "reads.json").exists() else None
    if not isinstance(reads, dict) or not reads.get("complete"):
        return False
    objects = tree_map(repo, commit)
    return all(objects.get(k, "-") == v for k, v in reads["reads"].items())


def read_twin(spec: Json, commit: str) -> Json | None:
    """The newest passed job with SPEC's identity but other code, whose every read file is unchanged at
    COMMIT, with "at" (its commit) and "reads" (how many files it read)."""
    folder = jobs_dir() / "reads" / job_key(spec, reads=True)
    repo = Path(spec["repo"])
    for link in sorted(folder.iterdir(), reverse=True) if folder.is_dir() else []:
        d = jobs_dir() / link.name
        if not (d / "spec.json").exists():
            continue
        job = load_job(d)
        if job["state"].get("state") != "done" or not job["spec"].get("commit"):
            continue
        if on_commits((f"reads:{link.name}", str(repo), commit), lambda d=d: reads_hold(repo, commit, d)):
            return {**job, "at": job["spec"]["commit"], "reads": len((read_json(d / "reads.json") or {})["reads"])}
    return None
