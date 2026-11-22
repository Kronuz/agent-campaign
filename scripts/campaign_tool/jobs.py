"""The job store: a job's identity, its records, the index of terminal jobs and the tags that make
it evidence."""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .core import (
    HOST,
    TERMINAL,
    CampaignError,
    Json,
    atomic_write,
    die,
    fingerprint,
    git,
    jobs_dir,
    read_json,
    write_json,
)
from .repo import _heads, _listed, code_tree, is_meta, on_commits

_derived: dict[tuple[str, int], tuple[list[Json], Any]] = {}


def derived(jobs: list[Json], name: str, make: Callable[[list[Json]], Any]) -> Any:
    """make(JOBS), once per list of jobs, for a command that asks the same of it for every quest."""
    held = _derived.get((name, id(jobs)))
    if held is None or held[0] is not jobs:
        if len(_derived) >= 16:
            del _derived[next(iter(_derived))]
        held = _derived[(name, id(jobs))] = (jobs, make(jobs))
    return held[1]


def quest_jobs(jobs: list[Json], campaign: str, pid: str) -> list[Json]:
    """A quest's jobs among JOBS, in order."""

    def group(jobs: list[Json]) -> dict[tuple[Any, Any], list[Json]]:
        groups: dict[tuple[Any, Any], list[Json]] = {}
        for j in jobs:
            groups.setdefault((j["spec"].get("campaign"), j["spec"].get("quest")), []).append(j)
        return groups

    found: list[Json] = derived(jobs, "quest", group).get((campaign, pid), [])
    return found


BUILD_FILES = ("CMakeLists*", "*.cmake", "CMakePresets*.json", "*toolchain*", "Makefile", "GNUmakefile", "*.mk")
BUILD_FILES += ("meson.build", "meson_options.txt", "pyproject.toml", "setup.py", "setup.cfg", ".gitmodules")
INPUTS_HELP = (
    "the files a check's jobs depend on (GLOBS comma-separated; empty clears): its evidence then survives "
    "commits that change nothing under them, the build files (CMakeLists*, *.cmake, toolchain files) and "
    "the paths its command names; a file it reads outside them is not checked"
)


def named_paths(cmd: str, cwd: str) -> list[str]:
    """The repository paths a command names (its scripts, and folders it enters), relative to the root."""
    out: list[str] = []
    for token in re.split(r"[\s;|&()<>=\"'`]+", cmd):
        word = token[2:] if token.startswith("./") else token
        if not word or word.startswith(("-", "/", "$", "~")) or set(word) & set("*?[") or word in (".", ".."):
            continue
        out += [os.path.normpath(os.path.join(cwd, word)).rstrip("/")] if cwd else [word.rstrip("/")]
    return [x for x in dict.fromkeys(out) if x and not x.startswith("..")]


def input_files(repo: Path, commit: str, spec: Json) -> list[str]:
    """The `mode sha path` entries of COMMIT that a job with declared inputs depends on: what its
    globs match, the build files, and the paths its command names (a named folder, all of it)."""
    globs, named = spec["inputs"], named_paths(spec["cmd"], spec.get("cwd", ""))

    def wanted(path: str) -> bool:
        if is_meta(path):
            return False
        name = path.rsplit("/", 1)[-1]
        return (
            any(fnmatch.fnmatchcase(path, g) for g in globs)
            or any(fnmatch.fnmatchcase(name, g) for g in BUILD_FILES)
            or any(path == x or path.startswith(x + "/") for x in named)
        )

    listing = git(repo, "ls-tree", "-r", "-z", "--full-tree", commit).split("\0")
    return [
        f"{meta.split()[0]} {meta.split()[2]} {path}"
        for meta, _, path in (x.partition("\t") for x in listing if x)
        if wanted(path)
    ]


def matches_inputs(repo: Path, commit: str, globs: list[str]) -> bool:
    listing = git(repo, "ls-tree", "-r", "-z", "--name-only", "--full-tree", commit).split("\0")
    return any(fnmatch.fnmatchcase(x, g) for x in listing if x and not is_meta(x) for g in globs)


def input_digest(repo: Path, commit: str, spec: Json) -> str:
    """What identifies the code of a job with declared inputs: the blobs input_files lists."""
    scope = json.dumps([spec["inputs"], spec["cmd"], spec.get("cwd", "")])
    tag = "inputs:" + hashlib.sha256(scope.encode()).hexdigest()[:16]
    digest: str = on_commits(
        (tag, str(repo), commit),
        lambda: hashlib.sha256("\n".join(input_files(repo, commit, spec)).encode()).hexdigest(),
    )
    return digest


