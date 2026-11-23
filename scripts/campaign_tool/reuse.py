"""Evidence reuse: which job speaks for a commit (its own run, an ancestor's, a twin whose read set is
unchanged), and queueing jobs."""

from __future__ import annotations

import codecs
import contextlib
import fnmatch
import functools
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .core import (
    HEAVY,
    TERMINAL,
    Json,
    die,
    fingerprint,
    git,
    jobs_dir,
    plural,
    read_json,
    read_json_shared,
    write_json,
)
from .jobs import BUILD_FILES, all_jobs, claim, finish, job_key, key_links, load_job, named_paths, resolve_job
from .repo import ancestor, blobs, changed, is_meta, main_branch, merge_base, on_commits, rev, tree_map


def own_blobs(repo: Path, main_head: str, commit: str) -> dict[str, str]:
    """The quest's own code changes at COMMIT (path -> blob), against its merge base with main."""
    base = merge_base(repo, main_head, commit)
    return blobs(repo, commit, [x for x in changed(repo, base, commit) if not is_meta(x)])


def job_for_check(
    repo: Path, commit: str, check: Json, tested: Sequence[str] = (), *, ci: bool = False, files: Sequence[str] = ()
) -> Json | None:
    """The job of a quest check at COMMIT's code. The build-test needs the exact code, documentation
    aside, unless CI at the exact commit (CI) tests the merge with main; every other check counts
    across merges from main that touch none of the quest's FILES."""
    spec = check_spec(repo, commit, check)
    exact = build_test(check)
    return job_at(spec, commit, tested, exact=exact, merged=ci or not exact, files=files)


def check_spec(repo: Path, commit: str, check: Json) -> Json:
    """A quest check's job identity at COMMIT: the spec `verify` queues and `brief` and `land` look up,
    with the bytes of the scripts it names outside the checkout (as job submit)."""
    from .jobcmds import script_digests  # noqa: PLC0415  # jobcmds imports this module

    return {
        "repo": str(repo),
        "commit": commit,
        "refs": {},
        "cmd": check["cmd"],
        "cwd": "",
        "level": check["level"],
        "env": {},
        "scripts": script_digests(check["cmd"], str(repo)),
        **({"inputs": check["inputs"]} if check.get("inputs") else {}),
    }


def build_test(check: Json) -> bool:
    """Whether a check is the build-test, a gate check without inputs: `land` needs it at the exact code."""
    return not check.get("later") and not check.get("inputs")


DOC_PATHS = ("docs/**", "website/**", "*.md")  # documentation: evidence outlives commits that change only these
CODE_DIRS = ("tests", "test", "bench", "benches")  # what lies under these runs, whatever its extension
CARRY_DEPTH = 40  # how many first-parent commits back evidence is looked for (docs-only commits, merges from main)


def doc_path(path: str, cmd: str, tested: Sequence[str] = ()) -> bool:
    """Whether PATH is documentation that the command CMD does not name, itself or by a folder, and
    that no TESTED glob (config `tested_docs`: docs that tests execute) matches."""
    return docs_unnamed(path, cmd, tuple(tested))


@functools.lru_cache(maxsize=1 << 16)
def docs_unnamed(path: str, cmd: str, tested: tuple[str, ...]) -> bool:
    folders = {path[: i + 1] for i, ch in enumerate(path) if ch == "/"}
    named = path in cmd or any(
        w.rstrip("/") + "/" in folders for w in cmd_words(cmd) if w.startswith(("docs", "website"))
    )
    parts = path.split("/")
    if named or any(fnmatch.fnmatchcase(path, g) for g in tested) or set(parts[:-1]) & set(CODE_DIRS):
        return False
    return not any(fnmatch.fnmatchcase(parts[-1], g) for g in BUILD_FILES) and any(
        fnmatch.fnmatchcase(path, g) for g in DOC_PATHS
    )


@functools.lru_cache(maxsize=1 << 12)
def cmd_words(cmd: str) -> tuple[str, ...]:
    """A command's words, as the shell splits them (by whitespace when it cannot)."""
    try:
        return tuple(shlex.split(cmd))
    except ValueError:
        return tuple(cmd.split())


