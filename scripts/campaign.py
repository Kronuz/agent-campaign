#!/usr/bin/env python3
"""campaign: quests, worktrees, landing and a host job queue for multi-agent projects.

State lives in .campaign/NAME/state.json in the campaign's home repository and is written only by
this tool, from that repository's main checkout. Jobs live in $CAMPAIGN_JOBS
(~/.scratch/campaign/jobs) on the host that runs them. `campaign help COMMAND` documents each
command; the skill's SKILL.md says when to use it.
"""

from __future__ import annotations

import argparse
import atexit
import codecs
import contextlib
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Collection, Generator, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn, TextIO

import report
from style import add_help, mark, paint, show_help, styled

SKILL = Path(__file__).resolve().parent.parent
TEMPLATES = SKILL / "templates"
META = ".campaign"  # .campaign/NAME/ holds a campaign's files in its home repository
EXCLUDE_META = (f":(exclude){META}",)
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
LEAD_FILES = ("state.json", "PLAN.md", "hunt.md", "quests/", "reviews/")  # the lead writes these, never a quest branch
STATUSES = ("planned", "active", "review", "landed", "blocked", "dropped")
FLIGHT = ("active", "review", "blocked")  # in flight: started, neither landed nor dropped
RISKS = ("low", "normal", "high", "critical")
LENSES = ("correctness", "security", "durability", "design", "system")
CODE_LENSES = ("correctness", "security", "durability")  # lenses that review code, not a design
ASSIGNMENTS = ("implementation", "design", "research", "performance", "exploration", "documentation", "maintenance")
ROLES = ("contributor", "reviewer")
LEVELS = ("none", "light", "strict", "exclusive")
DEFAULT_LENSES: dict[str, list[str]] = {
    "low": [],
    "normal": [],
    "high": ["correctness"],
    "critical": ["correctness", "design"],
}
SET_STATUS: dict[str, tuple[str, ...]] = {  # what `campaign set status=` may move a quest to
    "planned": ("blocked", "dropped"),
    "active": ("review", "blocked", "dropped"),
    "review": ("active", "blocked", "dropped"),
    "blocked": ("planned", "active", "review", "dropped"),
    "dropped": ("planned",),
    "landed": (),
}
TERMINAL = ("done", "failed", "timeout", "interrupted", "cancelled", "skipped", "corrupt")
FAILED = ("failed", "timeout", "interrupted")
HEAVY = ("strict", "exclusive")
SHORT_RUN = 10  # minutes: a measurement (strict, exclusive) that does not say --long; it drains the machine
LIGHT_RUN = 30  # minutes: a light fuzz or soak, its build included, that does not say --long
LONG_RUN = re.compile(r"fuzz|soak", re.I)  # runs that grow to fill any time they are given
STALE = float(os.environ.get("CAMPAIGN_STALE_SECONDS", "60"))
UNSTARTED_ATTEMPTS = 3  # a runner that dies before the command starts is requeued, up to this many tries
BEAT = float(os.environ.get("CAMPAIGN_HEARTBEAT_SECONDS", "15"))
HOST = socket.gethostname().split(".")[0]

Json = dict[str, Any]


class CampaignError(Exception):
    pass


def die(message: str) -> NoReturn:
    raise CampaignError(message)


# ---------------------------------------------------------------- small utilities


def sh(args: list[str], cwd: Path | None = None, check: bool = True) -> str:
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        die(f"{shlex.join(args)}: {(p.stderr or p.stdout).strip()}")
    return p.stdout.strip()


def git(repo: Path, *args: str, check: bool = True) -> str:
    return sh(["git", "-C", str(repo), *args], check=check)


def git_ok(repo: Path, *args: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True).returncode == 0


def stamp(t: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() if t is None else t))


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def write_json(path: Path, data: Json) -> None:
    atomic_write(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def read_json(path: Path) -> Json | None:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as e:
        die(f"{path}: corrupt JSON ({e})")
    if not isinstance(data, dict):
        die(f"{path}: expected a JSON object")
    return data


def csv(value: str | None) -> list[str]:
    return [x.strip() for x in (value or "").split(",") if x.strip()]


def alive(pid: int) -> bool:
    """Whether PID runs; a zombie (dead, not yet reaped by its parent) does not."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout
    return not stat.strip().startswith("Z")


def template(name: str, **fields: str) -> str:
    text = (TEMPLATES / name).read_text()
    for k, v in fields.items():
        text = text.replace("{" + k + "}", v)
    return text


def boot_id() -> str:
    linux = Path("/proc/sys/kernel/random/boot_id")
    if linux.exists():
        return linux.read_text().strip()
    # macOS: kern.boottime drifts with clock adjustments (seconds and microseconds); the session UUID does not
    return sh(["sysctl", "-n", "kern.bootsessionuuid"], check=False) or sh(
        ["sysctl", "-n", "kern.boottime"], check=False
    )


BOOTTIME = re.compile(r"\{ sec = (\d+),")


def same_boot(recorded: str | None) -> bool:
    """Whether a job's recorded boot is this boot; a macOS kern.boottime record matches within a minute."""
    now = boot_id()
    if recorded == now:
        return True
    old = BOOTTIME.match(recorded or "")
    if not old or not now:
        return False
    current = BOOTTIME.match(sh(["sysctl", "-n", "kern.boottime"], check=False))
    return bool(current) and abs(int(old.group(1)) - int(current.group(1))) < 60


FINGERPRINT_CMD = (
    "uname -srm; for t in cc c++ python3 cmake rustc; do command -v $t >/dev/null && $t --version 2>&1 | head -1; done; "
    "command -v go >/dev/null && go version; "
)
_fingerprint: list[str] = []


def fingerprint() -> str:
    """The host's toolchain, so a cached result is reused only on the toolchain that produced it."""
    if not _fingerprint:
        out = sh(["bash", "-c", FINGERPRINT_CMD + os.environ.get("CAMPAIGN_FINGERPRINT", "")], check=False)
        _fingerprint.append(hashlib.sha256(out.encode()).hexdigest()[:16])
    return _fingerprint[0]


# ---------------------------------------------------------------- repositories


def toplevel(path: Path) -> Path:
    return Path(git(path, "rev-parse", "--show-toplevel"))


def main_checkout(repo: Path) -> Path:
    return Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).parent


def main_branch(repo: Path) -> str:
    for name in (os.environ.get("CAMPAIGN_MAIN", ""), "main", "master"):
        if name and has_branch(repo, name):
            return name
    die(f"{repo}: no main or master branch (set CAMPAIGN_MAIN)")


def has_branch(repo: Path, name: str) -> bool:
    if _heads:
        return branch_head(repo, name) is not None
    return git_ok(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{name}")


SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_fixed: dict[tuple[str, ...], Any] = {}  # answers about commits, which never change
_heads: list[dict[str, dict[str, str]]] = []  # branch heads, while a command only reads


@contextlib.contextmanager
def frozen_heads() -> Generator[None, None, None]:
    """Reads each repository's branch heads once, for a command that only reads (status asks about
    each branch many times); commands that move branches always ask git."""
    _heads.append({})
    try:
        yield
    finally:
        _heads.pop()


def branch_head(repo: Path, ref: str) -> str | None:
    if not _heads or SHA.fullmatch(ref):
        return None
    snapshot = _heads[0]
    if str(repo) not in snapshot:
        listing = git(repo, "for-each-ref", "--format=%(objectname) %(refname)", "refs/heads", check=False)
        pairs = (x.partition(" ") for x in listing.splitlines())
        snapshot[str(repo)] = {r[len("refs/heads/") :]: sha for sha, _, r in pairs}
    return snapshot[str(repo)].get(ref)


def on_commits(key: tuple[str, ...], ask: Callable[[], Any]) -> Any:
    """ask(), remembered when every argument after the first two is a commit id: in the process,
    and on disk for every later command, since what git says of fixed commits never changes."""
    if not all(SHA.fullmatch(x) for x in key[2:]):
        return ask()
    if key not in _fixed:
        path = jobs_dir() / ".facts" / hashlib.sha256(json.dumps(key).encode()).hexdigest()[:32]
        try:
            _fixed[key] = json.loads(path.read_text())
        except (OSError, ValueError):
            _fixed[key] = ask()
            path.parent.mkdir(exist_ok=True)
            tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(_fixed[key]))
            tmp.replace(path)
    return _fixed[key]


def rev(repo: Path, ref: str) -> str:
    return branch_head(repo, ref) or git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")