def job_key(spec: Json, reads: bool = False) -> str:
    """The identity of a result: same key, same evidence. Host and toolchain are part of it, and
    the code is identified by its tree, not its commit; a job of a check with declared inputs, by
    the blobs under them. READS leaves the code out: the key read sets are filed under."""
    refs = sorted((name, code_tree(Path(r["repo"]), r["commit"])) for name, r in spec["refs"].items())
    repo = Path(spec["repo"])
    code = (
        "reads"
        if reads
        else ["inputs", input_digest(repo, spec["commit"], spec)]
        if spec.get("inputs")
        else code_tree(repo, spec["commit"])
    )
    identity = [
        HOST,
        fingerprint(),
        spec["repo"],
        code,
        refs,
        spec["cmd"],
        spec["cwd"],
        spec["level"],
        sorted(spec["env"].items()),
        *([sorted(spec["scripts"].items())] if spec.get("scripts") else []),  # an evidence job's outside files
        *([["build", spec["build"]]] if spec.get("build") else []),
        *([["fails_without", spec["fails_without"]]] if spec.get("fails_without") else []),
    ]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]


_jobs: dict[str, tuple[int, Json]] = {}  # this process's terminal jobs: id -> (its directory's inode, job)


def load_job(d: Path) -> Json:
    """A job's spec, state and tags; a terminal one read once per process."""
    try:
        inode = os.stat(d).st_ino
    except OSError:
        return read_job(d)
    hit = _jobs.get(d.name)
    if hit is not None and hit[0] == inode and hit[1]["dir"] == d:
        return hit[1]
    job = read_job(d)
    if job["state"].get("state") in TERMINAL:
        _jobs[d.name] = (inode, job)
    return job


def read_job(d: Path) -> Json:
    def tolerant(path: Path) -> Json:
        try:
            return read_json(path) or {}
        except CampaignError:
            return {"state": "corrupt"}

    job: Json = {"id": d.name, "dir": d, "spec": tolerant(d / "spec.json"), "state": tolerant(d / "state.json")}
    with contextlib.suppress(OSError, ValueError):
        job["charge"] = float((d / "charge").read_text())  # slot-seconds charged over one slot (add_charge)
    if (d / "for.json").exists():
        job["tags"] = (read_json(d / "for.json") or {}).get("tags", [])
    return job


def campaign_env(spec: Json) -> dict[str, str]:
    """The submitting campaign's `config env=` settings, read when the job runs, so queued jobs follow them."""
    state = read_json(Path(spec["campaign"]) / "state.json") if spec.get("campaign") else None
    return {k: v for k, _, v in (x.partition("=") for x in (state or {}).get("env", []))}


def tag(jid: str, campaign: str, quest: str, ids: list[str], fails_without: bool) -> None:
    """Records that job JID is evidence for criteria IDS of a quest: passing at its code, or, with
    FAILS_WITHOUT, exiting 0 because the test fails without the fix (a mutant or a revert)."""
    path = jobs_dir() / jid / "for.json"
    tags = (read_json(path) or {}).get("tags", []) if path.exists() else []
    for t in tags:
        both = set(ids) & set(t["for"]) if (t["campaign"], t["quest"]) == (campaign, quest) else set()
        if both and t["fails_without"] != fails_without:
            way = "passes" if fails_without else "fails without the fix"
            die(f"{jid} also {way} for {', '.join(sorted(both))}: one job cannot prove both")
    entry = {"campaign": campaign, "quest": quest, "fails_without": fails_without}
    old = next((t for t in tags if {k: t.get(k) for k in entry} == entry), None)
    if old is None:
        tags.append({**entry, "for": ids})
    else:
        old["for"] = list(dict.fromkeys([*old["for"], *ids]))
    write_json(path, {"tags": tags})
    _jobs.pop(jid, None)


def attempt_dir(job: Json) -> Path:
    return job["dir"] / f"a{int(job['state'].get('attempt', 1))}"


JOB_DAY = re.compile(r"\d{6}-")  # a job id starts with the day it was submitted


def job_shard(jid: str) -> str:
    """The job index file that holds a terminal job's record: one per day of ids."""
    return f"{jid[:6] if JOB_DAY.match(jid) else '_'}.json"