def tested_docs(spec: Json) -> list[str]:
    """The `tested_docs` globs of the campaign that submitted SPEC (its "campaign" folder): the
    documentation its tests execute."""
    folder = spec.get("campaign")
    state = read_json_shared(Path(folder) / "state.json") if folder else None
    return list(state.get("tested_docs", [])) if isinstance(state, dict) else []


def quest_globs(spec: Json) -> list[str]:
    """The file set of the quest that submitted SPEC, from its campaign's state; none for another job."""
    folder, quest = spec.get("campaign"), spec.get("quest")
    state = read_json_shared(Path(folder) / "state.json") if folder and quest else None
    q = state.get("quests", {}).get(quest, {}) if isinstance(state, dict) else {}
    return list(q.get("files", []))


def from_main(repo: Path, commit: str, main_head: str) -> bool:
    """Whether COMMIT brings main's changes: a merge whose other parents main holds, or main's own."""
    parents: list[str] = on_commits(
        ("parents", str(repo), commit), lambda: git(repo, "rev-parse", f"{commit}^@").split()
    )
    if len(parents) > 1:
        return all(ancestor(repo, x, main_head) for x in parents[1:])
    return ancestor(repo, commit, main_head)


def carried_ancestors(
    repo: Path, commit: str, spec: Json, tested: Sequence[str], *, merged: bool, files: Sequence[str]
) -> list[tuple[str, list[str], int]]:
    """COMMIT's nearest first-parent ancestors whose evidence counts at COMMIT, each with the docs it
    ignores and the merges from main it crosses: every commit between changes only documentation the
    command does not name, or, with MERGED, brings main's changes that touch none of the quest's
    FILES (its file set and own changes) nor the job's inputs (as input_files reads them). A
    measurement (strict, exclusive) without --inputs crosses only main's documentation."""
    cmd, inputs = spec["cmd"], spec.get("inputs") or []
    line = on_commits(
        ("first-parents", str(repo), commit),
        lambda: git(repo, "rev-list", "--first-parent", f"--max-count={CARRY_DEPTH + 1}", commit).split(),
    )
    main_head = rev(repo, main_branch(repo)) if merged else ""
    own = set(own_code(repo, main_head, commit, cmd, tested)) if merged else set()
    named = named_paths(cmd, spec.get("cwd", ""))
    measured = spec.get("level") in HEAVY and not inputs

    def touches(path: str) -> bool:
        name = path.rsplit("/", 1)[-1]
        return (
            measured
            or path in own
            or any(fnmatch.fnmatch(path, g) for g in files)
            or (
                bool(inputs)
                and (
                    any(fnmatch.fnmatchcase(path, g) for g in inputs)
                    or any(fnmatch.fnmatchcase(name, g) for g in BUILD_FILES)
                    or any(path == x or path.startswith(x + "/") for x in named)
                )
            )
        )

    out: list[tuple[str, list[str], int]] = []
    ignored: set[str] = set()
    merges, newer = 0, commit
    for older in line[1:]:
        paths = [x for x in changed(repo, older, newer) if not is_meta(x)]
        code = [x for x in paths if not doc_path(x, cmd, tested)]
        if merged and from_main(repo, newer, main_head):
            if any(touches(x) for x in code):
                break
            merges += 1
        elif code:
            break
        else:
            ignored.update(paths)
        out.append((older, sorted(ignored), merges))
        newer = older
    return out


def job_at(
    spec: Json,
    commit: str,
    tested: Sequence[str] | None = None,
    *,
    exact: bool = False,
    merged: bool = False,
    files: Sequence[str] | None = None,
) -> Json | None:
    """The job with SPEC's identity at COMMIT's code; when none passed there, the one that passed at
    the nearest ancestor carried_ancestors finds (with MERGED, across merges from main), with "at"
    (that commit), "docs" (the paths it ignores) and "merged" (the merges it crosses); unless EXACT,
    then its read_twin. FILES is the quest's file set (default: the submitting quest's)."""
    found = job_with_key({**spec, "commit": commit})
    if found is not None and found["state"].get("state") == "done":
        return found
    globs = tested_docs(spec) if tested is None else tested
    scope = quest_globs(spec) if files is None else files
    for older, paths, merges in carried_ancestors(Path(spec["repo"]), commit, spec, globs, merged=merged, files=scope):
        twin = job_with_key({**spec, "commit": older})
        if twin is not None and twin["state"].get("state") == "done":
            return {**twin, "at": older, **({"docs": paths} if paths else {}), **({"merged": merges} if merges else {})}
    return (None if exact else read_twin(spec, commit)) or found