def has_ref(repo: Path, ref: str) -> bool:
    return bool(branch_head(repo, ref)) or git_ok(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")


def ancestor(repo: Path, a: str, b: str) -> bool:
    return bool(on_commits(("ancestor", str(repo), a, b), lambda: git_ok(repo, "merge-base", "--is-ancestor", a, b)))


_reachable: dict[tuple[str, str], set[str]] = {}


def on_branch(repo: Path, commit: str, ref: str) -> bool:
    """Whether COMMIT is in REF's history: one listing of that history per command, not a git
    call per commit (Xapiand's 15k commits list in about 0.3 s)."""
    key = (str(repo), ref)
    if key not in _reachable:
        _reachable[key] = set(git(repo, "rev-list", ref, check=False).split())
    return commit in _reachable[key] if SHA.fullmatch(commit) else ancestor(repo, commit, ref)


def ever_on(repo: Path, commit: str, names: list[str]) -> bool:
    """Whether COMMIT is or was on one of these branches: in the history of a tip, or of an earlier
    head their reflogs record (a branch rebased since). One reflog read and one rev-list in all."""
    names = [b for b in names if has_ref(repo, b)]
    if not names:
        return False
    heads = git(repo, "log", "-g", "--format=%H", *(f"refs/heads/{b}" for b in names), check=False).split()
    outside = git(repo, "rev-list", "--count", commit, "--not", *names, *dict.fromkeys(heads), check=False)
    return outside == "0"


def merge_base(repo: Path, a: str, b: str) -> str:
    return str(on_commits(("merge-base", str(repo), a, b), lambda: git(repo, "merge-base", a, b)))


_common: dict[str, str] = {}


def common_dir(p: Path) -> str:
    """The repository a checkout belongs to (its common git dir), or "" outside one."""
    if str(p) not in _common:
        _common[str(p)] = git(p, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
    return _common[str(p)]


def same_repo(a: Path, b: Path) -> bool:
    """Whether two checkouts (a main checkout, its worktrees) share one repository."""
    return bool(common_dir(a)) and common_dir(a) == common_dir(b)


def changed(repo: Path, a: str, b: str) -> list[str]:
    """Paths that differ between the trees of a and b."""
    return list(
        on_commits(
            ("changed", str(repo), a, b), lambda: [x for x in git(repo, "diff", "--name-only", a, b).splitlines() if x]
        )
    )


def meta_rel(path: str) -> str | None:
    """A campaign file's path inside its campaign's directory; None for code."""
    return "/".join(path.split("/")[2:]) if path.startswith(META + "/") else None


def is_meta(path: str) -> bool:
    return meta_rel(path) is not None


def lead_file(path: str) -> bool:
    rel = meta_rel(path)
    return rel is not None and any(rel == o or (o.endswith("/") and rel.startswith(o)) for o in LEAD_FILES)


_cat: dict[str, tuple[int, subprocess.Popen[bytes]]] = {}
_trees: dict[tuple[str, str], str] = {}


def read_object(repo: Path, name: str) -> tuple[str, str, bytes] | None:
    """An object's id, type and raw content (`REF:path`, `COMMIT^{tree}`), or None when it does not
    exist, through one `git cat-file --batch` per repository."""
    held = _cat.get(str(repo))
    if not held or held[0] != os.getpid():
        proc = subprocess.Popen(
            ["git", "-C", str(repo), "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        held = _cat[str(repo)] = (os.getpid(), proc)
    proc = held[1]
    assert proc.stdin is not None and proc.stdout is not None
    try:
        proc.stdin.write(name.encode() + b"\n")
        proc.stdin.flush()
    except OSError:  # not a repository: git has exited
        return None
    header = proc.stdout.readline().split()
    if len(header) != 3 or header[-1] in (b"missing", b"ambiguous"):
        return None
    data = proc.stdout.read(int(header[2]))
    proc.stdout.read(1)
    return header[0].decode(), header[1].decode(), data


@atexit.register
def close_readers() -> None:
    """Ends the `git cat-file` processes this process started."""
    for pid, proc in _cat.values():
        if pid == os.getpid():
            for pipe in (proc.stdin, proc.stdout):
                if pipe:
                    pipe.close()
            proc.wait()
    _cat.clear()


def cat_object(repo: Path, name: str) -> tuple[str, bytes]:
    found = read_object(repo, name)
    if found is None:
        die(f"{repo}: no object {name}")
    return found[0], found[2]


def file_at(repo: Path, ref: str, path: str) -> str:
    """A file's text at a commit or branch; "" when it has none."""
    found = read_object(repo, f"{ref}:{path}")
    return found[2].decode(errors="replace") if found and found[1] == "blob" else ""


def names_at(repo: Path, ref: str, folder: str) -> list[str]:
    """The names in a folder at a commit or branch; none when it has no such folder."""
    found = read_object(repo, f"{ref}:{folder}")
    if not found or found[1] != "tree":
        return []
    return [name.decode() for name, _ in tree_entries(found[2], len(found[0]) // 2)]


def tree_entries(data: bytes, width: int) -> list[tuple[bytes, bytes]]:
    """A raw tree's (name, entry bytes) pairs, in git's order."""
    out: list[tuple[bytes, bytes]] = []
    i = 0
    while i < len(data):
        nul = data.index(b"\0", i)
        out.append((data[data.index(b" ", i) + 1 : nul], data[i : nul + 1 + width]))
        i = nul + 1 + width
    return out


def tree_id(entries: list[bytes], width: int) -> bytes:
    """The id git gives a tree of these entries (what `git mktree` writes), without writing it."""
    body = b"".join(entries)
    return hashlib.new("sha1" if width == 20 else "sha256", b"tree %d\0" % len(body) + body).digest()


def code_tree(repo: Path, commit: str) -> str:
    """The commit's tree without .campaign/: what builds and tests see, so evidence survives commits
    that touch only campaign files (a journal, a review). Computed from the raw tree, since status
    asks for every job's."""
    key = (str(repo), commit)
    if key in _trees:
        return _trees[key]
    oid, root = cat_object(repo, f"{commit}^{{tree}}")
    width = len(oid) // 2
    entries = tree_entries(root, width)
    kept = [entry for name, entry in entries if name != META.encode()]
    tree = tree_id(kept, width).hex() if len(kept) != len(entries) else oid
    if SHA.fullmatch(commit):  # a ref may move; a commit's tree never does
        _trees[key] = tree
    return tree


def blobs(repo: Path, commit: str, paths: list[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    if paths:
        for line in git(repo, "ls-tree", "-r", commit, "--", *paths).splitlines():
            meta, path = line.split("\t", 1)
            mode, _, sha = meta.split()
            found[path] = f"{mode}:{sha}"
    return {p: found.get(p, "deleted") for p in paths}


# ---------------------------------------------------------------- campaign state


def locate(cwd: Path) -> tuple[Path, str, bool]:
    """The home repository, the campaign a quest's worktree belongs to, and whether the home was named
    explicitly ($CAMPAIGN_HOME, or a $CAMPAIGN/--campaign this repository does not hold, found in the
    machine's registry) rather than found from the current directory."""
    env = os.environ.get("CAMPAIGN_HOME")
    if env:
        return Path(env).resolve(), "", True
    name = os.environ.get("CAMPAIGN", "")
    if git_ok(cwd, "rev-parse", "--git-dir"):
        pointer = Path(git(cwd, "rev-parse", "--path-format=absolute", "--git-dir")) / "campaign-home"
        if pointer.exists():
            home, _, pinned = pointer.read_text().strip().partition("\n")
            return Path(home), pinned.strip(), False
        home = main_checkout(cwd)
        if not name or name in campaigns_in(home):
            return home, "", False
    other = campaigns().get(name) if name else None
    if other:
        return Path(other["home"]), name, True
    if name:
        die(f"no campaign {name} here or on this machine (`campaign status --all` lists them)")
    die("not in a git repository: pass -C DIR, --campaign NAME, or set CAMPAIGN_HOME")


def campaigns_in(home: Path) -> dict[str, str]:
    """The campaigns a home holds: name -> their directory, relative to the home."""
    return {f.parent.name: f"{META}/{f.parent.name}" for f in sorted((home / META).glob("*/state.json"))}


def select(home: Path, name: str) -> tuple[str, str]:
    """(name, directory) of the campaign NAME, or of the only one the home holds."""
    found = campaigns_in(home)
    if not name and len(found) == 1:
        name = next(iter(found))
    if not name:
        if found:
            die(f"{home} holds campaigns {', '.join(found)}: pass --campaign NAME or set CAMPAIGN")
        die("no campaign here: run `campaign init` in the home repository's main checkout")
    if name not in found:
        die(f"no campaign {name} in {home}" + (f" (it holds {', '.join(found)})" if found else ""))
    return name, found[name]


def find_campaign(cwd: Path) -> Campaign:
    home, pinned, _ = locate(cwd)
    return Campaign(home, os.environ.get("CAMPAIGN") or pinned)


QUEST_FIELDS: dict[str, type] = {
    "title": str,
    "status": str,
    "risk": str,
    "repo": str,
    "needs": list,
    "after": list,
    "provides": list,
    "consumes": list,
    "files": list,
    "checks": list,
    "lenses": list,
    "branch": str,
    "worktree": str,
    "journal": str,  # a quest in another repository: its journal's worktree in the home
    "base": str,
    "tasks": dict,
    "accepted": dict,
    "landed": list,
    "messages": list,
    "history": list,  # changes to risk, lenses and checks
    "reason": str,
    "milestone": str,
    "updated": str,
}


def validate(data: Json) -> list[str]:
    errors: list[str] = []
    if data.get("version") != 2:
        errors.append("version must be 2")
    if "landing" in data and not isinstance(data["landing"], dict):
        errors.append("landing must be an object")
    if not isinstance(data.get("ci_later", []), list):
        errors.append("ci_later must be a list of CI workflow names")
    if not isinstance(data.get("disk_warn", 0.0), (int, float)) or data.get("disk_warn", 0) < 0:
        errors.append("disk_warn must be GiB of free disk, a number")
    if not isinstance(data.get("suites", {}), dict):
        errors.append("suites must map a repository's name to its suite command")
    if not isinstance(data.get("tested_docs", []), list):
        errors.append("tested_docs must be a list of path globs")
    quests = data.get("quests")
    if not isinstance(quests, dict):
        return [*errors, "quests must be an object"]
    for pid, p in quests.items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", pid) or "--" in pid:
            errors.append(f"{pid}: ids are lowercase letters, digits and single dashes")
        if not isinstance(p, dict):
            errors.append(f"{pid}: must be an object")
            continue
        for key in ("title", "status", "risk", "repo"):
            if key not in p:
                errors.append(f"{pid}: missing {key}")
        for key, value in p.items():
            if key not in QUEST_FIELDS:
                errors.append(f"{pid}: unknown field {key}")
            elif not isinstance(value, QUEST_FIELDS[key]):
                errors.append(f"{pid}: {key} must be a {QUEST_FIELDS[key].__name__}")
        if p.get("status") not in STATUSES:
            errors.append(f"{pid}: status must be one of {', '.join(STATUSES)}")
        if p.get("risk") not in RISKS:
            errors.append(f"{pid}: risk must be one of {', '.join(RISKS)}")
        for dep in [*p.get("needs", []), *p.get("after", [])]:
            if dep not in quests and not re.fullmatch(r"[\w.-]+:[a-z0-9]+(-[a-z0-9]+)*", dep):
                errors.append(f"{pid}: depends on unknown quest {dep} (another campaign's is NAME:ID)")
        for lens in p.get("lenses", []):
            if lens not in LENSES:
                errors.append(f"{pid}: unknown lens {lens} (one of {', '.join(LENSES)})")
        for check in p.get("checks", []):
            ok = isinstance(check, dict) and isinstance(check.get("name"), str)
            ok = ok and isinstance(check.get("cmd"), str) and check.get("level") in LEVELS
            ok = ok and isinstance(check.get("minutes"), int) and check["minutes"] > 0
            ok = ok and isinstance(check.get("later", False), bool)
            if not ok:
                errors.append(f"{pid}: a check needs name, cmd, level and minutes: {check}")
        if p.get("status") in ("blocked", "dropped") and not p.get("reason"):
            errors.append(f"{pid}: {p.get('status')} needs a reason")
        if p.get("status") == "landed" and not any(x.get("final") for x in p.get("landed", [])):
            errors.append(f"{pid}: landed without a final landing record")
    errors.extend(cycles(quests))
    return errors


def registry() -> Path:
    """The machine's campaigns: one file per name holding its home, beside the job queue."""
    return jobs_dir().parent / "campaigns"


def campaigns() -> dict[str, Json]:
    """Every registered campaign whose home still holds it: name -> {home, state}."""
    out: dict[str, Json] = {}
    for f in sorted(registry().glob("*")) if registry().is_dir() else []:
        home = Path(f.read_text().strip())
        meta = campaigns_in(home).get(f.name)
        try:
            state = read_json(home / meta / "state.json") if meta else None
        except CampaignError:
            continue
        if state and state.get("campaign") == f.name:
            out[f.name] = {"home": home, "state": state}
    return out


def foreign(dep: str) -> Json | None:
    """The quest another campaign's `name:id` dependency names, or None."""
    name, _, qid = dep.partition(":")
    other = campaigns().get(name)
    quest = other["state"].get("quests", {}).get(qid) if other else None
    return quest if isinstance(quest, dict) else None


def clashes(c: Campaign, pid: str) -> list[str]:
    """The active or review quests whose files may overlap this one's in its repository: one writer
    per file set, so `start` refuses them."""
    p, repo = c.quests[pid], c.repo(c.quests[pid])
    return [
        x
        for x, q in c.quests.items()
        if x != pid and q["status"] in ("active", "review") and c.repo(q) == repo and overlap(p["files"], q["files"])
    ]


def cross_overlaps(c: Campaign, pid: str) -> list[str]:
    """Quests of other campaigns whose files may overlap this one's in the same repository."""
    p = c.quests[pid]
    found: list[str] = []
    for name, o in campaigns().items():
        if name == c.name:
            continue
        for qid, q in o["state"].get("quests", {}).items():
            if (
                q.get("status") in ("active", "review")
                and (o["home"] / q.get("repo", ".")).resolve() == c.repo(p)
                and overlap(p["files"], q.get("files", []))
            ):
                found.append(f"{name}:{qid}")
    return found


def changes(home: Path, q: Json) -> set[str]:
    """What a quest changes for other campaigns: its contracts, and for a quest in another repository
    that repository, by its directory's name."""
    repo = (home / q.get("repo", ".")).resolve()
    return set(q.get("provides", [])) | ({repo.name} if repo != home.resolve() else set())


def consumed(consumes: list[str], changing: set[str]) -> list[str]:
    """The contracts in CONSUMES that CHANGING touches: the same name, or a repository's name for one
    of its contracts named `repository:part`."""
    return sorted(x for x in consumes if x in changing or x.partition(":")[0] in changing)


def cycles(quests: dict[str, Json]) -> list[str]:
    seen: dict[str, int] = {}
    found: list[str] = []

    def visit(pid: str, path: list[str]) -> None:
        if seen.get(pid) == 2:
            return
        if seen.get(pid) == 1:
            found.append("dependency cycle: " + " -> ".join([*path[path.index(pid) :], pid]))
            return
        seen[pid] = 1
        p = quests.get(pid)
        if isinstance(p, dict):
            for dep in [*p.get("needs", []), *p.get("after", [])]:
                if dep in quests:
                    visit(dep, [*path, pid])
        seen[pid] = 2

    for pid in quests:
        visit(pid, [])
    return found


class Campaign:
    def __init__(self, home: Path, name: str = "") -> None:
        self.home = home
        self.name, self.meta = select(home, name)
        self.path = home / self.meta / "state.json"
        data = read_json(self.path)
        if data is None:
            die(f"{self.path} does not exist")
        errors = validate(data)
        if errors:
            die("state.json is invalid:\n  " + "\n  ".join(errors))
        self.state: Json = data

    @property
    def ident(self) -> str:
        """How jobs and wait records name this campaign: its directory."""
        return str(self.home / self.meta)

    @property
    def quests(self) -> dict[str, Json]:
        return self.state["quests"]

    def quest(self, pid: str) -> Json:
        if pid not in self.quests:
            die(f"no quest {pid}")
        return self.quests[pid]

    def quest_file(self, pid: str) -> str:
        path = self.home / self.meta / "quests" / f"{pid}.md"
        return path.read_text() if path.exists() else ""

    def repo(self, p: Json) -> Path:
        return (self.home / p["repo"]).resolve()

    def landed(self, pid: str) -> bool:
        if ":" in pid:
            other = foreign(pid)
            return other is not None and other.get("status") == "landed"
        return self.quests[pid]["status"] == "landed"

    def save(self, message: str, extra: tuple[str, ...] = ()) -> None:
        errors = validate(self.state)
        if errors:
            die("refusing to write invalid state:\n  " + "\n  ".join(errors))
        write_json(self.path, self.state)
        register(self.state["campaign"], self.home)
        sidebar(self)
        if os.environ.get("CAMPAIGN_NO_COMMIT") == "1":
            return
        paths = [f"{self.meta}/state.json", *extra]
        git(self.home, "add", "--", *paths)
        if git_ok(self.home, "diff", "--cached", "--quiet", "--", *paths):
            return
        git(self.home, "commit", "-q", "-m", message, "--", *paths)


def register(name: str, home: Path) -> None:
    entry = registry() / name
    if not entry.exists() or entry.read_text().strip() != str(home):
        registry().mkdir(parents=True, exist_ok=True)
        entry.write_text(f"{home}\n")


def leads() -> Path:
    """Where each campaign's lead was last seen in cmux: one JSON file per campaign name."""
    return jobs_dir().parent / "leads"


def note_lead() -> None:
    """Records the cmux surface of whoever runs `campaign` in a campaign's checkouts, so a message
    can wake that lead (`wake_lead`). Jobs and workers run detached and are not leads."""
    surface = os.environ.get("CMUX_SURFACE_ID", "")
    if not surface:
        return
    try:
        c = find_campaign(Path.cwd())
    except (CampaignError, OSError):  # OSError: run from a removed directory
        return
    entry = leads() / c.name
    seen = {"surface": surface, "workspace": os.environ.get("CMUX_WORKSPACE_ID", "")}
    with contextlib.suppress(OSError, ValueError):
        if json.loads(entry.read_text()) == seen:
            return
    with contextlib.suppress(OSError):
        leads().mkdir(parents=True, exist_ok=True)
        entry.write_text(json.dumps(seen) + "\n")


def wake_lead(c: Campaign, whom: str, nid: str, text: str, by: str) -> str:
    """Wakes the campaign's lead in cmux with the message (an idle session reads it at once; a busy
    one at its next step); "" when the lead is unknown, is the sender, or cmux is unreachable."""
    try:
        seen = json.loads((leads() / c.name).read_text())
    except (OSError, ValueError):
        return ""
    surface = str(seen.get("surface", ""))
    if not surface or surface == os.environ.get("CMUX_SURFACE_ID") or not shutil.which("cmux"):
        return ""
    body = f"campaign {c.name}: {nid} for {whom}, from {by}: {text} (close or answer it by that id)"
    try:
        done = subprocess.run(
            ["cmux", "agent", "message", surface, "--from", by or "campaign", "--", body],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return surface if done.returncode == 0 else ""


def notify(title: str, body: str) -> None:
    """A notice in the owner's cmux feed; silent outside cmux."""
    if os.environ.get("CMUX_WORKSPACE_ID"):
        report.notify(title, body)


INBOX_STALE = 15 * 60  # seconds a message to a lead waits undelivered before the owner is told


def stuck_leads() -> list[Json]:
    """The recorded leads (but the caller) whose cmux inbox holds messages undelivered for longer than
    INBOX_STALE, as a session blocked on a prompt holds them: campaign, surface, message ids and the
    oldest's age. [] when cmux is absent or does not answer."""
    exe = shutil.which("cmux")
    folder = leads()
    if not exe or not folder.is_dir():
        return []
    me = os.environ.get("CMUX_SURFACE_ID", "")
    out: list[Json] = []
    for entry in sorted(folder.iterdir()):
        try:
            surface = str(json.loads(entry.read_text()).get("surface", ""))
            if not surface or surface == me:
                continue
            args = [exe, "agent", "inbox", "--surface", surface, "--state", "queued", "--json"]
            done = subprocess.run(args, capture_output=True, text=True, timeout=10)
            data: Any = json.loads(done.stdout) if done.returncode == 0 else {}
        except (OSError, ValueError, AttributeError, subprocess.TimeoutExpired):
            continue
        messages: list[Any] = (
            data.get("messages", []) if isinstance(data, dict) else data if isinstance(data, list) else []
        )
        now = time.time()
        stale = {
            str(m.get("id")): now - float(m["created_at"])
            for m in messages
            if isinstance(m, dict)
            and isinstance(m.get("created_at"), (int, float))
            and now - float(m["created_at"]) > INBOX_STALE
        }
        if stale:
            out.append(
                {"campaign": entry.name, "surface": surface, "ids": sorted(stale), "oldest": max(stale.values())}
            )
    return out


def stuck_warnings() -> list[tuple[str, bool]]:
    """A warning per lead holding stale messages, and whether it names one not reported before: the
    owner is notified once per message (ids kept in leads-told.json beside the leads' records)."""
    record = jobs_dir().parent / "leads-told.json"
    try:
        told: list[str] = [str(x) for x in json.loads(record.read_text())]
    except (OSError, ValueError, TypeError):
        told = []
    out: list[tuple[str, bool]] = []
    for lead in stuck_leads():
        text = (
            f"{plural(len(lead['ids']), 'message')} to {lead['campaign']}'s lead (cmux surface {lead['surface']}) "
            f"undelivered for up to {span(lead['oldest'])}: its session may be waiting on a prompt"
        )
        fresh = [x for x in lead["ids"] if x not in told]
        if fresh:
            notify(f"campaign: {lead['campaign']}'s lead is not reading messages", text)
            told += fresh
        out.append((text, bool(fresh)))
    if any(new for _, new in out):
        with contextlib.suppress(OSError):
            atomic_write(record, json.dumps(told[-500:]) + "\n")
    return out


_inbox_read = [0.0]


def watch_leads() -> None:
    """stuck_warnings at most once a minute of a wait; new ones go to stderr."""
    if time.time() - _inbox_read[0] < 60:
        return
    _inbox_read[0] = time.time()
    for text, new in stuck_warnings():
        if new:
            print(f"campaign: {text}", file=sys.stderr)


def in_flight(counts: dict[str, int]) -> str:
    """ "2 in flight (1 active, 1 blocked)": the quests started and not landed, by stage."""
    stages = [f"{counts[s]} {'in review' if s == 'review' else s}" for s in FLIGHT if counts[s]]
    return f"{sum(counts[s] for s in FLIGHT)} in flight" + (f" ({', '.join(stages)})" if stages else "")


def overview(c: Campaign) -> Json:
    """Counts by status, the launchable quests, the ready ones a file set holds, the hunt list's open
    rows, and the current milestone, among the map's in its order: the first with a quest in review
    or active, else with one ready to start, else not landed."""
    live = {k: p for k, p in c.quests.items() if p["status"] != "dropped"}
    counts = {s: sum(1 for p in live.values() if p["status"] == s) for s in STATUSES}
    ready = [k for k, p in live.items() if p["status"] == "planned" and all(c.landed(d) for d in p["needs"])]
    held = {k: x for k in ready if (x := clashes(c, k))}
    groups = [(name, ids) for name, _, ids in milestone_groups(c, {k: p["status"] for k, p in live.items()})]
    placed = {k for _, ids in groups for k in ids}
    groups.append(("", [k for k in live if k not in placed]))
    open_ = [(name, ids) for name, ids in groups if any(live[k]["status"] != "landed" for k in ids)]
    place = {k: (n, i) for n, (_, ids) in enumerate(groups) for i, k in enumerate(ids)}
    launchable = sorted(
        (k for k in ready if k not in held), key=lambda k: (place[k][0], -len(waiting_on(live, k)), place[k][1])
    )

    def first(test: Callable[[str], bool]) -> tuple[str, list[str]] | None:
        return next(((name, ids) for name, ids in open_ if any(test(k) for k in ids)), None)

    current = first(lambda k: live[k]["status"] in ("review", "active")) or first(lambda k: k in ready)
    current = current or next(iter(open_), None)
    view: Json = {"counts": counts, "launchable": launchable, "held": held, "hunt": hunt_open(c), "milestone": None}
    if current is not None:
        name, members = current
        view["milestone"] = {
            "name": name,
            "landed": sum(1 for k in members if live[k]["status"] == "landed"),
            "quests": len(members),
            "launchable": [k for k in launchable if k in members],
        }
    return view


def waiting_on(live: dict[str, Json], pid: str) -> set[str]:
    """The quests not landed that need PID, or land after it, directly or through others: its
    critical-path weight."""
    found: set[str] = set()
    todo = [pid]
    while todo:
        k = todo.pop()
        for x, q in live.items():
            if x not in found and q["status"] != "landed" and k in (*q["needs"], *q.get("after", [])):
                found.add(x)
                todo.append(x)
    return found


def hunt_open(c: Campaign) -> int | None:
    """The open rows of the hunt list (`hunt.md`, Status `open`), or None without one."""
    path = c.home / c.meta / "hunt.md"
    if not path.exists():
        return None
    return sum(1 for line in path.read_text().splitlines() if table_cells(line)[-1].lower() == "open")


def sidebar(c: Campaign) -> None:
    """A bar for the current milestone, a pill for the project and one per quest in flight.
    Only changes are sent; the last view per workspace is cached in the git directory."""
    workspace = os.environ.get("CMUX_WORKSPACE_ID", "")
    if not workspace:
        return
    o = overview(c)
    counts, m = o["counts"], o["milestone"]
    name = c.state["campaign"]
    want: dict[str, str] = {}
    project = f"status:campaign-{c.name}"
    want[project] = f"{name}: {counts['landed']} landed · {in_flight(counts)} · {counts['planned']} planned"
    jobs = all_jobs() if any(p["status"] in FLIGHT and p.get("branch") for p in c.quests.values()) else []
    with frozen_heads():  # one branch listing per repository, not two git calls per branch
        failing_by = {pid: unexplained_failures(c, pid, jobs) for pid, p in c.quests.items() if p["status"] in FLIGHT}
    for pid, p in c.quests.items():
        if p["status"] in ("active", "review"):  # blocked quests count in the campaign's pill only
            failing = failing_by[pid]
            want[f"status:campaign-{c.name}-{pid}"] = f"{pid}: {p['status']}" + (
                f" · 🔥 {plural(len(failing), 'failed job')}" if failing else ""
            )
    if m is not None:
        label = f"{name}{' ' + m['name'] if m['name'] else ''}: {m['landed']} of {m['quests']} quests landed" + (
            f" · launchable: {', '.join(m['launchable'][:3])}" if m["launchable"] else ""
        )
        want["bar"] = f"{m['landed']}/{m['quests']}\t{label}"
    cache = Path(git(c.home, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "campaign-sidebar.json"
    view = f"{workspace} {c.ident}"
    seen: dict[str, str] = {}
    with contextlib.suppress(FileNotFoundError, json.JSONDecodeError, AttributeError):
        seen = json.loads(cache.read_text()).get(view, {})
    for key in seen.keys() - want.keys():
        if key == "bar":
            report.clear()
        else:
            report.unstatus(key.split(":", 1)[1])
    for key, value in want.items():
        if seen.get(key) == value:
            continue
        if key == "bar":
            done, label = value.split("\t", 1)
            report.progress(report.fraction(done), label)
        else:
            report.status(key.split(":", 1)[1], value, "map" if key == project else "hammer")
    with contextlib.suppress(OSError, json.JSONDecodeError):
        views = json.loads(cache.read_text()) if cache.exists() else {}
        views.pop(workspace, None)
        views[view] = want
        cache.write_text(json.dumps(views))


@contextlib.contextmanager
def writing() -> Generator[Campaign, None, None]:
    """The one writer: the home repository's main checkout, on its main branch, under a lock."""
    cwd = Path.cwd()
    home, pinned, named = locate(cwd)
    if not named and (not git_ok(cwd, "rev-parse", "--git-dir") or toplevel(cwd) != home):
        die(f"state is written from the campaign's main checkout ({home}); run this there, or with -C {home}")
    branch = git(home, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if branch != main_branch(home):
        die(f"{home} is on {branch or 'a detached HEAD'}, not {main_branch(home)}")
    lock = Path(git(home, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "campaign.lock"
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        c = Campaign(home, os.environ.get("CAMPAIGN") or pinned)
        recover_landing(c)
        yield c


def landing_record(repo: Path, main: str, intent: Json) -> Json | None:
    """The landing an intent describes, when main contains its target."""
    target = intent["target"]
    if not ancestor(repo, target, main):
        return None
    commit = target
    if intent.get("mode") == "merge":
        for line in git(repo, "log", "--merges", "--format=%H %P", f"{target}..{main}").splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[2] == target:
                commit = parts[0]
    return {
        "commit": commit,
        "tested": target,
        "at": stamp(),
        "final": intent["final"],
        "reason": intent.get("reason", ""),
        "evidence": intent.get("evidence", []),
        **({"criteria": intent["criteria"]} if intent.get("criteria") else {}),
        **({"from": intent["from"]} if intent.get("from") else {}),
        **({"ci_skipped": intent["ci_skipped"]} if intent.get("ci_skipped") else {}),
    }


def recover_landing(c: Campaign) -> None:
    """Completes or clears a landing whose command died between moving main and recording it."""
    intent = c.state.pop("landing", None)
    if not intent:
        return
    p = c.quests.get(intent.get("id", ""))
    record = landing_record(c.repo(p), main_branch(c.repo(p)), intent) if p else None
    if p is not None and record is not None:
        p["landed"].append(record)
        if record["final"]:
            p["status"] = "landed"
        p["updated"] = stamp()
        c.save(f"campaign: complete the interrupted landing of {intent['id']} at {record['commit'][:12]}")
        print(f"campaign: completed the interrupted landing of {intent['id']}", file=sys.stderr)
    else:
        c.save(f"campaign: clear the interrupted landing of {intent.get('id')} (main never moved)")
        print(f"campaign: cleared an interrupted landing of {intent.get('id')}; main never moved", file=sys.stderr)


def parse_check(spec: str, later: bool = False) -> Json:
    head, sep, cmd = spec.partition("=")
    parts = head.split(":")
    if not sep or not cmd.strip() or not parts[0] or len(parts) > 3:
        die(f"--check NAME[:LEVEL[:MINUTES]]=CMD, not {spec!r}")
    level = parts[1] if len(parts) > 1 and parts[1] else "light"
    minutes = parts[2] if len(parts) > 2 else str(SHORT_RUN if level in HEAVY else 30)
    if level not in LEVELS or not minutes.isdigit() or int(minutes) < 1:
        die(f"--check {spec!r}: level is one of {', '.join(LEVELS)}, minutes a positive integer")
    if level in HEAVY and int(minutes) > SHORT_RUN:
        die(f"--check {spec!r}: a {level} check lasts at most {SHORT_RUN} minutes; split it")
    check: Json = {"name": parts[0], "level": level, "minutes": int(minutes), "cmd": cmd.strip()}
    if later:
        check["later"] = True
    return check


MILESTONE_RUN = re.compile(r"asan|ubsan|tsan|crash|fuzz|mutant", re.I)


def placed(spec: str, risk: str) -> Json:
    """A --check: sanitizer, crash, fuzz and mutant runs are milestone checks, save on CI and, at high
    risk, crash and TSan runs, which gate a format, durability or concurrency claim."""
    runs = {x.lower() for x in MILESTONE_RUN.findall(spec)}
    gate = spec.partition("=")[2].strip().startswith(CI_CHECK) or (
        risk in ("high", "critical") and runs <= {"crash", "tsan"}
    )
    return parse_check(spec, later=bool(runs) and not gate)


def gates(p: Json) -> list[Json]:
    return [x for x in p["checks"] if not x.get("later")]


def declare_inputs(p: Json, pid: str, items: list[str]) -> None:
    """Applies `--inputs NAME=GLOBS` to the quest's checks: a job of that check is identified by the
    files under GLOBS (and the build files and paths its command names), not by the whole tree. An
    empty GLOBS clears them. At least one gate check keeps whole-tree identity: the build-test."""
    for item in items:
        name, sep, globs = item.partition("=")
        check = next((x for x in p["checks"] if x["name"] == name), None)
        if not sep or not name:
            die(f"--inputs NAME=GLOBS, not {item!r}")
        if check is None:
            die(f"--inputs {name}: {pid} has no check {name}")
        if check["cmd"].startswith(CI_CHECK):
            die(f"--inputs {name}: a CI check runs on GitHub at the exact commit; inputs scope jobs")
        check.pop("inputs", None)
        if csv(globs):
            check["inputs"] = csv(globs)
    if any(x.get("inputs") for x in p["checks"]) and all(x.get("inputs") for x in gates(p)):
        die(
            f"{pid}: every gate check declares --inputs; keep one without them, the build-test `land` runs at the exact code"
        )


def deferred(p: Json) -> list[Json]:
    return [x for x in p["checks"] if x.get("later")]


def lens_floor(risk: str, lenses: list[str]) -> list[str]:
    """The reviews RISK requires that LENSES lack: high and critical a code review, critical a design."""
    need = (
        []
        if risk in ("low", "normal") or set(lenses) & set(CODE_LENSES)
        else ["a code review (correctness, security or durability)"]
    )
    return need + (["a design review"] if risk == "critical" and "design" not in lenses else [])


def weakening(before: Json, after: Json) -> list[str]:
    """What a change to a quest's risk, lenses or checks removes from what it must pass."""
    out: list[str] = []
    if RISKS.index(after["risk"]) < RISKS.index(before["risk"]):
        out.append(f"risk {before['risk']} -> {after['risk']}")
    out += [f"lens {x} removed" for x in before["lenses"] if x not in after["lenses"]]
    new = {x["name"]: x for x in after["checks"]}
    for old in before["checks"]:
        kind = "deferred check" if old.get("later") else "gate check"
        now = new.get(old["name"])
        if now is None:
            out.append(f"{kind} {old['name']} removed")
        elif now.get("later") and not old.get("later"):
            out.append(f"gate check {old['name']} deferred")
        elif (now["cmd"], now["level"]) != (old["cmd"], old["level"]):
            out.append(f"{kind} {old['name']} changed")
        elif now.get("inputs") and not set(old.get("inputs") or ["**"]) <= set(now["inputs"]):
            out.append(f"{kind} {old['name']} inputs narrowed to {','.join(now['inputs'])}")
    return out


def stem(glob: str) -> str:
    cuts = [i for i in (glob.find(ch) for ch in "*?[") if i >= 0]
    return glob[: min(cuts)] if cuts else glob


def overlap(a: list[str], b: list[str]) -> bool:
    """Whether two file sets may share a file (no globs means the whole repository): exact when one
    side names a file, by the globs' fixed prefixes when both are patterns."""

    def pair(x: str, y: str) -> bool:
        if stem(x) == x or stem(y) == y:  # a file: does the other side match it
            return fnmatch.fnmatch(x, y) or fnmatch.fnmatch(y, x)
        return stem(x).startswith(stem(y)) or stem(y).startswith(stem(x))

    if not a or not b:
        return True
    return any(pair(x, y) for x in a for y in b)


def outside(paths: list[str], globs: list[str]) -> list[str]:
    return [x for x in paths if not is_meta(x) and globs and not any(fnmatch.fnmatch(x, g) for g in globs)]


CRITERION = re.compile(r"^\s*[-*]\s*\**A\d+\b.*(?:\n[ \t]+\S.*)*", re.M)  # `- A1 ...` and its wrapped lines


def criteria_hash(c: Campaign, pid: str) -> str:
    """The quest file's criterion lines, hashed: editing them, not its prose, voids acceptances."""
    brief = c.home / c.meta / "quests" / f"{pid}.md"
    lines = CRITERION.findall(brief.read_text() if brief.exists() else "")
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16]


def worktree_path(repo: Path, campaign: str, name: str) -> Path:
    return repo.parent / f"{repo.name}-wt" / campaign / name


def branches(c: Campaign, pid: str) -> list[tuple[str, str, str, str]]:
    """(name, branch, worktree, base) for the quest and its tasks."""
    p = c.quests[pid]
    out: list[tuple[str, str, str, str]] = []
    if p.get("branch"):
        out.append((pid, p["branch"], p.get("worktree", ""), p.get("base", "")))
    for task, t in sorted(p.get("tasks", {}).items()):
        out.append((f"{pid}--{task}", t["branch"], t.get("worktree", ""), t.get("base", "")))
    return out


def mailbox(c: Campaign, pid: str) -> Path:
    """Where messages for a quest ("" for the campaign's lead) wait, in the home's .git: any
    agent on the machine can leave one there without being the campaign's one writer."""
    common = common_dir(c.home)
    if not common:
        die(f"{c.home} is not a git checkout")
    return Path(common) / "campaign-mail" / c.state["campaign"] / (pid or "@")


def leave_message(c: Campaign, pid: str, text: str, by: str, origin: str = "") -> str:
    """Leaves a message for a quest ("" for the lead) under a fresh id, and notifies."""
    folder = mailbox(c, pid)
    folder.mkdir(parents=True, exist_ok=True)
    record = c.quests[pid] if pid else c.state
    taken = [m["id"] for m in [*inbox(c, pid), *record.get("messages", [])]]
    n = 1 + max([int(str(x).partition("-")[2] or 0) for x in taken] or [0])
    tmp = folder / f".message.{os.getpid()}.tmp"
    while True:  # a fresh id even when two agents write at once
        message = {"id": f"msg-{n}", "at": stamp(), "by": by, "text": text, **({"origin": origin} if origin else {})}
        tmp.write_text(json.dumps(message, indent=2) + "\n")
        try:
            os.link(tmp, folder / f"msg-{n}.json")
            break
        except FileExistsError:
            n += 1
        finally:
            tmp.unlink(missing_ok=True)
    notify(f"Message for {pid or c.name}", f"{by}: {text}"[:200])
    wake_lead(c, pid or "the lead", f"msg-{n}", text, by)
    return f"msg-{n}"


def tell_peers(c: Campaign, pid: str) -> list[str]:
    """Messages for the leads of the other campaigns on this machine whose work a quest just
    started overlaps: the same files in the same repository, or a contract one changes and the other
    consumes. Once per finding."""
    p = c.quests[pid]
    told: list[str] = []
    found: list[tuple[str, str]] = [
        (
            other.split(":")[0],
            f"{c.name}:{pid} started and may change files your {other.split(':')[1]} changes; "
            f"order them (--after {c.name}:{pid}) or narrow the files",
        )
        for other in cross_overlaps(c, pid)
    ]
    for name, o in campaigns().items():
        if name == c.name:
            continue
        for qid, q in o["state"].get("quests", {}).items():
            if q.get("status") in ("planned", "active", "review"):
                found += [
                    (name, f"{c.name}:{pid} started and changes contract {x}, which your {qid} consumes")
                    for x in consumed(q.get("consumes", []), changes(c.home, p))
                ]
            if q.get("status") in ("active", "review"):
                found += [
                    (name, f"{c.name}:{pid} started and builds against contract {x}, which your {qid} is changing")
                    for x in consumed(p["consumes"], changes(Path(o["home"]), q))
                ]
    for name, text in found:
        other = campaigns().get(name)
        if other is None:
            continue
        peer = Campaign(Path(other["home"]), name)
        if any(m["text"] == text for m in inbox(peer, "")):
            continue
        leave_message(peer, "", text, f"campaign {c.name}")
        told.append(name)
    return told


def inbox(c: Campaign, pid: str) -> list[Json]:
    """The open messages for a quest ("" for the lead), oldest first."""
    found: list[Json] = []
    folder = mailbox(c, pid)
    for f in folder.glob("*-*.json") if folder.is_dir() else []:
        with contextlib.suppress(CampaignError):
            message = read_json(f)
            if message:
                found.append(message)
    return sorted(found, key=lambda n: int(str(n["id"]).partition("-")[2] or 0))


def answered(message: Json, journals: Collection[str]) -> bool:
    """Whether a journal names the message by its id."""
    return any(re.search(rf"\b{re.escape(str(message['id']))}\b", j) for j in journals)


# ---------------------------------------------------------------- quest commands


def cmd_init(a: argparse.Namespace) -> int:
    top = toplevel(Path.cwd())
    if top != main_checkout(top):
        die("run init in the repository's main checkout, not in a worktree")
    name = a.name or top.name
    if not NAME_RE.fullmatch(name):
        die(f"campaign name {name!r}: use letters, digits, '.', '_' and '-' (--name)")
    if name in campaigns_in(top):
        die(f"{top} already holds campaign {name}")
    taken = campaigns().get(name)
    if taken and taken["home"] != top:
        die(f"campaign {name} already runs from {taken['home']} on this machine: pick another --name")
    folder = f"{META}/{name}"
    write_json(top / folder / "state.json", {"version": 2, "campaign": name, "ci": a.ci, "quests": {}})
    register(name, top)
    for doc in ("PLAN.md", "hunt.md"):
        if not (top / folder / doc).exists():
            atomic_write(top / folder / doc, template(doc, campaign=name))
    if os.environ.get("CAMPAIGN_NO_COMMIT") != "1":
        git(top, "add", "--", folder)
        git(top, "commit", "-q", "-m", f"campaign: init {name}", "--", folder)
    print(f"campaign {name} in {top}; write the goals in {folder}/PLAN.md")
    return 0


CONFIG = ("ci", "ci_later", "suite", "peers", "outbox", "disk_warn", "tested_docs")


def cmd_config(a: argparse.Namespace) -> int:
    """Prints the campaign-wide settings as key=value lines, KEYs' values, or sets KEY=VALUEs."""
    keys = [x for x in a.assignments if "=" not in x]
    if keys and len(keys) < len(a.assignments):
        die("give either KEYs to read or KEY=VALUE assignments")
    unknown = [k for k in keys if k not in CONFIG and not k.startswith("suite.")]
    if unknown:
        die(f"no setting {', '.join(unknown)}; the settings are {', '.join(CONFIG)}")
    if not a.assignments or keys:
        c = find_campaign(Path.cwd())
    else:
        with writing() as c:
            for item in a.assignments:
                key, sep, value = item.partition("=")
                if key == "ci" and sep and value in ("github", ""):
                    c.state["ci"] = value
                elif key == "ci_later" and sep:
                    c.state["ci_later"] = csv(value)
                elif key == "peers" and sep and all(re.fullmatch(r"[\w.-]+=\S+", x) for x in csv(value)):
                    c.state["peers"] = csv(value)
                elif key in ("outbox", "suite") and sep:
                    c.state[key] = value
                elif key.startswith("suite.") and NAME_RE.fullmatch(key[6:]) and sep:
                    c.state.setdefault("suites", {})[key[6:]] = value
                    c.state["suites"] = {k: v for k, v in c.state["suites"].items() if v}
                elif key == "tested_docs" and sep:
                    c.state["tested_docs"] = csv(value)
                elif key == "disk_warn" and sep and re.fullmatch(r"\d+(\.\d+)?|", value):
                    c.state.pop("disk_warn", None)
                    if value:
                        c.state["disk_warn"] = float(value)
                else:
                    die(
                        "campaign config takes ci=github|'', ci_later=WORKFLOW,..., suite[.REPO]=CMD, peers=NAME=URL,..., "
                        f"outbox=REMOTE, disk_warn=GIB|'' and tested_docs=GLOB,..., not {item!r}"
                    )
            c.save("campaign: config " + " ".join(a.assignments))
    suites = {f"suite.{k}": v for k, v in c.state.get("suites", {}).items()}
    for key in keys or [*CONFIG, *suites]:
        value = suites.get(key) or c.state.get(key, DISK_WARN_GB if key == "disk_warn" else "") or ""
        text = ",".join(value) if isinstance(value, list) else f"{value:g}" if isinstance(value, float) else str(value)
        print(text if keys else f"{key}={text}")
    return 0


def cmd_new(a: argparse.Namespace) -> int:
    with writing() as c:
        if a.id in c.quests:
            die(f"{a.id} exists")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", a.id) or "--" in a.id:
            die(f"quest id {a.id!r}: use lowercase letters, digits and single dashes")
        repo = (c.home / a.repo).resolve()
        if not git_ok(repo, "rev-parse", "--git-dir"):
            die(f"{repo} is not a git repository")
        lenses = csv(a.lenses) if a.lenses is not None else list(DEFAULT_LENSES[a.risk])
        need = lens_floor(a.risk, lenses)
        if need:
            die(f"{a.risk} risk needs {' and '.join(need)}: --lenses {','.join(lenses + DEFAULT_LENSES[a.risk])}")
        checks = [placed(x, a.risk) for x in a.check]
        moved = [x["name"] for x in checks if x.get("later")]
        checks += [parse_check(x, later=True) for x in a.later]
        if not gates({"checks": checks}):
            home = same_repo(repo, c.home)
            suite = c.state.get("suite") if home else c.state.get("suites", {}).get(repo.name)
            if not suite:
                key = "suite" if home else f"suite.{repo.name}"
                die(f"no gate: --check NAME=CMD, or `campaign config {key}=CMD` (the native build plus tests)")
            checks.insert(0, parse_check(f"suite={suite}"))
        c.quests[a.id] = {
            "title": a.title,
            "status": "planned",
            "risk": a.risk,
            "repo": a.repo,
            "needs": csv(a.needs),
            "after": csv(a.after),
            "provides": csv(a.provides),
            "consumes": csv(a.consumes),
            "files": csv(a.files),
            "checks": checks,
            "lenses": list(dict.fromkeys(lenses)),
            "milestone": a.milestone or "",
            "accepted": {},
            "landed": [],
            "updated": stamp(),
        }
        declare_inputs(c.quests[a.id], a.id, a.inputs)
        brief = f"{c.meta}/quests/{a.id}.md"
        if not (c.home / brief).exists():
            atomic_write(c.home / brief, template("quest.md", id=a.id, title=a.title, risk=a.risk))
        c.save(f"campaign: new {a.id} ({a.risk}): {a.title}", (brief,))
    print(f"{a.id}: planned; write its outcome and criteria in {brief}")
    for name in moved:
        print(f"{name}: a milestone check (a sanitizer, crash, fuzz or mutant run)")
    return 0


QUEST_KEYS = ("title", "status", "risk", "reason", "repo", "milestone", "needs", "after", "provides", "consumes")
QUEST_KEYS += ("files", "lenses", "check", "later", "inputs", "branch", "worktree", "journal", "base", "tasks")
QUEST_KEYS += ("landed",)
TASK_KEYS = ("files", "status", "reason", "branch", "worktree", "journal", "base")


def fields(record: Json, keys: tuple[str, ...]) -> list[tuple[str, str]]:
    """A quest's or task's fields as (key, value) pairs, values in the form `set` takes."""
    out: list[tuple[str, str]] = []
    for key in keys:
        if key in ("check", "later"):
            out += [
                (key, f"{c['name']}:{c['level']}:{c['minutes']}={c['cmd']}")
                for c in record.get("checks", [])
                if c.get("later", False) == (key == "later")
            ]
        elif key == "inputs":
            out += [(key, f"{c['name']}={','.join(c['inputs'])}") for c in record.get("checks", []) if c.get("inputs")]
        elif key == "tasks":
            out.append((key, ",".join(record.get("tasks", {}))))
        elif key == "landed":
            out.append((key, ",".join(x["commit"][:12] for x in record.get("landed", []))))
        else:
            value = record.get(key, "active" if key == "status" else "")
            out.append((key, ",".join(value) if isinstance(value, list) else str(value)))
    return out


def cmd_get(a: argparse.Namespace) -> int:
    c = find_campaign(Path.cwd())
    p = c.quest(a.id)
    record, known = p, QUEST_KEYS
    if a.task:
        record = p.get("tasks", {}).get(a.task)
        if record is None:
            die(f"{a.id} has no task {a.task}")
        known = TASK_KEYS
    unknown = [k for k in a.keys if k not in known]
    if unknown:
        die(f"no field {', '.join(unknown)}; the fields are {', '.join(known)}")
    for key, value in fields(record, tuple(a.keys) or known):
        print(value if a.keys else f"{key}={value}")
    return 0


def cmd_set(a: argparse.Namespace) -> int:
    with writing() as c:
        p = c.quest(a.id)
        if a.task:
            task = p.get("tasks", {}).get(a.task)
            if task is None:
                die(f"{a.id} has no task {a.task}")
            given = dict(x.partition("=")[::2] for x in a.assignments)
            if not given or set(given) - {"files", "status", "reason"} or a.check or a.later or a.inputs:
                die("set --task takes files=GLOBS, or status=dropped reason=R (status=active undoes it)")
            if "files" in given:
                files = csv(given["files"])
                if not files:
                    die("a task's file set cannot be empty")
                clash = [
                    t
                    for t, x in p["tasks"].items()
                    if t != a.task and x.get("worktree") and overlap(files, x.get("files", []))
                ]
                if clash:
                    die(f"task {a.task}'s files overlap tasks {', '.join(clash)}")
                task["files"] = files
            if given.get("status") == "dropped":
                if not given.get("reason"):
                    die("dropping a task needs reason=R: its unmerged work will not land")
                task.update(status="dropped", reason=given["reason"])
            elif given.get("status") == "active":
                task.pop("status", None)
                task.pop("reason", None)
            elif "status" in given:
                die("a task's status is dropped or active")
            p["updated"] = stamp()
            c.save(f"campaign: set {a.id} task {a.task} " + " ".join(a.assignments))
            print(f"{a.id} task {a.task}: " + ("dropped" if task.get("status") == "dropped" else "updated"))
            return 0
        before = {k: json.loads(json.dumps(p[k])) for k in ("risk", "lenses", "checks")}
        for item in a.assignments:
            key, sep, value = item.partition("=")
            if not sep:
                die(f"expected key=value, not {item!r}")
            if key == "status":
                if value not in SET_STATUS.get(p["status"], ()):
                    die(f"{a.id}: {p['status']} cannot become {value} by set (start and land do that)")
                p["status"] = value
            elif key in ("needs", "after", "provides", "consumes", "files", "lenses"):
                p[key] = csv(value)
            elif key == "risk" and value not in RISKS:
                die(f"risk is one of {', '.join(RISKS)}, not {value!r}")
            elif key in ("title", "risk", "reason", "repo", "milestone"):
                p[key] = value
            else:
                die(
                    f"no field {key} to set; the fields are title, status, risk, reason, repo, milestone, needs, "
                    f"after, provides, consumes, files and lenses (`campaign get {a.id}`), and --check, --later and --inputs"
                )
        if a.check:
            p["checks"] = [placed(x, p["risk"]) for x in a.check] + deferred(p)
        if a.later:
            p["checks"] = gates(p) + [parse_check(x, later=True) for x in a.later]
        if a.check or a.later or a.inputs:
            declare_inputs(p, a.id, a.inputs)
        if RISKS.index(p["risk"]) > RISKS.index(before["risk"]):  # a raised risk brings its reviews
            p["lenses"] = list(dict.fromkeys(p["lenses"] + DEFAULT_LENSES[p["risk"]]))
        need = lens_floor(p["risk"], p["lenses"])
        if need:
            die(f"{a.id} is {p['risk']} risk and needs {' and '.join(need)}; lower the risk instead (--weaken REASON)")
        weakened = weakening(before, p)
        if weakened and not a.weaken:
            die(
                f"{a.id}: this weakens what it must pass ({'; '.join(weakened)}): --weaken REASON records why "
                "and voids its review acceptances"
            )
        after = {k: p[k] for k in before}
        if after != before:
            voided: list[str] = sorted(p.get("accepted", {})) if weakened or p["risk"] != before["risk"] else []
            if voided:
                p["accepted"].clear()
            p.setdefault("history", []).append(
                {
                    "at": stamp(),
                    **{k: [before[k], after[k]] for k in before if before[k] != after[k]},
                    "weakened": weakened,
                    "reason": a.weaken or "",
                    "voided": voided,
                }
            )
        elif a.weaken:
            die("--weaken: nothing here weakens the quest")
        if p["status"] not in ("blocked", "dropped") and not any(x.startswith("reason=") for x in a.assignments):
            p.pop("reason", None)
        p["updated"] = stamp()
        c.save(
            f"campaign: set {a.id} "
            + " ".join(
                a.assignments
                + [f"check={x}" for x in a.check]
                + [f"later={x}" for x in a.later]
                + [f"inputs={x}" for x in a.inputs]
            )
            + (f" (weakened: {'; '.join(weakened)}; why: {a.weaken})" if weakened else "")
        )
        if any(x == "status=blocked" for x in a.assignments):
            notify(f"Blocked: {a.id}", p.get("reason", ""))
    print(f"{a.id}: {p['status']}")
    return 0


def cmd_start(a: argparse.Namespace) -> int:
    with writing() as c:
        p = c.quest(a.id)
        repo = c.repo(p)
        main = main_branch(repo)
        if a.task:
            if p["status"] not in ("active", "review"):
                die(f"{a.id} is {p['status']}: start the quest before its tasks")
            if a.task in p.get("tasks", {}):
                die(f"{a.id} already has task {a.task}")
            files = csv(a.files) or p["files"]
            clash = [
                t for t, x in p.get("tasks", {}).items() if x.get("worktree") and overlap(files, x.get("files", []))
            ]
            if clash and not a.force:
                die(f"task {a.task}'s files overlap tasks {', '.join(clash)}: give --files, or --force REASON")
            recorded = p.get("tasks", {}).get(a.task, {}).get("branch")
            name, base = f"{a.id}--{a.task}", rev(repo, p["branch"])
            branch = recorded or f"quest/{c.name}/{name}"
        else:
            if p["status"] not in ("planned", "blocked"):
                die(f"{a.id} is {p['status']}")
            unmet = [d for d in p["needs"] if not c.landed(d)]
            if unmet and not a.force:
                die(f"{a.id} needs {', '.join(unmet)} landed first (--force REASON to start anyway)")
            clash = clashes(c, a.id)
            if clash and not a.force:
                die(
                    f"{a.id}'s files may overlap {', '.join(clash)} (one writer per file set): narrow files, order them with needs, or --force REASON"
                )
            name, branch, base = a.id, p.get("branch") or f"quest/{c.name}/{a.id}", rev(repo, main)
            for other in cross_overlaps(c, a.id):
                print(
                    f"warning: {a.id}'s files may overlap {other}: order them (--after {other}) or narrow files",
                    file=sys.stderr,
                )
        wt = worktree_path(repo, c.name, name)
        apart = not same_repo(repo, c.home)  # a quest in another repository keeps its journal in the home
        for path in [wt, *([worktree_path(c.home, c.name, name)] if apart else [])]:
            if path.exists():
                die(f"{path} exists")
        if has_ref(repo, branch):
            git(repo, "worktree", "add", "-q", str(wt), branch)
            base = merge_base(repo, base, branch)
        else:
            git(repo, "worktree", "add", "-q", "-b", branch, str(wt), base)
        gitdir = Path(git(wt, "rev-parse", "--path-format=absolute", "--git-dir"))
        (gitdir / "campaign-home").write_text(f"{c.home}\n{c.name}\n")
        jwt = journal_worktree(c, name, branch) if apart else wt
        work = Path(c.meta) / "journal" / f"{name}.md"
        if not (jwt / work).exists():
            atomic_write(jwt / work, template("journal.md", id=name, brief=f"{c.meta}/quests/{a.id}.md"))
            git(jwt, "add", "--", str(work))
            git(jwt, "commit", "-q", "-m", f"campaign: start {name}", "--", str(work))
        rel = os.path.relpath(wt, c.home)
        journal = {"journal": os.path.relpath(jwt, c.home)} if apart else {}
        if a.task:
            p.setdefault("tasks", {})[a.task] = {
                "branch": branch,
                "worktree": rel,
                **journal,
                "base": base,
                "files": csv(a.files) or p["files"],
            }
        else:
            p.update(branch=branch, worktree=rel, base=base, status="active", **journal)
            if a.force:
                p["reason"] = f"started by force: {a.force}"
        p["updated"] = stamp()
        c.save(f"campaign: start {name} on {branch} at {base[:12]}")
    print(f"{name}: {branch} in {wt}" + (f"; its journal in {jwt}" if apart else ""))
    if not a.task:
        for peer in tell_peers(c, a.id):
            print(f"told campaign {peer}'s lead that {a.id} overlaps its work (a message)")
    return 0


def own_blobs(repo: Path, main_head: str, commit: str) -> dict[str, str]:
    """The quest's own code changes at COMMIT (path -> blob), against its merge base with main."""
    base = merge_base(repo, main_head, commit)
    return blobs(repo, commit, [x for x in changed(repo, base, commit) if not is_meta(x)])


def own_diffs(repo: Path, main_head: str, commit: str) -> dict[str, str]:
    """The quest's own changes at COMMIT (path -> hash of its changed lines against the merge base
    with main): unlike its blob, a path's hash survives main's lines merged into the same file."""
    base = merge_base(repo, main_head, commit)
    paths = [x for x in changed(repo, base, commit) if not is_meta(x)]
    text = git(repo, "diff", "--no-color", "--no-ext-diff", "-U0", base, commit, "--", *paths) if paths else ""
    out: dict[str, str] = {}
    for part in text.split("diff --git ")[1:]:
        lines = part.splitlines()
        path = next((x[6:] for x in lines if x.startswith("+++ b/")), "")
        path = path or next((x[6:] for x in lines if x.startswith("--- a/")), "")
        body = [x for x in lines[1:] if not x.startswith(("index ", "@@"))]  # no positions, no blob ids
        out[path] = hashlib.sha256("\n".join(body).encode()).hexdigest()[:16]
    return out


def cmd_accept(a: argparse.Namespace) -> int:
    with writing() as c:
        p = c.quest(a.id)
        if a.lens not in LENSES:
            die(f"unknown lens {a.lens}")
        if not p.get("branch"):
            die(f"{a.id} has no branch")
        repo = c.repo(p)
        commit = rev(repo, a.commit or p["branch"])
        main_head = rev(repo, main_branch(repo))
        path = review_path(c, a.id, a.lens)
        rel = path.relative_to(c.home)
        reviewed = ""
        findings: dict[str, Json] = {}
        fix_scope: Json = {}
        if not path.exists():
            die(f"no review at {rel}")
        data = path.read_bytes()  # parsed and identified from one read
        r = read_review(data.decode(errors="replace"))
        blocking = [f["id"] for f in r["findings"] if f["blocking"]]
        if r["verdict"] not in ("APPROVE", "CHANGES", "REJECT"):
            die(
                f"{rel}: the verdict is {r['verdict'] or 'missing'!r}, not one of APPROVE, CHANGES, REJECT; a "
                "conditional approval is CHANGES, its conditions blocking findings"
            )
        if r["verdict"] == "REJECT":
            die(f"{a.lens} review rejected {a.id}: re-plan the quest")
        if r["verdict"] == "APPROVE" and blocking:
            die(
                f"{rel}: APPROVE but lists blocking findings {', '.join(blocking)}: the reviewer corrects one or the other"
            )
        if r["verdict"] == "CHANGES" and not blocking:
            die(f"{rel}: CHANGES but lists no findings table")
        if r["verdict"] == "APPROVE" and a.confirm:
            die("an APPROVE needs no --confirm")
        seen: dict[str, str] = {}
        docs = c.state.get("tested_docs", [])
        if a.lens != "design":  # a design is reviewed before the code exists
            if not r["commit"] or not git_ok(repo, "cat-file", "-e", f"{r['commit']}^{{commit}}"):
                die(f"{rel}: no 'Commit:' line naming the reviewed commit in {repo}")
            reviewed = rev(repo, r["commit"])
            seen = own_blobs(repo, main_head, reviewed)
            since = drift(repo, main_head, commit, seen, diffs=own_diffs(repo, main_head, reviewed), tested=docs)
            if r["verdict"] == "APPROVE" and since:
                die(
                    f"the review approved {reviewed[:12]}; {commit[:12]} changes {', '.join(since[:8])} since: "
                    f"have the reviewer review them, or accept --commit {reviewed[:12]}"
                )
            defects = [f["id"] for f in r["findings"] if f["blocking"] and f["kind"] == "defect"]
            if r["verdict"] == "CHANGES" and not since and defects:
                die(
                    f"{', '.join(defects)}: blocking defects, and {commit[:12]} has the code the review saw: fix "
                    "them, or the reviewer withdraws them"
                )
            unseen = [x for x in since if x not in seen]
            if unseen and r["verdict"] == "CHANGES":
                if not a.fix_scope:
                    die(
                        f"the fix changes {', '.join(unseen[:8])}, which the {a.lens} review never saw: have the reviewer "
                        "review the fix (the review names the fixed commit and approves), or --fix-scope REASON when "
                        "it adds no mechanism (a regression test for a finding)"
                    )
                fix_scope = {"paths": unseen, "reason": a.fix_scope}
        if r["verdict"] == "CHANGES":
            findings = confirmations(repo, commit, reviewed, table=r["findings"], given=a.confirm)
        p.setdefault("accepted", {})[a.lens] = {
            "commit": commit,
            "base": merge_base(repo, main_head, commit),
            "criteria": criteria_hash(c, a.id),
            "by": "review",
            "verdict": r["verdict"],
            "note": a.note or "",
            "review": blob_id(c.home, data),
            "reviewed": reviewed,
            "findings": findings,
            **({"fix_scope": fix_scope} if fix_scope else {}),
            "blobs": own_blobs(repo, main_head, commit),
            "diffs": own_diffs(repo, main_head, commit),
            "at": stamp(),
        }
        if a.lens not in p["lenses"]:
            p["lenses"].append(a.lens)
        p["updated"] = stamp()
        # the review is committed with its acceptance, so git holds exactly what was accepted
        c.save(f"campaign: accept {a.id} {a.lens} at {commit[:12]}", (str(rel),))
    for fid, f in findings.items():
        if f["kind"] == "defect" and not f["reproduced"]:
            print(f"note: {fid} is a defect and no job shows its probe failing at the reviewed {reviewed[:12]}")
    print(f"{a.id}: {a.lens} accepted at {commit[:12]} ({len(p['accepted'][a.lens]['blobs'])} paths)")
    return 0


def confirmations(repo: Path, commit: str, reviewed: str, *, table: list[Json], given: list[str]) -> dict[str, Json]:
    """Each blocking finding of a CHANGES review with the jobs that confirm its fix: jobs of the quest's
    repository that passed at the accepted commit's code, named for that finding (`--confirm C1=J1,J2`).
    Records whether a run of the same command failed at the reviewed code (the failure reproduced)."""
    kinds = {f["id"]: f["kind"] for f in table}
    named: dict[str, list[str]] = {}
    for item in given:
        fid, sep, jobs = item.partition("=")
        if not sep or not csv(jobs):
            die(f"--confirm FINDING=JOB[,JOB], not {item!r}")
        if fid not in kinds:
            die(f"no finding {fid} in the review (its findings: {', '.join(kinds) or 'none'})")
        named.setdefault(fid, []).extend(csv(jobs))
    missing = [f["id"] for f in table if f["blocking"] and f["id"] not in named]
    if missing:
        die(
            "; ".join(f"{fid} has no confirming job" for fid in missing)
            + ": the suite job passing its fix's test at the fixed commit, as --confirm FINDING=JOB"
        )
    problem = job_problem(repo, [commit])
    out: dict[str, Json] = {}
    for fid, jids in named.items():
        why = [w for w in map(problem, dict.fromkeys(jids)) if w]
        if why:
            die(f"{fid}: " + "; ".join(why) + f" (a confirming job passed at {commit[:12]}'s code in {repo})")
        jobs = [passed_at(load_job(jobs_dir() / jid), commit) for jid in dict.fromkeys(jids)]
        out[fid] = {
            "kind": kinds[fid],
            "jobs": [job_summary(j) for j in jobs],
            "reproduced": bool(reviewed) and any(failed_at(j, reviewed) for j in jobs),
        }
    return out


def passed_at(job: Json, commit: str) -> Json:
    """The run of this job's command that passed at COMMIT's code: the job itself, or its rerun."""
    twin = job_at(job["spec"], commit, merged=True)
    return twin if twin is not None else job


def failed_at(job: Json, commit: str) -> bool:
    """Whether the same job (command, directory, level, environment) failed at COMMIT's code."""
    with contextlib.suppress(CampaignError, KeyError):
        twin = job_with_key({**job["spec"], "commit": commit})
        return twin is not None and twin["state"].get("state") in ("failed", "timeout")
    return False


def drift(
    repo: Path,
    main_head: str,
    target: str,
    accepted: dict[str, str],
    *,
    diffs: dict[str, str] | None = None,
    tested: Sequence[str] = (),
) -> list[str]:
    """Quest paths whose own changes at target differ from what was accepted: a path's blob, or with
    DIFFS its changed lines, so main's lines merged into a quest file keep it. Only the quest's own
    changes count, and documentation no TESTED glob matches never does."""
    base = merge_base(repo, main_head, target)
    now_paths = [x for x in changed(repo, base, target) if not is_meta(x) and not doc_path(x, "", tested)]
    at_target = blobs(repo, target, now_paths)
    out = [x for x in now_paths if accepted.get(x) != at_target[x]]
    if diffs is not None and out:
        own = own_diffs(repo, main_head, target)
        out = [x for x in out if diffs.get(x) is None or diffs[x] != own.get(x)]
    rest = [x for x in accepted if x not in at_target and not doc_path(x, "", tested)]
    at_main = blobs(repo, main_head, rest)
    return out + [x for x in rest if at_main[x] != accepted[x]]


GH_FRESH = float(os.environ.get("CAMPAIGN_GH_FRESH", "60"))  # seconds a changeable answer is reused
_gh: dict[str, tuple[int, Any]] = {}


def gh_json(repo: Path, args: list[str], final: Callable[[Any], bool]) -> tuple[int, Any]:
    """`gh ARGS` (JSON out) in REPO, cached on disk for the host: forever once FINAL says the answer
    cannot change (a run that passed), else for GH_FRESH seconds; (returncode, data or stderr)."""
    key = hashlib.sha256(json.dumps([str(repo), args]).encode()).hexdigest()[:32]
    if key in _gh:
        return _gh[key]
    path = jobs_dir() / ".gh" / f"{key}.json"
    with contextlib.suppress(OSError, ValueError):
        hit = json.loads(path.read_text())
        if hit["final"] or time.time() - hit["t"] < GH_FRESH:
            _gh[key] = (0, hit["data"])
            return _gh[key]
    p = subprocess.run(["gh", *args], cwd=repo, capture_output=True, text=True)
    if p.returncode != 0:
        return p.returncode, p.stderr.strip()
    data = json.loads(p.stdout or "null")
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"t": time.time(), "final": final(data), "data": data}))
    tmp.replace(path)
    _gh[key] = (0, data)
    return _gh[key]


def all_passed(items: Any) -> bool:
    """Finished and green: final, while a failure can still be re-run."""
    ok = ("success", "skipped", "neutral")
    return bool(items) and all(x.get("status") == "completed" and x.get("conclusion") in ok for x in items)


def ci_commit(repo: Path, commit: str) -> str:
    """The commit whose CI speaks for COMMIT: itself, or its nearest first-parent ancestor with the
    same code (only campaign files differ), since a journal commit changes nothing CI tests."""
    tree = code_tree(repo, commit)
    log = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--first-parent", "--max-count=20", commit],
        capture_output=True,
        text=True,
    ).stdout.split()
    for k in log:
        if code_tree(repo, k) != tree:
            break
        code, runs = gh_json(repo, ["run", "list", "--commit", k, "--json", "databaseId", "--limit", "1"], bool)
        if code == 0 and runs:  # a commit's runs, once there, stay
            return k
    return commit


def ci_state(repo: Path, commit: str, later: list[str]) -> str:
    if not shutil.which("gh"):
        return "gh is not installed"
    commit = ci_commit(repo, commit)
    code, listed = gh_json(
        repo, ["run", "list", "--commit", commit, "--json", "status,conclusion,name", "--limit", "50"], all_passed
    )
    if code != 0:
        return f"gh failed: {listed}"
    runs = [r for r in listed or [] if r.get("name") not in later]
    if not runs:
        return "no CI runs for this commit (push the branch)"
    pending = [r["name"] for r in runs if r.get("status") != "completed"]
    if pending:
        return "running: " + ", ".join(pending)
    bad = [
        f"{r['name']} ({r.get('conclusion')})"
        for r in runs
        if r.get("conclusion") not in ("success", "skipped", "neutral")
    ]
    return "failed: " + ", ".join(bad) if bad else "success"


CI_CHECK = "ci:"  # a check whose command is ci:JOB is met by that GitHub CI job, not by a local one
CI_CITE = re.compile(r"`ci:([^`]+)`")  # an Evidence row citing a CI job: `ci:ThreadSanitizer`


def ci_jobs(repo: Path, commit: str) -> list[Json] | str:
    """The GitHub CI jobs of every run at COMMIT (name, status, conclusion), or why there are none."""
    if not shutil.which("gh"):
        return "gh is not installed"
    commit = ci_commit(repo, commit)
    code, runs = gh_json(
        repo, ["run", "list", "--commit", commit, "--json", "databaseId", "--limit", "50"], lambda _: False
    )
    if code != 0:
        return f"gh failed: {runs}"
    found: list[Json] = []
    for run in runs or []:
        code, view = gh_json(
            repo, ["run", "view", str(run["databaseId"]), "--json", "jobs"], lambda v: all_passed(v.get("jobs"))
        )
        if code != 0:
            return f"gh failed: {view}"
        found += (view or {}).get("jobs", [])
    return found


def ci_job(repo: Path, commit: str, name: str) -> str:
    """ "success" when every CI job at COMMIT whose name contains NAME passed; else why not."""
    jobs = ci_jobs(repo, commit)
    if isinstance(jobs, str):
        return jobs
    mine = [j for j in jobs if name.lower() in str(j.get("name", "")).lower()]
    if not mine:
        return f"no CI job named {name!r} at {commit[:12]} (push the branch)"
    if any(j.get("status") != "completed" for j in mine):
        return f"CI job {name!r} is running"
    bad = [str(j.get("name")) for j in mine if j.get("conclusion") not in ("success", "skipped", "neutral")]
    return f"CI job {', '.join(bad)} failed" if bad else "success"


def ci_covers(c: Campaign) -> bool:
    """Whether landing needs CI green at the exact commit, which tests the merge with main."""
    return c.state.get("ci") == "github"


def check_evidence(c: Campaign, p: Json, target: str, ci: bool = False) -> tuple[list[str], list[Json]]:
    """Each gate check's job at TARGET's code, or why there is none; with CI, across merges from main."""
    repo = c.repo(p)
    missing: list[str] = []
    evidence: list[Json] = []
    if gates(p) and all(x.get("inputs") for x in gates(p)):
        missing.append("build-test: every gate check declares inputs; one without them must pass at the exact code")
    for check in gates(p):
        if check["cmd"].startswith(CI_CHECK):
            state = ci_job(repo, target, check["cmd"][len(CI_CHECK) :].strip())
            if state == "success":
                evidence.append({"check": check["name"], "ci": check["cmd"], "commit": target, "state": "done"})
            else:
                missing.append(f"{check['name']}: {state}")
            continue
        job = job_for_check(repo, target, check, c.state.get("tested_docs", []), ci=ci)
        if job is None:
            missing.append(f"{check['name']}: no job")
        elif job["state"].get("state") != "done":
            missing.append(f"{check['name']}: {job['state'].get('state')} ({job['id']})")
        else:
            evidence.append(job_summary(job))
    return missing, evidence


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# the whole verdict is one word: "APPROVE WITH CONDITIONS" is no APPROVE
VERDICT = re.compile(r"^Verdict:\s*\**\s*(\S[^\n]*?)[\s*.]*$", re.M)
REVIEWED = re.compile(r"^Commit:\s*\**\s*`?(?:[\w.-]+:)?([0-9a-f]{7,64})\b", re.M)  # sha or repo:sha


def review_path(c: Campaign, pid: str, lens: str) -> Path:
    return c.home / c.meta / "reviews" / f"{pid}-{lens}.md"


def read_review(text: str) -> Json:
    """A review file's verdict, the commit it reviewed, and its findings table: ID, blocking, kind.
    An appended re-check counts: the last `Commit:` line starts the round that is read. A nit never
    blocks, and a CHANGES whose findings all do not block is an APPROVE."""
    rounds = list(REVIEWED.finditer(text))
    text = text[rounds[-1].start() :] if rounds else text
    verdict, reviewed = VERDICT.search(text), REVIEWED.search(text)
    findings: list[Json] = []
    for row in (section(text, "Findings") or text).splitlines():
        cells = [x.strip("*`") for x in table_cells(row)] if row.startswith("|") else []
        if len(cells) < 3 or cells[0].lower() in ("id", "") or set(cells[0]) <= set("-: "):
            continue
        kind = cells[2].lower()
        findings.append({"id": cells[0], "blocking": cells[1].lower() == "yes" and kind != "nit", "kind": kind})
    said = verdict.group(1) if verdict else ""
    return {
        "verdict": "APPROVE" if said == "CHANGES" and findings and not any(f["blocking"] for f in findings) else said,
        "commit": reviewed.group(1) if reviewed else "",
        "findings": findings,
    }


def blob_id(repo: Path, data: bytes) -> str:
    p = subprocess.run(["git", "-C", str(repo), "hash-object", "--stdin"], input=data, capture_output=True)
    if p.returncode != 0:
        die(f"git hash-object: {p.stderr.decode(errors='replace').strip()}")
    return p.stdout.decode().strip()


def review_identity(c: Campaign, pid: str, lens: str) -> str:
    """The review file's git blob id: what an acceptance accepted, "" when there is none."""
    path = review_path(c, pid, lens)
    return blob_id(c.home, path.read_bytes()) if path.exists() else ""


def verdict(c: Campaign, pid: str, lens: str) -> str:
    """A reviewer's verdict on the quest; "none yet" without a review file."""
    review = review_path(c, pid, lens)
    if not review.exists():
        return "none yet"
    return read_review(review.read_text())["verdict"] or "no verdict"


def review_step(said: str, pid: str, lens: str) -> str:
    """What a review's verdict asks for next."""
    if said == "APPROVE":
        return f"approved; `campaign accept {pid} {lens}`"
    if said == "CHANGES":
        return f"verdict CHANGES; fix what blocks, a test each, then `campaign accept {pid} {lens} --confirm FINDING=JOB ...` (the suite job)"
    if said == "REJECT":
        return "verdict REJECT; re-plan the quest"
    if said == "none yet":
        return f"not reviewed yet; launch a {lens} reviewer"
    return f"its review has {said}; the reviewer writes `Verdict: APPROVE|CHANGES|REJECT`"


def landing_gaps(
    c: Campaign, pid: str, target: str, *, final: bool, ci: bool, jobs: list[Json] | None = None
) -> list[str]:
    """What keeps a quest from landing TARGET, each with what to do about it; empty when it can
    land. `land` refuses with these and `status` lists them."""
    p = c.quests[pid]
    repo = c.repo(p)
    main = main_branch(repo)
    gaps: list[str] = []
    unmet = [d for d in p["needs"] + (p["after"] if final else []) if not c.landed(d)]
    if unmet:
        gaps.append(f"land {', '.join(unmet)} first")
    main_head = rev(repo, main)
    base = merge_base(repo, main_head, target)
    moved = [x for x in changed(repo, base, main_head) if not is_meta(x)]
    if moved:
        gaps.append(
            f"{main} changed {plural(len(moved), 'file')} since {p['branch']} branched (e.g. {moved[0]}): merge {main} into it"
        )
    paths = changed(repo, base, target)
    owned = [x for x in paths if lead_file(x)]
    if owned:
        gaps.append(f"{p['branch']} edits the lead's files: {', '.join(owned)}")
    apart = not same_repo(repo, c.home)
    if apart:
        meta = [x for x in paths if is_meta(x)]
        if meta:
            gaps.append(
                f"{p['branch']} adds campaign files to {repo.name} ({', '.join(meta[:4])}): its journal lives in the home (`campaign get {pid} journal`)"
            )
        gaps += journal_gaps(c, pid)
    stray = outside(paths, p["files"])
    if stray:
        gaps.append(
            f"{p['branch']} changes files outside its file set ({', '.join(p['files'])}): {', '.join(stray[:8])}"
        )
    for task, t in sorted(p.get("tasks", {}).items()) if final else []:
        if t.get("status") != "dropped" and has_ref(repo, t["branch"]) and not ancestor(repo, t["branch"], target):
            gaps.append(
                f"task {task} is not merged into {target[:12]}: merge {t['branch']}, or drop it (`campaign set {pid} --task {task} status=dropped reason=R`)"
            )
    carried = (landing_journals(c, pid) if apart else journals_at(c, repo, target, pid)) if final else {}
    rows = criteria_evidence(c, pid, [target], carried, jobs) if final else {}
    unproven = [x for x, r in rows.items() if not any(e["met"] for e in r)]
    if unproven:
        gaps.append(
            f"criteria without evidence at {target[:12]}: {id_ranges(unproven)} (`campaign status {pid}` lists them)"
        )
    unanswered = [m["id"] for m in inbox(c, pid) if not answered(m, carried.values())] if final else []
    if unanswered:
        gaps.append(
            f"unanswered messages: {', '.join(unanswered)}: answer each in a journal by its id, or close it (`campaign message {pid} --close MSG --reason R`)"
        )
    stale = sorted({w.split()[0] for x in unproven for e in rows[x] for w in e["why"] if w.endswith("ran other code")})
    if stale:
        gaps.append(f"evidence jobs ran other code than {target[:12]}: `campaign job rerun {' '.join(stale)}`")
    for missing in check_evidence(c, p, target, ci=ci and ci_covers(c))[0]:
        name, _, why = missing.partition(": ")
        gaps.append(f"check {name} is not green at {target[:12]}: {why} (`campaign verify {pid}`)")
    gaps += review_gaps(c, pid, target, main_head) if final else []
    if ci and c.state.get("ci") == "github":
        state = ci_state(repo, target, c.state.get("ci_later", []))
        if state != "success":
            gaps.append(f"CI at {target[:12]}: {state} (--no-ci REASON to land without it)")
    return gaps


def review_gaps(c: Campaign, pid: str, target: str, main_head: str) -> list[str]:
    """What the quest's reviews still lack for TARGET's code: a lens its risk requires, an acceptance,
    or one that no longer holds (a criterion, the review, or the code changed since)."""
    p = c.quests[pid]
    repo = c.repo(p)
    base = merge_base(repo, main_head, target)
    gaps: list[str] = []
    need = lens_floor(p["risk"], p["lenses"])
    if need:
        gaps.append(f"{p['risk']} risk needs {' and '.join(need)}: `campaign set {pid} lenses=...`")
    for lens in p["lenses"]:
        acc = p.get("accepted", {}).get(lens)
        if not acc:
            gaps.append(f"{lens} review not accepted: {review_step(verdict(c, pid, lens), pid, lens)}")
        elif acc.get("criteria") != criteria_hash(c, pid):
            gaps.append(f"a criterion changed after {lens} was accepted: re-accept against the current criteria")
        elif "review" not in acc:
            gaps.append(f"the {lens} acceptance names no review content: `campaign accept {pid} {lens}` again")
        elif acc["review"] != review_identity(c, pid, lens):
            gaps.append(
                f"the {lens} review changed after it was accepted (verdict now {verdict(c, pid, lens)}): "
                f"`campaign accept {pid} {lens}` again"
            )
        elif lens != "design":  # design approves the approach before the code exists
            moved_since = drift(
                repo, main_head, target, acc["blobs"], diffs=acc.get("diffs"), tested=c.state.get("tested_docs", [])
            )
            upstream = [x for x in changed(repo, acc.get("base", base), base) if not is_meta(x)]
            if moved_since:
                gaps.append(f"{lens} was accepted at {acc['commit'][:12]}; changed since: {', '.join(moved_since[:8])}")
            elif upstream and p["risk"] in ("high", "critical"):
                gaps.append(
                    f"{lens}: main changed {plural(len(upstream), 'code file')} under this {p['risk']}-risk quest "
                    f"since its review (`git diff {acc.get('base', base)[:12]} {base[:12]}`); read them, then re-accept"
                )
    return gaps


@contextlib.contextmanager
def repo_lock(c: Campaign, repo: Path) -> Generator[None, None, None]:
    """The lock of a repository other than the home: one campaign writes to it at a time."""
    common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    if common == Path(git(c.home, "rev-parse", "--path-format=absolute", "--git-common-dir")):
        yield
        return
    deadline = time.time() + float(os.environ.get("CAMPAIGN_LOCK_WAIT", "120"))
    with open(common / "campaign.lock", "w") as fh:
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() > deadline:  # bounded, so two campaigns landing into each other never deadlock
                    die(f"{repo} is busy: another campaign is writing to it; retry")
                time.sleep(0.2)
        yield


def cmd_land(a: argparse.Namespace) -> int:
    with writing() as c, contextlib.ExitStack() as held:
        p = c.quest(a.id)
        repo = c.repo(p)
        held.enter_context(repo_lock(c, repo))
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
            c.save(
                f"campaign: record {a.id} landed at {target[:12]}" + (" (unverified)" if unverified else ""),
                extra=refresh_map(c),
            )
            print(
                f"{a.id}: recorded {target[:12]}" + (f"; never verified: {'; '.join(unverified)}" if unverified else "")
            )
            for item in kept:
                print(f"kept {item} (`campaign clean {a.id}` once resolved)")
            return 0
        if a.earlier:
            die("--earlier describes a landing --record records")
        if p["status"] not in ("active", "review"):
            die(f"{a.id} is {p['status']}")
        final = a.upto is None
        if not final and not a.reason:
            die("an increment needs --reason (why this prefix is safe to land alone)")
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
        gaps = landing_gaps(c, a.id, target, final=final, ci=a.no_ci is None)
        if gaps:
            die(f"{a.id} cannot land yet:\n  " + "\n  ".join(gaps))
        evidence = check_evidence(c, p, target, ci=a.no_ci is None and ci_covers(c))[1]
        carried = landing_journals(c, a.id) if not same_repo(repo, c.home) else journals_at(c, repo, target, a.id)
        proven: dict[str, list[Json]] = criteria_evidence(c, a.id, [target], carried) if final else {}
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
        c.save(message + f" at {landed[:12]}", extra=refresh_map(c))
        for m in filed:
            (mailbox(c, a.id) / f"{m['id']}.json").unlink(missing_ok=True)
        notify(f"Landed {a.id}" if final else f"Landed part of {a.id}", f"{p['title']} at {landed[:12]}")
        if a.push:
            for where in dict.fromkeys([checkout, c.home]):
                pushed = subprocess.run(
                    ["git", "-C", str(where), "push", "-q", "origin", main_branch(where)],
                    capture_output=True,
                    text=True,
                )
                if pushed.returncode != 0:
                    die(f"landed locally, but the push from {where} failed: {pushed.stderr.strip()}")
    via = "" if landed == target else f" (merge {landed[:12]})"
    print(f"{a.id}: landed {target[:12]} on {main}{via}" + ("" if final else " (increment)"))
    for item in kept:
        print(f"kept {item} (`campaign clean {a.id}` once resolved)")
    return 0


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
    if busy:
        kept.append(f"its build slots: jobs {', '.join(busy)} use them")
    else:
        worktree_remove(str(repo), jobs_dir() / "slots" / quest_slot(c, pid, repo))
        for slot in (jobs_dir() / "slots").glob(f"{quest_slot(c, pid, repo)}@*"):  # its other repositories'
            if slot.is_dir():
                common = git(slot, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
                worktree_remove(str(Path(common).parent) if common else str(slot), slot)
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


# ---------------------------------------------------------------- status and check


def journal_place(c: Campaign, name: str) -> Path | None:
    """The worktree holding NAME's journal (a quest, or QUEST--TASK): its own, or for a quest in another
    repository, its journal's worktree in the home; None when it has none."""
    pid, _, task = name.partition("--")
    p = c.quests[pid]
    record = p.get("tasks", {}).get(task, {}) if task else p
    rel = record.get("journal") or record.get("worktree", "")
    return (c.home / rel).resolve() if rel else None


def journal_text(c: Campaign, name: str, branch: str, *, committed: bool = False) -> str:
    """A quest's or task's journal: in its journal's worktree (unless only what is committed), else on
    its branch in the home, where a quest in another repository keeps a branch of the same name."""
    path = f"{c.meta}/journal/{name}.md"
    wt = journal_place(c, name)
    if wt and not committed and (wt / path).exists():
        return (wt / path).read_text()
    return file_at(c.home, branch, path) if branch else ""


def journal_worktree(c: Campaign, name: str, branch: str) -> Path:
    """A sparse worktree of the home on BRANCH holding only the campaign's directory: where a quest in
    another repository keeps its journal, so the campaign's records stay in the home and the other
    repository's history holds only its code and docs."""
    jwt = worktree_path(c.home, c.name, name)
    if has_ref(c.home, branch):
        git(c.home, "worktree", "add", "-q", "--no-checkout", str(jwt), branch)
    else:
        home_main = rev(c.home, main_branch(c.home))
        git(c.home, "worktree", "add", "-q", "--no-checkout", "-b", branch, str(jwt), home_main)
    git(jwt, "sparse-checkout", "set", "--no-cone", f"/{c.meta}/")
    git(jwt, "checkout", "-q")
    gitdir = Path(git(jwt, "rev-parse", "--path-format=absolute", "--git-dir"))
    (gitdir / "campaign-home").write_text(f"{c.home}\n{c.name}\n")
    return jwt


def landing_journals(c: Campaign, pid: str) -> dict[str, str]:
    """The committed journals a landing of a quest in another repository carries into the home."""
    found: dict[str, str] = {}
    for name, branch, _, _ in branches(c, pid):
        text = file_at(c.home, branch, f"{c.meta}/journal/{name}.md") if has_ref(c.home, branch) else ""
        if text:
            found[name] = text
    return found


def journal_gaps(c: Campaign, pid: str) -> list[str]:
    """What keeps the home journal branches of a quest in another repository from merging cleanly: a
    branch changing more than its quest's journals, or the home's main changing them meanwhile."""
    main = main_branch(c.home)
    main_head = rev(c.home, main)
    own = {f"{c.meta}/journal/{name}.md" for name, _, _, _ in branches(c, pid)}
    gaps: list[str] = []
    for name, branch, _, _ in branches(c, pid):
        if not has_ref(c.home, branch):
            gaps.append(f"{name}'s journal branch {branch} is missing in the home")
            continue
        tip = rev(c.home, branch)
        base = merge_base(c.home, main_head, tip)
        extra = [x for x in changed(c.home, base, tip) if x not in own]
        if extra:
            gaps.append(f"{branch} in the home changes more than its journals: {', '.join(extra[:8])}")
        both = [x for x in changed(c.home, base, main_head) if x in own]
        if both:
            gaps.append(f"the home's {main} changed {', '.join(both)} since {branch} branched: merge {main} into it")
    return gaps


def land_journals(c: Campaign, pid: str) -> None:
    """Merges the journal branches of a quest in another repository into the home's main."""
    main = main_branch(c.home)
    for name, branch, _, _ in branches(c, pid):
        if has_ref(c.home, branch) and not ancestor(c.home, rev(c.home, branch), rev(c.home, main)):
            git(c.home, "merge", "-q", "--no-ff", "-m", f"campaign: journal of {name}", branch)


def journals_at(c: Campaign, repo: Path, commit: str, pid: str) -> dict[str, str]:
    """The quest's and its tasks' journals in a commit: what a landing would carry."""
    folder = f"{c.meta}/journal"
    names = names_at(repo, commit, folder)
    mine = sorted(n for n in names if n == f"{pid}.md" or (n.startswith(f"{pid}--") and n.endswith(".md")))
    return {n[:-3]: file_at(repo, commit, f"{folder}/{n}") for n in mine}


def journal_state(c: Campaign, name: str, branch: str) -> str:
    return state_of(journal_text(c, name, branch, committed=True))


def state_of(journal: str) -> str:
    m = re.search(r"^State:\s*(.+)$", journal, re.M)
    return m.group(1).strip() if m else "(no State line)" if journal.strip() else "(no journal)"


def table_cells(row: str) -> list[str]:
    """A Markdown table row's cells; `\\|` is a pipe inside a cell, as GitHub writes it."""
    text = row.strip()
    text = text[1:] if text.startswith("|") else text
    text = text[:-1] if text.endswith("|") and not text.endswith("\\|") else text
    return [x.strip().replace("\\|", "|") for x in re.split(r"(?<!\\)\|", text)]


def section(text: str, title: str) -> str:
    m = re.search(rf"^## {title}\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1) if m else ""


PROOF = ("job", "artifact", "decision", "manual")  # what a criterion's evidence must be; job by default


def criteria(c: Campaign, pid: str) -> dict[str, Json]:
    """A quest's acceptance criteria (A1, A2, ...) from its quest file: the first line of each and the
    kind of evidence it needs, `- A2 [artifact] ...` (job when untagged)."""
    brief = c.home / c.meta / "quests" / f"{pid}.md"
    text = section(brief.read_text() if brief.exists() else "", "Acceptance criteria")
    found: dict[str, Json] = {}
    tags = "|".join(PROOF)
    for cid, kind, line in re.findall(rf"^\s*[-*]\s*\**(A\d+)\b\**[:.]?\**\s*(?:\[({tags})\]\s*)?(.*)$", text, re.M):
        if not line.startswith("<"):  # the template's unfilled placeholder
            found.setdefault(cid, {"text": line.strip(), "kind": kind or "job"})
    return found


def decisions(c: Campaign) -> set[str]:
    """The ids in PLAN.md's Decisions table: what a [decision] criterion's evidence names."""
    plan = c.home / c.meta / "PLAN.md"
    rows = section(plan.read_text() if plan.exists() else "", "Decisions").splitlines()
    ids = (table_cells(r)[0] for r in rows if r.startswith("|"))
    return {x for x in ids if x and x.lower() != "id" and not set(x) <= set("-: ")}


def proof(c: Campaign, repo: Path, commits: list[str]) -> Callable[[str, str], list[str]]:
    """What keeps an Evidence row's text from proving a criterion of a kind, at one of these commits:
    a job criterion needs a job that passed at the code; an artifact one a `path` in the commit; a
    decision one an id from PLAN.md's Decisions table; a manual one only a non-empty row. Every job a row
    names must have passed, whatever its kind."""
    job = job_problem(repo, commits)
    recorded = decisions(c)

    def check(kind: str, text: str) -> list[str]:
        cited = list(dict.fromkeys(JOB_ID.findall(text)))
        why = [w for w in map(job, cited) if w]
        for name in dict.fromkeys(CI_CITE.findall(text)):
            state = ci_job(repo, commits[0], name.strip()) if commits else "no commit"
            if state == "success":
                cited.append(f"ci:{name}")
            else:
                why.append(state)
        for path, quote in QUOTED.findall(text):
            if commits and not any(quoted(repo, k, path, quote) for k in commits):
                why.append(f'"{quote[:60]}" is not in {path} at {commits[0][:12]}')
        bare = QUOTE.sub("", text)  # what a quote says is never a path
        if kind == "job" and not cited:
            why.append(
                "names no job (a job criterion needs a job id or a `ci:JOB`; tag it [manual] or [artifact] otherwise)"
            )
        elif kind == "artifact":
            paths = re.findall(r"`([^`\s]+)`", bare)
            missing = [x for x in paths if not any(read_object(repo, f"{k}:{x}") for k in commits)]
            why += [f"{x} is not in {commits[0][:12]}" for x in missing] if commits else []
            if not paths:
                why.append("names no file (an artifact criterion names a `path` in the repository)")
        elif kind == "decision" and not set(re.findall(r"[\w.-]+", text)) & recorded:
            why.append("names no decision in PLAN.md's Decisions table")
        return why

    return check


UNMET = re.compile(r"<|(pending|queued|running|failed|missing|open|todo|none)\b", re.I)
QUOTE = re.compile(r'"[^"]+"')
QUOTED = re.compile(r'`([^`\s]+)`:?\s*"([^"]+)"')  # `path`: "text the file holds"
_quotes: dict[tuple[str, str, str], str] = {}


def quoted(repo: Path, commit: str, path: str, quote: str) -> bool:
    """Whether PATH at COMMIT holds QUOTE, whitespace aside: evidence that cites what a record says,
    not only that the record exists."""
    key = (str(repo), commit, path)
    if key not in _quotes or not SHA.fullmatch(commit):
        _quotes[key] = " ".join(file_at(repo, commit, path).split())
    return " ".join(quote.split()) in _quotes[key]


JOB_ID = re.compile(r"\b\d{6}-\d{6}-[0-9a-f]{6}\b")


def job_problem(repo: Path | None, commits: list[str]) -> Callable[[str], str]:
    """What is wrong with a job as evidence; "" when it, or the same job rerun, passed at the code of
    one of these commits in REPO (`campaign job rerun` makes that one command)."""

    def problem(jid: str) -> str:
        d = jobs_dir() / jid
        if not (d / "spec.json").exists():
            return f"{jid} is not a job on this machine"
        job = load_job(d)
        state = job["state"].get("state")
        if state != "done":
            return f"{jid} {state or 'has no result'}"
        if not job["spec"].get("commit"):
            return f"{jid} is unversioned"
        if repo is not None and not same_repo(Path(job["spec"]["repo"]), repo):
            return f"{jid} ran in {job['spec']['repo']}, not the quest's repository"
        for commit in commits:
            with contextlib.suppress(CampaignError, KeyError):
                twin = job_at(job["spec"], commit, merged=True)
                if twin and twin["state"].get("state") == "done":
                    return ""
        return f"{jid} ran other code"

    return problem


def evidence(
    journals: dict[str, str], wanted: dict[str, Json], check: Callable[[str, str], list[str]] | None = None
) -> dict[str, list[Json]]:
    """Each criterion's rows in the journals' Evidence tables (the quest's and its tasks'): which
    journal, what it cites, and whether that is evidence: a row whose other cells are not empty, a
    <placeholder>, pending or failed, and that `check` finds proves its criterion's kind."""
    rows: dict[str, list[Json]] = {x: [] for x in wanted}
    for name, text in journals.items():
        for row in section(text, "Evidence").splitlines():
            cells = table_cells(row)
            if len(cells) < 2 or cells[0].startswith("<"):
                continue
            rest = [x for x in cells[1:] if x]
            met = bool(rest) and not any(UNMET.match(x) or set(x) <= set("-: ") for x in rest)
            for cid in re.findall(r"\bA\d+\b", cells[0]):
                if cid in rows:
                    why = check(wanted[cid]["kind"], " ".join(rest)) if met and check else []
                    rows[cid].append(
                        {"journal": name, "evidence": " | ".join(rest), "met": met and not why, "why": why}
                    )
    return rows


def evidence_shape(spec: Json) -> str:
    """A job up to its code: what a tag names, so each run of the same job at later code counts."""
    refs = sorted((name, r.get("repo")) for name, r in spec.get("refs", {}).items())
    fields = [spec.get(k) for k in ("repo", "cmd", "cwd", "level", "inputs")]
    return json.dumps([*fields, refs, sorted(spec.get("env", {}).items())])


def tagged(c: Campaign, pid: str, jobs: list[Json]) -> dict[str, Json]:
    """Each criterion the quest's jobs are tagged for (`job submit --for`): the newest job of each
    tagged command ("runs"), and the jobs tagged as failing without the fix ("fails")."""
    out: dict[str, Json] = {}
    for j in sorted(jobs, key=lambda j: j["spec"].get("submitted", 0)):
        for t in j.get("tags", []):
            if t.get("campaign") == c.ident and t.get("quest") == pid:
                for cid in t.get("for", []):
                    entry = out.setdefault(cid, {"runs": {}, "fails": []})
                    if t.get("fails_without"):
                        entry["fails"].append(j)
                    else:
                        entry["runs"][evidence_shape(j["spec"])] = j
    return out


def tagged_row(repo: Path, commits: list[str], entry: Json) -> Json:
    """A tagged criterion's evidence: a run of one of its tagged jobs that passed at one of COMMITS'
    code (across merges from main), and, when any is tagged, a fails-without job that exited 0."""
    passed: list[str] = []
    why: list[str] = []
    for j in entry["runs"].values():
        mine = same_repo(Path(j["spec"].get("repo") or "/"), repo)
        twin = None
        for k in commits if mine else []:
            with contextlib.suppress(CampaignError, KeyError):
                found = job_at(j["spec"], k, merged=True)
                twin = twin or (found if found and found["state"].get("state") == "done" else None)
        state = j["state"].get("state")
        if twin is not None:
            passed.append(twin["id"] + (f" ({docs_note(twin)})" if docs_note(twin) else ""))
        elif not mine:
            why.append(f"{j['id']} ran in {j['spec'].get('repo')}, not the quest's repository")
        else:
            why.append(f"{j['id']} ran other code" if state == "done" else f"{j['id']} {state}")
    fails = [j["id"] for j in entry["fails"] if j["state"].get("state") == "done"]
    if entry["fails"] and not fails:
        why += [f"{j['id']} {j['state'].get('state')}: the test passed without the fix" for j in entry["fails"]]
    text = ", ".join(passed) + (f"; fails without: {', '.join(fails)}" if fails else "")
    met = bool(passed) and (bool(fails) or not entry["fails"])
    return {"journal": "jobs", "evidence": text or "none passed", "met": met, "why": [] if met else why}


def criteria_evidence(
    c: Campaign, pid: str, commits: list[str], journals: dict[str, str], jobs: list[Json] | None = None
) -> dict[str, list[Json]]:
    """Each criterion's evidence at one of COMMITS: the jobs tagged for it, else its rows in the
    journals' Evidence tables (journals written before tags, and non-job criteria)."""
    wanted = criteria(c, pid)
    repo = c.repo(c.quests[pid])
    tags = tagged(c, pid, all_jobs() if jobs is None else jobs)
    rows = evidence(journals, {x: w for x, w in wanted.items() if x not in tags}, proof(c, repo, commits))
    return {x: [tagged_row(repo, commits, tags[x])] if x in tags else rows[x] for x in wanted}


def evidence_table(rows: dict[str, list[Json]]) -> list[str]:
    """The journal's Evidence table, generated: what proves each criterion, or what it lacks."""
    out = ["| Criterion | Evidence |", "|---|---|"]
    for cid, found in rows.items():
        met = [r["evidence"] for r in found if r["met"]]
        lacks = "; ".join(w for r in found for w in r["why"]) or "no evidence yet"
        out.append(f"| {cid} | {'; '.join(met) if met else f'<{lacks}>'} |")
    return out


def criteria_for(c: Campaign, pid: str, given: str) -> list[str]:
    """--for IDS, checked against the quest file: a job proves only job criteria."""
    wanted = criteria(c, pid)
    ids = csv(given)
    for cid in ids:
        if cid not in wanted:
            die(f"--for {cid}: {pid}'s quest file has no criterion {cid} (it has {', '.join(wanted) or 'none'})")
        if wanted[cid]["kind"] != "job":
            die(f"--for {cid}: an [{wanted[cid]['kind']}] criterion is proven in the journal, not by a job")
    return ids


def replaced_citations(journals: dict[str, str], jobs: list[Json]) -> list[Json]:
    """Evidence rows citing a job a newer one of its shape replaced: at its code or a descendant,
    submitted later, ended, and cited by no journal of the quest. The row should name that result."""
    by_id = {j["id"]: j for j in jobs}
    shapes = {j["id"]: shape(j["spec"]) for j in jobs}
    cited = {x for text in journals.values() for x in JOB_ID.findall(section(text, "Evidence"))}
    out: list[Json] = []
    for name, text in journals.items():
        for row in section(text, "Evidence").splitlines():
            cells = table_cells(row)
            if len(cells) < 2 or cells[0].startswith("<"):
                continue
            for jid in dict.fromkeys(JOB_ID.findall(" ".join(cells[1:]))):
                old = by_id.get(jid)
                if old is None:
                    continue
                newer = [
                    j
                    for j in jobs
                    if shapes[j["id"]] == shapes[jid]
                    and j["spec"].get("submitted", 0) > old["spec"].get("submitted", 0)
                    and j["state"].get("state") in ("done", *FAILED)
                    and descends(old["spec"], j["spec"])
                ]
                if newer and not cited & {j["id"] for j in newer}:
                    n = max(newer, key=lambda j: j["spec"].get("submitted", 0))
                    out.append(
                        {
                            "journal": name,
                            "criteria": cells[0],
                            "job": jid,
                            "by": n["id"],
                            "state": n["state"].get("state"),
                            "commit": str(n["spec"].get("commit", ""))[:12],
                        }
                    )
    return out


def id_ranges(ids: list[str]) -> str:
    """A1, A2, A3, A5 as "A1-A3, A5"."""
    nums = sorted(int(x[1:]) for x in ids)
    runs: list[list[int]] = []
    for n in nums:
        if runs and n == runs[-1][1] + 1:
            runs[-1][1] = n
        else:
            runs.append([n, n])
    return ", ".join(f"A{a}" if a == b else f"A{a}-A{b}" for a, b in runs)


def next_step(journal: str) -> str:
    """The first step in a journal's Next list."""
    m = re.search(r"^\s*(?:\d+\.|[-*])\s+(.+)$", section(journal, "Next"), re.M)
    step = m.group(1).strip() if m else ""
    return "" if step.startswith("<") else step


def problems(c: Campaign) -> list[tuple[str, str]]:
    """(severity, message) for everything git, the worktrees and the queue contradict."""
    out: list[tuple[str, str]] = []
    if c.state.get("landing"):
        out.append(
            (
                "warning",
                f"the landing of {c.state['landing'].get('id')} was interrupted; the next state-writing command completes or clears it",
            )
        )
    touched: dict[str, list[tuple[str, str]]] = {}
    for pid, p in c.quests.items():
        repo = c.repo(p)
        if not common_dir(repo):
            out.append(("error", f"{pid}: repository {repo} is missing"))
            continue
        main = main_branch(repo)
        for entry in p.get("landed", []):
            if not on_branch(repo, entry["commit"], main):
                out.append(("error", f"{pid}: landed commit {entry['commit'][:12]} is not on {main}"))
        if p["status"] not in ("active", "review"):
            continue
        unmet = [d for d in p["needs"] if not c.landed(d)]
        if unmet:
            out.append(("warning", f"{pid}: active before its needs landed: {', '.join(unmet)}"))
        for name, branch, rel, _ in branches(c, pid):
            if not has_ref(repo, branch):
                out.append(("error", f"{name}: branch {branch} is gone; drop the quest or restart it"))
                continue
            wt = (c.home / rel).resolve() if rel else None
            if wt is None or not wt.exists():
                out.append(("warning", f"{name}: worktree {rel or '(none)'} is missing"))
            if not same_repo(repo, c.home):
                if not has_ref(c.home, branch):
                    out.append(("error", f"{name}: its journal branch {branch} is missing in the home"))
                jp = journal_place(c, name)
                if jp is None or jp == wt or not jp.exists():
                    out.append(("warning", f"{name}: its journal's worktree in the home is missing"))
            head = rev(repo, branch)
            if name == pid and ancestor(repo, head, main) and head != rev(repo, p["base"]):
                out.append(
                    (
                        "warning",
                        f"{pid}: {branch} is merged into {main} but not recorded (`campaign land {pid} --record {head[:12]}`)",
                    )
                )
            against = rev(repo, main) if name == pid else rev(repo, p["branch"])
            paths = changed(repo, merge_base(repo, against, head), head)
            owned = [x for x in paths if lead_file(x)]
            if owned:
                out.append(("warning", f"{name}: edits the lead's files {', '.join(owned[:5])}"))
            code = [x for x in paths if not is_meta(x)]
            scope = p["files"] if name == pid else p.get("tasks", {}).get(name.partition("--")[2], {}).get("files", [])
            if scope:
                outside = [x for x in code if not any(fnmatch.fnmatch(x, g) for g in scope)]
                if outside:
                    out.append(("warning", f"{name}: changes files outside its scope: {', '.join(outside[:5])}"))
            for x in code:
                touched.setdefault(f"{p['repo']}:{x}", []).append((pid, name))
    overlaps: dict[tuple[str, ...], list[str]] = {}
    for path, who in touched.items():
        pids = tuple(sorted({w[0] for w in who}))
        if len(pids) > 1:
            overlaps.setdefault(pids, []).append(path.split(":", 1)[1])
    for pids, paths in sorted(overlaps.items()):
        out.append(("warning", f"one writer per file: {' and '.join(pids)} both change {', '.join(paths[:5])}"))
    for pid, p in c.quests.items():
        live = p["status"] in ("planned", "active", "review")
        for dep in [*p["needs"], *p["after"]]:
            if ":" in dep and live and foreign(dep) is None:
                out.append(("warning", f"{pid}: depends on {dep}, which no campaign on this machine has"))
        if p["status"] in ("active", "review"):
            for other in cross_overlaps(c, pid):
                out.append(
                    ("warning", f"{pid}: files may overlap {other}: order them (--after {other}) or narrow files")
                )
        if not live:
            continue
        for name, o in campaigns().items():
            if name == c.name:
                continue
            # only a started quest changes a contract; starting one tells the other lead already
            for qid, q in o["state"].get("quests", {}).items():
                if q.get("status") not in ("planned", "active", "review"):
                    continue
                if q.get("status") != "planned":
                    for x in consumed(p["consumes"], changes(Path(o["home"]), q)):
                        out.append(
                            ("warning", f"{pid}: contract {x} is changing in {name}:{qid}; build against its brief")
                        )
                if p["status"] != "planned":
                    for x in consumed(q.get("consumes", []), changes(c.home, p)):
                        out.append(
                            ("warning", f"{pid}: changes contract {x}, which {name}:{qid} consumes; tell its lead")
                        )
    providers: dict[str, list[str]] = {}
    for pid, p in c.quests.items():
        if p["status"] != "dropped":
            for contract in p["provides"]:
                providers.setdefault(contract, []).append(pid)
    for contract, pids in sorted(providers.items()):
        if len(pids) > 1:
            out.append(
                ("warning", f"contract {contract} is provided by {', '.join(pids)}: one provider, or a system review")
            )
        changing = [x for x in pids if c.quests[x]["status"] in ("active", "review")]
        consumers = [
            x
            for x, q in c.quests.items()
            if contract in q["consumes"] and q["status"] in ("planned", "active", "review") and x not in pids
        ]
        if changing and consumers:
            out.append(
                (
                    "info",
                    f"contract {contract} is changing in {', '.join(changing)}; consumers {', '.join(consumers)} build against its brief",
                )
            )
    jobs = all_jobs()
    for pid, p in c.quests.items():
        for record in p["landed"]:
            for jid in record.get("later", []):
                job = load_job(jobs_dir() / jid)
                st = job["state"].get("state", "missing")
                if st == "missing":
                    out.append(("warning", f"{pid}: deferred check {jid}'s job is gone; its result is unknown"))
                elif st in ("failed", "timeout", "interrupted", "cancelled", "skipped"):
                    at = str(job["spec"].get("commit", record["commit"]))[:12]
                    out.append(
                        (
                            "error",
                            f"{pid}: deferred check {jid} is {st} at {at}: fix forward or revert, then `campaign verify {pid} --deferred`",
                        )
                    )
    if any(j["state"].get("state") == "queued" for j in jobs) and not worker_alive():
        out.append(
            ("warning", "jobs are queued and no worker runs (`campaign worker`, or any submit or wait starts one)")
        )
    for j in jobs:
        if j["state"].get("state") == "interrupted" and j["spec"].get("campaign") == c.ident:
            out.append(("warning", f"job {j['id']} ({j['spec'].get('what')}) was interrupted and not retried"))
    return out


def quest_view(c: Campaign, pid: str, jobs: list[Json], brief: bool = False, table: bool = False) -> Json:
    """A quest as status shows it: what it still needs ("missing"), its branch, checks, reviews and
    journals, and the jobs that failed at its current code, each noted or not in a journal. Brief
    keeps only what an agent acts on."""
    p = c.quests[pid]
    view: Json = {"id": pid, "status": p["status"], "risk": p["risk"], "title": p["title"], "missing": []}
    view["weakened"] = [h for h in p.get("history", []) if h.get("weakened")]
    view["files"] = p["files"]
    unmet = [d for d in p["needs"] if not c.landed(d)]
    clash: list[str] = clashes(c, pid) if p["status"] == "planned" and not unmet else []
    view["launchable"] = p["status"] == "planned" and not unmet and not clash
    if p["status"] == "planned" and unmet:
        view["missing"] = [f"land {', '.join(unmet)} first"]
    elif clash:
        view["missing"] = [f"files may overlap {', '.join(clash)}: start once it lands, or narrow files"]
    elif p["status"] == "blocked":
        view["missing"] = [f"blocked: {p.get('reason', '')}"]
    repo = c.repo(p)
    mine = [j for j in jobs if j["spec"].get("quest") == pid and j["spec"].get("campaign") == c.ident]
    view["jobs"] = {s: sum(1 for j in mine if j["state"].get("state") == s) for s in ("queued", "running")}
    view["landed"] = [
        x["commit"][:12]
        + ("" if x.get("final") else " (increment)")
        + (" (before the campaign)" if x.get("earlier") else "")
        + (f" (unverified: {'; '.join(x['unverified'])})" if x.get("unverified") else "")
        + (f" (CI skipped: {x['ci_skipped']})" if x.get("ci_skipped") else "")
        for x in p["landed"]
    ]
    later = [load_job(jobs_dir() / j)["state"].get("state", "missing") for x in p["landed"] for j in x.get("later", [])]
    if later:
        view["deferred"] = {s: later.count(s) for s in sorted(set(later))}
    checked: set[str] = set()
    texts: dict[str, str] = {}
    if p["status"] in ("active", "review") and p.get("branch") and has_ref(repo, p["branch"]):
        main = main_branch(repo)
        head = rev(repo, p["branch"])
        ahead = git(repo, "rev-list", "--count", f"{main}..{head}")
        view.update(branch=p["branch"], head=head[:12], ahead=int(ahead))
        view["missing"] = landing_gaps(c, pid, head, final=True, ci=True, jobs=jobs)
        view["docs_only"] = [
            f"{x['name']}: {j['id']} {docs_note(j)}"
            for x in gates(p)
            if not x["cmd"].startswith(CI_CHECK)
            and (j := job_for_check(repo, head, x, c.state.get("tested_docs", []), ci=ci_covers(c)))
            and docs_note(j)
        ]
        if brief:
            for name, branch, _, _ in branches(c, pid):
                texts[name] = journal_text(c, name, branch)
        if table:
            tips = [rev(repo, b) for _, b, _, _ in branches(c, pid) if has_ref(repo, b)]
            view["evidence"] = evidence_table(criteria_evidence(c, pid, tips, texts, jobs))
    if not brief and "branch" in view:
        head = rev(repo, p["branch"])
        checks: dict[str, str] = {}
        view["checks"] = checks
        view["later"] = [x["name"] for x in deferred(p)]
        for check in gates(p):
            job = job_for_check(repo, head, check, c.state.get("tested_docs", []), ci=ci_covers(c))
            note = f" ({docs_note(job)})" if job and docs_note(job) else ""
            checks[check["name"]] = (job["state"].get("state", "?") + note) if job else "not run at head"
            if job:
                checked.add(job["id"])
        view["reviews"] = {lens: verdict(c, pid, lens) for lens in p["lenses"]}
        for lens, acc in p.get("accepted", {}).items():
            if lens in view["reviews"]:
                view["reviews"][lens] = acceptance(acc, verdict(c, pid, lens))
        work: dict[str, Json] = {}
        view["journal"] = work
        for name, branch, rel, _ in branches(c, pid):
            dirty = 0  # the code's and the journal's worktrees; -1 when one is missing
            for place in dict.fromkeys([(c.home / rel).resolve() if rel else None, journal_place(c, name)]):
                if place is None or not place.exists():
                    dirty = -1
                    break
                dirty += len(git(place, "status", "--porcelain").splitlines())
            texts[name] = journal_text(c, name, branch)
            task = p.get("tasks", {}).get(name.partition("--")[2], {})
            state = f"dropped: {task.get('reason', '')}" if task.get("status") == "dropped" else state_of(texts[name])
            work[name] = {"state": state, "dirty": dirty, "worktree": rel}
        journal = texts.get(pid) or journal_text(c, pid, p["branch"])
        wanted = criteria(c, pid)
        tasks = [w["state"] for name, w in work.items() if name != pid and not w["state"].startswith("dropped: ")]
        tips = [rev(repo, b) for _, b, _, _ in branches(c, pid) if has_ref(repo, b)]
        journals = {pid: journal, **{n: t for n, t in texts.items() if n != pid}}
        rows = criteria_evidence(c, pid, tips, journals, jobs)
        view["progress"] = {
            "criteria": len(wanted),
            "met": [x for x, r in rows.items() if any(e["met"] for e in r)],
            "unlisted": [x for x, r in rows.items() if not r],
            "detail": [{"id": x, **wanted[x], "rows": r} for x, r in rows.items()],
            "tasks": len(tasks),
            "tasks_done": sum(1 for t in tasks if re.match(r"(complete|done)\b", t, re.I)),
            "next": next_step(journal),
        }
    view["replaced"] = replaced_citations(texts, mine)
    ended = ended_at_head(c, pid, mine)
    view["done_at_head"] = sum(1 for j in ended if j["state"].get("state") == "done")
    view["messages"] = [{**m, "answered": answered(m, texts.values())} for m in inbox(c, pid)]
    noted = "\n".join(texts.values())
    view["bad_at_head"] = [
        {**job_summary(j), "check": j["id"] in checked, "noted": j["id"] in noted}
        for j in ended
        if j["state"].get("state") in FAILED
    ]
    return view


def unexplained_failures(
    c: Campaign, pid: str, jobs: list[Json], journals: Collection[str] | None = None
) -> list[Json]:
    """The quest's jobs that failed at its current code and that no journal of it names: what the map
    marks 🔥 and the sidebar counts."""
    ended = [j for j in ended_at_head(c, pid, jobs) if j["state"]["state"] in FAILED]
    if not ended:
        return []
    if journals is None:
        journals = [journal_text(c, name, branch) for name, branch, _, _ in branches(c, pid)]
    noted = "\n".join(journals)
    return [j for j in ended if j["id"] not in noted]


def ended_at_head(c: Campaign, pid: str, jobs: list[Json]) -> list[Json]:
    """The quest's finished jobs at the code its branches have now; failures count only there, older
    ones are history (`campaign job list`)."""
    p = c.quests[pid]
    if p["status"] not in FLIGHT:
        return []
    repo = c.repo(p)
    heads = {code_tree(repo, rev(repo, b)) for _, b, _, _ in branches(c, pid) if has_ref(repo, b)}

    def at_head(j: Json) -> bool:  # read through the quest's repository: one reader for every worktree
        with contextlib.suppress(CampaignError, KeyError):
            return code_tree(repo, j["spec"]["commit"]) in heads
        return False

    mine = (j for j in jobs if j["spec"].get("quest") == pid and j["spec"].get("campaign") == c.ident)
    return [j for j in mine if j["state"].get("state") in TERMINAL and at_head(j)]


def acceptance(acc: Json, now: str) -> str:
    """How an acceptance reads: at its commit, or void when it names no review content (an acceptance
    by an earlier tool, which `land` refuses), with the review's verdict now."""
    if "review" not in acc:
        return f"{now}; its acceptance names no review content, so it is void: accept again"
    return f"accepted at {acc['commit'][:12]}"


def first_paragraph(text: str, limit: int = 400) -> str:
    """A section's first paragraph on one line, without the template's <placeholders>."""
    for para in re.split(r"\n\s*\n", text.strip()):
        line = " ".join(para.split())
        if line and not line.startswith("<"):
            return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"
    return ""


def plan_milestones(c: Campaign) -> list[Json]:
    """PLAN.md's Milestones table in order: each milestone's name, quests and validation."""
    plan = c.home / c.meta / "PLAN.md"
    rows: list[Json] = []
    for line in section(plan.read_text() if plan.exists() else "", "Milestones").splitlines():
        cells = table_cells(line)
        if len(cells) < 2 or not cells[0] or cells[0].startswith("<") or set(cells[0]) <= set("-: "):
            continue
        if cells[0].lower() == "milestone":
            continue
        ids = [x for x in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", cells[1]) if x in c.quests]
        rows.append({"name": cells[0], "quests": ids, "validation": cells[2] if len(cells) > 2 else ""})
    return rows


def quest_map(c: Campaign, pid: str, jobs: list[Json]) -> Json:
    """A quest as the map shows it: its intent (the quest file's Outcome), its result (the journal's
    Result), the documents its landing added to its repository, its criteria and reviews, and the jobs
    that failed at its current code with no journal naming them."""
    p = c.quests[pid]
    repo = c.repo(p)
    wanted = criteria(c, pid)
    final = [x for x in p["landed"] if x.get("final")]
    journals: dict[str, str] = {}
    main = main_branch(repo)
    if final:
        journals = (
            journals_at(c, repo, main, pid)
            or journals_at(c, c.home, main_branch(c.home), pid)
            or journals_at(c, repo, final[-1]["commit"], pid)
        )
    for name, branch, _, _ in branches(c, pid):
        journals.setdefault(name, journal_text(c, name, branch))
    journal = journals.get(pid, "")
    rows: dict[str, list[Json]] = {}
    if final and not final[-1].get("unverified") and not final[-1].get("earlier"):
        met = set(wanted)  # landing required every criterion
    elif final:  # recorded after the fact, or before the campaign: what its journal's evidence proves
        rows = criteria_evidence(c, pid, [final[-1]["commit"], rev(repo, main)], journals, jobs)
        met = {x for x, r in rows.items() if any(e["met"] for e in r)}
    elif p["status"] in ("active", "review") and p.get("branch") and has_ref(repo, p["branch"]):
        tips = [rev(repo, b) for _, b, _, _ in branches(c, pid) if has_ref(repo, b)]
        rows = criteria_evidence(c, pid, tips, journals, jobs)
        met = {x for x, r in rows.items() if any(e["met"] for e in r)}
    else:
        met = set()
    docs: list[str] = []
    if final:
        start = final[-1].get("from") or p.get("base", "")
        if start:
            changed = git(
                repo, "diff", "--name-only", "--diff-filter=AM", start, final[-1]["commit"], "--", "docs", check=False
            )
            prefix = "" if repo == c.home.resolve() else f"{repo.name}:"
            docs = [prefix + x for x in changed.splitlines()]
    reviews = {}
    for lens in p["lenses"]:
        acc = p.get("accepted", {}).get(lens)
        reviews[lens] = acceptance(acc, verdict(c, pid, lens)) if acc else verdict(c, pid, lens)
    blocked = first_paragraph(section(journal, "Blocked on"))
    return {
        "id": pid,
        "title": p["title"],
        "risk": p["risk"],
        "status": p["status"],
        "reason": p.get("reason", ""),
        "milestone": p.get("milestone", ""),
        "needs": [d for d in p["needs"] if not c.landed(d)],
        "landed": {
            "at": final[-1]["at"],
            "commit": final[-1]["commit"],
            "repo": repo.name,
            "how": "landed before the campaign"
            if final[-1].get("earlier")
            else "recorded as landed"
            if final[-1].get("recorded")
            else "landed",
        }
        if final
        else None,
        "outcome": first_paragraph(section(c.quest_file(pid), "Outcome")),
        "result": "\n".join(x for x in section(journal, "Result").strip().splitlines() if not x.startswith("<")),
        "docs": docs,
        "criteria": [
            {"id": x, "text": w["text"], "met": x in met, "listed": bool(rows.get(x))} for x, w in wanted.items()
        ],
        "failing": [
            {"job": j["id"], "state": j["state"]["state"], "what": job_summary(j)["what"]}
            for j in unexplained_failures(c, pid, jobs, journals.values())
        ],
        "reviews": reviews,
        "next": "" if final else next_step(journal),
        "blocked": "" if final or re.match(r"nothing\b", blocked, re.I) else blocked,
    }


MAP_RANK = {"landed": 0, "review": 1, "active": 2, "blocked": 3, "planned": 4}


def milestone_groups(c: Campaign, statuses: dict[str, str]) -> list[tuple[str, str, list[str]]]:
    """(name, validation, quest ids) per milestone among the quests in STATUSES: PLAN.md's rows in
    order, then the other milestones quests name; a row's quests first, then the quests naming it, by
    status. Dropped quests are in none."""
    named = plan_milestones(c)
    rows = {m["name"]: m for m in named}
    names = [m["name"] for m in named] + sorted({c.quests[x].get("milestone", "") for x in statuses} - set(rows) - {""})
    out: list[tuple[str, str, list[str]]] = []
    for name in names:
        row = rows.get(name, {"quests": [], "validation": ""})
        ids = [x for x in row["quests"] if x in statuses] + sorted(
            (x for x in statuses if c.quests[x].get("milestone", "") == name and x not in row["quests"]),
            key=lambda x: (MAP_RANK.get(statuses[x], 5), x),
        )
        out.append((name, row["validation"], [x for x in dict.fromkeys(ids) if statuses[x] != "dropped"]))
    return out


def map_data(c: Campaign, only: str = "") -> Json:
    """The campaign as a map: its goals, its milestones in PLAN.md's order with their quests, the
    quests in no milestone, and the dropped ones."""
    if only:
        c.quest(only)
    jobs = all_jobs()
    with frozen_heads():
        quests = {pid: quest_map(c, pid, jobs) for pid in c.quests if not only or pid == only}
    plan = c.home / c.meta / "PLAN.md"
    goals: list[str] = []
    for line in section(plan.read_text() if plan.exists() else "", "Goals").splitlines():
        text = line.strip()
        if text.startswith(("- ", "* ")):
            goals.append(text[2:])
        elif text and line[:1].isspace() and goals:  # a wrapped goal's next line
            goals[-1] += " " + text
    goals = [g for g in goals if not g.startswith("<")]
    placed: set[str] = set()
    groups: list[Json] = []
    for name, validation, ids in milestone_groups(c, {x: q["status"] for x, q in quests.items()}):
        placed.update(ids)
        if ids or not only:
            groups.append({"name": name, "validation": validation, "quests": [quests[x] for x in ids]})
    rest = sorted(
        (x for x, q in quests.items() if x not in placed and q["status"] != "dropped"),
        key=lambda x: (MAP_RANK.get(quests[x]["status"], 5), x),
    )
    live = [q for q in quests.values() if q["status"] != "dropped"]
    return {
        "campaign": c.state["campaign"],
        "goals": goals,
        "landed": sum(1 for q in live if q["status"] == "landed"),
        "quests": len(live),
        "criteria": sum(len(q["criteria"]) for q in live),
        "met": sum(1 for q in live for x in q["criteria"] if x["met"]),
        "milestones": groups,
        "other": [quests[x] for x in rest],
        "dropped": [q for q in quests.values() if q["status"] == "dropped"],
    }


def quest_mark(q: Json) -> tuple[str, str]:
    """A quest's emoji and color on the map: landed (with criteria left open or not), failing at its
    current code with no journal naming it, or its status."""
    if q["status"] == "landed":
        return ("✅", "green") if all(x["met"] for x in q["criteria"]) else ("⚠️", "yellow")
    if q["failing"]:
        return "🔥", "red"
    tint, emoji = STAGES.get(q["status"], ("dim", "❔"))
    return emoji, tint


MAP_LEGEND = (
    "✅ landed · ⚠️ landed, criteria left open · 🔥 failing · 🔍 in review · 🔨 active · 🚧 blocked · 📋 planned"
)


def milestone_mark(quests: list[Json]) -> tuple[str, str]:
    """A milestone's emoji and color: done, failing, under way, waiting only on blocked quests, or
    planned."""
    statuses = {q["status"] for q in quests}
    if not statuses:
        return "⬜", "dim"
    if any(q["failing"] for q in quests):
        return "🔥", "red"
    if statuses == {"landed"}:
        return "✅", "green"
    if statuses & {"landed", "active", "review"}:
        return "🔨", "cyan"
    return ("🚧", "red") if statuses == {"blocked"} else ("📋", "dim")


def map_text(c: Campaign, only: str = "", color: bool = False) -> str:
    """The map as Markdown: a checkbox per quest, with its intent, result, documents and criteria. Its
    emoji are part of the document (MAP.md shows them); color only with `color`, for a terminal."""
    m = map_data(c, only)

    def p(text: str, *styles: str) -> str:
        return paint(text, *styles) if color else text

    out = [p(f"# {m['campaign']}: map", "bold"), ""]

    def criteria_count(n: int) -> str:
        return f"{n} criterion" if n == 1 else f"{n} criteria"

    if not only:
        out += [f"- 🎯 {g}" for g in m["goals"]] + ([""] if m["goals"] else [])
        out += [
            f"{m['landed']} of {plural(m['quests'], 'quest')} landed, {m['met']} of {criteria_count(m['criteria'])} "
            f"met. Generated by `campaign map` on {stamp()[:10]}; edit the quest files and journals, not this.",
            "",
            p(MAP_LEGEND, "dim"),
            "",
        ]

    def quest(q: Json, full: bool) -> list[str]:
        done = q["status"] == "landed"
        met = sum(1 for x in q["criteria"] if x["met"])
        emoji, tint = quest_mark(q)
        name = p("**" + q["id"] + "**", "bold", tint)
        head = f"- {emoji} {name}: {q['title']} "
        if done:
            left = [x["id"] for x in q["criteria"] if not x["met"]]
            landed = q["landed"]
            head += p(
                f"({q['risk']} risk, {landed['how']} {landed['at'][:10]} at {landed['repo']}:{landed['commit'][:12]}",
                "dim",
            )
            head += (p("; ", "dim") + p(f"left open: {', '.join(left)}", "yellow") if left else "") + p(")", "dim")
        else:
            head += p(f"({q['risk']} risk, {q['status']}, {met} of {criteria_count(len(q['criteria']))} met)", "dim")
        lines = [head]

        def note(label: str, text: str, style: str = "dim") -> None:
            lines.append(f"  - {p(label + ':', style)} {text}")

        if q["outcome"]:
            note("Outcome", q["outcome"])
        if done or q["result"]:
            result = q["result"] if full else first_paragraph(q["result"])
            note("Result", result.replace("\n", "\n    ") if result else "not written")
        if q["docs"]:
            note("Docs", ", ".join(q["docs"]))
        # a landing before the campaign was reviewed by its own process, which its Result names
        shown = {
            k: v for k, v in q["reviews"].items() if not (done and q["landed"]["how"] != "landed" and v == "none yet")
        }
        if shown:
            note("Reviews", ", ".join(f"{lens} {v}" for lens, v in shown.items()))
        if q["needs"]:
            note("Needs", ", ".join(q["needs"]))
        if q["blocked"]:
            note("Blocked on", q["blocked"], "red")
        if q["next"]:
            note("Next", q["next"], "cyan")
        if q["status"] == "blocked" and q["reason"]:
            note("Blocked", q["reason"], "red")
        if q["failing"]:
            note("Failing", "; ".join(f"{f['job']} {f['state']}: {f['what']}" for f in q["failing"]), "red")
        if full or q["status"] in ("active", "review"):
            for x in q["criteria"]:
                text = x["text"] if full else x["text"].split(": ", 1)[0][:100]
                sign = "✅" if x["met"] else "⏳" if x["listed"] else "⬜"
                lines.append(f"  - {sign} {x['id']} {text}")
        return lines

    for g in m["milestones"]:
        n = len(g["quests"])
        k = sum(1 for q in g["quests"] if q["status"] == "landed")
        state = "no quests yet" if not n else "done" if k == n else f"{k} of {n} landed"
        emoji, tint = milestone_mark(g["quests"])
        out += [p(f"## {emoji} {g['name']}: ", "bold") + p(state, "bold", tint), ""]
        if g["validation"] and not g["validation"].startswith("<"):
            out += [f"{p('Validation on main:', 'dim')} {g['validation']}", ""]
        for q in g["quests"]:
            out += quest(q, bool(only))
        out.append("")
    if m["other"]:
        out += (
            ([p("## Not in a milestone", "bold"), ""] if m["milestones"] else [])
            + [line for q in m["other"] for line in quest(q, bool(only))]
            + [""]
        )
    if m["dropped"]:
        out += [p("## Dropped", "bold"), ""]
        out += [f"- 🚫 {p('~~' + q['id'] + '~~', 'dim')}: {q['title']} ({q['reason']})" for q in m["dropped"]] + [""]
    return "\n".join(out).rstrip() + "\n"


def refresh_map(c: Campaign) -> tuple[str, ...]:
    """Rewrites MAP.md when the campaign keeps one; the paths to commit with the state."""
    path = c.home / c.meta / "MAP.md"
    if not path.exists():
        return ()
    atomic_write(path, map_text(c))
    return (f"{c.meta}/MAP.md",)


def cmd_map(a: argparse.Namespace) -> int:
    if a.write:
        with writing() as c:
            path = c.home / c.meta / "MAP.md"
            atomic_write(path, map_text(c))
            c.save("campaign: map", extra=(f"{c.meta}/MAP.md",))
        print(f"wrote {path}; `land` keeps it current")
        return 0
    c = find_campaign(Path.cwd())
    if a.json:
        print(json.dumps(map_data(c, a.id or ""), indent=2))
    else:
        print(map_text(c, a.id or "", color=styled()), end="")
    return 0


def machine_status() -> int:
    for name, o in campaigns().items():
        quests = o["state"].get("quests", {})
        counts = {s: sum(1 for q in quests.values() if q.get("status") == s) for s in STATUSES}
        flying = [q for q, x in quests.items() if x.get("status") in FLIGHT]
        print(
            f"{name}  {o['home']}: "
            + (", ".join(f"{n} {s}" for s, n in counts.items() if n) or "no quests")
            + (f"; in flight: {', '.join(flying)}" if flying else "")
        )
    return 0


STAGES = {  # color and mark per quest status
    "planned": ("dim", "📋"),
    "active": ("cyan", "🔨"),
    "review": ("magenta", "🔍"),
    "blocked": ("red", "🚧"),
    "landed": ("green", "✅"),
    "dropped": ("dim", "🚫"),
}
JOB_MARKS = {
    "done": ("green", "✅"),
    "failed": ("yellow", "❌"),
    "timeout": ("yellow", "⏰"),
    "interrupted": ("yellow", "⚡"),
    "cancelled": ("dim", "🚫"),
    "queued": ("dim", "⏳"),
    "running": ("cyan", "🔄"),
}


def bar(done: float, width: int = 20) -> str:
    """A progress bar of CAMPAIGN_BAR's two characters (default block characters every terminal font
    has); "#-" when the output cannot encode them."""
    done = max(0.0, min(done, 1.0))
    chars = os.environ.get("CAMPAIGN_BAR", "")
    full, empty = (chars[0], chars[1]) if len(chars) == 2 else ("\u2588", "\u2591")
    try:
        (full + empty).encode(sys.stdout.encoding or "ascii")
    except (UnicodeEncodeError, LookupError):
        full, empty = "#", "-"
    n = round(done * width)
    return paint(full * n, "green") + paint(empty * (width - n), "dim") + f" {round(done * 100)}%"


def print_criteria(pid: str, detail: list[Json]) -> None:
    """Each criterion: met, pending (listed without evidence) or unrecorded, with the rows behind it."""
    width = shutil.get_terminal_size((100, 24)).columns

    def fit(text: str, indent: int) -> str:
        return text if indent + len(text) <= width else text[: max(width - indent - 3, 10)] + "..."

    print(f"  {mark('📜')}criteria:")
    pad = max(len(d["id"]) for d in detail)
    lead = "   " if styled() else ""  # under a mark
    for d in detail:
        state = "met" if any(r["met"] for r in d["rows"]) else "pending" if d["rows"] else "no row"
        style, emoji = {"met": ("green", "✅"), "pending": ("yellow", "⏳")}.get(state, ("dim", "⬜"))
        indent = len(lead) + pad + 15
        text = d["text"] if d.get("kind", "job") == "job" else f"[{d['kind']}] {d['text']}"
        print(f"    {mark(emoji)}{d['id']:<{pad}}  {paint(f'{state:<7}', style)}  {fit(text, indent)}")
        for r in d["rows"]:
            where = "journal" if r["journal"] == pid else r["journal"].split("--", 1)[-1]
            line = f"{where}: {r['evidence']}" + (f" ({'; '.join(r['why'])})" if r.get("why") else "")
            print(f"    {lead}{'':<{pad}}  {'':<7}  {fit(line, indent)}")


def task_mark(state: str) -> str:
    first = state.split(maxsplit=1)[0].rstrip(";:,.").lower() if state.strip() else ""
    marks = {
        "complete": "✅",
        "done": "✅",
        "dropped": "🚫",
        "parked": "💤",
        "waiting": "⏳",
        "blocked": "🚧",
    }
    return marks.get(first, "🔨")


def print_quest(v: Json, full: bool) -> None:
    color, emoji = STAGES.get(v["status"], ("dim", "❔"))
    print(f"\n{mark(emoji)}{paint(v['id'], 'bold')}  {paint(v['status'], color)}  {v['risk']}  {v['title']}")
    for h in v.get("weakened", []) if full else []:
        voided = f"; voided {', '.join(h['voided'])}" if h.get("voided") else ""
        print(
            f"  {mark('⚠️')}"
            + paint(f"weakened {h['at'][:16]}: {'; '.join(h['weakened'])}", "yellow")
            + f" ({h['reason']}{voided})"
        )
    g = v.get("progress")
    if g and (g["criteria"] or g["tasks"]):
        tasks = f"{g['tasks_done']} of {plural(g['tasks'], 'task')} complete" if g["tasks"] else ""
        if g["criteria"]:
            n = g["criteria"]
            met = f"{len(g['met'])} of {n} {'criterion' if n == 1 else 'criteria'} with evidence"
            met += f" ({id_ranges(g['met'])})" if g["met"] else ""
            print(f"  {bar(len(g['met']) / g['criteria'])} {met}" + (f" · {tasks}" if tasks else ""))
            if g["unlisted"]:
                print(f"  {mark('📝')}no evidence yet: {id_ranges(g['unlisted'])}")
        else:
            print(f"  {tasks}")
        if g["next"]:
            print(f"  {mark('🧭')}next: {g['next']}")
        if full and g["criteria"]:
            print_criteria(v["id"], g["detail"])
    if "branch" in v:
        print(f"  {mark('🌿')}branch {v['branch']} at {v['head']}, {plural(v['ahead'], 'commit')} ahead of main")
        if v["missing"]:
            print(f"  {mark('🛬')}" + paint("to land:", "yellow"))
            for gap in v["missing"]:
                print(f"    - {gap}")
        else:
            print(f"  {mark('✅')}" + paint(f"ready to land: `campaign land {v['id']}`", "green"))
        checks = [f"{k} {s}" for k, s in v["checks"].items()]
        if v["later"]:
            checks.append("after landing: " + ", ".join(v["later"]))
        if checks:
            print(f"  {mark('🧪')}checks: " + "; ".join(checks))
        tasks = {}
        for name, w in v["journal"].items():
            dirty = " [worktree missing]" if w["dirty"] < 0 else f" [{w['dirty']} uncommitted]" if w["dirty"] else ""
            if name == v["id"]:
                print(f"  {mark('📓')}journal: {w['state']}{dirty}")
            else:  # a task's id is QUEST--TASK
                tasks[name.split("--", 1)[1]] = f"{w['state']}{dirty}"
        if tasks:
            print(f"  {mark('👥')}tasks:")
            for task, state in tasks.items():
                print(f"    {mark(task_mark(state))}{task}: {state}")
    elif v["launchable"]:
        print(f"  ready to start: `campaign start {v['id']}`")
    else:
        for gap in v["missing"]:
            print(f"  {gap}")
    for m in v.get("messages", []):
        if not m["answered"]:
            print(f"  {mark('📨')}" + paint(f"{m['id']} from {m['by']}", "yellow") + f" ({m['at'][:16]}): {m['text']}")
        elif full:
            print(f"  {mark('📨')}{m['id']} from {m['by']}: answered in a journal")
    # a failure is part of iterating; one no journal mentions yet is the one to look at
    bad = v["bad_at_head"]
    unread = [f for f in bad if not f["noted"]]
    counts = [f"{v['done_at_head']} passed"] if v["done_at_head"] else []
    failed = f"{len(bad)} failed"
    counts += [paint(failed, "yellow") if unread else failed] if bad else []
    counts += [f"{n} {k}" for k, n in v["jobs"].items() if n]
    if counts:
        line = f"  {mark('🔬')}jobs at this code: {', '.join(counts)}"
        if unread:
            some = "" if len(unread) == len(bad) else f"{len(unread)} "
            line += f"; {some}not in a journal yet (`campaign job log ID`):"
        elif bad:
            line += " (noted in the journals)"
        print(line)
    for f in unread:
        how = "" if f["state"] == "failed" else f"{f['state']}: "
        print(f"    {mark('👀')}{f['job']}  {how}{f['what']}" + (" (a check)" if f["check"] else ""))
    if full and v["landed"]:
        print(f"  {mark('🏁')}landed: " + ", ".join(v["landed"]))
    if v.get("deferred"):
        print(f"  {mark('⏳')}deferred checks: " + ", ".join(f"{n} {s}" for s, n in v["deferred"].items()))


def here_quest(c: Campaign, cwd: Path) -> str:
    """The quest whose worktree (or a task's) this is; "" elsewhere."""
    branch = git(cwd, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    return next((pid for pid in c.quests if any(b == branch for _, b, _, _ in branches(c, pid))), "")


def brief(c: Campaign, views: list[Json], queue: dict[str, int], scoped: bool) -> Json:
    """Status for agents: per quest, only what to act on. Scoped to one quest, its file set too."""
    out: Json = {"campaign": c.state["campaign"], "home": str(c.home), "jobs": queue}
    if not scoped:
        out["counts"] = overview(c)["counts"]
        out["messages"] = [{k: m[k] for k in ("id", "by", "at", "text")} for m in inbox(c, "")]
    quests: list[Json] = []
    out["quests"] = quests
    for v in views:
        if v["status"] in ("landed", "dropped") and not scoped:
            continue
        q: Json = {"id": v["id"], "status": v["status"], "risk": v["risk"]}
        if "branch" in v:
            q.update(branch=v["branch"], head=v["head"], ahead=v["ahead"])
        if scoped:
            q["files"] = v["files"]
        q["next"] = (
            f"campaign start {v['id']}"
            if v["launchable"]
            else f"campaign land {v['id']}"
            if "branch" in v and not v["missing"]
            else ""
        )
        q["to_land"] = v["missing"]
        q["docs_only"] = v.get("docs_only", [])
        q["messages"] = [
            {k: m[k] for k in ("id", "by", "at", "text")} for m in v.get("messages", []) if not m["answered"]
        ]
        q["failed"] = [{k: f[k] for k in ("job", "state", "what")} for f in v["bad_at_head"] if not f["noted"]]
        q["replaced"] = v["replaced"]
        q["pending"] = {s: k for s, k in v["jobs"].items() if k}
        if "evidence" in v:
            q["evidence"] = v["evidence"]
        quests.append(q)
    return out


def print_brief(b: Json) -> None:
    """The brief as one line per thing to act on, no decoration."""
    if "counts" in b:
        n, jobs = b["counts"], b["jobs"]
        print(
            f"campaign {b['campaign']}: {in_flight(n)}, {n['planned']} planned, {n['landed']} landed; "
            f"jobs {jobs['queued']} queued, {jobs['running']} running"
        )
    for m in b.get("messages", []):
        print(f"- {m['id']} for the lead, from {m['by']}: {m['text']}")
    if not b["quests"] and "counts" in b:
        print("nothing in flight or planned (`campaign new ID --title T` plans a quest)")
    for q in b["quests"]:
        where = f": {q['branch']} at {q['head']}, {plural(q['ahead'], 'commit')} ahead" if "branch" in q else ""
        print(f"{q['id']} {q['status']} {q['risk']}{where}")
        if "files" in q:
            print(f"  files: {', '.join(q['files']) or '(none)'}")
        if q["next"]:
            print(f"  ready to {q['next'].split()[1]}: `{q['next']}`")
        for gap in q["to_land"]:
            print(f"  - {gap}")
        for note in q["docs_only"]:
            print(f"  evidence across docs-only commits: {note}")
        for m in q["messages"]:
            print(f"  - {m['id']} from {m['by']}: {m['text']}")
        for f in q["failed"]:
            print(f"  - {f['job']} {f['state']}, in no journal yet: {f['what']}")
        for r in q["replaced"]:
            print(
                f"  - {r['criteria']} in {r['journal']} cites {r['job']}; {r['by']} replaces it "
                f"({r['state']} at {r['commit']}): cite that one"
            )
        if q["pending"]:
            print(f"  - jobs: {', '.join(f'{k} {s}' for s, k in q['pending'].items())}")
        if len(q.get("evidence", [])) > 2:
            print("  evidence, for the journal's Evidence table:")
            print("\n".join(f"  {row}" for row in q["evidence"]))


def cmd_status(a: argparse.Namespace) -> int:
    if a.all:
        return machine_status()
    with frozen_heads():
        return show_status(a)


def show_status(a: argparse.Namespace) -> int:
    c = find_campaign(Path.cwd())
    reap()
    if not a.id and (a.brief or not a.json):  # in a quest's worktree, that quest
        a.id = here_quest(c, Path.cwd())
    if not a.brief:
        sidebar(c)
    jobs = all_jobs()
    pids = [a.id] if a.id else list(c.quests)
    for pid in pids:
        c.quest(pid)
    views = [quest_view(c, pid, jobs, brief=a.brief, table=a.brief and bool(a.id)) for pid in pids]
    queue = {s: sum(1 for j in jobs if j["state"].get("state") == s) for s in ("queued", "running")}
    if a.brief:
        b = brief(c, views, queue, scoped=bool(a.id))
        if a.json:
            print(json.dumps(b, indent=2))
        else:
            print_brief(b)
        return 0
    found: list[tuple[str, str]] = [] if a.brief else problems(c)
    o = overview(c)
    if a.json:
        print(
            json.dumps(
                {
                    "campaign": c.state["campaign"],
                    "home": str(c.home),
                    "overview": o,
                    "quests": views,
                    "problems": found,
                    "jobs": queue,
                },
                indent=2,
            )
        )
        return 0
    n = o["counts"]
    print(f"{mark('🏰')}{paint('campaign ' + c.state['campaign'], 'bold')} ({c.home})")
    print(f"  quests: {n['landed']} landed, {in_flight(n)}, {n['planned']} planned")
    if n["planned"]:
        print(f"  launchable now: {', '.join(o['launchable']) or 'none'}")
    holders: dict[str, list[str]] = {}
    for k, by in o["held"].items():
        for x in by:
            holders.setdefault(x, []).append(k)
    if holders:
        print("  held by file sets: " + "; ".join(f"{x} holds {', '.join(ks)}" for x, ks in holders.items()))
    if o["hunt"] is not None:
        print(f"  {mark('🔎')}hunt list: {o['hunt']} open ({c.meta}/hunt.md)")
    live = [v for v in views if v["status"] != "dropped"]
    if live and not a.id:
        done = sum(
            1.0 if v["status"] == "landed" else len(v["progress"]["met"]) / v["progress"]["criteria"]
            for v in live
            if v["status"] == "landed" or (v.get("progress") or {}).get("criteria")
        )
        print(f"  {bar(done / len(live))} of the campaign (landed quests, plus criteria with evidence)")
    m = o["milestone"]
    if m and m["name"]:
        print(f"  {mark('🎯')}milestone {m['name']}: {m['landed']} of {plural(m['quests'], 'quest')} landed")
    up = worker_alive()
    worker = f"worker {'up' if up else 'down'}"
    print(
        f"  {mark('🔬')}jobs: {queue['queued']} queued, {queue['running']} running; {mark('⚡' if up else '💤')}{worker}"
    )
    for line in health_lines(queue_health(jobs, time.time())) if not a.id else []:
        print(f"  {line}")
    for m in inbox(c, "") if not a.id else []:
        print(f"  {mark('📨')}" + paint(f"{m['id']} for the lead, from {m['by']}", "yellow") + f": {m['text']}")
    if not views:
        print("  no quests yet (`campaign new ID --title T` plans one)")
    if a.id:
        print_quest(views[0], full=True)
    else:
        for status in ("active", "review", "blocked", "planned"):
            for v in views:
                if v["status"] == status:
                    print_quest(v, full=False)
        landed = [v for v in views if v["status"] == "landed"]
        if landed:
            print(f"\n{mark('🏁')}landed: " + ", ".join(v["id"] for v in landed))
            for v in landed:
                red = {s: k for s, k in v.get("deferred", {}).items() if s != "done"}
                if red:
                    print(f"  {v['id']}: deferred checks: " + ", ".join(f"{k} {s}" for s, k in v["deferred"].items()))
    if found:
        print()
    for severity, message in found:
        print_problem(severity, message)
    return 0


def print_problem(severity: str, message: str) -> None:
    style, emoji = {"error": ("red", "❌"), "warning": ("yellow", "❗")}.get(severity, ("dim", "💬"))
    print(f"{mark(emoji)}{paint(severity, style)}: {message}")


# Usage: read from what the harnesses and jobs already record; agents write nothing for it.
ROLE = re.compile(
    r"You (?:are (?:the |an? )?(?:(?P<lens>\S+) )?(?P<role>contributor|reviewer)\b|(?P<resume>resume) quest )"
)
QUEST = re.compile(r"\bquest (?P<id>[\w.-]+?)[:,.]?(?:\s|$)")
NAMED = re.compile(r"\bcampaign (?P<name>[\w.-]+)(?: \((?P<home>[^)]+)\))?")
ASSIGNMENT = re.compile(r"^Assignment: (\w+)", re.M)


def launch(prompt: str) -> Json | None:
    """The role, quest, assignment and campaign a launch prompt names (references/prompts.md)."""
    for line in prompt.splitlines():
        role, named = ROLE.search(line), NAMED.search(line)
        if role and named:
            quest, given = QUEST.search(line), ASSIGNMENT.search(prompt)
            kind = "contributor" if role["resume"] else role["role"]
            reviewer = kind == "reviewer"
            assignment = given[1] if given and given[1] in ASSIGNMENTS else "implementation"
            return {
                "role": kind,
                "resume": bool(role["resume"]),
                "lens": role["lens"] or "" if reviewer else "",
                "quest": quest["id"] if quest else "-",
                "assignment": "" if reviewer else assignment,
                "name": named["name"],
                "home": named["home"] or "",
            }
    return None


# (usage field, Copilot's name): inputTokens includes the cached reads, as Codex's input_tokens does
COPILOT_TOKENS = (
    ("cached_input_tokens", "cacheReadTokens"),
    ("cache_write_tokens", "cacheWriteTokens"),
    ("output_tokens", "outputTokens"),
    ("reasoning_tokens", "reasoningTokens"),
)
BREAKDOWN = ("cached_input_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens", "requests")


def copilot_runs(since: float) -> tuple[list[Json], list[Json]]:
    """Agent runs and lead sessions from Copilot CLI's session events."""
    root = Path(os.environ.get("CAMPAIGN_COPILOT_LOGS") or Path.home() / ".copilot" / "session-state")
    runs: list[Json] = []
    sessions: list[Json] = []
    for log in sorted(root.glob("*/events.jsonl")):
        if log.stat().st_mtime < since:
            continue
        prompts: dict[str, str] = {}
        agents: dict[str, Json] = {}
        lead: Json = {
            "harness": "copilot",
            "session": log.parent.name,
            "cost": 0.0,
            "tokens": 0,
            "cost_available": True,
        }
        with log.open(errors="replace") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                t, d = e.get("type", ""), e.get("data") or {}
                if t == "tool.execution_start" and d.get("toolName") == "task":
                    prompts[d.get("toolCallId", "")] = str((d.get("arguments") or {}).get("prompt", ""))
                    continue
                if t == "session.usage_record":
                    u = d.get("usage") or {}
                    who = agents.setdefault(e["agentId"], {}) if e.get("agentId") else lead
                    who["cost"] = who.get("cost", 0.0) + float(u.get("cost") or 0)
                    who["tokens"] = (
                        who.get("tokens", 0) + int(u.get("inputTokens") or 0) + int(u.get("outputTokens") or 0)
                    )
                    for field, key in COPILOT_TOKENS:
                        who[field] = who.get(field, 0) + int(u.get(key) or 0)
                    who["requests"] = who.get("requests", 0) + 1
                    continue
                if not t.startswith("subagent.") or not e.get("agentId"):
                    continue
                a = agents.setdefault(e["agentId"], {})
                a.setdefault("began", e.get("timestamp", ""))
                a["call"] = d.get("toolCallId") or a.get("call", "")
                a["type"] = d.get("agentName") or a.get("type", "")
                a["model"] = d.get("model") or a.get("model", "")
                a["effort"] = d.get("reasoningEffort") or a.get("effort", "")
                if t == "subagent.completed":
                    a["seconds"] = a.get("seconds", 0.0) + float(d.get("durationMs") or 0) / 1000
                    a["tool_calls"] = a.get("tool_calls", 0) + int(d.get("totalToolCalls") or 0)
        mine = 0
        for aid, a in agents.items():
            what = launch(prompts.get(a.get("call", ""), ""))
            if what is None:
                continue
            mine += 1
            runs.append(
                {
                    "harness": "copilot",
                    "session": log.parent.name,
                    "agent": aid,
                    **what,
                    **{k: a.get(k, d) for k, d in (("type", ""), ("model", ""), ("effort", ""), ("began", ""))},
                    **{k: a.get(k, 0) for k in ("tokens", "cost", "seconds", "tool_calls", *BREAKDOWN)},
                    "cost_available": True,
                }
            )
        if mine:
            sessions.append(lead)
    return runs, sessions


def codex_runs(since: float) -> tuple[list[Json], list[Json]]:
    """Read thread-local cumulative usage; cached/reasoning tokens are subsets, not additions."""
    root = Path(
        os.environ.get("CAMPAIGN_CODEX_LOGS")
        or Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "sessions"
    )
    threads: dict[str, Json] = {}
    launches: dict[tuple[str, str], str] = {}

    def timestamp(value: str) -> float:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            return 0.0

    for log in sorted(root.rglob("rollout-*.jsonl")):
        if log.stat().st_mtime < since:
            continue
        meta: Json = {}
        what: Json | None = None
        model, effort, began, ended = "", "", "", ""
        records: dict[str, Json] = {}
        cumulative: Json = {}
        turns: dict[str, Json] = {}
        starts: dict[str, float] = {}
        durations: dict[tuple[str, str], float] = {}
        calls = 0
        with log.open(errors="replace") as f:
            for line in f:
                if not any(
                    key in line
                    for key in (
                        '"session_meta"',
                        '"turn_context"',
                        '"token_usage_record"',
                        '"token_count"',
                        '"task_started"',
                        '"task_complete"',
                        '"function_call"',
                        '"agent_message"',
                        '"role":"user"',
                        '"role": "user"',
                    )
                ):
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(e, dict) or not isinstance(e.get("payload"), dict):
                    continue
                kind, d, at = e.get("type"), e["payload"], e.get("timestamp", "")
                if kind == "session_meta":
                    meta = d
                    began = d.get("timestamp") or at
                elif kind == "turn_context":
                    model = d.get("model") or model
                    effort = d.get("effort") or d.get("reasoning_effort") or effort
                    turns[d.get("turn_id", "")] = {"model": model, "effort": effort}
                elif kind == "token_usage_record":
                    # A response may be recorded again after a retry; replace, never sum it twice.
                    ident = d.get("response_id")
                    if ident:
                        records[ident] = {
                            "usage": d.get("usage") or {},
                            "model": model,
                            "effort": effort,
                            "turn": d.get("turn_id", ""),
                            "at": at,
                        }
                    ended = at
                elif kind == "event_msg":
                    if d.get("type") == "token_count" and isinstance(d.get("info"), dict):
                        cumulative = d["info"].get("total_token_usage") or cumulative
                        ended = at
                    elif d.get("type") == "task_started":
                        starts[d.get("turn_id", "")] = timestamp(at)
                    elif d.get("type") == "task_complete":
                        turn = d.get("turn_id", "")
                        config = turns.get(turn, {"model": model, "effort": effort})
                        key = (config["model"], config["effort"])
                        durations[key] = durations.get(key, 0.0) + max(
                            0.0, timestamp(at) - starts.pop(turn, timestamp(at))
                        )
                        ended = at
                elif kind == "response_item":
                    if d.get("type") == "function_call":
                        calls += 1
                        if d.get("name", "").split(".")[-1] == "spawn_agent":
                            try:
                                args = json.loads(d.get("arguments") or "{}")
                            except (ValueError, TypeError):
                                continue
                            if isinstance(args, dict):
                                launches[(meta.get("id", ""), args.get("task_name", ""))] = args.get(
                                    "message", args.get("prompt", "")
                                )
                    elif what is None and (d.get("type") == "agent_message" or d.get("role") == "user"):
                        prompt = "".join(c.get("text", "") for c in d.get("content", []) if isinstance(c, dict))
                        what = launch(prompt)
        ident = meta.get("id")
        if not ident:
            continue
        groups: dict[tuple[str, str], Json] = {}
        for record in records.values():
            config = turns.get(record["turn"], record)
            key = (config["model"], config["effort"])
            g = groups.setdefault(
                key, {"tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}
            )
            u = record["usage"]
            g["tokens"] += int(
                u.get("total_tokens", int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0)) or 0
            )
            for field in ("cached_input_tokens", "output_tokens"):
                g[field] += int(u.get(field) or 0)
            g["reasoning_tokens"] += int(u.get("reasoning_output_tokens") or 0)
            g["requests"] = g.get("requests", 0) + 1
        if not records:
            groups[(model, effort)] = {
                "tokens": int(
                    cumulative.get(
                        "total_tokens",
                        int(cumulative.get("input_tokens") or 0) + int(cumulative.get("output_tokens") or 0),
                    )
                    or 0
                ),
                "cached_input_tokens": int(cumulative.get("cached_input_tokens") or 0),
                "output_tokens": int(cumulative.get("output_tokens") or 0),
                "reasoning_tokens": int(cumulative.get("reasoning_output_tokens") or 0),
            }
        for turn, start in starts.items():
            config = turns.get(turn, {"model": model, "effort": effort})
            key = (config["model"], config["effort"])
            durations[key] = durations.get(key, 0.0) + max(0.0, timestamp(ended) - start)
        source = meta.get("source") or {}
        spawn: Json = source.get("subagent", {}).get("thread_spawn", {}) if isinstance(source, dict) else {}
        threads[ident] = {
            "meta": meta,
            "parent": meta.get("parent_thread_id") or spawn.get("parent_thread_id", ""),
            "path": meta.get("agent_path") or spawn.get("agent_path", ""),
            "what": what,
            "groups": groups,
            "durations": durations,
            "began": began,
            "ended": ended,
            "calls": calls,
        }
    runs: list[Json] = []
    sessions: list[Json] = []
    for ident, t in threads.items():
        meta, parent = t["meta"], t["parent"]
        sid = meta.get("session_id") or parent or ident
        attribution: Json = {}
        if parent:
            path = t["path"]
            # This installation encrypts agent messages, but plaintext harnesses retain attribution.
            prompt = launches.get((parent, path), launches.get((parent, path.rsplit("/", 1)[-1]), ""))
            what = launch(prompt) or t["what"]
            if what is None:
                what = {
                    "name": "",
                    "home": meta.get("cwd", ""),
                    "quest": "-",
                    "role": "unattributed",
                    "assignment": "",
                    "lens": "",
                }
            attribution = what
        for (model, effort), totals in t["groups"].items():
            row = {
                "harness": "codex",
                "session": sid,
                "agent": ident,
                "path": t["path"],
                "model": model,
                "effort": effort,
                "began": t["began"],
                "ended": t["ended"],
                "cost": 0.0,
                "cost_available": False,
                "tool_calls": t["calls"],
                **totals,
                "seconds": t["durations"].get((model, effort), max(0.0, timestamp(t["ended"]) - timestamp(t["began"]))),
            }
            if parent:
                runs.append({**row, **attribution})
            else:
                sessions.append({**row, "session": ident, "home": meta.get("cwd", "")})
    return runs, sessions


def claude_runs(since: float) -> tuple[list[Json], list[Json]]:
    """Agent runs and lead sessions from Claude Code's transcripts: `<project>/<session>.jsonl` is the
    lead, `<project>/<session>/subagents/agent-<id>.jsonl` (with its `.meta.json`) each agent. A
    response's content blocks are logged as separate lines with one message id: each id counts once."""
    root = Path(os.environ.get("CAMPAIGN_CLAUDE_LOGS") or Path.home() / ".claude" / "projects")

    def stamp_of(value: str) -> float:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError):
            return 0.0

    def read(log: Path) -> Json:
        """Totals of the responses logged at or after `since` (a lead session may predate it)."""
        seen: dict[str, Json] = {}
        t: Json = {"prompt": "", "cwd": "", "model": "", "effort": "", "began": "", "ended": "", "calls": 0}
        with log.open(errors="replace") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                kind, stamp = e.get("type"), e.get("timestamp") or ""
                if stamp:
                    t["began"] = t["began"] or stamp
                    t["ended"] = stamp
                t["cwd"] = t["cwd"] or e.get("cwd") or ""
                if kind == "api-request":
                    effort = ((e.get("params") or {}).get("output_config") or {}).get("effort")
                    t["effort"] = effort or t["effort"]
                    continue
                m = e.get("message") if isinstance(e.get("message"), dict) else {}
                if kind == "user" and not t["prompt"]:
                    body = m.get("content")
                    t["prompt"] = body if isinstance(body, str) else ""
                if kind != "assistant":
                    continue
                t["model"] = m.get("model") or t["model"]
                content: list[Json] = m["content"] if isinstance(m.get("content"), list) else []
                t["calls"] += sum(1 for b in content if isinstance(b, dict) and b.get("type") == "tool_use")
                if m.get("usage") and stamp_of(stamp) >= since:
                    seen[m.get("id") or f"line{len(seen)}"] = m["usage"]
        totals = dict.fromkeys(("tokens", *BREAKDOWN), 0)
        for u in seen.values():
            fresh, made = int(u.get("input_tokens") or 0), int(u.get("cache_creation_input_tokens") or 0)
            cached, out = int(u.get("cache_read_input_tokens") or 0), int(u.get("output_tokens") or 0)
            totals["tokens"] += fresh + made + cached + out
            totals["cached_input_tokens"] += cached
            totals["cache_write_tokens"] += made
            totals["output_tokens"] += out
            totals["reasoning_tokens"] += int((u.get("output_tokens_details") or {}).get("thinking_tokens") or 0)
        totals["requests"] = len(seen)
        return {**t, **totals}

    def seconds(t: Json) -> float:
        began = max(stamp_of(t["began"]), since)
        return max(0.0, stamp_of(t["ended"]) - began) if t["ended"] else 0.0

    runs: list[Json] = []
    sessions: list[Json] = []
    for lead_log in sorted(root.glob("*/*.jsonl")):
        agents = sorted((lead_log.parent / lead_log.stem / "subagents").glob("agent-*.jsonl"))
        fresh = [a for a in agents if a.stat().st_mtime >= since]
        if lead_log.stat().st_mtime < since and not fresh:
            continue
        mine = 0
        for log in fresh:
            t = read(log)
            what = launch(t["prompt"])
            if what is None:
                continue
            mine += 1
            meta: Json
            try:
                meta = json.loads(log.with_suffix(".meta.json").read_text())
            except (OSError, ValueError):
                meta = {}
            runs.append(
                {
                    "harness": "claude",
                    "session": lead_log.stem,
                    "agent": log.stem.removeprefix("agent-"),
                    **what,
                    "type": meta.get("agentType", ""),
                    "model": t["model"] or meta.get("pickedEffortModel") or meta.get("model", ""),
                    "effort": t["effort"],
                    "began": t["began"],
                    "ended": t["ended"],
                    "seconds": seconds(t),
                    "tool_calls": t["calls"],
                    "cost": 0.0,
                    "cost_available": False,
                    **{k: t[k] for k in ("tokens", *BREAKDOWN)},
                }
            )
        if mine or lead_log.stat().st_mtime >= since:
            t = read(lead_log)
            sessions.append(
                {
                    "harness": "claude",
                    "session": lead_log.stem,
                    "home": t["cwd"],
                    "model": t["model"],
                    "effort": t["effort"],
                    "began": t["began"],
                    "ended": t["ended"],
                    "seconds": seconds(t),
                    "cost": 0.0,
                    "cost_available": False,
                    **{k: t[k] for k in ("tokens", *BREAKDOWN)},
                }
            )
    return runs, sessions


USAGE_READERS = {
    "claude": claude_runs,
    "copilot": copilot_runs,
    "codex": codex_runs,
}  # harness -> reader(since) -> (agent runs, lead sessions)


def task_attribution(c: Campaign, task: str) -> Json | None:
    """Role, quest and assignment or lens from a launch's task name, for harnesses that keep only the
    name in plaintext (Codex, whose names allow only lowercase letters, digits and underscores):
    `<role>_<quest>[_<assignment or lens>]`, `_` read as `-`. A name counts toward a quest when it
    holds one of the campaign's quest or task ids as whole words (the longest wins); a lens marks a
    reviewer, and a quest or task without one its contributor. Parts of words are never guessed at."""
    words = task.lower().replace("_", "-").split("-")  # "--" leaves an empty word, as in task ids
    role = words[0] if words[0] in ROLES else ""
    lens = next((w for w in words if w in LENSES), "")
    given = next((w for w in words[1:] if w in ASSIGNMENTS), "")

    def holds(ident: str) -> bool:
        part = ident.split("-")
        return any(words[i : i + len(part)] == part for i in range(len(words) - len(part) + 1))

    found: dict[str, int] = {}
    for pid, q in c.quests.items():
        for ident in [pid, *(f"{pid}--{t}" for t in q.get("tasks", {})), *q.get("tasks", {})]:
            if holds(ident):
                found[pid] = max(found.get(pid, 0), len(ident.split("-")))
    best = max(found.values(), default=0)
    quests = [pid for pid, n in found.items() if n == best]
    quest = quests[0] if len(quests) == 1 else "-"
    role = role or ("reviewer" if lens else "contributor" if quest != "-" else "")
    if not role:
        return None
    reviewer = role == "reviewer"
    assignment = "" if reviewer else given or "implementation"
    return {"role": role, "quest": quest, "assignment": assignment, "lens": lens if reviewer else ""}


def usage(c: Campaign) -> Json:
    created = git(c.home, "log", "--follow", "--diff-filter=A", "--format=%ct", "--", f"{c.meta}/state.json").split()
    since = float(created[-1]) if created else 0.0
    runs: list[Json] = []
    sessions: list[Json] = []
    for reader in USAGE_READERS.values():
        r, s = reader(since)
        runs += r
        sessions += s
    home = c.home.resolve()
    runs = [
        r
        for r in runs
        if r["name"] == c.state["campaign"] or (r["home"] and Path(r["home"]).expanduser().resolve() == home)
    ]
    for r in runs:
        r["task"] = str(r.get("path", "")).rsplit("/", 1)[-1]
        if r["role"] == "unattributed" and r["task"]:
            r.update(task_attribution(c, r["task"]) or {})
    related = {(r["harness"], r["session"]) for r in runs}
    sessions = [
        s
        for s in sessions
        if (s["harness"], s["session"]) in related or (s.get("home") and Path(s["home"]).expanduser().resolve() == home)
    ]
    jobs: list[Json] = []
    for j in all_jobs():
        if j["spec"].get("campaign") != c.ident:
            continue
        st, sp = j["state"], j["spec"]
        jobs.append(
            {
                "id": j["id"],
                "quest": sp.get("quest") or "-",
                "level": sp.get("level", ""),
                "state": st.get("state", ""),
                "waited": max(0.0, st["began"] - sp["submitted"]) if st.get("began") and sp.get("submitted") else 0.0,
                "ran": max(0.0, st["ended"] - st["began"]) if st.get("ended") and st.get("began") else 0.0,
            }
        )
    return {"campaign": c.state["campaign"], "since": since, "runs": runs, "lead": sessions, "jobs": jobs}


def amount(n: float) -> str:
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e4 else f"{n:,.0f}"


def span(seconds: float) -> str:
    m = round(seconds / 60)
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def cost_of(rows: list[Json]) -> str:
    """The rows' cost in their harness's unit; "n/a" for what a harness does not report, never 0."""
    known = [r for r in rows if r.get("cost_available", True)]
    text = amount(sum(r["cost"] for r in known)) if known else ""
    return text + ("+n/a" if text and len(known) < len(rows) else "" if text else "n/a")


def cached_share(rows: list[Json]) -> str:
    tokens = sum(r["tokens"] for r in rows)
    return f"{round(100 * sum(r.get('cached_input_tokens', 0) for r in rows) / tokens)}%" if tokens else "-"


def cmd_usage(a: argparse.Namespace) -> int:
    u = usage(find_campaign(Path.cwd()))
    if a.json:
        print(json.dumps(u, indent=2))
        return 0
    runs, jobs = u["runs"], u["jobs"]

    def table(title: str, key: Callable[[Json], str]) -> None:
        groups: dict[str, list[Json]] = {}
        for r in runs:
            groups.setdefault(key(r), []).append(r)
        width = max(len(title), *map(len, groups))
        print("\n" + paint(f"{title:<{width}} runs  tokens cached  output    cost  agent time", "bold"))
        for k, rs in sorted(groups.items(), key=lambda x: (-sum(r["tokens"] for r in x[1]), x[0])):
            tokens, out = sum(r["tokens"] for r in rs), sum(r.get("output_tokens", 0) for r in rs)
            print(
                f"{k:<{width}} {len(rs):>4} {amount(tokens):>7} {cached_share(rs):>6} {amount(out):>7} "
                f"{cost_of(rs):>7}  {span(sum(r['seconds'] for r in rs)):>10}"
            )

    when = time.strftime("%Y-%m-%d", time.localtime(u["since"]))
    print(
        f"{mark('📊')}{paint('campaign ' + u['campaign'], 'bold')} since {when}: {len(runs)} agent runs, "
        f"{amount(sum(r['tokens'] for r in runs))} tokens ({cached_share(runs)} cached input), cost {cost_of(runs)}; "
        f"{len(jobs)} jobs ran {span(sum(j['ran'] for j in jobs))} and waited {span(sum(j['waited'] for j in jobs))}"
    )
    if runs:
        table("by quest", lambda r: "(unattributed)" if r["quest"] == "-" else r["quest"])
        table(
            "by role, assignment, model and effort",
            lambda r: " ".join(
                x
                for x in (
                    r["role"],
                    "resume" if r.get("resume") else "",
                    r["lens"],
                    r["assignment"],
                    r["task"] if r["role"] == "unattributed" else "",
                    r["model"],
                    r["effort"],
                )
                if x
            ),
        )
        resumed = [r for r in runs if r.get("resume")]
        if resumed:
            print(
                f"\nrecovery: {plural(len(resumed), 'fresh resume')}, {amount(sum(r['tokens'] for r in resumed))} tokens"
            )
    if jobs:
        quests = sorted({j["quest"] for j in jobs})
        labels = {q: "(no quest)" if q == "-" else q for q in quests}
        width = max(len("jobs by quest"), *map(len, labels.values()))
        print("\n" + paint(f"{'jobs by quest':<{width}}  jobs     ran  waited", "bold"))
        for q in quests:
            js = [j for j in jobs if j["quest"] == q]
            print(
                f"{labels[q]:<{width}}  {len(js):>4} {span(sum(j['ran'] for j in js)):>7} {span(sum(j['waited'] for j in js)):>7}"
            )
    for s in u["lead"]:
        print(
            f"\nlead session {s['harness']} {s['session'][:8]}: {amount(s['tokens'])} tokens "
            f"({cached_share([s])} cached input), cost {cost_of([s])} (the whole session, which may include other work)"
        )
    print(
        "\n"
        + paint(
            f"read: {', '.join(USAGE_READERS)}. Tokens are cumulative per request (input, cached reads included, plus "
            "output): a measure of work, not of billing. Cost is in each harness's own unit; Claude Code and "
            "Codex record none (n/a, never 0).",
            "dim",
        )
    )
    return 0


def cmd_check(a: argparse.Namespace) -> int:
    home, pinned, _ = locate(Path.cwd())
    path = home / select(home, os.environ.get("CAMPAIGN") or pinned)[1] / "state.json"
    data = read_json(path)
    if data is None:
        die(f"{path} does not exist")
    errors = validate(data)
    for e in errors:
        print_problem("error", e)
    if errors:
        return 1
    reap()
    c = Campaign(home)
    found = problems(c)
    notes = [*oversized_checks(c), *housekeeping(c), *(("warning", text) for text, _ in stuck_warnings())]
    notes += stalls(all_jobs(), time.time())
    for severity, message in [*found, *notes]:
        print_problem(severity, message)
    if not any(s != "note" for s, _ in [*found, *notes]):
        print(f"{mark('✅')}{paint('ok', 'green')}")
    return 1 if any(s == "error" for s, _ in found) else 0


LIGHT_BUILD_JOBS = int(os.environ.get("CAMPAIGN_LIGHT_BUILD_JOBS") or 4)  # per light job; several run at once
PARALLEL = re.compile(r"(?:\s-j\s*|--parallel[=\s]+)(\d+)")


def oversized_checks(c: Campaign) -> list[tuple[str, str]]:
    """Light checks of live quests that ask for more build jobs than a light hold allows: several light
    jobs run at once, so each keeps to LIGHT_BUILD_JOBS."""
    out: list[tuple[str, str]] = []
    for pid, p in c.quests.items():
        if p["status"] in ("landed", "dropped"):
            continue
        for kind in ("checks", "later"):
            for check in p.get(kind, []):
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


def quest_slot(c: Campaign, pid: str, repo: Path) -> str:
    """The build slot of a quest's jobs in REPO; the one in the quest's own repository is verify's."""
    name = f"{c.state['campaign']}-{pid}"
    return name if same_repo(repo, c.repo(c.quests[pid])) else f"{name}@{repo.name}"


def submit_checks(
    c: Campaign, qid: str, checks: list[Json], commit: str, *, priority: int, fresh: bool = False, force: bool = False
) -> list[str]:
    p = c.quests[qid]
    repo = c.repo(p)
    ids: list[str] = []
    for check in checks:
        if check["cmd"].startswith(CI_CHECK):
            continue  # GitHub runs it when the branch is pushed
        spec: Json = {
            "repo": str(repo),
            "commit": commit,
            "refs": {},
            "cwd": "",
            "cmd": check["cmd"],
            "level": check["level"],
            "minutes": check["minutes"],
            "env": {},
            "quest": qid,
            "campaign": c.ident,
            "what": f"{qid} {'deferred ' if check.get('later') else ''}check {check['name']} at {commit[:12]}",
            "slot": "" if fresh or p["risk"] == "critical" else quest_slot(c, qid, repo),
            "cmux": os.environ.get("CMUX_WORKSPACE_ID", ""),
            "priority": priority,
            "retries": 1,
            **({"inputs": check["inputs"]} if check.get("inputs") else {}),
        }
        exact = build_test(check)
        earlier = None if force else job_at(spec, commit, exact=exact, merged=ci_covers(c) or not exact)
        if earlier is not None and docs_note(earlier) and not earlier["state"].get("slimmed"):
            print(f"{check['name']}: {earlier['id']} (reused: {docs_note(earlier)})")
            ids.append(earlier["id"])
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
    for jid in submit_checks(c, a.id, checks, commit, priority=1, fresh=a.fresh, force=a.force):
        if ids:
            tag(jid, c.ident, a.id, ids, False)
    print(f"wait for them with `campaign wait --quest {a.id}`")
    return 0


# ---------------------------------------------------------------- jobs


def jobs_dir() -> Path:
    d = Path(os.environ.get("CAMPAIGN_JOBS") or Path.home() / ".scratch" / "campaign" / "jobs")
    d.mkdir(parents=True, exist_ok=True)
    return d


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
    ]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]


def load_job(d: Path) -> Json:
    def tolerant(path: Path) -> Json:
        try:
            return read_json(path) or {}
        except CampaignError:
            return {"state": "corrupt"}

    job = {"id": d.name, "dir": d, "spec": tolerant(d / "spec.json"), "state": tolerant(d / "state.json")}
    if (d / "for.json").exists():
        job["tags"] = (read_json(d / "for.json") or {}).get("tags", [])
    return job


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


def attempt_dir(job: Json) -> Path:
    return job["dir"] / f"a{int(job['state'].get('attempt', 1))}"


def all_jobs() -> list[Json]:
    return [load_job(d) for d in sorted(jobs_dir().iterdir()) if d.is_dir() and (d / "spec.json").exists()]


def job_for_check(repo: Path, commit: str, check: Json, tested: Sequence[str] = (), ci: bool = False) -> Json | None:
    """The job of a quest check at COMMIT's code. The build-test needs the exact code, documentation
    aside, unless CI at the exact commit (CI) tests the merge with main; every other check counts
    across merges from main."""
    nothing: Json = {}
    spec: Json = {
        "repo": str(repo),
        "commit": commit,
        "refs": nothing,
        "cmd": check["cmd"],
        "cwd": "",
        "level": check["level"],
        "env": nothing,
        **({"inputs": check["inputs"]} if check.get("inputs") else {}),
    }
    exact = build_test(check)
    return job_at(spec, commit, tested, exact=exact, merged=ci or not exact)


def build_test(check: Json) -> bool:
    """Whether a check is the build-test, a gate check without inputs: `land` needs it at the exact code."""
    return not check.get("later") and not check.get("inputs")


DOC_PATHS = ("docs/**", "*.md")  # documentation: evidence outlives commits that change only these
DOCS_DEPTH = 20  # how many commits back evidence is looked for across documentation-only commits


def doc_path(path: str, cmd: str, tested: Sequence[str] = ()) -> bool:
    """Whether PATH is documentation that the command CMD does not name, itself or by a folder, and
    that no TESTED glob (config `tested_docs`: docs that tests execute) matches."""
    folders = [path[: i + 1] for i, ch in enumerate(path) if ch == "/"]
    named = path in cmd or any(f in cmd for f in folders if f.startswith("docs/"))
    if named or any(fnmatch.fnmatchcase(path, g) for g in tested):
        return False
    return any(fnmatch.fnmatchcase(path, g) for g in DOC_PATHS)


def tested_docs(spec: Json) -> list[str]:
    """The `tested_docs` globs of the campaign that submitted SPEC (its "campaign" folder): the
    documentation its tests execute."""
    folder = spec.get("campaign")
    state = read_json(Path(folder) / "state.json") if folder and (Path(folder) / "state.json").is_file() else None
    return list(state.get("tested_docs", [])) if isinstance(state, dict) else []


def docs_only_ancestors(repo: Path, commit: str, cmd: str, tested: Sequence[str] = ()) -> list[tuple[str, list[str]]]:
    """COMMIT's first-parent ancestors whose code differs from COMMIT's only in documentation CMD does
    not name and no TESTED glob matches, nearest first, each with the paths that differ."""
    out: list[tuple[str, list[str]]] = []
    ignored: set[str] = set()
    newer = commit
    line = on_commits(
        ("first-parents", str(repo), commit),
        lambda: git(repo, "rev-list", "--first-parent", f"--max-count={DOCS_DEPTH + 1}", commit).split(),
    )
    for older in line[1:]:
        paths = [x for x in changed(repo, older, newer) if not is_meta(x)]
        if not all(doc_path(x, cmd, tested) for x in paths):
            break
        ignored.update(paths)
        out.append((older, sorted(ignored)))
        newer = older
    return out


def job_at(
    spec: Json, commit: str, tested: Sequence[str] | None = None, exact: bool = False, merged: bool = False
) -> Json | None:
    """The job with SPEC's identity at COMMIT's code; when none passed there, the one that passed at
    the nearest ancestor whose code differs only in documentation the command does not name, with
    "at" (that commit) and "docs" (the paths it ignores); with MERGED, at an ancestor that differs
    only by merges from main, with "at" and "merged"; unless EXACT, then its read_twin."""
    found = job_with_key({**spec, "commit": commit})
    if found is not None and found["state"].get("state") == "done":
        return found
    globs = tested_docs(spec) if tested is None else tested
    for older, paths in docs_only_ancestors(Path(spec["repo"]), commit, spec["cmd"], globs):
        twin = job_with_key({**spec, "commit": older})
        if twin is not None and twin["state"].get("state") == "done":
            return {**twin, "at": older, "docs": paths}
    for older in merged_ancestors(Path(spec["repo"]), commit, spec["cmd"], globs) if merged else []:
        twin = job_with_key({**spec, "commit": older})
        if twin is not None and twin["state"].get("state") == "done":
            return {**twin, "at": older, "merged": True}
    return (None if exact else read_twin(spec, commit)) or found


def own_code(repo: Path, commit: str, cmd: str, tested: Sequence[str]) -> dict[str, str]:
    """The quest's own code at COMMIT: the paths it changes against its merge base with main, with
    their blobs, documentation CMD does not name aside."""
    main_head = rev(repo, main_branch(repo))
    own: dict[str, str] = on_commits(("own", str(repo), main_head, commit), lambda: own_blobs(repo, main_head, commit))
    return {x: b for x, b in own.items() if not doc_path(x, cmd, tested)}


def merged_ancestors(repo: Path, commit: str, cmd: str, tested: Sequence[str] = ()) -> list[str]:
    """COMMIT's nearest first-parent ancestors whose own code (own_code) is COMMIT's: what lies between
    them came from main. CI at COMMIT tests that integration; their evidence stands for COMMIT's."""
    line = on_commits(
        ("first-parents", str(repo), commit),
        lambda: git(repo, "rev-list", "--first-parent", f"--max-count={DOCS_DEPTH + 1}", commit).split(),
    )
    main_head = rev(repo, main_branch(repo))
    own = own_code(repo, commit, cmd, tested)
    out: list[str] = []
    for older in line[1:]:
        if ancestor(repo, older, main_head) or own_code(repo, older, cmd, tested) != own:
            break  # main's own history, or the quest's code changed there
        out.append(older)
    return out


def docs_note(job: Json) -> str:
    """How a job found at other code counts: "" for one at the exact code."""
    if job.get("docs"):
        return f"passed at {job['at'][:12]}; ignores docs-only {', '.join(job['docs'])}"
    if job.get("reads"):
        return f"passed at {job['at'][:12]}; the {job['reads']} files it read are unchanged"
    if job.get("merged"):
        return f"passed at {job['at'][:12]}; only merges from main since"
    return ""


def job_with_key(spec: Json) -> Json | None:
    """The job with this spec's identity (code, command, toolchain), if one was submitted: alone,
    or queued after others (filed as KEY-ORDER), the one that passed first."""
    keys = jobs_dir() / "keys"
    key = job_key(spec)
    found = []
    for link in [keys / key, *keys.glob(f"{key}-*")]:
        d = jobs_dir() / os.readlink(link) if link.is_symlink() else None
        if d is not None and (d / "spec.json").exists():
            found.append(load_job(d))
    return next((j for j in found if j["state"].get("state") == "done"), found[0] if found else None)


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
    if key and spec["quest"] and not (force or exact or order):
        twin = job_at(spec, spec["commit"], merged=True)
        twin = twin if twin is not None and twin["state"].get("state") == "done" else None
    elif key and not (force or exact or order):
        twin = read_twin(spec, spec["commit"])
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
    return json.dumps([*(spec.get(k) for k in fields), refs, env])


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


def same_role(old: Json, new: Json) -> bool:
    """Whether two jobs are one piece of evidence at other code though their commands differ: the
    same label, repository and level, tagged for a shared criterion of the same quest."""

    def role(j: Json) -> set[tuple[Any, ...]]:
        return {
            (t.get("campaign"), t.get("quest"), cid, t.get("fails_without"))
            for t in j.get("tags", [])
            for cid in t.get("for", [])
        }

    def label(j: Json) -> str:
        return re.sub(r" at [0-9a-f]{7,40}$", "", str(j["spec"].get("what", "")))

    keys = ("repo", "level")
    return (
        bool(role(old) & role(new))
        and label(old) == label(new) != ""
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
            [sys.executable, str(Path(__file__).resolve()), *args],
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


def kill_group(child: subprocess.Popen[bytes], beat: Path) -> None:
    """TERMs a job's process group, then KILLs it if it outlives 20 seconds. The leader is reaped as
    it goes: on Linux a zombie leader keeps its group signalable."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(child.pid, signal.SIGTERM)
    for _ in range(100):
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


# ---------------------------------------------------------------- read sets

STRACE_ARGS = ("-f", "-q", "-y", "-s", "65535", "-e", "trace=%file,%process,fchdir")
FORKS = ("clone(", "clone3(", "fork(", "vfork(")
_tracers: dict[str, bool] = {}
_maps: dict[tuple[str, str], dict[str, str]] = {}


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


def tree_map(repo: Path, commit: str) -> dict[str, str]:
    """Each file and submodule of COMMIT's code (without .campaign/) to its mode and object, and "**"
    to a digest of every path name."""
    key = (str(repo), commit)
    if key not in _maps:
        lines = git(repo, "ls-tree", "-r", "-z", "--full-tree", commit).split("\0")
        found = {path: " ".join(meta.split()[::2]) for meta, _, path in (x.partition("\t") for x in lines if x)}
        found = {path: v for path, v in found.items() if not is_meta(path)}
        found["**"] = hashlib.sha256("\0".join(sorted(found)).encode()).hexdigest()[:24]
        _maps[key] = found
    return _maps[key]


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


def prepare(spec: Json, d: Path) -> Path:
    """The job's sources at its exact commits; the agent's own worktree is never used."""
    if not spec.get("commit"):
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
    for name, ref in spec["refs"].items():
        worktree_remove(ref["repo"], d / f"ref-{name}")
        worktree_add(Path(ref["repo"]), d / f"ref-{name}", ref["commit"], d / "heartbeat")
    return root / spec["cwd"]


def release(spec: Json, d: Path) -> None:
    """Drops this runner's slot lock, if it holds one, and the job's own checkouts unless it keeps them."""
    fh = _slot_locks.pop(spec.get("slot") or "", None)
    if fh is not None:
        fh.close()
    if spec.get("commit") and not spec.get("keep"):
        worktree_remove(spec["repo"], d / "src")
        for name, ref in spec.get("refs", {}).items():
            worktree_remove(ref["repo"], d / f"ref-{name}")


def quiet_argv() -> list[str]:
    found = shutil.which("quiet")
    return [found] if found else [sys.executable, str(SKILL / "scripts" / "quiet.py")]


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
    write_json(d / "state.json", st)
    return True


def cmd_run(a: argparse.Namespace) -> int:
    """One attempt of one job, supervised by this process, the leader of its own session."""
    d = Path(a.jobdir)
    job = load_job(d)
    spec, st = job["spec"], job["state"]
    me = os.getpid()
    st.update(state="running", runner=me, sid=os.getsid(0), boot=boot_id(), host=HOST, started=time.time())
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
    if spec.get("level") not in HEAVY:  # a light hold's build parallelism, where a command names none
        env.setdefault("CMAKE_BUILD_PARALLEL_LEVEL", str(LIGHT_BUILD_JOBS))
    if spec.get("cmux"):
        env["CMUX_WORKSPACE_ID"] = spec["cmux"]  # quiet's pill and notice go to the submitter's
    if spec.get("path"):
        env["PATH"] = spec["path"]  # the toolchain its fingerprint was taken with, whoever started the worker
    env.update(spec["env"])
    env.update(
        JOB_ID=d.name,
        JOB_DIR=str(ad),
        JOB_OUT=str(ad / "out"),
        JOB_SRC=str(cwd),
        JOB_COMMIT=spec.get("commit") or "",
        QUIET_KILL="1",
        QUIET_WHO=f"campaign job {d.name}",
    )
    for name in spec["refs"]:
        env[f"JOB_REF_{name.upper()}"] = str(d / f"ref-{name}")
    argv = ["bash", "-c", 'date +%s > "$JOB_DIR/began"\n' + spec["cmd"]]
    traced = tracer() if spec.get("commit") and spec["level"] in ("none", "light") else ""  # never timed
    if traced:  # what it reads decides where its result counts (record_reads)
        argv = [traced, *STRACE_ARGS, "-o", str(ad / "strace"), "--", *argv]
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
    with open(ad / "log", "wb") as log:
        child = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            preexec_fn=os.setpgrp,
        )
    st["pgid"] = child.pid
    write_json(d / "state.json", st)
    limit = time.time() + 60 * spec["minutes"] + float(os.environ.get("CAMPAIGN_WAIT_MAX", str(24 * 3600)))
    cancelled = timed_out = False
    while True:
        try:
            code = child.wait(timeout=BEAT)
            break
        except subprocess.TimeoutExpired:
            pass
        beat.touch()
        began = ad / "began"
        if (d / "cancel").exists() and not cancelled:
            cancelled = True
            kill_group(child, beat)
        elif (
            not timed_out
            and spec["level"] == "none"
            and began.exists()
            and time.time() > began.stat().st_mtime + 60 * spec["minutes"]
        ) or (not timed_out and time.time() > limit):
            timed_out = True
            kill_group(child, beat)
    sweep(os.getsid(0), beat)
    (ad / "exit").write_text(str(code))
    state = "cancelled" if cancelled else "timeout" if timed_out or code == 124 else "done" if code == 0 else "failed"
    reads: Json | None = None
    if traced and state == "done":
        root = jobs_dir() / "slots" / spec["slot"] if spec.get("slot") else d / "src"
        try:
            reads = record_reads(spec, root, cwd, ad / "strace")
        except Exception as e:  # recorded: the job keeps whole-tree identity
            reads = {"complete": False, "why": [f"recording its reads failed: {e}"], "reads": {}}
    (ad / "strace").unlink(missing_ok=True)
    release(spec, d)
    if finish(job, code, state, fence=me) and reads is not None:
        save_reads(job, reads)
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
                finish(
                    job,
                    code,
                    "done" if code == 0 else "timeout" if code == 124 else "failed",
                    "recorded after its runner died",
                )
                continue
            release(spec, d)
            attempt = int(st.get("attempt", 1))
            unstarted = not (attempt_dir(job) / "began").exists()  # its command never ran: safe to repeat
            if attempt <= int(spec.get("retries", 0)) or (unstarted and attempt < UNSTARTED_ATTEMPTS):
                shutil.rmtree(d / "claim", ignore_errors=True)
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


def claim(job: Json) -> bool:
    try:
        (job["dir"] / "claim").mkdir()
    except FileExistsError:
        return False
    (job["dir"] / "heartbeat").touch()
    write_json(job["dir"] / "state.json", {**job["state"], "state": "running", "claimed": time.time(), "host": HOST})
    return True


# a strict hold pins its own cores only where quiet can (Linux, FreeBSD's cpuset); elsewhere it drains
SPLITS = sys.platform == "linux" or (sys.platform.startswith("freebsd") and bool(shutil.which("cpuset")))


def expected_seconds(spec: Json, took: dict[tuple[str, str, str], list[float]]) -> float:
    """How long a job should run: the longest of its command's last three runs, at most its --minutes."""
    declared = 60.0 * int(spec.get("minutes") or 30)
    last = took.get(run_kind(spec), [])[-3:]
    return min(declared, max(last)) if last else declared


def run_kind(spec: Json) -> tuple[str, str, str]:
    """What a job runs, whatever the commit: its command, directory and repository."""
    return (str(spec.get("cmd", "")), str(spec.get("cwd", "")), str(spec.get("repo", "")))


def drains(job: Json) -> bool:
    """A job whose hold waits for every light hold and blocks new ones while it waits."""
    level = job["spec"].get("level")
    return level == "exclusive" or (level == "strict" and not SPLITS)


def window_info() -> Json:
    """The drain window's record: until, for and opened ({} when none was opened)."""
    with contextlib.suppress(OSError, ValueError):
        info = json.loads((jobs_dir() / ".window").read_text())
        return info if isinstance(info, dict) else {}
    return {}


def window_until() -> float:
    """When the open drain window closes (0 when none is open)."""
    with contextlib.suppress(ValueError, KeyError, TypeError):
        until = float(window_info()["until"])
        return until if until > time.time() else 0.0
    return 0.0


def cmd_window(a: argparse.Namespace) -> int:
    path = jobs_dir() / ".window"
    if a.close:
        path.unlink(missing_ok=True)
        print("window closed: drains and light jobs take turns again")
        return 0
    if a.minutes:
        until = time.time() + 60 * a.minutes
        atomic_write(path, json.dumps({"until": until, "for": a.purpose, "opened": time.time()}) + "\n")
        print(f"window open until {stamp(until)}: queued drains run back to back, light jobs wait")
        return 0
    until = window_until()
    print(f"window open until {stamp(until)}" if until else "no window open")
    return 0


def slot_of(spec: Json) -> str:
    """The build slot a job checks its commit out in, or "" when it has its own checkout."""
    return str(spec.get("slot") or "") if spec.get("commit") else ""


def camp(job: Json) -> str:
    """The campaign a job serves, for the worker's fair share."""
    return str(job["spec"].get("campaign") or "")


def light_slots(heavy: bool = False) -> int:
    """Light jobs at once, as quiet admits them: half the CPUs (a quarter beside a heavy job)."""
    if os.environ.get("CAMPAIGN_LIGHT_JOBS"):
        return int(os.environ["CAMPAIGN_LIGHT_JOBS"])
    ncpu = os.cpu_count() or 4
    return max(1, ncpu // 4) if heavy else max(1, ncpu // 2)


STALL = 30  # minutes runnable work may wait beside idle slots before `check` warns
WASTED = ("cancelled", "skipped", "interrupted", "timeout")  # ended without a result


def spans(jobs: list[Json], now: float) -> list[tuple[Json, float, float]]:
    """Each job that held a slot, from its runner's start to its end (now while it runs)."""
    out: list[tuple[Json, float, float]] = []
    for j in jobs:
        st = j["state"]
        end = st.get("ended") or (now if st.get("state") == "running" else None)
        if st.get("started") and end and j["spec"].get("level") != "none":
            out.append((j, float(st["started"]), float(end)))
    return out


def waits(jobs: list[Json], now: float) -> list[tuple[Json, float, float]]:
    """Each job's runnable wait: from its submission or its last dependency's end to its start (its
    cancellation, or now while queued). A job behind a dependency that did not pass has none."""
    by_id = {j["id"]: j for j in jobs}
    out: list[tuple[Json, float, float]] = []
    for j in jobs:
        deps = [by_id.get(x) for x in j["spec"].get("after", [])]
        if any(d is None or d["state"].get("state") != "done" for d in deps):
            continue
        ready = max([float(j["spec"].get("submitted") or 0), *(float(d["state"].get("ended") or 0) for d in deps if d)])
        st = j["state"]
        until = float(st.get("started") or st.get("ended") or (now if st.get("state") == "queued" else 0))
        if until > ready:
            out.append((j, ready, until))
    return out


def idle_for(job: Json, live: list[Json], slots: int) -> bool:
    """Whether a slot JOB could take was free beside the LIVE jobs: a light slot with no drain, a
    heavy job's own cores, or, for a drain, the whole machine."""
    level = job["spec"].get("level")
    if level == "none":
        return False
    if drains(job):
        return not live
    if level in HEAVY:
        return not any(x["spec"].get("level") in HEAVY for x in live)
    return not any(drains(x) for x in live) and sum(x["spec"].get("level") not in HEAVY for x in live) < slots


def idle_minutes(
    jobs: list[Json], now: float, since: float, only: Collection[str] | None = None
) -> tuple[dict[str, int], int, int]:
    """Minute by minute since SINCE: per job (of ONLY), the minutes it waited runnable beside a free slot
    it could take; the minutes any job did; and the minutes a drain held the machine."""
    held = [x for x in spans(jobs, now) if x[2] > since]
    waiting = [x for x in waits(jobs, now) if x[2] > since and (only is None or x[0]["id"] in only)]
    if only is not None:  # sample only from the first wait
        since = max(since, min((a for _, a, _ in waiting), default=now))
    slots = light_slots()
    per_job: Counter[str] = Counter()
    idle = drained = 0
    for t in range(int(since) + 30, int(now), 60):
        live = [j for j, a, b in held if a <= t < b]
        drained += any(drains(j) for j in live)
        stuck = [j["id"] for j, a, b in waiting if a <= t < b and idle_for(j, live, slots)]
        per_job.update(stuck)
        idle += bool(stuck)
    return dict(per_job), idle, drained


def queue_health(jobs: list[Json], now: float) -> Json:
    """The host's last hour from the job records: light slot-minutes used against the slots, minutes a
    drain held it, minutes runnable work waited beside idle slots, and the jobs that ended, by result."""
    since = now - 3600
    held = [x for x in spans(jobs, now) if x[2] > since]
    used = sum(min(b, now) - max(a, since) for j, a, b in held if j["spec"].get("level") not in HEAVY) / 60
    _, idle, drained = idle_minutes(jobs, now, since)
    ended = [j for j in jobs if float(j["state"].get("ended") or 0) > since]
    return {
        "used": round(used),
        "capacity": 60 * light_slots(),
        "drained": drained,
        "idle": idle,
        "ended": len(ended),
        "failed": sum(j["state"].get("state") == "failed" for j in ended),
        "wasted": dict(Counter(j["state"]["state"] for j in ended if j["state"].get("state") in WASTED)),
    }


def health_lines(h: Json) -> list[str]:
    """The two lines a lead's report quotes: host use and wasted jobs, over the last hour."""
    share = round(100 * h["used"] / h["capacity"]) if h["capacity"] else 0
    kinds = ", ".join(f"{n} {k}" for k, n in sorted(h["wasted"].items()))
    return [
        f"host, last hour: light slots {share}% used ({h['used']} of {h['capacity']} slot-minutes), "
        f"drains {h['drained']} min, runnable work beside idle slots {h['idle']} min",
        f"jobs ended, last hour: {h['ended']}, {h['failed']} failed, {sum(h['wasted'].values())} produced nothing"
        + (f" ({kinds})" if kinds else ""),
    ]


def stalls(jobs: list[Json], now: float) -> list[tuple[str, str]]:
    """A warning when a queued job waited over STALL minutes beside idle slots: the lead's to fix."""
    queued = {j["id"] for j in jobs if j["state"].get("state") == "queued"}
    per_job: dict[str, int] = idle_minutes(jobs, now, now - 86400, queued)[0] if queued else {}
    stuck = {k: m for k, m in per_job.items() if m > STALL}
    if not stuck:
        return []
    worst = max(stuck, key=lambda k: stuck[k])
    down = "" if worker_alive() else "; the worker is down (a submit or wait starts it)"
    text = f"{plural(len(stuck), 'queued job')} waited over {STALL} minutes beside idle slots (longest {worst}, "
    return [("warning", text + f"{stuck[worst]} min){down}: `campaign job show {worst}`")]


def cmd_worker(a: argparse.Namespace) -> int:
    jd = jobs_dir()
    home = jd / ".worker"
    me = str(os.getpid())
    if a.restart:
        try:
            pid = int((home / "pid").read_text())
        except (FileNotFoundError, ValueError):
            pid = 0
        if not pid or not worker_alive():
            print("no worker is running; the next submit or wait starts one on the current code")
            return 0
        os.kill(pid, signal.SIGHUP)
        print(f"worker {pid}: restarts on the current code after its tick; running jobs keep running")
        return 0
    asked: list[str] = []  # "restart" or "stop", acted on between ticks, never mid-claim
    signal.signal(signal.SIGHUP, lambda *_: asked.append("restart"))
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: asked.append("stop"))

    def take() -> bool:
        """Puts a home holding our pid and a fresh heartbeat in place at once: no starter sees it half made."""
        new = jd / f".worker.new.{me}"
        new.mkdir()
        (new / "pid").write_text(me)
        (new / "heartbeat").touch()
        try:
            new.rename(home)  # refused while another worker's home (never empty) is there
            return True
        except OSError:
            shutil.rmtree(new, ignore_errors=True)
            return False

    try:
        ours = (home / "pid").read_text() == me  # re-executed by a restart: the home is already ours
    except FileNotFoundError:
        ours = False
    if ours or take():
        pass
    elif worker_alive():
        return 0
    else:
        stale = jd / f".worker.stale.{me}"
        with contextlib.suppress(OSError):
            home.rename(stale)
        shutil.rmtree(stale, ignore_errors=True)
        if not take():
            return 0
    (home / "started").write_text(f"{time.time()}\n")  # when this code began scheduling
    tick = float(os.environ.get("CAMPAIGN_WORKER_TICK", "2"))
    drain_wait = float(os.environ.get("CAMPAIGN_DRAIN_WAIT", "3600"))
    drain_slack = float(os.environ.get("CAMPAIGN_DRAIN_SLACK", "600"))
    drain_turn = float(os.environ.get("CAMPAIGN_DRAIN_TURN", "3600"))
    try:
        while True:
            try:
                if (home / "pid").read_text() != me:
                    return 0  # another worker took the home: one worker at a time
            except FileNotFoundError:
                return 0
            (home / "heartbeat").touch()
            reap()
            jobs = all_jobs()
            states = {j["id"]: j["state"].get("state") for j in jobs}
            queued = sorted(
                (j for j in jobs if states[j["id"]] == "queued"),
                key=lambda j: (-int(j["spec"].get("priority", 0)), j["id"]),
            )
            running = [j for j in jobs if states[j["id"]] == "running"]
            busy = {slot_of(j["spec"]) for j in running} - {""}  # a slot is one build directory: one job at a time
            if not queued and not running:
                if daily_prune((home / "heartbeat").touch):
                    continue  # whatever arrived while it pruned
                return 0
            heavy = any(j["spec"].get("level") in HEAVY for j in running)
            # a "none" job takes no hold: it never waits for a light slot, and has its own as many
            light = sum(1 for j in running if j["spec"].get("level") not in (*HEAVY, "none"))
            bare = sum(1 for j in running if j["spec"].get("level") == "none")
            late = sum(
                1 for j in running if j["spec"].get("level") not in HEAVY and int(j["spec"].get("priority", 0)) < 0
            )
            slots = light_slots(heavy)
            # fair share: campaigns with light work split the slots; within one, the shortest job first
            holds = Counter(camp(j) for j in running if j["spec"].get("level") not in (*HEAVY, "none"))
            wanting = {camp(j) for j in queued if j["spec"].get("level") not in (*HEAVY, "none")}
            share = max(1, slots // max(1, len(wanting | {c for c, n in holds.items() if n})))
            queued.sort(
                key=lambda j: (
                    -int(j["spec"].get("priority", 0)),
                    holds[camp(j)],
                    int(j["spec"].get("minutes", 0) or 0),
                    j["id"],
                )
            )
            now = time.time()
            # how long each command took, oldest run first
            took: dict[tuple[str, str, str], list[float]] = {}
            for j in sorted(jobs, key=lambda j: j["id"]):
                st = j["state"]
                if st.get("state") == "done" and st.get("started") and st.get("ended"):
                    took.setdefault(run_kind(j["spec"]), []).append(float(st["ended"]) - float(st["started"]))

            # a draining hold blocks new light holds until the running ones end: start one only when
            # they all end soon, and past its wait start no new light job, so the drain stays short
            lights_end = max(
                (
                    float(r["state"].get("started") or now) + expected_seconds(r["spec"], took)
                    for r in running
                    if r["spec"].get("level") not in (*HEAVY, "none")
                ),
                default=now,
            )
            drain_ok = lights_end - now <= drain_slack
            # a drain's wait counts from the last drain that ran: one cancelled or skipped while queued
            # never held the machine, so it must not restart the wait
            last_drain = max(
                (float(j["state"].get("ended") or 0) for j in jobs if drains(j) and j["state"].get("started")),
                default=0.0,
            )
            turn = window_info()
            starved = [
                j
                for j in queued
                if j["spec"].get("level") not in (*HEAVY, "none")
                and int(j["spec"].get("priority", 0)) >= 1
                and now - float(j["spec"].get("submitted") or now) > drain_turn
                and all(states.get(x) == "done" for x in j["spec"].get("after", []))
            ]
            if (
                starved
                and turn.get("for") == "drain turn"
                and not any(drains(j) for j in running)
                and last_drain >= float(turn.get("opened") or now)
            ):
                # direct-path work that waited a whole turn ends it once its running drain ends: light
                # work has DRAIN_WAIT from that drain's end, so a long drain queue cannot hold it for hours
                (jobs_dir() / ".window").unlink(missing_ok=True)
            window = window_until() > now  # an open window: every ready drain is due, back to back
            overdue = [
                j
                for j in queued
                if drains(j)
                and (window or now - max(float(j["spec"].get("submitted") or 0), last_drain) >= drain_wait)
                and all(states.get(x) == "done" for x in j["spec"].get("after", []))
            ]
            if overdue and not window:
                # an overdue drain opens a turn: the queued drains run back to back for DRAIN_TURN, then
                # light work has DRAIN_WAIT again, so a queue of drains takes turns, not one per hour
                until = now + drain_turn
                atomic_write(
                    jobs_dir() / ".window",
                    json.dumps({"until": until, "for": "drain turn", "opened": now}) + "\n",
                )
                window = True
            for j in queued:
                deps = [states.get(x, "missing") for x in j["spec"].get("after", [])]
                if any(s not in ("queued", "running", "done") for s in deps):
                    if claim(j):
                        finish(load_job(j["dir"]), -1, "skipped", "a job it runs after did not succeed")
                    continue
                is_heavy = j["spec"].get("level") in HEAVY
                is_bare = j["spec"].get("level") == "none"
                is_late = int(j["spec"].get("priority", 0)) < 0
                if (
                    any(s != "done" for s in deps)
                    or (is_heavy and heavy)
                    or (is_bare and bare >= slots)
                    or (not is_heavy and not is_bare and light >= slots)
                ):
                    continue
                if drains(j) and j not in overdue and (not drain_ok or wanting):
                    continue  # for a lull in light work, or its turn after an hour of it
                if overdue and not is_heavy and not is_bare:
                    continue  # an overdue drain waits: no new light job lengthens it
                if is_late and not is_heavy and late >= max(1, slots // 2):
                    continue  # deferred work never takes every slot from the direct path
                if not is_heavy and not is_bare and len(wanting) > 1 and holds[camp(j)] >= share:
                    continue  # its campaign has its share while another campaign waits
                if slot_of(j["spec"]) in busy:
                    continue  # its slot's job runs: it waits its turn, and the jobs behind it go on
                if claim(j):
                    spawn(["_run", str(j["dir"])], j["dir"] / "runner.log")
                    busy |= {slot_of(j["spec"])} - {""}
                    heavy = heavy or is_heavy
                    light += 0 if is_heavy or is_bare else 1
                    bare += 1 if is_bare else 0
                    holds[camp(j)] += 0 if is_heavy or is_bare else 1
                    late += 1 if is_late and not is_heavy else 0
            if "stop" in asked:
                return 0
            if asked:  # a restart keeps the pid and the home: no starter sees the worker gone
                sys.stdout.flush()
                os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve()), "worker"])
            time.sleep(tick)
    finally:
        with contextlib.suppress(FileNotFoundError, ValueError):
            if int((home / "pid").read_text()) == os.getpid():
                shutil.rmtree(home, ignore_errors=True)


def resolve_job(jid: str) -> Json:
    d = jobs_dir() / jid
    if not (d / "spec.json").exists():
        die(f"no job {jid}")
    return load_job(d)


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
    }
    spec["env"] = dict(x.split("=", 1) for x in a.env)
    cwd = Path.cwd()
    default_ref = "HEAD"
    repo = Path(a.repo).resolve() if a.repo else cwd
    c: Campaign | None = None
    p: Json = {}
    if (a.criteria or a.fails_without) and not a.quest:
        die("--for and --fails-without name a quest's criteria: add --quest ID")
    if a.fails_without and not a.criteria:
        die("--fails-without needs --for: the criteria whose test fails without the fix")
    ids: list[str] = []
    if a.quest:
        c = find_campaign(cwd)
        p = c.quest(a.quest)
        spec.update(quest=a.quest, campaign=c.ident)
        ids = criteria_for(c, a.quest, a.criteria or "")
        repo = Path(a.repo).resolve() if a.repo else c.repo(p)
        default_ref = p.get("branch") or "HEAD"
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
        if c and a.level == "light" and not (a.slot or a.fresh or a.keep) and p["risk"] != "critical":
            name = quest_slot(c, a.quest, top)
            # a free slot builds incrementally; with a job of its own pending, a fresh checkout runs beside it
            if not any(slot_of(j["spec"]) == name and j["state"].get("state") not in TERMINAL for j in all_jobs()):
                spec["slot"] = name
        if not a.commit and not a.quest and git(top, "status", "--porcelain", "--untracked-files=no"):
            print(
                f"note: uncommitted changes in {top} are not in the job; it runs {spec['commit'][:12]}", file=sys.stderr
            )
    outside = outside_inputs(command, spec.get("repo", ""))
    if outside:
        print(
            f"warning: {', '.join(outside)} {'is' if len(outside) == 1 else 'are'} outside this job's own checkout, so "
            "not part of its identity: a rerun at the same code may see other files. Build it in this job, or pass "
            "another repository with --ref NAME=PATH@REF",
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


def outbox_ref(name: str) -> str:
    """A campaign's outbox: a ref of its own in its home, published under the same name."""
    return f"refs/campaign/outbox/{name}"


def peers(c: Campaign) -> dict[str, str]:
    """The campaigns on other machines this one exchanges messages with: name -> where its outbox is."""
    return dict(x.split("=", 1) for x in c.state.get("peers", []) if "=" in x)


def send_message(c: Campaign, peer: str, pid: str, text: str, by: str) -> str:
    """Adds a message for a campaign on another machine to this campaign's outbox (a ref of its own,
    never main) and pushes it; one that can't be pushed now goes with the next push."""
    uid = f"{c.name}-{secrets.token_hex(4)}"
    message = {"uid": uid, "from": c.name, "quest": pid, "at": stamp(), "by": by, "text": text}
    blob = subprocess.run(
        ["git", "-C", str(c.home), "hash-object", "-w", "--stdin"],
        input=json.dumps(message, indent=2) + "\n",
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    index = Path(git(c.home, "rev-parse", "--path-format=absolute", "--git-dir")) / f"outbox.{os.getpid()}.index"
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}

    def plumb(*args: str) -> str:
        cmd = ["git", "-C", str(c.home), *args]
        return subprocess.run(cmd, env=env, capture_output=True, text=True, check=True).stdout.strip()

    for _ in range(5):  # another agent may add a message at the same moment
        parent = git(c.home, "rev-parse", "--verify", "--quiet", outbox_ref(c.name), check=False)
        try:
            plumb("read-tree", *([parent] if parent else ["--empty"]))
            plumb("update-index", "--add", "--cacheinfo", f"100644,{blob},{peer}/{uid}.json")
            parents = ["-p", parent] if parent else []
            commit = plumb("commit-tree", plumb("write-tree"), *parents, "-m", f"campaign: message for {peer}")
        finally:
            index.unlink(missing_ok=True)
        if git_ok(c.home, "update-ref", outbox_ref(c.name), commit, parent or "0" * 40):
            push_outbox(c)
            return uid
    die("the outbox kept changing under this message; try again")


def push_outbox(c: Campaign) -> bool:
    """Publishes this campaign's outbox where its peers fetch it ($outbox remote, default origin)."""
    if not git_ok(c.home, "rev-parse", "--verify", "--quiet", outbox_ref(c.name)):
        return True
    remote = c.state.get("outbox") or "origin"
    ok = git_ok(c.home, "push", "--quiet", remote, f"{outbox_ref(c.name)}:{outbox_ref(c.name)}")
    if not ok:
        print(
            f"campaign: the outbox could not be pushed to {remote}; the next message or wait retries", file=sys.stderr
        )
    return ok


def fetch_peers(c: Campaign) -> list[str]:
    """Fetches each peer's outbox and leaves the messages it holds for this campaign; their ids."""
    imported = mailbox(c, "").parent / "imported.json"
    seen: set[str] = set(((read_json(imported) if imported.exists() else None) or {}).get("uids", []))
    left: list[str] = []
    for name, where in peers(c).items():
        local = f"refs/campaign/peer/{name}"
        if not git_ok(c.home, "fetch", "--quiet", "--no-tags", where, f"+{outbox_ref(name)}:{local}"):
            continue
        for path in git(c.home, "ls-tree", "-r", "--name-only", local, "--", f"{c.name}/", check=False).splitlines():
            with contextlib.suppress(ValueError, CampaignError):
                message = json.loads(git(c.home, "show", f"{local}:{path}"))
                if message["uid"] in seen:
                    continue
                pid = message.get("quest", "")
                open_quest = pid in c.quests and c.quests[pid]["status"] not in ("landed", "dropped")
                text = message["text"] if open_quest or not pid else f"for {pid}: {message['text']}"
                left.append(
                    leave_message(
                        c, pid if open_quest else "", text, f"{message['from']}: {message['by']}", message["uid"]
                    )
                )
                seen.add(message["uid"])
    if left:
        write_json(imported, {"uids": sorted(seen)})
    return left


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
    nid = leave_message(c, pid, a.text, a.by)
    then = "its journal answers it by that id, or the lead closes it" if pid else "the lead closes it"
    print(f"{nid} for {whom}: {then}")
    return 0


def cmd_job_rerun(a: argparse.Namespace) -> int:
    """The same jobs (command, level, directory, environment, other repositories) at another commit:
    by default the head of their quest's branch, in whichever of the job's repositories holds it
    (its own or a `--ref`'s), else of their repository."""
    specs = {j["id"]: dict(j["spec"]) for j in map(resolve_job, a.jobs)}
    order: list[str] = []

    def visit(jid: str) -> None:  # a job after its prerequisites, so a rerun chain keeps its order
        if jid not in order:
            for x in specs[jid].get("after", []):
                if x in specs:
                    visit(x)
            order.append(jid)

    for jid in specs:
        visit(jid)
    c: Campaign | None = None
    with contextlib.suppress(CampaignError):
        c = find_campaign(Path.cwd())

    def quest_code(spec: Json) -> tuple[str, Path | None, list[str]]:
        """The quest's branch, its repository and every branch its code is on (its tasks', main)."""
        if a.commit or not spec.get("quest") or c is None:
            return "", None, []
        with contextlib.suppress(CampaignError):
            quest = c.quest(spec["quest"])
            home = c.repo(quest)
            return quest.get("branch", ""), home, [b for _, b, _, _ in branches(c, spec["quest"])] + [main_branch(home)]
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
    renamed: dict[str, str] = {}
    queued: list[str] = []
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
        commit = commit or rev(Path(spec["repo"]), a.commit or branch or "HEAD")
        if spec.get("quest"):  # an evidence job's outside scripts as they are now
            spec["scripts"] = script_digests(spec["cmd"], spec["repo"])
        what, old = spec.get("what", ""), f" at {spec['commit'][:12]}"
        spec.update(
            commit=commit,
            after=[renamed.get(x, x) for x in spec.get("after", [])],
            cmux=os.environ.get("CMUX_WORKSPACE_ID", ""),
            what=(what[: -len(old)] if what.endswith(old) else what) + f" at {commit[:12]}",
        )
        new, reused = submit(spec)
        renamed[jid] = new
        moved = "".join(
            f", {k} at {r['commit'][:12]}"
            for k, r in spec.get("refs", {}).items()
            if home and same_repo(Path(r["repo"]), home)
        )
        print(f"{new}" + (" (reused)" if reused else "") + f": {jid} at {commit[:12]}{moved}")
        if not reused:
            queued.append(new)
    supersede(queued)
    ensure_worker()
    return 0


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
                            job_for_check(repo, head, x, c.state.get("tested_docs", []), ci=ci_covers(c))
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
            log.write(f"{stamp()} slimmed {j['id']} {j['spec'].get('what', '')}\n")
            beat()
        for x in plan["slots"]:
            fh = slot_lock_try(x)
            if fh is None:
                continue  # a job took it since the plan
            with fh:
                common = git(x, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
                shutil.rmtree(x, ignore_errors=True)
                if common:
                    git(Path(common).parent, "worktree", "prune", check=False)
                x.with_name(x.name + ".lock").unlink(missing_ok=True)  # a waiter that opened it retries
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


# ---------------------------------------------------------------- disk


def size_text(n: int) -> str:
    return f"{n / 2**30:.1f} GiB" if n >= 2**30 else f"{n / 2**20:.0f} MiB"


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


def branch_heads(c: Campaign) -> dict[tuple[str, str], str]:
    """(name, branch) -> head, for every branch of the quests in flight."""
    heads: dict[tuple[str, str], str] = {}
    for pid, p in c.quests.items():
        if p["status"] in FLIGHT:
            repo = c.repo(p)
            for name, branch, _, _ in branches(c, pid):
                if has_ref(repo, branch):
                    heads[(name, branch)] = rev(repo, branch)
    return heads


def wait_events(c: Campaign, timeout: float) -> int:
    """Returns at the first commit on a quest branch in flight or job of this campaign that ended
    since the lead last saw them (recorded in the home's .git), so nothing that happens
    between two waits is missed; then lists what is still in flight, to wait for next."""
    seen_file = Path(git(c.home, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "campaign-seen.json"
    records: Json = (read_json(seen_file) if seen_file.exists() else None) or {}
    recorded: Json | None = records.get(c.ident)

    def remember(heads: dict[str, str], ended: dict[str, Json]) -> None:
        records[c.ident] = {"heads": heads, "jobs": sorted(ended), "messages": sorted(messages())}
        write_json(seen_file, records)

    def messages() -> dict[str, Json]:
        open_quests = [pid for pid, q in c.quests.items() if q["status"] not in ("landed", "dropped")]
        return {f"{pid or c.name + ':'} {m['id']}": m for pid in ["", *open_quests] for m in inbox(c, pid)}

    exchanged = [0.0]

    def exchange() -> None:  # the peers' outboxes, and this one's, about once a minute
        if peers(c) and time.time() - exchanged[0] >= float(os.environ.get("CAMPAIGN_PEER_SECONDS", "60")):
            fetch_peers(c)
            push_outbox(c)
            exchanged[0] = time.time()

    tick = float(os.environ.get("CAMPAIGN_WAIT_TICK", "5"))
    start = time.time()

    def current() -> tuple[dict[str, str], dict[str, Json]]:
        heads = {f"{name} {branch}": head for (name, branch), head in branch_heads(c).items()}
        ended = {
            j["id"]: j
            for j in all_jobs()
            if j["spec"].get("campaign") == c.ident and j["state"].get("state") in TERMINAL
        }
        return heads, ended

    heads, ended = current()
    if recorded is None:  # the first wait starts from now
        remember(heads, ended)
        seen_heads, seen_jobs, seen_messages = dict(heads), set(ended), set(messages())
    else:
        seen_heads = {str(k): str(v) for k, v in dict(recorded.get("heads") or {}).items()}
        seen_jobs = {str(x) for x in recorded.get("jobs") or []}
        seen_messages = {str(x) for x in recorded.get("messages") or []}
    while True:
        reap()
        exchange()
        watch_leads()
        heads, ended = current()
        waiting = messages()
        moved = sorted(k for k, h in heads.items() if seen_heads.get(k) != h)
        finished = sorted(set(ended) - seen_jobs)
        arrived = sorted(set(waiting) - seen_messages)
        if moved or finished or arrived:
            break
        running = [j for j in all_jobs() if j["spec"].get("campaign") == c.ident]
        if not heads and not peers(c) and not any(j["state"].get("state") not in TERMINAL for j in running):
            print("nothing to wait for: no quest in flight and no job of this campaign")
            return 0
        if timeout and time.time() - start >= timeout:
            print(f"nothing new after {timeout:g}s")
            return 3
        if any(j["state"].get("state") == "queued" for j in running):
            ensure_worker()
        time.sleep(tick)
    for key in moved:
        name, branch = key.split(" ", 1)
        print(f"{name}: {branch} moved to {heads[key][:12]}; journal: {journal_state(c, name, branch)}")
    for key in arrived:
        n = waiting[key]
        target = key.split()[0]
        whom = f" for the lead, from {n['by']}" if target.endswith(":") else f" from {n['by']}"
        print(f"{target.rstrip(':')}: {n['id']}{whom}: {n['text']}")
    for jid in finished:
        s = job_summary(ended[jid])
        print(f"{jid}  {s['state']}" + ("" if s["exit"] is None else f" exit {s['exit']}") + f"  {s['what']}")
    remember(heads, ended)
    flight = [f"{name} ({journal_state(c, name, branch)})" for (name, branch) in branch_heads(c)]
    pending = sum(
        1 for j in all_jobs() if j["spec"].get("campaign") == c.ident and j["state"].get("state") not in TERMINAL
    )
    if flight or pending:
        print(f"still in flight: {', '.join(flight) or 'no quest'}; {pending} job(s) pending; wait again after this")
    return 0


def wait_for(ids: list[str], any_one: bool, timeout: float) -> int:
    start = time.time()
    tick = float(os.environ.get("CAMPAIGN_WAIT_TICK", "5"))
    while True:
        reap()
        ensure_worker()
        watch_leads()
        jobs = {x: load_job(jobs_dir() / x) for x in ids}
        pending = [x for x, j in jobs.items() if j["state"].get("state") not in TERMINAL]
        if not pending or (any_one and len(pending) < len(ids)):
            break
        if timeout and time.time() - start >= timeout:
            print(f"still pending after {timeout:g}s: {', '.join(pending)}")
            return 3
        time.sleep(tick)
    for x, j in jobs.items():
        s = job_summary(j)
        took = f" in {s['seconds']}s" if s["seconds"] is not None else ""
        code = "" if s["exit"] is None else f" exit {s['exit']}"
        print(f"{x}  {s['state']}{code}{took}  {s['what']}  log {attempt_dir(j) / 'log'}")
    finished = [j for j in jobs.values() if j["state"].get("state") in TERMINAL]
    return 0 if all(j["state"].get("state") == "done" for j in finished) else 1


def cmd_wait(a: argparse.Namespace) -> int:
    reap()
    if a.commits:
        return wait_events(find_campaign(Path.cwd()), a.timeout)
    if a.jobs:
        ids = [resolve_job(x)["id"] for x in a.jobs]
    else:
        mine = [j for j in all_jobs() if not a.quest or j["spec"].get("quest") == a.quest]
        ids = [j["id"] for j in mine if j["state"].get("state") not in TERMINAL]
        if not ids and a.quest:
            ids = [j["id"] for j in mine]  # all finished: report them
    if not ids:
        print("nothing to wait for")
        return 0
    return wait_for(ids, a.any, a.timeout)


# ---------------------------------------------------------------- command line


EXAMPLES = """examples:
  campaign init --name shop                          # in the home repository's main checkout
  campaign new cart --title "Cart totals" --risk normal --files 'src/cart/**' --check 'unit=make test'
  campaign start cart                                # its branch and worktree
  campaign brief                                     # in the worktree: what the quest still needs
  campaign job submit --quest cart --level strict --minutes 10 -- ./bench.sh
  campaign verify cart && campaign wait --quest cart
  campaign accept cart correctness && campaign land cart
  campaign message shop:cart "totals round half-even" --from explorer   # from anywhere on the machine"""


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="campaign", description=__doc__, epilog=EXAMPLES, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("-C", dest="chdir", metavar="DIR", help="run as if started in DIR, like git -C")
    ap.add_argument(
        "--campaign", metavar="NAME", help="which campaign: one its home holds, or any on this machine ($CAMPAIGN)"
    )
    sub = ap.add_subparsers(dest="command", metavar="COMMAND", title="commands", required=True)
    common = argparse.ArgumentParser(add_help=False)  # --campaign after the command too
    common.add_argument("--campaign", metavar="NAME", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    check = "NAME[:LEVEL[:MIN]]=CMD"

    def command(name: str, summary: str, description: str, *, parents: bool = True) -> argparse.ArgumentParser:
        return sub.add_parser(name, parents=[common] if parents else [], help=summary, description=description)

    p = command(
        "init",
        "start a campaign in this repository's main checkout",
        "Creates the campaign's directory (.campaign/NAME/: state.json, PLAN.md and hunt.md) in this repository's main "
        "checkout, registers it on this machine and commits it. This repository becomes the campaign's home; "
        "the owner writes its goals in PLAN.md.",
        parents=False,
    )
    p.add_argument("--name", help="campaign name, unique on this machine (default: the directory's)")
    p.add_argument(
        "--ci", choices=("github", ""), default="", metavar="github", help="landing waits for green GitHub CI"
    )
    p.set_defaults(fn=cmd_init)

    p = command(
        "config",
        "show or set campaign-wide settings: ci, ci_later, suite, peers, outbox",
        "With no argument, prints the settings as key=value lines; with KEYs, their values; with KEY=VALUE, sets "
        "them (in the home's main checkout). ci=github makes landing wait for green GitHub CI but its "
        "ci_later=WORKFLOW,... workflows. suite=CMD (the home's) and suite.REPO=CMD (another repository's, by "
        "directory name), its build plus tests, gate a quest `new` gives no check. peers=NAME=URL,... names "
        "other machines' campaigns and their outbox repositories; outbox=REMOTE is where this one pushes "
        "(default origin). disk_warn=GIB is the free disk under which `check` warns (default 100).",
    )
    p.add_argument("assignments", nargs="*", metavar="KEY[=VALUE]", help="settings to read or set")
    p.set_defaults(fn=cmd_config)

    p = command(
        "new",
        "plan a quest",
        "Records a quest, writing .campaign/NAME/quests/ID.md. Its gate: its repository's suite, or --check; "
        "sanitizer, crash, fuzz, mutant checks go --later, but crash and TSan at high risk.",
    )
    p.add_argument("id", metavar="ID", help="the quest's id: lowercase letters, digits and single dashes")
    p.add_argument("--title", required=True, help="what it delivers, in a few words")
    p.add_argument("--risk", choices=RISKS, default="normal", help="sets the reviews it needs (default normal)")
    p.add_argument("--repo", default=".", metavar="PATH", help="its repository, relative to the home checkout")
    p.add_argument("--needs", metavar="IDS", help="quests that must land before it starts")
    p.add_argument(
        "--after", metavar="IDS", help="quests that must land before it lands (NAME:ID for another campaign's)"
    )
    p.add_argument("--files", metavar="GLOBS", help="the files it may change")
    p.add_argument("--provides", metavar="CONTRACTS", help="APIs, formats or protocols it defines or changes")
    p.add_argument("--consumes", metavar="CONTRACTS", help="APIs, formats or protocols it builds against")
    p.add_argument("--lenses", metavar="LENSES", help="the reviews it needs (default: by risk)")
    p.add_argument("--milestone", metavar="NAME", help="the PLAN milestone it belongs to")
    p.add_argument(
        "--check", action="append", default=[], metavar=check, help="a gate check: must pass on the landed code"
    )
    p.add_argument(
        "--later",
        action="append",
        default=[],
        metavar=check,
        help="a deferred check: runs on the landed code at low priority; gates the milestone",
    )
    p.add_argument("--inputs", action="append", default=[], metavar="NAME=GLOBS", help=INPUTS_HELP)
    p.set_defaults(fn=cmd_new)

    p = command(
        "get",
        "print a quest's fields",
        "Prints a quest's fields as key=value lines, the form `set` takes (checks as check= and later= lines); "
        "with KEYs, only their values, one per line. With --task T, the task's.",
    )
    p.add_argument("id", metavar="ID", help="the quest")
    p.add_argument("keys", nargs="*", metavar="KEY", help="print only these fields' values")
    p.add_argument("--task", metavar="TASK", help="one of the quest's tasks")
    p.set_defaults(fn=cmd_get)

    p = command(
        "set",
        "change a quest's fields",
        "Sets a quest's fields: title, status, risk, reason, repo, milestone, needs, after, provides, consumes, "
        "files, lenses, and with --check or --later its checks. A blocked or dropped status needs reason=R. "
        "Lowering the risk, removing a lens, or removing, changing, deferring or narrowing the inputs of a check "
        "needs --weaken REASON: "
        "the change is recorded in the quest's history and voids its review acceptances; any risk change voids "
        "them too. A risk's minimum reviews (a code review at high, a design review at critical) stay. "
        "With --task T: the task's files=GLOBS, or status=dropped reason=R. `campaign get ID` prints them.",
    )
    p.add_argument("id", metavar="ID", help="the quest")
    p.add_argument("--task", metavar="TASK", help="one of the quest's tasks")
    p.add_argument("assignments", nargs="*", metavar="KEY=VALUE", help="the fields to set (lists comma-separated)")
    p.add_argument("--check", action="append", default=[], metavar=check, help="replace the gate checks")
    p.add_argument("--later", action="append", default=[], metavar=check, help="replace the deferred checks")
    p.add_argument("--inputs", action="append", default=[], metavar="NAME=GLOBS", help=INPUTS_HELP)
    p.add_argument(
        "--weaken",
        metavar="REASON",
        help="needed to lower the risk, remove a lens, or remove, change, defer or narrow a check: recorded, voids acceptances",
    )
    p.set_defaults(fn=cmd_set)

    p = command(
        "start",
        "branch and worktree for a quest or one of its tasks",
        "Creates the quest's branch (quest/CAMPAIGN/ID) in its own worktree with a journal, and makes it active. "
        "With --task T, a parallel writer inside the quest: its own branch, worktree and journal, for --files.",
    )
    p.add_argument("id", metavar="ID", help="the quest")
    p.add_argument("--task", metavar="TASK", help="a parallel writer inside the quest, branched from it")
    p.add_argument("--files", metavar="GLOBS", help="the task's files (default: the quest's)")
    p.add_argument("--force", metavar="REASON", help="start before its needs landed, or over an overlapping file set")
    p.set_defaults(fn=cmd_start)

    p = command(
        "status",
        "each quest and what it still needs, for people",
        "Each quest and what it still needs to land, with its criteria met; `status ID` lists each criterion "
        "with its evidence. Two lines give the host's last hour: slot use, drains, idle minutes with runnable "
        "work, and jobs that produced nothing. Colored on a terminal (NO_COLOR turns it off); CAMPAIGN_BAR "
        "sets the progress bar's two characters. Agents read `campaign brief`.",
    )
    p.add_argument("id", metavar="ID", nargs="?", help="only this quest")
    p.add_argument("--json", action="store_true", help="the same as JSON")
    p.add_argument("--all", action="store_true", help="every campaign on this machine")
    p.set_defaults(fn=cmd_status, brief=False)

    p = command(
        "brief",
        "what each quest still needs, for agents",
        "What a quest still needs, one line each: landing gaps, unanswered messages, failed jobs no journal "
        "names, pending jobs. In a quest's worktree, that quest, its file set and its generated Evidence table; "
        "elsewhere, every quest not landed.",
    )
    p.add_argument("id", metavar="ID", nargs="?", help="only this quest")
    p.add_argument("--json", action="store_true", help="the same as JSON")
    p.set_defaults(fn=cmd_status, brief=True, all=False)

    p = command(
        "usage",
        "tokens, cost and time per quest, role and assignment",
        "Agent runs and job time since the campaign began, per quest and per role, assignment, model and effort, "
        "read from the harnesses' session logs (Copilot CLI, Codex) and the job records. Tokens are cumulative "
        "(context rereads included); cost is in each harness's own unit.",
    )
    p.add_argument("--json", action="store_true", help="the same as JSON")
    p.set_defaults(fn=cmd_usage)

    p = command(
        "map",
        "the campaign's milestones and quests, with intent, result and criteria",
        "The campaign as a map, in Markdown: its goals, then each milestone of PLAN.md with a checkbox per "
        "quest, its intent (the quest file's Outcome), its result (the journal's Result), the documents its "
        "landing added to its repository's docs/, its reviews, and the open quests' criteria as checkboxes. "
        "With ID, that quest in full. --write keeps it as .campaign/NAME/MAP.md, which each landing refreshes. "
        "Its emoji mark each quest's and milestone's state in the file too; color only on a terminal "
        "(NO_COLOR turns it off).",
    )
    p.add_argument("id", metavar="ID", nargs="?", help="only this quest, in full")
    p.add_argument("--json", action="store_true", help="the same as JSON")
    p.add_argument("--write", action="store_true", help="write and commit .campaign/NAME/MAP.md")
    p.set_defaults(fn=cmd_map)

    p = command(
        "check",
        "contradictions between state, git, worktrees and jobs",
        "Reports errors and warnings where state.json, git, the worktrees and the job records disagree, and "
        "what other campaigns on this machine overlap; exits 1 on an error. Notes report what is left behind "
        "(uncleaned quests, prunable jobs, unknown campaigns' jobs). Warnings: free disk under disk_warn, with "
        "the largest consumers; a queued job that waited over 30 minutes beside idle slots; a message to a "
        "lead undelivered in cmux over 15 minutes (the owner is told once). It changes nothing.",
    )
    p.set_defaults(fn=cmd_check)

    p = command(
        "sweep",
        "remove clean worktrees of merged branches and finished quests",
        "Lists the worktrees of the repositories this machine's campaigns use (outside the job store, never an "
        "unfinished quest's) whose branch is merged into main or whose quest landed or was dropped, and removes "
        "each with `git worktree remove`, deleting its branch when merged. It keeps a worktree with "
        "uncommitted or untracked files, one a process runs in, one a job reads from, and one whose .scratch "
        "holds files unless --force (commit reusable .scratch files first). Removals are logged in the job "
        "store's prune.log.",
        parents=False,
    )
    p.add_argument("--dry-run", action="store_true", help="list what it would remove or keep, with sizes")
    p.add_argument("--force", action="store_true", help="remove worktrees whose .scratch holds files too")
    p.set_defaults(fn=cmd_sweep)

    p = command(
        "verify",
        "queue a quest's gate checks at its branch head",
        "Queues the quest's gate checks at its branch head (or --commit), reusing results that still count.",
    )
    p.add_argument("id", metavar="ID", help="the quest")
    p.add_argument("--commit", metavar="REF", help="verify this commit instead of the head")
    p.add_argument("--for", dest="criteria", metavar="A1,A2", help="the criteria the gate jobs prove")
    p.add_argument("--force", action="store_true", help="rerun even if an identical job succeeded")
    p.add_argument("--fresh", action="store_true", help="a fresh checkout instead of the quest's build slot")
    p.add_argument("--all", action="store_true", help="the deferred checks too")
    p.add_argument("--deferred", action="store_true", help="a landed quest's deferred checks, on main")
    p.set_defaults(fn=cmd_verify)

    p = command(
        "accept",
        "record a review lens as passed for the code it reviewed",
        "Records a review lens as accepted and commits the review file with it: the review's content, the "
        "criteria, and the quest's changed "
        "files at the accepted commit. An APPROVE covers the code at the review's Commit: line (metadata-only "
        "commits after it keep it). After CHANGES, --confirm names, for each blocking finding, jobs that passed "
        "at the fixed code; a fix that changes files the review never saw needs the reviewer or --fix-scope.",
    )
    p.add_argument("id", metavar="ID", help="the quest")
    p.add_argument("lens", metavar="LENS", choices=LENSES, help=f"the review: {', '.join(LENSES)}")
    p.add_argument("--commit", metavar="REF", help="the commit accepted (default: the branch head)")
    p.add_argument(
        "--confirm",
        action="append",
        default=[],
        metavar="FINDING=JOBS",
        help="after CHANGES, for each blocking finding: jobs that passed at the fixed code (repeat per finding)",
    )
    p.add_argument(
        "--fix-scope",
        metavar="REASON",
        help="the fix changes files the review never saw; why they add no mechanism (else the reviewer reviews the fix)",
    )
    p.add_argument("--note", metavar="TEXT", help="a remark kept with the acceptance")
    p.set_defaults(fn=cmd_accept)

    p = command(
        "land",
        "land a quest on main",
        "Lands the quest when nothing `campaign brief` lists is left: dependencies, a merge of main, file set, "
        "tasks, criteria (from the jobs tagged for them), messages, checks, reviews and CI. It fast-forwards "
        "main (or merges when only campaign files differ), checks that the landed code is the tested code, "
        "records the landing, cleans the quest as `clean` does (--keep keeps it) and refreshes MAP.md. --upto "
        "lands a prefix safe alone; --record records a landing that already happened.",
    )
    p.add_argument("id", metavar="ID", help="the quest")
    p.add_argument("--upto", metavar="REF", help="land the branch up to this commit, as an increment")
    p.add_argument("--reason", metavar="TEXT", help="why the increment is safe to land alone")
    p.add_argument("--push", action="store_true", help="push main after landing")
    p.add_argument("--no-ci", metavar="REASON", help="land without green CI, saying why (recorded with the landing)")
    p.add_argument(
        "--record", metavar="REF", help="record a landing that already happened (with --reason; unchecked gates kept)"
    )
    p.add_argument(
        "--earlier",
        action="store_true",
        help="with --record: it landed before this campaign, reviewed and verified by the process of its time, "
        "which --reason names (its ledger, review and CI); no gate is listed as unverified",
    )
    p.add_argument("--keep", action="store_true", help="keep the quest's worktrees, branches and build slot")
    p.set_defaults(fn=cmd_land)

    p = command(
        "message",
        "send a finding to a quest or a lead, from anywhere",
        "Sends a message to a quest (ID, or NAME:ID from outside its home) or to a campaign's lead "
        "(NAME:, or : for this campaign's), from anywhere on the machine. A quest's message stays open, shown by "
        "status and brief and blocking its landing, until one of its journals names the message's id or the "
        "lead closes it; a lead's message until it is closed (--close MSG --reason R, from the "
        "home). For a campaign on another machine named in `campaign config peers`, the message goes to this "
        "campaign's outbox, which `campaign wait` there fetches. Starting a quest sends a message to the "
        "leads of the other campaigns on this machine whose work it overlaps.",
    )
    p.add_argument("quest", metavar="TARGET", help="ID, NAME:ID, NAME: or :")
    p.add_argument("text", metavar="TEXT", nargs="?", default="", help="what was found")
    p.add_argument(
        "--from",
        dest="by",
        metavar="WHO",
        default=os.environ.get("USER", "someone"),
        help="who found it (default $USER)",
    )
    p.add_argument("--close", metavar="MSG", help="close an open message (the lead)")
    p.add_argument("--reason", metavar="TEXT", help="why it is closed")
    p.set_defaults(fn=cmd_message)

    p = command(
        "clean",
        "remove a landed or dropped quest's worktrees and branches",
        "Removes a landed or dropped quest's worktrees, its merged branches and its build slot (a final `land` "
        "does this itself). A worktree with uncommitted or untracked files, or that a job's command, directory or "
        "environment names, stops it unless --force; an unmerged branch is kept unless --force. Committed "
        "files, journals, reviews and job records stay.",
    )
    p.add_argument("id", metavar="ID", help="the quest")
    p.add_argument("--force", action="store_true", help="remove them anyway")
    p.set_defaults(fn=cmd_clean)

    p = command(
        "wait",
        "block until jobs finish, or until anything new happens; no tokens",
        "Blocks until the jobs finish (exit 0 all done, 1 a failure, 3 the timeout). With --commits, until "
        "anything new happens in the campaign since the last such wait: a quest branch moves, a job ends, or "
        "a message arrives. Like `check`, it tells the owner of a message to a lead undelivered in cmux over 15 "
        "minutes.",
    )
    p.add_argument("jobs", nargs="*", metavar="JOB", help="the jobs to wait for")
    p.add_argument("--quest", metavar="ID", help="every pending job of this quest")
    p.add_argument("--any", action="store_true", help="return when the first one finishes")
    p.add_argument(
        "--commits",
        action="store_true",
        help="return at the first quest-branch commit, ended job or message of this campaign not seen by an earlier wait",
    )
    p.add_argument("--timeout", type=float, default=0, metavar="SECONDS", help="exit 3 when it passes (default: none)")
    p.set_defaults(fn=cmd_wait)

    p = command(
        "worker",
        "run queued jobs until the queue is empty",
        "Runs queued jobs under quiet, in priority order, and exits when nothing is queued or running. A "
        "submit or a wait starts one, so it is rarely run by hand.",
        parents=False,
    )
    p.add_argument(
        "--restart",
        action="store_true",
        help="have the running worker finish its tick and restart on the current code; running jobs keep running",
    )
    p.set_defaults(fn=cmd_worker)

    p = command(
        "window",
        "let queued drains run back to back for a while",
        "Opens a window in which every queued strict or exclusive job that drains the machine (exclusive, "
        "or strict where quiet cannot pin cores) starts as soon as the last one ends, while new light jobs "
        "wait; it closes after --minutes or with --close. Without a window, drains and light work take "
        "turns. With no option it shows the open window.",
        parents=False,
    )
    p.add_argument("--minutes", type=float, default=0, metavar="N", help="open it for N minutes")
    p.add_argument("--for", dest="purpose", default="", metavar="TEXT", help="what it is for, recorded with it")
    p.add_argument("--close", action="store_true", help="close the open window now")
    p.set_defaults(fn=cmd_window)

    p = sub.add_parser("_run")  # a job's runner, started by the worker
    p.add_argument("jobdir")
    p.set_defaults(fn=cmd_run)

    jobs = command(
        "job",
        "submit and inspect machine jobs",
        "Machine jobs: commands run at an exact commit in their own checkout under quiet, their results "
        "recorded and reused by identity (code, command, toolchain).",
        parents=False,
    )
    job = jobs.add_subparsers(dest="job_command", metavar="COMMAND", title="commands", required=True)

    def job_command(name: str, summary: str, description: str) -> argparse.ArgumentParser:
        return job.add_parser(name, parents=[common], help=summary, description=description)

    p = job_command(
        "submit",
        "queue a command at an exact commit",
        "Queues CMD at an exact commit (the quest's branch with --quest, else HEAD) in its own checkout, under "
        "quiet at --level. An identical job that is pending or still counts is returned instead. Paths "
        "outside the checkout are not part of its identity (a --quest job's holds the files its command "
        "names). A --quest job cancels the quest's queued jobs it replaces.",
    )
    p.add_argument("--quest", metavar="ID", help="the quest it is evidence for")
    p.add_argument("--for", dest="criteria", metavar="A1,A2", help="the criteria it proves by passing")
    p.add_argument("--fails-without", action="store_true", help="--for a mutant: exits 0 as the test fails")
    p.add_argument("--repo", metavar="PATH", help="repository (default: this one, or the quest's)")
    p.add_argument("--commit", metavar="REF", help="the commit to run (default: HEAD, or the quest's branch)")
    p.add_argument(
        "--ref",
        action="append",
        default=[],
        metavar="NAME=[PATH@]REF",
        help="another commit, of any repository, at $JOB_REF_NAME",
    )
    p.add_argument("--cwd", metavar="DIR", help="the directory to run in, inside the checkout")
    p.add_argument("--level", choices=LEVELS, default="light", help="its quiet level (default light)")
    p.add_argument(
        "--minutes",
        type=int,
        metavar="N",
        help=f"its bound (default 30; at most {SHORT_RUN} if heavy, {LIGHT_RUN} for a light fuzz or soak)",
    )
    p.add_argument("--long", metavar="REASON", help="lift that cap; the job is deferred")
    p.add_argument(
        "--priority",
        type=int,
        metavar="N",
        help="higher runs first (default 0; 1 for a light job of an active --quest)",
    )
    p.add_argument("--after", metavar="JOBS", help="jobs that must succeed first")
    p.add_argument("--env", action="append", default=[], metavar="K=V", help="part of the job's identity")
    p.add_argument("--slot", metavar="NAME", help="reuse a named checkout (default for a --quest light job)")
    p.add_argument("--fresh", action="store_true", help="a fresh checkout instead of the quest's build slot")
    p.add_argument("--keep", action="store_true", help="keep the source checkout")
    p.add_argument("--retries", type=int, default=0, metavar="N", help="requeues after a lost runner")
    p.add_argument("--what", metavar="TEXT", help="what it is for, as status shows it")
    p.add_argument("--unversioned", action="store_true", help="run in --cwd as it is (probes; never evidence)")
    p.add_argument("--force", action="store_true", help="run even if an identical job succeeded")
    p.add_argument("--sync", action="store_true", help="run now and wait (no worker); same records")
    p.add_argument("cmd", nargs=argparse.REMAINDER, metavar="-- CMD", help="the command and its arguments")
    p.usage = "%(prog)s [options] -- CMD [ARG ...]"
    p.set_defaults(fn=cmd_job_submit)
    p = job_command(
        "list",
        "list jobs",
        "Lists jobs, oldest first: id, state, level, quest, what and exit code. Where a campaign is found, only "
        "its jobs; --all lists every campaign's on this machine.",
    )
    p.add_argument("--quest", metavar="ID", help="only this quest's")
    p.add_argument("--state", help="queued, running, done, failed, timeout, interrupted, cancelled or skipped")
    p.add_argument("--all", action="store_true", help="every campaign's jobs on this machine")
    p.set_defaults(fn=cmd_job_list)
    p = job_command(
        "show", "a job's spec and state", "Prints a job's spec and state, and where its log and output are, as JSON."
    )
    p.add_argument("job", metavar="JOB", help="the job's id")
    p.set_defaults(fn=cmd_job_show)
    p = job_command("log", "the end of a job's log", "Prints the last lines of a job's log.")
    p.add_argument("job", metavar="JOB", help="the job's id")
    p.add_argument("-n", "--lines", type=int, default=40, metavar="N", help="how many (default 40)")
    p.set_defaults(fn=cmd_job_log)
    p = job_command(
        "rerun",
        "the same jobs at the quest's head",
        "Resubmits jobs with their command, level, directory, environment and other repositories at another "
        "commit: by default the head of their quest's branch, else of their repository; a job whose commit was "
        "never on the quest's branches or main (a mutant) is refused without --commit. Each cancels its "
        "quest's still-queued jobs it replaces (the same job at newer code), unless a pending job waits on one.",
    )
    p.add_argument("jobs", nargs="+", metavar="JOB", help="the jobs to run again")
    p.add_argument("--commit", metavar="REF", help="the commit to run (default: the quest's branch, else HEAD)")
    p.set_defaults(fn=cmd_job_rerun)
    p = job_command(
        "prune",
        "delete old finished jobs nothing needs; slim the ones evidence names",
        "For finished jobs that ended more than --days ago: deletes those nothing names, with their kept "
        "checkouts and identity links; slims those only landed or dropped quests name (a landing, acceptance, "
        "deferred check, journal or review): their kept checkouts and output files over 1 MiB go, their spec, "
        "state, log and smaller outputs stay, and they are never reused by identity again. Jobs of quests not "
        "landed or dropped, and of campaigns this machine does not know, are kept whole. Build slots no open "
        "quest uses go too, unless a pending job names one or a quest's job ran in it within 7 days, "
        "and the build directories (build, build-*, .deps, _deps, ignored by git) of worktrees "
        "of the campaigns' repositories that no unfinished quest owns, once nothing in them changed for "
        "--build-days. Each deletion is logged in the job store's prune.log. The worker runs this with its "
        "defaults once a day when its queue empties (CAMPAIGN_AUTO_PRUNE=0 turns that off).",
    )
    p.add_argument(
        "--days", type=float, default=14, metavar="N", help="keep jobs that ended within N days (default 14)"
    )
    p.add_argument(
        "--build-days",
        type=float,
        default=BUILD_DAYS,
        metavar="N",
        help=f"keep build directories changed within N days (default {BUILD_DAYS:g})",
    )
    p.add_argument("--dry-run", action="store_true", help="list what would be deleted")
    p.set_defaults(fn=cmd_job_prune)
    p = job_command("cancel", "cancel a queued or running job", "Cancels a queued job, or stops a running one.")
    p.add_argument("job", metavar="JOB", help="the job's id")
    p.set_defaults(fn=cmd_job_cancel)

    add_help(sub, "campaign")
    return ap


# options that bypass or weaken a safeguard: each needs a non-empty reason, which is recorded
REASONS = {"no_ci": "--no-ci", "weaken": "--weaken", "fix_scope": "--fix-scope"}
REASONS.update(reason="--reason", force="--force", long="--long")


def main(argv: list[str] | None = None) -> int:
    ap = parser()
    args, extra = ap.parse_known_args(argv)
    if args.chdir:
        try:
            os.chdir(args.chdir)
        except OSError as e:
            ap.error(f"-C {args.chdir}: {e.strerror}")
    if args.command == "help":
        return show_help(ap, args.topic)
    if getattr(args, "campaign", None):
        os.environ["CAMPAIGN"] = args.campaign  # job runners and the worker inherit it
    # Python 3.9's argparse leaves a trailing list after an option unparsed (`get ID --task T files status`)
    trailing = {"set": "assignments", "get": "keys", "config": "assignments", "wait": "jobs", "rerun": "jobs"}
    dest = trailing.get(getattr(args, "job_command", None) or args.command, "")
    if extra and dest and not any(x.startswith("-") for x in extra):
        setattr(args, dest, [*getattr(args, dest), *extra])
    elif extra:
        ap.error(f"unrecognized arguments: {' '.join(extra)}")
    for dest, flag in REASONS.items():
        value = getattr(args, dest, None)
        if isinstance(value, str) and not value.strip():
            ap.error(f"{flag} needs a reason")
    if args.command not in ("job", "_run", "worker"):
        note_lead()
    try:
        return int(args.fn(args))
    except CampaignError as e:
        print(f"campaign: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