def all_jobs(cache: dict[str, tuple[int, Json]] | None = None) -> list[Json]:
    """Every job, by id. A terminal job is read once (its record never changes again) and served from
    CACHE (this process's, unless a caller keeps its own) while its directory is the same. Across
    processes, the job index serves it while its directory and state.json have the stat they had.
    While a command only reads (frozen), the store is listed once."""
    if cache is None and _listed:
        return list(_listed[0])
    if cache is None and _heads:
        _listed.append(all_jobs(_jobs))
        return list(_listed[0])
    cache = _jobs if cache is None else cache
    jobs: list[Json] = []
    root = jobs_dir()
    with os.scandir(root) as it:
        entries = sorted((e for e in it if e.is_dir() and not e.name.startswith(".")), key=lambda e: e.name)
    shards: dict[str, dict[str, list[Any]]] = {}
    dirty: set[str] = set()
    for e in entries:
        hit = cache.pop(e.name, None)
        if hit is not None and hit[0] == e.inode():
            cache[e.name] = hit
            jobs.append(hit[1])
            continue
        shard = job_shard(e.name)
        if shard not in shards:
            shards[shard] = {}
            with contextlib.suppress(OSError, ValueError):
                shards[shard] = json.loads((root / ".index" / shard).read_text())
        try:
            st = os.stat(f"{e.path}/state.json")
        except OSError:
            st = None
        ident: list[int] = [e.inode(), e.stat().st_mtime_ns, st.st_mtime_ns, st.st_size] if st else []
        record = shards[shard].get(e.name)
        d = Path(e.path)
        job: Json
        if ident and record is not None and record[0] == ident:
            job = {"id": e.name, "dir": d, "spec": record[1], "state": record[2]}
            if record[3] is not None:
                job["tags"] = record[3]
        elif os.path.exists(f"{e.path}/spec.json"):
            job = read_job(d)
            if ident and job["state"].get("state") in TERMINAL:
                shards[shard][e.name] = [ident, job["spec"], job["state"], job.get("tags")]
                dirty.add(shard)
        else:
            continue
        if job["state"].get("state") in TERMINAL:
            cache[e.name] = (e.inode(), job)
        jobs.append(job)
    names = {e.name for e in entries}
    for k in cache.keys() - names:
        del cache[k]  # pruned
    for shard, records in shards.items():
        gone = records.keys() - names
        for k in gone:
            del records[k]
        if shard in dirty or gone:
            atomic_write(root / ".index" / shard, json.dumps(records, separators=(",", ":")))
    return jobs


_keys: list[Any] = []  # [the keys folder's mtime, {key: the job ids its links name}]


def key_links(key: str) -> list[str]:
    """The jobs filed under KEY in the keys folder (KEY, then KEY-ORDER in folder order), from one
    listing per process, listed again whenever the folder changes, unless the command only reads.
    A link's target never changes, so the listing reads only links it has not seen (by inode and
    mtime) since the last listing on the host."""
    keys = jobs_dir() / "keys"
    if not (_keys and _heads):
        try:
            changed_at = keys.stat().st_mtime_ns
        except FileNotFoundError:
            return []
        if not _keys or _keys[0] != changed_at:
            _keys[:] = [changed_at, list_keys(keys)]
    found: list[str] = _keys[1].get(key, [])
    return found


def list_keys(keys: Path) -> dict[str, list[str]]:
    seen_path = jobs_dir() / ".index" / "keys.json"
    seen: dict[str, list[Any]] = {}
    with contextlib.suppress(OSError, ValueError):
        seen = json.loads(seen_path.read_text())
    links: dict[str, list[Any]] = {}
    with os.scandir(keys) as it:
        for e in it:
            if e.name.startswith(".") or not e.is_symlink():
                continue
            ident = [e.inode(), e.stat(follow_symlinks=False).st_mtime_ns]
            old = seen.get(e.name)
            links[e.name] = old if old is not None and old[:2] == ident else [*ident, os.readlink(e.path)]
    if links != seen:
        atomic_write(seen_path, json.dumps(links, separators=(",", ":")))
    index: dict[str, list[str]] = {}
    exact: dict[str, str] = {}
    for name, (_, _, jid) in links.items():
        key, dash, _ = name.partition("-")
        if dash:
            index.setdefault(key, []).append(jid)
        else:
            exact[key] = jid
    for key, jid in exact.items():
        index[key] = [jid, *index.get(key, [])]
    return index