def own_code(repo: Path, main_head: str, commit: str, cmd: str, tested: Sequence[str]) -> dict[str, str]:
    """The quest's own code at COMMIT: the paths it changes against its merge base with main, with
    their blobs, documentation CMD does not name aside."""
    own: dict[str, str] = on_commits(("own", str(repo), main_head, commit), lambda: own_blobs(repo, main_head, commit))
    return {x: b for x, b in own.items() if not doc_path(x, cmd, tested)}


def docs_note(job: Json) -> str:
    """How a job found at other code counts: "" for one at the exact code."""
    verb = "measured" if job["spec"].get("level") in HEAVY else "passed"
    if job.get("reads"):
        return f"{verb} at {job['at'][:12]}; the {job['reads']} files it read are unchanged"
    docs = f"ignores docs-only {', '.join(job['docs'])}" if job.get("docs") else ""
    if job.get("merged"):
        merges = f"{plural(job['merged'], 'merge')} from main since, none touching its files"
        return f"{verb} at {job['at'][:12]}; " + (f"{merges}; {docs}" if docs else f"only {merges}")
    return f"{verb} at {job['at'][:12]}; {docs}" if docs else ""


def job_with_key(spec: Json) -> Json | None:
    """The job with this spec's identity (code, command, toolchain), if one was submitted: alone,
    or queued after others (filed as KEY-ORDER), the one that passed first."""
    found = []
    for jid in key_links(job_key(spec)):
        d = jobs_dir() / jid
        if (d / "spec.json").exists():
            found.append(load_job(d))
    return next((j for j in found if j["state"].get("state") == "done"), found[0] if found else None)


def submit(spec: Json, force: bool = False, exact: bool = False) -> tuple[str, bool]:
    """Queues a job, or returns an identical one, after the same jobs, that is pending or succeeded;
    unless EXACT, also its read_twin."""
    jd = jobs_dir()
    defaults: list[tuple[str, object]] = [("refs", {}), ("env", {}), ("after", [])]
    for key, default in (
        *defaults,
        ("retries", 0),
        ("priority", 0),
        ("slot", ""),
        ("keep", False),
        ("quest", ""),
        ("campaign", ""),
    ):
        spec.setdefault(key, default)
    if spec["level"] in HEAVY and spec["minutes"] > int(os.environ.get("QUIET_MAX_MIN", "30")):
        die("a strict or exclusive job lasts at most 30 minutes; split it")
    key = job_key(spec) if spec.get("commit") else ""
    # a job that runs after others is a different request: reusing one queued after other jobs
    # would inherit their ordering (and their failure)
    order = hashlib.sha256(json.dumps(sorted(spec["after"])).encode()).hexdigest()[:8] if spec["after"] else ""
    link = jd / "keys" / (f"{key}-{order}" if order else key)
    if key and not force and link.is_symlink():
        old = load_job(jd / os.readlink(link))
        # a slimmed job is a record of its result, not its outputs: asking again runs it again
        if old["state"].get("state") in ("queued", "running", "done") and not old["state"].get("slimmed"):
            return old["id"], True
    twin = None
    if key and not (force or exact):  # a passed twin's order is moot: it ran after what it needed
        twin = job_at(spec, spec["commit"], merged=bool(spec["quest"]))
        twin = twin if twin is not None and twin["state"].get("state") == "done" else None
    if twin is not None and not twin["state"].get("slimmed"):
        return twin["id"], True
    jid = time.strftime("%y%m%d-%H%M%S-") + secrets.token_hex(3)
    d = jd / jid
    d.mkdir()
    spec.update(key=key, submitted=time.time(), fingerprint=fingerprint(), path=os.environ.get("PATH", ""))
    if key:
        spec["rkey"] = job_key(spec, reads=True)
    write_json(d / "spec.json", spec)
    write_json(d / "state.json", {"state": "queued", "attempt": 1})
    if key:
        link.parent.mkdir(exist_ok=True)
        tmp = link.with_name(f".{key}.{os.getpid()}")
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        tmp.symlink_to(jid)
        os.replace(tmp, link)
    return jid, False