def job_summary(job: Json) -> Json:
    s, st = job["spec"], job["state"]
    return {
        "job": job["id"],
        "what": s.get("what", ""),
        "commit": s.get("commit"),
        "cmd": s.get("cmd"),
        "level": s.get("level"),
        "host": st.get("host", HOST),
        "state": st.get("state"),
        "exit": st.get("exit"),
        "seconds": round(st["ended"] - st["began"], 1) if st.get("ended") and st.get("began") else None,
        **({"docs": job["docs"]} if job.get("docs") else {}),
        **({"reads": job["reads"], "at": job["at"]} if job.get("reads") else {}),
        **({"inputs": s["inputs"]} if s.get("inputs") else {}),
    }


def finish(job: Json, code: int, state: str, note: str = "", fence: int = 0) -> bool:
    """Records an attempt's result; with fence (the runner's pid), only if it still owns the attempt."""
    d, st = job["dir"], dict(job["state"])
    if fence:
        current = load_job(d)["state"]
        if current.get("attempt") != st.get("attempt") or current.get("runner") != fence:
            return False
    began = attempt_dir(job) / "began"
    st.update(state=state, exit=code, ended=time.time())
    if began.exists():
        with contextlib.suppress(ValueError):
            st["began"] = float(began.read_text().strip())
    if note:
        st["note"] = note
    with contextlib.suppress(OSError):  # the light holds a strict run shared the machine with
        st["beside"] = (attempt_dir(job) / "beside").read_text().splitlines()
    write_json(d / "state.json", st)
    return True


FAILS_WITHOUT = 2  # spec["fails_without"]: the command must fail; no field is a self-inverted command that exits 0
NO_RUN = {  # the command ran no test
    125: "exited 125 (git bisect's skip / the command's own refusal)",
    126: "exit 126: the command did not run (it is not runnable)",
    127: "exit 127: the command did not run (it was not found)",
}
KILLS = {2: "SIGINT", 9: "SIGKILL", 15: "SIGTERM"}  # sent from outside (the OOM killer, an operator), unlike a crash
NO_TEST_LINES = (  # a runner's line saying a test it was asked to run gave no result
    rb"^No tests were found",  # ctest: nothing matched
    rb"^ *\d+/\d+ Test +#\d+: .*\*\*\*Not Run",  # ctest: a selected test's executable was never built
    rb"^ *\d+/\d+ Test +#\d+: .*\*\*\*Timeout",  # ctest: a test past its TIMEOUT, no more than exit 124
    rb"^NO TESTS RAN",  # unittest: nothing matched (exit 5)
    rb"^ImportError: Failed to import test module:",  # unittest: a test module that did not import
)
NO_TESTS = re.compile(b"|".join(NO_TEST_LINES), re.M)


def ran_no_test(log: Path) -> str:
    """The first line of a job's LOG saying a test runner gave no result (`NO_TEST_LINES`), or "": the
    one fact a fails-without record has only in its log."""
    try:
        found = NO_TESTS.search(log.read_bytes())
    except OSError:
        return ""
    return " ".join(found.group().decode(errors="replace").split()) if found else ""


def fails_without_miss(st: Json, log: Path | None = None) -> str:
    """Why a finished --fails-without attempt (ST, with its LOG once) shows nothing about the fix, or
    "" when it does: its build built, its command ran a test, and the test failed."""
    built, code = (st.get("build") or {}).get("exit"), st.get("exit")
    signum = -code if isinstance(code, int) and code < 0 else code - 128 if isinstance(code, int) else 0
    if built not in (None, 0):
        return f"the mutant did not build: its build failed (exit {built}), so it shows nothing about the fix"
    if code == 0:
        return "the test passed without the fix: it must fail there"
    if code == 124:
        return "it timed out, so it shows nothing about the fix"
    if code in NO_RUN:
        return f"{NO_RUN[code]}, so it shows nothing about the fix"
    if signum in KILLS:
        return f"exit {code}: it was killed by {KILLS[signum]} from outside, so it shows nothing about the fix"
    if log is not None and (line := ran_no_test(log)):
        return f"no test result ({line}; exit {code}), so it shows nothing about the fix"
    return ""


def claim(job: Json) -> bool:
    try:
        (job["dir"] / "claim").mkdir()
    except FileExistsError:
        return False
    (job["dir"] / "heartbeat").touch()
    write_json(job["dir"] / "state.json", {**job["state"], "state": "running", "claimed": time.time(), "host": HOST})
    return True


def slot_of(spec: Json) -> str:
    """The build slot a job checks its commit out in, or "" when it has its own checkout."""
    return str(spec.get("slot") or "") if spec.get("commit") else ""


def resolve_job(jid: str) -> Json:
    d = jobs_dir() / jid
    if not (d / "spec.json").exists():
        die(f"no job {jid}")
    return load_job(d)