def shape(spec: Json) -> str:
    """A quest's job up to its commits: campaign, quest, repository, command, directory, level,
    environment and the other repositories it names. Jobs of one shape are one job at other code."""
    refs = sorted((name, r.get("repo")) for name, r in spec.get("refs", {}).items())
    env = sorted(spec.get("env", {}).items())
    fields = ("campaign", "quest", "repo", "cmd", "cwd", "level")
    build = [spec["build"]] if spec.get("build") else []
    inverted = [spec["fails_without"]] if spec.get("fails_without") else []
    return json.dumps([*(spec.get(k) for k in fields), refs, env, *build, *inverted])


def code_of(spec: Json) -> list[Any]:
    """The commits a job runs: its own and each other repository's."""
    return [spec.get("commit"), sorted((name, r.get("commit")) for name, r in spec.get("refs", {}).items())]


def descends(old: Json, new: Json) -> bool:
    """Whether job spec NEW runs OLD's code or newer: each of OLD's commits is an ancestor of NEW's
    in its repository. A sibling (a mutant, a run at the base) is not newer."""
    pairs = [(old.get("repo"), old.get("commit"), new.get("commit"))] + [
        (r.get("repo"), r.get("commit"), new.get("refs", {}).get(k, {}).get("commit"))
        for k, r in old.get("refs", {}).items()
    ]
    return all(repo and a and b and ancestor(Path(repo), a, b) for repo, a, b in pairs)


def what_stem(j: Json) -> str:
    """A job's label without the commit it ran at: the same stem is the same kind of run."""
    return re.sub(r" at [0-9a-f]{7,40}$", "", str(j["spec"].get("what", "")))


def same_role(old: Json, new: Json) -> bool:
    """Whether two jobs are one piece of evidence at other code though their commands differ: the
    same label, repository and level, tagged for a shared criterion of the same quest."""

    def role(j: Json) -> set[tuple[Any, ...]]:
        return {
            (t.get("campaign"), t.get("quest"), cid, t.get("fails_without"))
            for t in j.get("tags", [])
            for cid in t.get("for", [])
        }

    keys = ("repo", "level")
    return (
        bool(role(old) & role(new))
        and what_stem(old) == what_stem(new) != ""
        and all(old["spec"].get(k) == new["spec"].get(k) for k in keys)
    )


def supersede(ids: list[str]) -> None:
    """Cancels the queued or running jobs these newly queued quest jobs replace: same shape or same
    tagged role (same_role), older code (descends), submitted earlier. One a pending job waits on (--after) is kept unless that job is replaced too."""
    new = [resolve_job(x) for x in dict.fromkeys(ids)]
    jobs = [j for j in all_jobs() if j["id"] not in ids]
    replaced: dict[str, str] = {}
    for n in new:
        if not n["spec"].get("quest") or not n["spec"].get("commit"):
            continue
        for j in jobs:
            s = j["spec"]
            if (
                j["state"].get("state") in ("queued", "running")
                and (shape(s) == shape(n["spec"]) or same_role(j, n))
                and code_of(s) != code_of(n["spec"])
                and s.get("submitted", 0) <= n["spec"].get("submitted", 0)
                and descends(s, n["spec"])
            ):
                replaced.setdefault(j["id"], n["id"])
    pending = [j for j in all_jobs() if j["state"].get("state") not in TERMINAL and j["id"] not in replaced]
    for jid, by in replaced.items():
        waiter = next((p["id"] for p in pending if jid in p["spec"].get("after", [])), "")
        if waiter:
            print(f"kept {jid}: {waiter} waits on it")
            continue
        j = load_job(jobs_dir() / jid)
        if j["state"].get("state") == "queued" and claim(j):
            finish(load_job(j["dir"]), -1, "cancelled", note=f"superseded by {by}")
            print(f"superseded {jid}: queued at {j['spec']['commit'][:12]}, cancelled for {by}")
        elif j["state"].get("state") == "running":
            (j["dir"] / "cancel").touch()
            print(f"superseded {jid}: running at {j['spec']['commit'][:12]}, stopped for {by}")


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
