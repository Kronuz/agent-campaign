#!/usr/bin/env python3
"""campaign: quests, worktrees, landing and a host job queue for multi-agent projects.

State lives in .campaign/NAME/state.json in the campaign's home repository and is written only by
this tool, from that repository's main checkout. Jobs live in $CAMPAIGN_JOBS
(~/.scratch/campaign/jobs) on the host that runs them. `campaign COMMAND --help` documents each
command; the skill's SKILL.md says when to use it.
"""

from __future__ import annotations

import argparse
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
from collections.abc import Callable, Collection, Generator
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn

import report
from style import mark, paint, styled

SKILL = Path(__file__).resolve().parent.parent
TEMPLATES = SKILL / "templates"
META = ".campaign"  # .campaign/NAME/ holds a campaign's files in its home repository
LEGACY_META = "docs/campaign"  # where an older campaign keeps them until `campaign migrate`
EXCLUDE_META = (f":(exclude){META}", f":(exclude){LEGACY_META}")
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
GAME_MASTER_FILES = ("state.json", "PLAN.md", "quests/", "reviews/")
STATUSES = ("planned", "active", "review", "landed", "blocked", "dropped")
FLIGHT = ("active", "review", "blocked")  # in flight: started, neither landed nor dropped
RISKS = ("low", "normal", "high", "critical")
LENSES = ("correctness", "security", "durability", "design", "system")
CLASSES = ("sage", "wizard", "artificer", "scout", "bard", "squire")
ROLES = ("adventurer", "paladin", "scout")
LEVELS = ("none", "light", "strict", "exclusive")
DEFAULT_LENSES: dict[str, list[str]] = {
    "low": [],
    "normal": ["correctness"],
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
TERMINAL = ("done", "failed", "timeout", "interrupted", "cancelled", "skipped")
HEAVY = ("strict", "exclusive")
DEFERRED_HEAVY_MAX = 10  # minutes: a gate job never waits longer behind a deferred measurement
STALE = float(os.environ.get("CAMPAIGN_STALE_SECONDS", "60"))
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
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def template(name: str, **fields: str) -> str:
    text = (TEMPLATES / name).read_text()
    for k, v in fields.items():
        text = text.replace("{" + k + "}", v)
    return text


def boot_id() -> str:
    linux = Path("/proc/sys/kernel/random/boot_id")
    if linux.exists():
        return linux.read_text().strip()
    return sh(["sysctl", "-n", "kern.boottime"], check=False)


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
        if name and git_ok(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{name}"):
            return name
    die(f"{repo}: no main or master branch (set CAMPAIGN_MAIN)")


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
        _heads.clear()


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
    """ask(), remembered when every argument after the first two is a commit id."""
    if not all(SHA.fullmatch(x) for x in key[2:]):
        return ask()
    if key not in _fixed:
        _fixed[key] = ask()
    return _fixed[key]


def rev(repo: Path, ref: str) -> str:
    return branch_head(repo, ref) or git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")


def has_ref(repo: Path, ref: str) -> bool:
    return bool(branch_head(repo, ref)) or git_ok(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")


def ancestor(repo: Path, a: str, b: str) -> bool:
    return bool(on_commits(("ancestor", str(repo), a, b), lambda: git_ok(repo, "merge-base", "--is-ancestor", a, b)))


def merge_base(repo: Path, a: str, b: str) -> str:
    return str(on_commits(("merge-base", str(repo), a, b), lambda: git(repo, "merge-base", a, b)))


def changed(repo: Path, a: str, b: str) -> list[str]:
    """Paths that differ between the trees of a and b."""
    return list(
        on_commits(
            ("changed", str(repo), a, b), lambda: [x for x in git(repo, "diff", "--name-only", a, b).splitlines() if x]
        )
    )


def meta_rel(path: str) -> str | None:
    """A campaign file's path inside its campaign's directory; None for code."""
    if path.startswith(META + "/"):
        return "/".join(path.split("/")[2:])
    if path.startswith(LEGACY_META + "/"):
        return path[len(LEGACY_META) + 1 :]
    return None


def is_meta(path: str) -> bool:
    return meta_rel(path) is not None


def game_master_file(path: str) -> bool:
    rel = meta_rel(path)
    return rel is not None and any(rel == o or (o.endswith("/") and rel.startswith(o)) for o in GAME_MASTER_FILES)


_cat: dict[str, tuple[int, subprocess.Popen[bytes]]] = {}
_trees: dict[tuple[str, str], str] = {}


def cat_object(repo: Path, name: str) -> tuple[str, bytes]:
    """An object's id and raw content, through one `git cat-file --batch` per repository."""
    held = _cat.get(str(repo))
    if not held or held[0] != os.getpid():
        proc = subprocess.Popen(
            ["git", "-C", str(repo), "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE
        )
        held = _cat[str(repo)] = (os.getpid(), proc)
    proc = held[1]
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(name.encode() + b"\n")
    proc.stdin.flush()
    header = proc.stdout.readline().split()
    if len(header) != 3:
        die(f"{repo}: no object {name}")
    data = proc.stdout.read(int(header[2]))
    proc.stdout.read(1)
    return header[0].decode(), data


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
    """The commit's tree without the campaign's files (.campaign, and docs/campaign of older
    campaigns): what builds and tests see, so evidence survives commits that touch only campaign
    files (a journal, a review, the move by `campaign migrate`). Computed from the raw trees,
    since status asks for every job's."""
    key = (str(repo), commit)
    if key in _trees:
        return _trees[key]
    oid, root = cat_object(repo, f"{commit}^{{tree}}")
    width = len(oid) // 2
    kept: list[bytes] = []
    changed = False
    for name, entry in tree_entries(root, width):
        docs = name == b"docs" and entry.startswith(b"40000 ")
        inner = tree_entries(cat_object(repo, entry[-width:].hex())[1], width) if docs else []
        rest = [e for n, e in inner if n != b"campaign"]
        if name == META.encode() or len(rest) != len(inner):
            changed = True
            if rest:
                kept.append(b"40000 docs\0" + tree_id(rest, width))
        else:
            kept.append(entry)
    tree = tree_id(kept, width).hex() if changed else oid
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):  # a ref may move; a commit's tree never does
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
    found = {f.parent.name: f"{META}/{f.parent.name}" for f in sorted((home / META).glob("*/state.json"))}
    legacy: Json | None = None
    with contextlib.suppress(CampaignError):
        legacy = read_json(home / LEGACY_META / "state.json")
    if legacy and legacy.get("campaign") and legacy["campaign"] not in found:
        found[str(legacy["campaign"])] = LEGACY_META
    return found


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
    "base": str,
    "tasks": dict,
    "accepted": dict,
    "landed": list,
    "notes": list,
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
        """How jobs and wait records name this campaign: its directory (its home, for an older one)."""
        return str(self.home) if self.meta == LEGACY_META else str(self.home / self.meta)

    @property
    def quests(self) -> dict[str, Json]:
        return self.state["quests"]

    def quest(self, pid: str) -> Json:
        if pid not in self.quests:
            die(f"no quest {pid}")
        return self.quests[pid]

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


def notify(title: str, body: str) -> None:
    """A notice in the patron's cmux feed; silent outside cmux."""
    if os.environ.get("CMUX_WORKSPACE_ID"):
        report.notify(title, body)


def in_flight(counts: dict[str, int]) -> str:
    """ "2 in flight (1 active, 1 blocked)": the quests started and not landed, by stage."""
    stages = [f"{counts[s]} {'in review' if s == 'review' else s}" for s in FLIGHT if counts[s]]
    return f"{sum(counts[s] for s in FLIGHT)} in flight" + (f" ({', '.join(stages)})" if stages else "")


def overview(c: Campaign) -> Json:
    """Counts by status, the quests ready to start, and the first milestone not yet landed."""
    live = {k: p for k, p in c.quests.items() if p["status"] != "dropped"}
    counts = {s: sum(1 for p in live.values() if p["status"] == s) for s in STATUSES}
    order: list[str] = []
    for p in live.values():
        if p.get("milestone", "") not in order:
            order.append(p.get("milestone", ""))
    current = next(
        (m for m in order if any(p.get("milestone", "") == m and p["status"] != "landed" for p in live.values())), None
    )
    ready = [k for k, p in live.items() if p["status"] == "planned" and all(c.landed(d) for d in p["needs"])]
    view: Json = {"counts": counts, "ready": ready, "milestone": None}
    if current is not None:
        members = [k for k, p in live.items() if p.get("milestone", "") == current]
        done = sum(1 for k in members if live[k]["status"] == "landed")
        view["milestone"] = {
            "name": current,
            "landed": done,
            "quests": len(members),
            "ready": [k for k in ready if k in members],
        }
    return view


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
    for pid, p in c.quests.items():
        if p["status"] in FLIGHT:
            want[f"status:campaign-{c.name}-{pid}"] = f"{pid}: {p['status']}" + (
                f" ({p.get('reason', '')})" if p["status"] == "blocked" else ""
            )
    if m is not None:
        label = f"{name}{' ' + m['name'] if m['name'] else ''}: {m['landed']} of {m['quests']} quests landed" + (
            f" · ready: {', '.join(m['ready'][:3])}" if m["ready"] else ""
        )
        want["bar"] = f"{m['landed']}/{m['quests']}\t{label}"
    cache = Path(git(c.home, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "campaign-sidebar.json"
    view = f"{workspace} {c.ident}"
    seen: dict[str, str] = {}
    with contextlib.suppress(FileNotFoundError, json.JSONDecodeError, AttributeError):
        views = json.loads(cache.read_text())
        # a view from before a home could hold several campaigns is keyed by the workspace alone
        seen = views[view] if view in views else views.get(workspace, {})
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
    minutes = parts[2] if len(parts) > 2 else "30"
    if level not in LEVELS or not minutes.isdigit() or int(minutes) < 1:
        die(f"--check {spec!r}: level is one of {', '.join(LEVELS)}, minutes a positive integer")
    if later and level in HEAVY and int(minutes) > DEFERRED_HEAVY_MAX:
        die(f"--later {spec!r}: a deferred {level} check lasts at most {DEFERRED_HEAVY_MAX} minutes; chain longer runs")
    check: Json = {"name": parts[0], "level": level, "minutes": int(minutes), "cmd": cmd.strip()}
    if later:
        check["later"] = True
    return check


def gates(p: Json) -> list[Json]:
    return [x for x in p["checks"] if not x.get("later")]


def deferred(p: Json) -> list[Json]:
    return [x for x in p["checks"] if x.get("later")]


def stem(glob: str) -> str:
    cuts = [i for i in (glob.find(ch) for ch in "*?[") if i >= 0]
    return glob[: min(cuts)] if cuts else glob


def overlap(a: list[str], b: list[str]) -> bool:
    """Whether two file sets may share a file (no globs means the whole repository)."""
    if not a or not b:
        return True
    return any(stem(x).startswith(stem(y)) or stem(y).startswith(stem(x)) for x in a for y in b)


def outside(paths: list[str], globs: list[str]) -> list[str]:
    return [x for x in paths if not is_meta(x) and globs and not any(fnmatch.fnmatch(x, g) for g in globs)]


def brief_hash(c: Campaign, pid: str) -> str:
    brief = c.home / c.meta / "quests" / f"{pid}.md"
    return hashlib.sha256(brief.read_bytes()).hexdigest()[:16] if brief.exists() else ""


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


def note_inbox(c: Campaign, pid: str) -> Path:
    """Where notes for a quest wait, in the home's .git: any agent on the machine can leave one there
    without being the campaign's one writer."""
    common = Path(git(c.home, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    return common / "campaign-notes" / c.state["campaign"] / pid


def inbox_notes(c: Campaign, pid: str) -> list[Json]:
    found: list[Json] = []
    folder = note_inbox(c, pid)
    for f in folder.glob("note-*.json") if folder.is_dir() else []:
        with contextlib.suppress(CampaignError):
            note = read_json(f)
            if note:
                found.append(note)
    return sorted(found, key=lambda n: int(str(n["id"]).partition("-")[2] or 0))


def answered(note: Json, journals: Collection[str]) -> bool:
    """Whether a journal names the note by its id."""
    return any(re.search(rf"\b{re.escape(str(note['id']))}\b", j) for j in journals)


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
    plan = top / folder / "PLAN.md"
    if not plan.exists():
        atomic_write(plan, template("PLAN.md", campaign=name))
    if os.environ.get("CAMPAIGN_NO_COMMIT") != "1":
        git(top, "add", "--", folder)
        git(top, "commit", "-q", "-m", f"campaign: init {name}", "--", folder)
    print(f"campaign {name} in {top}; write the goals in {folder}/PLAN.md")
    return 0


def migration_blockers(c: Campaign) -> tuple[list[str], list[str]]:
    """(quests in flight, jobs queued or running): what keeps an older campaign from moving."""
    busy = sorted(pid for pid, p in c.quests.items() if p["status"] in FLIGHT)
    pending = sorted(
        j["id"] for j in all_jobs() if j["spec"].get("campaign") == c.ident and j["state"].get("state") not in TERMINAL
    )
    return busy, pending


def migration_wait(c: Campaign) -> str:
    """When `campaign migrate` can run: now (""), or after what."""
    busy, pending = migration_blockers(c)
    if busy:
        return f" once no quest is in flight (now: {', '.join(busy)})"
    return " once its jobs finish" if pending else ""


def cmd_migrate(_a: argparse.Namespace) -> int:
    """Moves an older campaign's files to .campaign/NAME in one commit. Quests in flight carry
    journals at the old path on their branches, and the campaign's pending jobs carry its old tag,
    so it waits until there are none."""
    with writing() as c:
        folder = f"{META}/{c.name}"
        if c.meta != LEGACY_META:
            print(f"campaign {c.name} already keeps its files in {c.meta}")
            return 0
        busy, pending = migration_blockers(c)
        if busy:
            die(
                f"quests in flight keep journals in {LEGACY_META} on their branches: land or drop {', '.join(busy)} first"
            )
        if pending:
            die(f"jobs {', '.join(pending)} are queued or running under the old layout: `campaign wait` for them first")
        if os.environ.get("CAMPAIGN_NO_COMMIT") == "1":
            die("migrate commits the move; unset CAMPAIGN_NO_COMMIT")
        (c.home / META).mkdir(exist_ok=True)
        git(c.home, "mv", LEGACY_META, folder)
        git(c.home, "commit", "-q", "-m", f"campaign: move {LEGACY_META} to {folder}", "--", LEGACY_META, folder)
        with contextlib.suppress(OSError):
            (c.home / "docs").rmdir()  # only when the campaign was all it held
    print(f"campaign {c.name} keeps its files in {folder}")
    return 0


CONFIG = ("ci", "ci_later")


def cmd_config(a: argparse.Namespace) -> int:
    """Prints the campaign-wide settings as key=value lines, after setting any given."""
    if not a.assignments:
        c = find_campaign(Path.cwd())
    else:
        with writing() as c:
            for item in a.assignments:
                key, sep, value = item.partition("=")
                if key == "ci" and sep and value in ("github", ""):
                    c.state["ci"] = value
                elif key == "ci_later" and sep:
                    c.state["ci_later"] = csv(value)
                else:
                    die(f"campaign config takes ci=github|'' and ci_later=WORKFLOW,..., not {item!r}")
            c.save("campaign: config " + " ".join(a.assignments))
    for key in CONFIG:
        value = c.state.get(key) or ""
        print(f"{key}={','.join(value) if isinstance(value, list) else value}")
    return 0


def cmd_new(a: argparse.Namespace) -> int:
    with writing() as c:
        if a.id in c.quests:
            die(f"{a.id} exists")
        repo = (c.home / a.repo).resolve()
        if not git_ok(repo, "rev-parse", "--git-dir"):
            die(f"{repo} is not a git repository")
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
            "checks": [parse_check(x) for x in a.check] + [parse_check(x, later=True) for x in a.later],
            "lenses": csv(a.lenses) if a.lenses is not None else list(DEFAULT_LENSES[a.risk]),
            "milestone": a.milestone or "",
            "accepted": {},
            "landed": [],
            "updated": stamp(),
        }
        brief = f"{c.meta}/quests/{a.id}.md"
        if not (c.home / brief).exists():
            atomic_write(c.home / brief, template("quest.md", id=a.id, title=a.title, risk=a.risk))
        c.save(f"campaign: new {a.id} ({a.risk}): {a.title}", (brief,))
    print(f"{a.id}: planned; write its outcome and criteria in {brief}")
    return 0


def cmd_set(a: argparse.Namespace) -> int:
    with writing() as c:
        p = c.quest(a.id)
        if a.task:
            task = p.get("tasks", {}).get(a.task)
            if task is None:
                die(f"{a.id} has no task {a.task}")
            given = dict(x.partition("=")[::2] for x in a.assignments)
            if not given or set(given) - {"files", "status", "reason"} or a.check or a.later:
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
            elif key in ("title", "risk", "reason", "repo", "milestone"):
                p[key] = value
            else:
                die(f"cannot set {key}")
        if a.check:
            p["checks"] = [parse_check(x) for x in a.check] + deferred(p)
        if a.later:
            p["checks"] = gates(p) + [parse_check(x, later=True) for x in a.later]
        if p["status"] not in ("blocked", "dropped") and not any(x.startswith("reason=") for x in a.assignments):
            p.pop("reason", None)
        p["updated"] = stamp()
        c.save(
            f"campaign: set {a.id} "
            + " ".join(a.assignments + [f"check={x}" for x in a.check] + [f"later={x}" for x in a.later])
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
            clash = [
                x
                for x, q in c.quests.items()
                if x != a.id
                and q["status"] in ("active", "review")
                and c.repo(q) == repo
                and overlap(p["files"], q["files"])
            ]
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
        if wt.exists():
            die(f"{wt} exists")
        if has_ref(repo, branch):
            git(repo, "worktree", "add", "-q", str(wt), branch)
            base = merge_base(repo, base, branch)
        else:
            git(repo, "worktree", "add", "-q", "-b", branch, str(wt), base)
        gitdir = Path(git(wt, "rev-parse", "--path-format=absolute", "--git-dir"))
        (gitdir / "campaign-home").write_text(f"{c.home}\n{c.name}\n")
        work = Path(c.meta) / "journal" / f"{name}.md"
        if not (wt / work).exists():
            atomic_write(wt / work, template("journal.md", id=name, brief=f"{c.meta}/quests/{a.id}.md"))
            git(wt, "add", "--", str(work))
            git(wt, "commit", "-q", "-m", f"campaign: start {name}", "--", str(work))
        rel = os.path.relpath(wt, c.home)
        if a.task:
            p.setdefault("tasks", {})[a.task] = {
                "branch": branch,
                "worktree": rel,
                "base": base,
                "files": csv(a.files) or p["files"],
            }
        else:
            p.update(branch=branch, worktree=rel, base=base, status="active")
            if a.force:
                p["reason"] = f"started by force: {a.force}"
        p["updated"] = stamp()
        c.save(f"campaign: start {name} on {branch} at {base[:12]}")
    print(f"{name}: {branch} in {wt}")
    return 0


def cmd_accept(a: argparse.Namespace) -> int:
    with writing() as c:
        p = c.quest(a.id)
        if a.lens not in LENSES:
            die(f"unknown lens {a.lens}")
        if not p.get("branch"):
            die(f"{a.id} has no branch")
        repo = c.repo(p)
        commit = rev(repo, a.commit or p["branch"])
        verdict = ""
        if a.waive:
            if p["risk"] in ("high", "critical"):
                die(f"{a.id} is {p['risk']} risk: its {a.lens} review cannot be waived")
        else:
            review = c.home / c.meta / "reviews" / f"{a.id}-{a.lens}.md"
            if not review.exists():
                die(f"no review at {review.relative_to(c.home)} (or --waive REASON)")
            m = re.search(r"^Verdict:\s*\**\s*(APPROVE|CHANGES|REJECT)\b", review.read_text(), re.M)
            if not m:
                die(f"{review.name}: no 'Verdict: APPROVE|CHANGES|REJECT' line")
            verdict = m.group(1)
            if verdict == "REJECT":
                die(f"{a.lens} review rejected {a.id}: re-plan the quest")
            if verdict == "CHANGES" and not a.jobs:
                die("CHANGES: confirm each blocking fix with a job that reruns its probe, then --jobs")
        confirmations: list[Json] = []
        for jid in csv(a.jobs):
            job = load_job(jobs_dir() / jid)
            if job["state"].get("state") != "done":
                die(f"job {jid} is {job['state'].get('state')}, not done")
            confirmations.append(job_summary(job))
        main_head = rev(repo, main_branch(repo))
        base = merge_base(repo, main_head, commit)
        paths = [x for x in changed(repo, base, commit) if not is_meta(x)]
        p.setdefault("accepted", {})[a.lens] = {
            "commit": commit,
            "base": base,
            "brief": brief_hash(c, a.id),
            "by": "waived" if a.waive else "review",
            "verdict": verdict,
            "note": a.waive or a.note or "",
            "jobs": confirmations,
            "blobs": blobs(repo, commit, paths),
            "at": stamp(),
        }
        if a.lens not in p["lenses"] and not a.waive:
            p["lenses"].append(a.lens)
        p["updated"] = stamp()
        c.save(f"campaign: accept {a.id} {a.lens} at {commit[:12]}" + (" (waived)" if a.waive else ""))
    print(f"{a.id}: {a.lens} accepted at {commit[:12]} ({len(paths)} paths)")
    return 0


def drift(repo: Path, main_head: str, target: str, accepted: dict[str, str]) -> list[str]:
    """Quest paths whose content at target differs from what was accepted. Only the quest's own
    changes count, the paths accept records; what main changed is the rebase's business."""
    base = merge_base(repo, main_head, target)
    now_paths = [x for x in changed(repo, base, target) if not is_meta(x)]
    at_target = blobs(repo, target, now_paths)
    out = [x for x in now_paths if accepted.get(x) != at_target[x]]
    rest = [x for x in accepted if x not in at_target]
    at_main = blobs(repo, main_head, rest)
    return out + [x for x in rest if at_main[x] != accepted[x]]


def ci_state(repo: Path, commit: str, later: list[str]) -> str:
    if not shutil.which("gh"):
        return "gh is not installed"
    p = subprocess.run(
        ["gh", "run", "list", "--commit", commit, "--json", "status,conclusion,name", "--limit", "50"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if p.returncode != 0:
        return f"gh failed: {p.stderr.strip()}"
    runs = [r for r in json.loads(p.stdout or "[]") if r.get("name") not in later]
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


def check_evidence(c: Campaign, p: Json, target: str) -> tuple[list[str], list[Json]]:
    repo = c.repo(p)
    missing: list[str] = []
    evidence: list[Json] = []
    for check in gates(p):
        job = job_for_check(repo, target, check)
        if job is None:
            missing.append(f"{check['name']}: no job")
        elif job["state"].get("state") != "done":
            missing.append(f"{check['name']}: {job['state'].get('state')} ({job['id']})")
        else:
            evidence.append(job_summary(job))
    return missing, evidence


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def verdict(c: Campaign, pid: str, lens: str) -> str:
    """A paladin's verdict on the quest; "none yet" without a review file."""
    review = c.home / c.meta / "reviews" / f"{pid}-{lens}.md"
    if not review.exists():
        return "none yet"
    m = re.search(r"^Verdict:\s*\**\s*(\w+)", review.read_text(), re.M)
    return m.group(1) if m else "no verdict"


def review_step(said: str, pid: str, lens: str) -> str:
    """What a review's verdict asks for next."""
    if said == "APPROVE":
        return f"approved; `campaign accept {pid} {lens}`"
    if said == "CHANGES":
        return f"verdict CHANGES; fix what blocks, rerun its probes as jobs, then `campaign accept {pid} {lens} --jobs IDS`"
    if said == "REJECT":
        return "verdict REJECT; re-plan the quest"
    if said == "none yet":
        return f"not reviewed yet; launch a {lens} paladin"
    return f"its review has {said}; the paladin writes `Verdict: APPROVE|CHANGES|REJECT`"


def landing_gaps(c: Campaign, pid: str, target: str, *, final: bool, ci: bool) -> list[str]:
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
            f"{main} changed {plural(len(moved), 'file')} since {p['branch']} branched (e.g. {moved[0]}): rebase onto {main}, run `campaign verify {pid}`, then land"
        )
    paths = changed(repo, base, target)
    owned = [x for x in paths if game_master_file(x)]
    if owned:
        gaps.append(f"{p['branch']} edits the game master's files: {', '.join(owned)}")
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
    wanted = criteria(c, pid) if final else {}
    carried = journals_at(c, repo, target, pid) if final else {}
    rows = evidence(carried, list(wanted), cited_jobs([target])) if wanted else {}
    unproven = [x for x, r in rows.items() if not any(e["met"] for e in r)]
    if unproven:
        gaps.append(
            f"criteria without evidence in a journal's Evidence table at {target[:12]}: {id_ranges(unproven)} (`campaign status {pid}` lists them)"
        )
    unanswered = [n["id"] for n in inbox_notes(c, pid) if not answered(n, carried.values())] if final else []
    if unanswered:
        gaps.append(
            f"open notes: {', '.join(unanswered)}: answer each in a journal by its id, or close it (`campaign note {pid} --close NOTE --reason R`)"
        )
    stale = sorted({w.split()[0] for x in unproven for e in rows[x] for w in e["why"] if w.endswith("ran other code")})
    if stale:
        gaps.append(f"evidence jobs ran other code than {target[:12]}: `campaign job rerun {' '.join(stale)}`")
    for missing in check_evidence(c, p, target)[0]:
        name, _, why = missing.partition(": ")
        gaps.append(f"check {name} is not green at {target[:12]}: {why} (`campaign verify {pid}`)")
    for lens in p["lenses"] if final else []:
        acc = p.get("accepted", {}).get(lens)
        if not acc:
            gaps.append(f"{lens} review not accepted: {review_step(verdict(c, pid, lens), pid, lens)}")
        elif acc.get("brief") != brief_hash(c, pid):
            gaps.append(f"the quest file changed after {lens} was accepted: re-accept against the current criteria")
        elif lens != "design":  # design approves the approach before the code exists
            moved_since = drift(repo, main_head, target, acc["blobs"])
            upstream = [x for x in changed(repo, acc.get("base", base), base) if not is_meta(x)]
            if moved_since:
                gaps.append(f"{lens} was accepted at {acc['commit'][:12]}; changed since: {', '.join(moved_since[:8])}")
            elif upstream and p["risk"] in ("high", "critical"):
                gaps.append(
                    f"{lens}: {main} changed {plural(len(upstream), 'code file')} under this {p['risk']}-risk quest since its review (`git diff {acc.get('base', base)[:12]} {base[:12]}`); read them, then re-accept"
                )
    if ci and c.state.get("ci") == "github":
        state = ci_state(repo, target, c.state.get("ci_later", []))
        if state != "success":
            gaps.append(f"CI at {target[:12]}: {state} (--no-ci to land without it)")
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
            final = rev(repo, p["branch"]) == target if p.get("branch") else True
            p["landed"].append(
                {
                    "commit": target,
                    "tested": target,
                    "at": stamp(),
                    "final": final,
                    "reason": "recorded after the fact",
                    "evidence": [],
                }
            )
            if final:
                p["status"] = "landed"
            p["updated"] = stamp()
            c.save(f"campaign: record {a.id} landed at {target[:12]}")
            print(f"{a.id}: recorded {target[:12]}")
            return 0
        if p["status"] not in ("active", "review"):
            die(f"{a.id} is {p['status']}")
        final = a.upto is None
        if not final and not a.reason:
            die("an increment needs --reason (why this prefix is safe to land alone)")
        head = rev(repo, p["branch"])
        target = rev(repo, a.upto) if a.upto else head
        if not ancestor(repo, target, head):
            die(f"{a.upto} is not on {p['branch']}")
        if git(checkout, "symbolic-ref", "--quiet", "--short", "HEAD", check=False) != main:
            die(f"{checkout} is not on {main}")
        if git(checkout, "status", "--porcelain", "--untracked-files=no"):
            die(f"{checkout} has uncommitted changes")
        gaps = landing_gaps(c, a.id, target, final=final, ci=not a.no_ci)
        if gaps:
            die(f"{a.id} cannot land yet:\n  " + "\n  ".join(gaps))
        evidence = check_evidence(c, p, target)[1]
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
            "mode": mode,
            "at": stamp(),
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
        p["landed"].append(record)
        landed = record["commit"]
        filed: list[Json] = inbox_notes(c, a.id) if final else []
        if final:
            p["status"] = "landed"
            p.setdefault("notes", []).extend({**n, "answered": landed[:12]} for n in filed)
            if deferred(p):
                try:
                    record["later"] = submit_checks(c, a.id, deferred(p), landed, priority=-1)
                except CampaignError as e:
                    print(f"campaign: landed, but the deferred checks were not queued: {e}", file=sys.stderr)
        p["updated"] = stamp()
        c.save(message + f" at {landed[:12]}")
        for n in filed:
            (note_inbox(c, a.id) / f"{n['id']}.json").unlink(missing_ok=True)
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
    return 0


def cmd_clean(a: argparse.Namespace) -> int:
    with writing() as c:
        p = c.quest(a.id)
        if p["status"] not in ("landed", "dropped"):
            die(f"{a.id} is {p['status']}: clean only landed or dropped quests")
        repo = c.repo(p)
        busy = [
            j["id"] for j in all_jobs() if j["spec"].get("quest") == a.id and j["state"].get("state") not in TERMINAL
        ]
        if busy:
            die(f"jobs still pending for {a.id}: {', '.join(busy)}")
        for name, branch, rel, _ in branches(c, a.id):
            wt = (c.home / rel).resolve() if rel else None
            if wt and wt.exists():
                dirty = git(wt, "status", "--porcelain")
                if dirty and not a.force:
                    die(f"{wt} has uncommitted or untracked files (commit or remove them, or --force):\n{dirty}")
                git(repo, "worktree", "remove", *(["--force"] if a.force else []), str(wt))
            if has_ref(repo, branch):
                merged = ancestor(repo, branch, main_branch(repo))
                if merged or a.force:
                    git(repo, "branch", "-q", "-D" if a.force else "-d", branch)
                else:
                    print(f"kept {branch}: not merged (--force deletes it)")
            print(f"{name}: cleaned")
        worktree_remove(str(repo), jobs_dir() / "slots" / f"{c.state['campaign']}-{a.id}")
        p["worktree"] = ""
        for t in p.get("tasks", {}).values():
            t["worktree"] = ""
        p["updated"] = stamp()
        c.save(f"campaign: clean {a.id}")
    return 0


# ---------------------------------------------------------------- status and check


def journal_text(c: Campaign, name: str, branch: str, rel: str) -> str:
    """A quest's or task's journal: in its worktree, else on its branch."""
    path = f"{c.meta}/journal/{name}.md"
    wt = (c.home / rel).resolve() if rel else None
    if wt and (wt / path).exists():
        return (wt / path).read_text()
    if branch:
        p = c.quests[name.split("--", maxsplit=1)[0]]
        return git(c.repo(p), "show", f"{branch}:{path}", check=False)
    return ""


def journals_at(c: Campaign, repo: Path, commit: str, pid: str) -> dict[str, str]:
    """The quest's and its tasks' journals in a commit: what a landing would carry."""
    folder = f"{c.meta}/journal"
    names = git(repo, "ls-tree", "--name-only", f"{commit}:{folder}", check=False).splitlines()
    mine = sorted(n for n in names if n == f"{pid}.md" or (n.startswith(f"{pid}--") and n.endswith(".md")))
    return {n[:-3]: git(repo, "show", f"{commit}:{folder}/{n}", check=False) for n in mine}


def journal_state(c: Campaign, name: str, branch: str, rel: str) -> str:
    return state_of(journal_text(c, name, branch, rel))


def state_of(journal: str) -> str:
    m = re.search(r"^State:\s*(.+)$", journal, re.M)
    return m.group(1).strip() if m else "(no State line)" if journal.strip() else "(no journal)"


def section(text: str, title: str) -> str:
    m = re.search(rf"^## {title}\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1) if m else ""


def criteria(c: Campaign, pid: str) -> dict[str, str]:
    """A quest's acceptance criteria (A1, A2, ...) and the first line of each, from its quest file."""
    brief = c.home / c.meta / "quests" / f"{pid}.md"
    text = section(brief.read_text() if brief.exists() else "", "Acceptance criteria")
    found: dict[str, str] = {}
    for cid, line in re.findall(r"^\s*[-*]\s*\**(A\d+)\b\**[:.]?\**\s*(.*)$", text, re.M):
        if not line.startswith("<"):  # the template's unfilled placeholder
            found.setdefault(cid, line.strip())
    return found


UNMET = re.compile(r"<|(pending|queued|running|failed|missing|open|todo|none)\b", re.I)


JOB_ID = re.compile(r"\b\d{6}-\d{6}-[0-9a-f]{6}\b")


def cited_jobs(commits: list[str]) -> Callable[[str], str]:
    """What is wrong with a job an Evidence row cites; "" when it, or the same job rerun, passed at
    the code of one of these commits (`campaign job rerun` makes that one command)."""

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
        for commit in commits:
            with contextlib.suppress(CampaignError, KeyError):
                twin = job_with_key({**job["spec"], "commit": commit})
                if twin and twin["state"].get("state") == "done":
                    return ""
        return f"{jid} ran other code"

    return problem


def evidence(
    journals: dict[str, str], ids: list[str], cited: Callable[[str], str] | None = None
) -> dict[str, list[Json]]:
    """Each criterion's rows in the journals' Evidence tables (the quest's and its tasks'): which
    journal, what it cites, and whether that is evidence, a row whose other cells are not empty, a
    <placeholder>, pending or failed, and whose cited jobs (checked by `cited`) all passed at the code."""
    rows: dict[str, list[Json]] = {x: [] for x in ids}
    for name, text in journals.items():
        for row in section(text, "Evidence").splitlines():
            cells = [x.strip() for x in row.strip().strip("|").split("|")]
            if len(cells) < 2 or cells[0].startswith("<"):
                continue
            rest = [x for x in cells[1:] if x]
            met = bool(rest) and not any(UNMET.match(x) or set(x) <= set("-: ") for x in rest)
            why = [w for w in map(cited, dict.fromkeys(JOB_ID.findall(" ".join(rest)))) if w] if met and cited else []
            for cid in re.findall(r"\bA\d+\b", cells[0]):
                if cid in rows:
                    rows[cid].append(
                        {"journal": name, "evidence": " | ".join(rest), "met": met and not why, "why": why}
                    )
    return rows


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
    if c.meta == LEGACY_META:
        out.append(
            (
                "warning",
                f"this campaign keeps its files in {LEGACY_META}: run `campaign migrate`" + migration_wait(c),
            )
        )
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
        if not git_ok(repo, "rev-parse", "--git-dir"):
            out.append(("error", f"{pid}: repository {repo} is missing"))
            continue
        main = main_branch(repo)
        for entry in p.get("landed", []):
            if not ancestor(repo, entry["commit"], main):
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
            owned = [x for x in paths if game_master_file(x)]
            if owned:
                out.append(("warning", f"{name}: edits the game master's files {', '.join(owned[:5])}"))
            code = [x for x in paths if not is_meta(x)]
            if p["files"]:
                outside = [x for x in code if not any(fnmatch.fnmatch(x, g) for g in p["files"])]
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
            for qid, q in o["state"].get("quests", {}).items():
                if q.get("status") not in ("planned", "active", "review"):
                    continue
                for x in sorted(set(p["consumes"]) & set(q.get("provides", []))):
                    out.append(("warning", f"{pid}: contract {x} is changing in {name}:{qid}; build against its brief"))
                for x in sorted(set(p["provides"]) & set(q.get("consumes", []))):
                    out.append(
                        ("warning", f"{pid}: changes contract {x}, which {name}:{qid} consumes; tell its game master")
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


def quest_view(c: Campaign, pid: str, jobs: list[Json], brief: bool = False) -> Json:
    """A quest as status shows it: what it still needs ("missing"), its branch, checks, reviews and
    journals, and the jobs that failed at its current code, each noted or not in a journal. Brief
    keeps only what an agent acts on."""
    p = c.quests[pid]
    view: Json = {"id": pid, "status": p["status"], "risk": p["risk"], "title": p["title"], "missing": []}
    view["files"] = p["files"]
    unmet = [d for d in p["needs"] if not c.landed(d)]
    view["ready"] = p["status"] == "planned" and not unmet
    if p["status"] == "planned" and unmet:
        view["missing"] = [f"land {', '.join(unmet)} first"]
    elif p["status"] == "blocked":
        view["missing"] = [f"blocked: {p.get('reason', '')}"]
    repo = c.repo(p)
    mine = [j for j in jobs if j["spec"].get("quest") == pid and j["spec"].get("campaign") == c.ident]
    # failures count only at the code the quest has now; older ones are history (`campaign job list`)
    heads = set()
    if p["status"] in FLIGHT:
        heads = {code_tree(repo, rev(repo, b)) for _, b, _, _ in branches(c, pid) if has_ref(repo, b)}

    def at_head(j: Json) -> bool:
        with contextlib.suppress(CampaignError, KeyError):
            return code_tree(Path(j["spec"]["repo"]), j["spec"]["commit"]) in heads
        return False

    view["jobs"] = {s: sum(1 for j in mine if j["state"].get("state") == s) for s in ("queued", "running")}
    view["landed"] = [x["commit"][:12] + ("" if x.get("final") else " (increment)") for x in p["landed"]]
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
        view["missing"] = landing_gaps(c, pid, head, final=True, ci=False)
        if brief:
            for name, branch, rel, _ in branches(c, pid):
                texts[name] = journal_text(c, name, branch, rel)
    if not brief and "branch" in view:
        head = rev(repo, p["branch"])
        checks: dict[str, str] = {}
        view["checks"] = checks
        view["later"] = [x["name"] for x in deferred(p)]
        for check in gates(p):
            job = job_for_check(repo, head, check)
            checks[check["name"]] = job["state"].get("state", "?") if job else "not run at head"
            if job:
                checked.add(job["id"])
        view["reviews"] = {lens: verdict(c, pid, lens) for lens in p["lenses"]}
        for lens, acc in p.get("accepted", {}).items():
            if lens in view["reviews"]:
                view["reviews"][lens] = f"accepted at {acc['commit'][:12]}" + (
                    " (waived)" if acc["by"] == "waived" else ""
                )
        work: dict[str, Json] = {}
        view["journal"] = work
        for name, branch, rel, _ in branches(c, pid):
            wt = (c.home / rel).resolve() if rel else None
            dirty = len(git(wt, "status", "--porcelain").splitlines()) if wt and wt.exists() else -1
            texts[name] = journal_text(c, name, branch, rel)
            task = p.get("tasks", {}).get(name.partition("--")[2], {})
            state = f"dropped: {task.get('reason', '')}" if task.get("status") == "dropped" else state_of(texts[name])
            work[name] = {"state": state, "dirty": dirty, "worktree": rel}
        journal = texts.get(pid) or journal_text(c, pid, p["branch"], p.get("worktree", ""))
        wanted = criteria(c, pid)
        tasks = [w["state"] for name, w in work.items() if name != pid and not w["state"].startswith("dropped: ")]
        tips = [rev(repo, b) for _, b, _, _ in branches(c, pid) if has_ref(repo, b)]
        journals = {pid: journal, **{n: t for n, t in texts.items() if n != pid}}
        rows = evidence(journals, list(wanted), cited_jobs(tips))
        view["progress"] = {
            "criteria": len(wanted),
            "met": [x for x, r in rows.items() if any(e["met"] for e in r)],
            "unlisted": [x for x, r in rows.items() if not r],
            "detail": [{"id": x, "text": wanted[x], "rows": r} for x, r in rows.items()],
            "tasks": len(tasks),
            "tasks_done": sum(1 for t in tasks if re.match(r"(complete|done)\b", t, re.I)),
            "next": next_step(journal),
        }
    ended = [j for j in mine if j["state"].get("state") in TERMINAL and at_head(j)]
    view["done_at_head"] = sum(1 for j in ended if j["state"].get("state") == "done")
    view["notes"] = [{**n, "answered": answered(n, texts.values())} for n in inbox_notes(c, pid)]
    noted = "\n".join(texts.values())
    view["bad_at_head"] = [
        {**job_summary(j), "check": j["id"] in checked, "noted": j["id"] in noted}
        for j in ended
        if j["state"].get("state") in ("failed", "timeout", "interrupted")
    ]
    return view


def review_rows(c: Campaign) -> list[Json]:
    rows: list[Json] = []
    folder = c.home / c.meta / "reviews"
    for f in sorted(folder.glob("*.md")) if folder.exists() else []:
        text = f.read_text()
        m = re.search(r"^Verdict:\s*\**\s*(\w+)", text, re.M)
        table = [r for r in text.splitlines() if r.startswith("|") and not re.match(r"^\|\s*(ID|-)", r)]
        blocking = [r for r in table if re.match(r"^\|[^|]*\|\s*yes\s*\|", r, re.I)]
        defects = [r for r in blocking if re.match(r"^\|[^|]*\|[^|]*\|\s*defect\s*\|", r, re.I)]
        rows.append(
            {
                "file": f.name,
                "verdict": m.group(1) if m else "?",
                "findings": len(table),
                "blocking": len(blocking),
                "blocking_defects": len(defects),
            }
        )
    return rows


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
    "landed": ("green", "🏁"),
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
        print(f"    {mark(emoji)}{d['id']:<{pad}}  {paint(f'{state:<7}', style)}  {fit(d['text'], indent)}")
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
    g = v.get("progress")
    if g and (g["criteria"] or g["tasks"]):
        tasks = f"{g['tasks_done']} of {plural(g['tasks'], 'task')} complete" if g["tasks"] else ""
        if g["criteria"]:
            n = g["criteria"]
            met = f"{len(g['met'])} of {n} {'criterion' if n == 1 else 'criteria'} with evidence"
            met += f" ({id_ranges(g['met'])})" if g["met"] else ""
            print(f"  {bar(len(g['met']) / g['criteria'])} {met}" + (f" · {tasks}" if tasks else ""))
            if g["unlisted"]:
                print(f"  {mark('📝')}in no journal's Evidence table yet: {id_ranges(g['unlisted'])}")
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
    elif v["ready"]:
        print(f"  ready to start: `campaign start {v['id']}`")
    else:
        for gap in v["missing"]:
            print(f"  {gap}")
    for n in v.get("notes", []):
        if not n["answered"]:
            print(f"  {mark('📌')}" + paint(f"{n['id']} from {n['by']}", "yellow") + f" ({n['at'][:16]}): {n['text']}")
        elif full:
            print(f"  {mark('📌')}{n['id']} from {n['by']}: answered in a journal")
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
            if v["ready"]
            else f"campaign land {v['id']}"
            if "branch" in v and not v["missing"]
            else ""
        )
        q["to_land"] = v["missing"]
        q["notes"] = [{k: n[k] for k in ("id", "by", "at", "text")} for n in v.get("notes", []) if not n["answered"]]
        q["failed"] = [{k: f[k] for k in ("job", "state", "what")} for f in v["bad_at_head"] if not f["noted"]]
        q["pending"] = {s: k for s, k in v["jobs"].items() if k}
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
    for q in b["quests"]:
        where = f": {q['branch']} at {q['head']}, {plural(q['ahead'], 'commit')} ahead" if "branch" in q else ""
        print(f"{q['id']} {q['status']} {q['risk']}{where}")
        if "files" in q:
            print(f"  files: {', '.join(q['files']) or '(none)'}")
        if q["next"]:
            print(f"  ready to {q['next'].split()[1]}: `{q['next']}`")
        for gap in q["to_land"]:
            print(f"  - {gap}")
        for n in q["notes"]:
            print(f"  - {n['id']} from {n['by']}: {n['text']}")
        for f in q["failed"]:
            print(f"  - {f['job']} {f['state']}, in no journal yet: {f['what']}")
        if q["pending"]:
            print(f"  - jobs: {', '.join(f'{k} {s}' for s, k in q['pending'].items())}")


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
    views = [quest_view(c, pid, jobs, brief=a.brief) for pid in pids]
    queue = {s: sum(1 for j in jobs if j["state"].get("state") == s) for s in ("queued", "running")}
    if a.brief:
        b = brief(c, views, queue, scoped=bool(a.id))
        if a.json:
            print(json.dumps(b, indent=2))
        else:
            print_brief(b)
        return 0
    found: list[tuple[str, str]] = [] if a.brief else problems(c)
    reviews: list[Json] = review_rows(c) if a.reviews else []
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
                    "reviews": reviews,
                },
                indent=2,
            )
        )
        return 0
    n = o["counts"]
    print(f"{mark('🏰')}{paint('campaign ' + c.state['campaign'], 'bold')} ({c.home})")
    print(
        f"  quests: {n['landed']} landed, {in_flight(n)}, {n['planned']} planned"
        + (f"; ready to start: {', '.join(o['ready'])}" if o["ready"] else "")
    )
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
    if reviews:
        print()
    for r in reviews:
        print(
            f"review {r['file']}: {r['verdict']}, {r['findings']} findings, {r['blocking']} blocking ({r['blocking_defects']} defects)"
        )
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
    r"You (?:are (?:the |an? )?(?:(?P<lens>\S+) )?(?P<role>adventurer|paladin|scout)\b|(?P<resume>resume) quest )"
)
QUEST = re.compile(r"\bquest (?P<id>[\w.-]+?)[:,.]?(?:\s|$)")
NAMED = re.compile(r"\bcampaign (?P<name>[\w.-]+)(?: \((?P<home>[^)]+)\))?")
CLASS = re.compile(r"^Class: (\w+)", re.M)


def launch(prompt: str) -> Json | None:
    """The role, quest, class and campaign a launch prompt names (references/prompts.md)."""
    for line in prompt.splitlines():
        role, named = ROLE.search(line), NAMED.search(line)
        if role and named:
            quest, cls = QUEST.search(line), CLASS.search(prompt)
            kind = "adventurer" if role["resume"] else role["role"]
            return {
                "role": kind,
                "lens": role["lens"] or "" if kind == "paladin" else "",
                "quest": quest["id"] if quest else "-",
                "class": cls[1] if cls else "",
                "name": named["name"],
                "home": named["home"] or "",
            }
    return None


def copilot_runs(since: float) -> tuple[list[Json], list[Json]]:
    """Agent runs and game master sessions from Copilot CLI's session events."""
    root = Path(os.environ.get("CAMPAIGN_COPILOT_LOGS") or Path.home() / ".copilot" / "session-state")
    runs: list[Json] = []
    sessions: list[Json] = []
    for log in sorted(root.glob("*/events.jsonl")):
        if log.stat().st_mtime < since:
            continue
        prompts: dict[str, str] = {}
        agents: dict[str, Json] = {}
        lead: Json = {"harness": "copilot", "session": log.parent.name, "cost": 0.0, "tokens": 0}
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
                    **{k: a.get(k, 0) for k in ("tokens", "cost", "seconds", "tool_calls")},
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
            g = groups.setdefault(key, {"tokens": 0, "cached_input_tokens": 0, "output_tokens": 0})
            u = record["usage"]
            g["tokens"] += int(
                u.get("total_tokens", int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0)) or 0
            )
            for field in ("cached_input_tokens", "output_tokens"):
                g[field] += int(u.get(field) or 0)
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
                    "class": "",
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


USAGE_READERS = {
    "copilot": copilot_runs,
    "codex": codex_runs,
}  # harness -> reader(since) -> (agent runs, game master sessions)


def task_attribution(c: Campaign, task: str) -> Json | None:
    """Role, quest and class or lens from a launch's task name, for harnesses that keep only the
    name in plaintext (Codex, whose names allow only lowercase letters, digits and underscores):
    `<role>_<quest>[_<class or lens>]`, `_` read as `-`. A name counts toward a quest when it holds
    one of the campaign's quest or task ids as whole words (the longest wins); a lens marks a
    paladin, and a quest or task without one its adventurer. Parts of words are never guessed at."""
    words = task.lower().replace("_", "-").split("-")  # "--" leaves an empty word, as in task ids
    role = words[0] if words[0] in ROLES else ""
    lens = next((w for w in words if w in LENSES), "")
    cls = next((w for w in words[1:] if w in CLASSES), "")

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
    role = role or ("paladin" if lens else "adventurer" if quest != "-" else "")
    if not role:
        return None
    paladin = role == "paladin"
    return {"role": role, "quest": quest, "class": "" if paladin else cls.capitalize(), "lens": lens if paladin else ""}


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
    return {"campaign": c.state["campaign"], "since": since, "runs": runs, "game_master": sessions, "jobs": jobs}


def amount(n: float) -> str:
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e4 else f"{n:,.0f}"


def span(seconds: float) -> str:
    m = round(seconds / 60)
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


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
        print("\n" + paint(f"{title:<{width}} runs  tokens    cost  agent time", "bold"))
        for k, rs in sorted(
            groups.items(), key=lambda x: (-sum(r["cost"] for r in x[1]), -sum(r["tokens"] for r in x[1]))
        ):
            tokens, cost = sum(r["tokens"] for r in rs), sum(r["cost"] for r in rs)
            print(
                f"{k:<{width}} {len(rs):>4} {amount(tokens):>7} {amount(cost):>7}  {span(sum(r['seconds'] for r in rs)):>10}"
            )

    when = time.strftime("%Y-%m-%d", time.localtime(u["since"]))
    print(
        f"{mark('📊')}{paint('campaign ' + u['campaign'], 'bold')} since {when}: {len(runs)} agent runs, {amount(sum(r['tokens'] for r in runs))} tokens, "
        f"cost {amount(sum(r['cost'] for r in runs))}; {len(jobs)} jobs ran {span(sum(j['ran'] for j in jobs))} "
        f"and waited {span(sum(j['waited'] for j in jobs))}"
    )
    if runs:
        table("by quest", lambda r: "(unattributed)" if r["quest"] == "-" else r["quest"])
        table(
            "by role, class, model and effort",
            lambda r: " ".join(
                x
                for x in (
                    r["role"],
                    r["lens"],
                    r["class"],
                    r["task"] if r["role"] == "unattributed" else "",
                    r["model"],
                    r["effort"],
                )
                if x
            ),
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
    for s in u["game_master"]:
        print(
            f"\n{mark('🧙')}game master session {s['harness']} {s['session'][:8]}: {amount(s['tokens'])} tokens, cost {amount(s['cost'])} "
            "(the whole session, which may include other work)"
        )
    print(
        "\n"
        + paint(
            f"read: {', '.join(USAGE_READERS)}; tokens are cumulative (context rereads included), cost in each harness's own unit; Codex cost unavailable (reported as 0)",
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
    found = problems(Campaign(home))
    for severity, message in found:
        print_problem(severity, message)
    if not found:
        print(f"{mark('✅')}{paint('ok', 'green')}")
    return 1 if any(s == "error" for s, _ in found) else 0


def submit_checks(
    c: Campaign, qid: str, checks: list[Json], commit: str, *, priority: int, fresh: bool = False, force: bool = False
) -> list[str]:
    p = c.quests[qid]
    repo = c.repo(p)
    ids: list[str] = []
    for check in checks:
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
            "slot": "" if fresh or p["risk"] == "critical" else f"{c.state['campaign']}-{qid}",
            "cmux": os.environ.get("CMUX_WORKSPACE_ID", ""),
            "priority": priority,
            "retries": 1,
        }
        jid, reused = submit(spec, force=force)
        print(f"{check['name']}: {jid}" + (" (reused)" if reused else ""))
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
    submit_checks(c, a.id, checks, commit, priority=1, fresh=a.fresh, force=a.force)
    return 0


# ---------------------------------------------------------------- jobs


def jobs_dir() -> Path:
    d = Path(os.environ.get("CAMPAIGN_JOBS") or Path.home() / ".scratch" / "campaign" / "jobs")
    d.mkdir(parents=True, exist_ok=True)
    return d


def job_key(spec: Json) -> str:
    """The identity of a result: same key, same evidence. Host and toolchain are part of it, and
    the code is identified by its tree, not its commit."""
    refs = sorted((name, code_tree(Path(r["repo"]), r["commit"])) for name, r in spec["refs"].items())
    identity = [
        HOST,
        fingerprint(),
        spec["repo"],
        code_tree(Path(spec["repo"]), spec["commit"]),
        refs,
        spec["cmd"],
        spec["cwd"],
        spec["level"],
        sorted(spec["env"].items()),
    ]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]


def load_job(d: Path) -> Json:
    def tolerant(path: Path) -> Json:
        try:
            return read_json(path) or {}
        except CampaignError:
            return {"state": "corrupt"}

    return {"id": d.name, "dir": d, "spec": tolerant(d / "spec.json"), "state": tolerant(d / "state.json")}


def attempt_dir(job: Json) -> Path:
    return job["dir"] / f"a{int(job['state'].get('attempt', 1))}"


def all_jobs() -> list[Json]:
    return [load_job(d) for d in sorted(jobs_dir().iterdir()) if d.is_dir() and (d / "spec.json").exists()]


def job_for_check(repo: Path, commit: str, check: Json) -> Json | None:
    nothing: Json = {}
    spec: Json = {
        "repo": str(repo),
        "commit": commit,
        "refs": nothing,
        "cmd": check["cmd"],
        "cwd": "",
        "level": check["level"],
        "env": nothing,
    }
    return job_with_key(spec)


def job_with_key(spec: Json) -> Json | None:
    """The job with this spec's identity (code, command, toolchain), if one was submitted."""
    link = jobs_dir() / "keys" / job_key(spec)
    if not link.is_symlink():
        return None
    d = jobs_dir() / os.readlink(link)
    return load_job(d) if (d / "spec.json").exists() else None


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
    }


def submit(spec: Json, force: bool = False) -> tuple[str, bool]:
    """Queues a job, or returns an identical one, after the same jobs, that is pending or succeeded."""
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
        if old["state"].get("state") in ("queued", "running", "done"):
            return old["id"], True
    jid = time.strftime("%y%m%d-%H%M%S-") + secrets.token_hex(3)
    d = jd / jid
    d.mkdir()
    spec.update(key=key, submitted=time.time(), fingerprint=fingerprint())
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


def slot_lock(slot: Path, beat: Path) -> None:
    lock = slot.with_name(slot.name + ".lock")
    while True:
        try:
            lock.mkdir(parents=True)
            (lock / "pid").write_text(str(os.getpid()))
            return
        except FileExistsError:
            try:
                holder = int((lock / "pid").read_text())
            except (FileNotFoundError, ValueError):
                holder = 0
            if holder and not alive(holder):
                shutil.rmtree(lock, ignore_errors=True)
                continue
            beat.touch()
            time.sleep(2)


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


def release(spec: Json, d: Path, owner: int) -> None:
    if spec.get("slot"):
        lock = jobs_dir() / "slots" / (spec["slot"] + ".lock")
        with contextlib.suppress(FileNotFoundError, ValueError):
            if int((lock / "pid").read_text()) == owner:
                shutil.rmtree(lock, ignore_errors=True)
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
        release(spec, d, me)
        (ad / "exit").write_text("125")
        finish(job, 125, "failed", f"could not prepare sources: {e}", fence=me)
        return 0
    env = dict(os.environ)
    env.pop("CMUX_WORKSPACE_ID", None)  # the worker may have started in another workspace
    if spec.get("cmux"):
        env["CMUX_WORKSPACE_ID"] = spec["cmux"]  # quiet's pill and notice go to the submitter's
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
    release(spec, d, me)
    state = "cancelled" if cancelled else "timeout" if timed_out or code == 124 else "done" if code == 0 else "failed"
    finish(job, code, state, fence=me)
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
            same_boot = st.get("boot") == boot_id()
            if st.get("sid") and same_boot:
                sweep(int(st["sid"]))
            exit_file = attempt_dir(job) / "exit"
            if exit_file.exists() and same_boot:
                code = int(exit_file.read_text().strip() or "1")
                finish(
                    job,
                    code,
                    "done" if code == 0 else "timeout" if code == 124 else "failed",
                    "recorded after its runner died",
                )
                continue
            release(spec, d, int(st.get("runner", 0)))
            attempt = int(st.get("attempt", 1))
            if attempt <= int(spec.get("retries", 0)):
                shutil.rmtree(d / "claim", ignore_errors=True)
                write_json(
                    d / "state.json",
                    {
                        "state": "queued",
                        "attempt": attempt + 1,
                        "note": f"requeued at {stamp()}: attempt {attempt} lost its runner",
                    },
                )
            else:
                finish(
                    job,
                    -1,
                    "interrupted",
                    f"attempt {attempt} lost its runner{'' if same_boot else ' (the host rebooted)'}; not retried",
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


def cmd_worker(a: argparse.Namespace) -> int:
    jd = jobs_dir()
    home = jd / ".worker"
    try:
        home.mkdir()
    except FileExistsError:
        if worker_alive():
            return 0
        stale = jd / f".worker.stale.{os.getpid()}"
        with contextlib.suppress(OSError):
            home.rename(stale)
        shutil.rmtree(stale, ignore_errors=True)
        try:
            home.mkdir()
        except FileExistsError:
            return 0
    (home / "pid").write_text(str(os.getpid()))
    light_slots = int(os.environ.get("CAMPAIGN_LIGHT_JOBS") or max(1, (os.cpu_count() or 4) // 4))
    tick = float(os.environ.get("CAMPAIGN_WORKER_TICK", "2"))
    try:
        while True:
            (home / "heartbeat").touch()
            reap()
            jobs = all_jobs()
            states = {j["id"]: j["state"].get("state") for j in jobs}
            queued = sorted(
                (j for j in jobs if states[j["id"]] == "queued"),
                key=lambda j: (-int(j["spec"].get("priority", 0)), j["id"]),
            )
            running = [j for j in jobs if states[j["id"]] == "running"]
            if not queued and not running:
                return 0
            heavy = any(j["spec"].get("level") in HEAVY for j in running)
            light = sum(1 for j in running if j["spec"].get("level") not in HEAVY)
            late = sum(
                1 for j in running if j["spec"].get("level") not in HEAVY and int(j["spec"].get("priority", 0)) < 0
            )
            for j in queued:
                deps = [states.get(x, "missing") for x in j["spec"].get("after", [])]
                if any(s not in ("queued", "running", "done") for s in deps):
                    if claim(j):
                        finish(load_job(j["dir"]), -1, "skipped", "a job it runs after did not succeed")
                    continue
                is_heavy = j["spec"].get("level") in HEAVY
                is_late = int(j["spec"].get("priority", 0)) < 0
                if any(s != "done" for s in deps) or (is_heavy and heavy) or (not is_heavy and light >= light_slots):
                    continue
                if is_late and not is_heavy and late >= max(1, light_slots // 2):
                    continue  # deferred work never takes every slot from the direct path
                if claim(j):
                    spawn(["_run", str(j["dir"])], j["dir"] / "runner.log")
                    heavy = heavy or is_heavy
                    light += 0 if is_heavy else 1
                    late += 1 if is_late and not is_heavy else 0
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


def outside_inputs(command: str, repo: str) -> list[str]:
    """Paths in a command into another job's files or a checkout of its repository: inputs that are
    not part of the job's identity, so a rerun at the same code may see different files."""
    roots = [jobs_dir().resolve()]
    if repo:
        listing = git(Path(repo), "worktree", "list", "--porcelain", check=False)
        roots += [Path(x[len("worktree ") :]).resolve() for x in listing.splitlines() if x.startswith("worktree ")]
    found: list[str] = []
    for token in re.findall(r"(?<![\w.~/-])~?/[^\s'\"`;|&()<>,=]+", command):
        path = Path(token).expanduser().resolve()
        if any(path == r or path.is_relative_to(r) for r in roots):
            found.append(token)
    return list(dict.fromkeys(found))


def cmd_job_submit(a: argparse.Namespace) -> int:
    cmd = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd
    if not cmd:
        die("give the command after --")
    command = cmd[0] if len(cmd) == 1 else shlex.join(cmd)
    spec: Json = {
        "cmd": command,
        "level": a.level,
        "minutes": a.minutes,
        "priority": a.priority,
        "after": csv(a.after),
        "retries": a.retries,
        "keep": a.keep,
        "slot": a.slot or "",
    }
    spec["env"] = dict(x.split("=", 1) for x in a.env)
    cwd = Path.cwd()
    default_ref = "HEAD"
    repo = Path(a.repo).resolve() if a.repo else cwd
    if a.quest:
        c = find_campaign(cwd)
        p = c.quest(a.quest)
        spec.update(quest=a.quest, campaign=c.ident)
        repo = Path(a.repo).resolve() if a.repo else c.repo(p)
        default_ref = p.get("branch") or "HEAD"
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
    spec["cmux"] = os.environ.get("CMUX_WORKSPACE_ID", "")
    spec["what"] = a.what or command[:60] + (f" at {spec['commit'][:12]}" if spec.get("commit") else "")
    spec.setdefault("quest", "")
    if "campaign" not in spec:
        spec["campaign"] = ""
        with contextlib.suppress(CampaignError):
            spec["campaign"] = find_campaign(Path.cwd()).ident
    jid, reused = submit(spec, force=a.force)
    print(jid + (f" (reused: {resolve_job(jid)['spec'].get('what', '')})" if reused else ""))
    if a.sync:
        job = resolve_job(jid)
        if job["state"].get("state") == "queued" and claim(job):
            spawn(["_run", str(job["dir"])], job["dir"] / "runner.log").wait()
        return wait_for([jid], any_one=False, timeout=0)
    ensure_worker()
    return 0


def cmd_job_list(a: argparse.Namespace) -> int:
    reap()
    for j in all_jobs():
        s, st = j["spec"], j["state"]
        if (a.quest and s.get("quest") != a.quest) or (a.state and st.get("state") != a.state):
            continue
        exit_code = "" if st.get("exit") is None else f" exit {st['exit']}"
        state = st.get("state", "?")
        color, emoji = JOB_MARKS.get(state, ("dim", "❔"))
        print(
            f"{mark(emoji)}{j['id']}  {paint(f'{state:11}', color)} {s.get('level', '?'):9} {s.get('quest') or '-':14} {s.get('what', '')}{exit_code}"
        )
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


def cmd_note(a: argparse.Namespace) -> int:
    """Passes a finding to a quest from anywhere on the machine, or (the game master) closes one."""
    name, sep, pid = a.quest.rpartition(":")
    if a.close:
        if not a.reason:
            die("closing a note needs --reason R")
        with writing() as c:
            p = c.quest(pid)
            note = next((n for n in inbox_notes(c, pid) if n["id"] == a.close), None)
            if note is None:
                die(f"{pid} has no open note {a.close}")
            p.setdefault("notes", []).append({**note, "closed": a.reason})
            p["updated"] = stamp()
            c.save(f"campaign: close {a.close} on {pid}")
            (note_inbox(c, pid) / f"{a.close}.json").unlink()
        print(f"{pid}: {a.close} closed")
        return 0
    if sep:
        other = campaigns().get(name)
        if other is None:
            die(f"no campaign {name} on this machine (`campaign status --all` lists them)")
        c = Campaign(Path(other["home"]), name)
    else:
        c = find_campaign(Path.cwd())
    p = c.quest(pid)
    if p["status"] in ("landed", "dropped"):
        die(f"{pid} is {p['status']}: a finding about it is a new quest; tell the game master")
    if not a.text:
        die("give the note's text")
    folder = note_inbox(c, pid)
    folder.mkdir(parents=True, exist_ok=True)
    taken = [n["id"] for n in [*inbox_notes(c, pid), *p.get("notes", [])]]
    n = 1 + max([int(str(x).partition("-")[2] or 0) for x in taken] or [0])
    tmp = folder / f".note.{os.getpid()}.tmp"
    while True:  # a fresh id even when two agents write at once
        note = {"id": f"note-{n}", "at": stamp(), "by": a.by, "text": a.text}
        tmp.write_text(json.dumps(note, indent=2) + "\n")
        try:
            os.link(tmp, folder / f"note-{n}.json")
            break
        except FileExistsError:
            n += 1
        finally:
            tmp.unlink(missing_ok=True)
    notify(f"Note for {pid}", f"{a.by}: {a.text}"[:200])
    print(f"note-{n} for {pid}: its journal answers it by that id, or the game master closes it")
    return 0


def cmd_job_rerun(a: argparse.Namespace) -> int:
    """The same jobs (command, level, directory, environment, other repositories) at another commit:
    by default the head of their quest's branch, else of their repository."""
    branch = ""
    for jid in a.jobs:
        spec = dict(resolve_job(jid)["spec"])
        if not spec.get("commit"):
            die(f"{jid} is unversioned: submit it again")
        if not a.commit and spec.get("quest"):
            with contextlib.suppress(CampaignError):
                branch = find_campaign(Path.cwd()).quest(spec["quest"]).get("branch", "")
        commit = rev(Path(spec["repo"]), a.commit or branch or "HEAD")
        what, old = spec.get("what", ""), f" at {spec['commit'][:12]}"
        spec.update(
            commit=commit,
            after=[],
            cmux=os.environ.get("CMUX_WORKSPACE_ID", ""),
            what=(what[: -len(old)] if what.endswith(old) else what) + f" at {commit[:12]}",
        )
        new, reused = submit(spec)
        print(f"{new}" + (" (reused)" if reused else "") + f": {jid} at {commit[:12]}")
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
    since the game master last saw them (recorded in the home's .git), so nothing that happens
    between two waits is missed; then lists what is still in flight, to wait for next."""
    seen_file = Path(git(c.home, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "campaign-seen.json"
    records: Json = (read_json(seen_file) if seen_file.exists() else None) or {}
    if "heads" in records:  # written before a home could hold several campaigns
        records = {str(c.home): records}
    recorded: Json | None = records.get(c.ident)

    def remember(heads: dict[str, str], ended: dict[str, Json]) -> None:
        records[c.ident] = {"heads": heads, "jobs": sorted(ended), "notes": sorted(notes())}
        write_json(seen_file, records)

    def notes() -> dict[str, Json]:
        return {
            f"{pid} {n['id']}": n
            for pid, q in c.quests.items()
            if q["status"] not in ("landed", "dropped")
            for n in inbox_notes(c, pid)
        }

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
        seen_heads, seen_jobs, seen_notes = dict(heads), set(ended), set(notes())
    else:
        seen_heads = {str(k): str(v) for k, v in dict(recorded.get("heads") or {}).items()}
        seen_jobs = {str(x) for x in recorded.get("jobs") or []}
        seen_notes = {str(x) for x in recorded.get("notes") or []}
    while True:
        reap()
        heads, ended = current()
        waiting = notes()
        moved = sorted(k for k, h in heads.items() if seen_heads.get(k) != h)
        finished = sorted(set(ended) - seen_jobs)
        arrived = sorted(set(waiting) - seen_notes)
        if moved or finished or arrived:
            break
        running = [j for j in all_jobs() if j["spec"].get("campaign") == c.ident]
        if not heads and not any(j["state"].get("state") not in TERMINAL for j in running):
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
        print(f"{name}: {branch} moved to {heads[key][:12]}; journal: {journal_state(c, name, branch, '')}")
    for key in arrived:
        n = waiting[key]
        print(f"{key.split()[0]}: {n['id']} from {n['by']}: {n['text']}")
    for jid in finished:
        s = job_summary(ended[jid])
        print(f"{jid}  {s['state']}" + ("" if s["exit"] is None else f" exit {s['exit']}") + f"  {s['what']}")
    remember(heads, ended)
    flight = [f"{name} ({journal_state(c, name, branch, '')})" for (name, branch) in branch_heads(c)]
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


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="campaign", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("-C", dest="chdir", metavar="DIR", help="run as if started in DIR, like git -C")
    sub = ap.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--campaign", metavar="NAME", help="which campaign, when its home holds several (default $CAMPAIGN)"
    )

    p = sub.add_parser("init", help="start a campaign in this repository's main checkout")
    p.add_argument("--name", help="campaign name (default: the directory's)")
    p.add_argument("--ci", choices=("github", ""), default="", help="require green GitHub CI to land")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser(
        "config",
        parents=[common],
        help="show or set campaign-wide settings: ci, ci_later",
        description="With no assignment, prints the settings as key=value lines. ci=github makes landing "
        "wait for green GitHub CI; ci_later=WORKFLOW,... names workflows that do not gate it.",
    )
    p.add_argument("assignments", nargs="*", metavar="key=value")
    p.set_defaults(fn=cmd_config)

    p = sub.add_parser("new", parents=[common], help="plan a quest")
    p.add_argument("id")
    p.add_argument("--title", required=True)
    p.add_argument("--risk", choices=RISKS, default="normal")
    p.add_argument("--repo", default=".", help="its repository, relative to the home checkout")
    p.add_argument("--needs", help="quests that must land before it starts")
    p.add_argument("--after", help="quests that must land before it lands")
    p.add_argument("--files", help="globs it may change")
    p.add_argument("--provides", help="contracts it defines or changes")
    p.add_argument("--consumes", help="contracts it builds against")
    p.add_argument("--lenses", help="required reviews (default by risk)")
    p.add_argument("--milestone", help="the PLAN milestone it belongs to (the sidebar counts the current one)")
    p.add_argument(
        "--check",
        action="append",
        default=[],
        metavar="NAME[:LEVEL[:MIN]]=CMD",
        help="a gate check: must pass on the landed code",
    )
    p.add_argument(
        "--later",
        action="append",
        default=[],
        metavar="NAME[:LEVEL[:MIN]]=CMD",
        help="a deferred check: runs on the landed code at low priority; gates the milestone",
    )
    p.set_defaults(fn=cmd_new)

    p = sub.add_parser("set", parents=[common], help="change a quest's fields")
    p.add_argument("id")
    p.add_argument("--task", help="update an existing task's files=GLOBS")
    p.add_argument("assignments", nargs="*", metavar="key=value")
    p.add_argument(
        "--check", action="append", default=[], metavar="NAME[:LEVEL[:MIN]]=CMD", help="replace the gate checks"
    )
    p.add_argument(
        "--later", action="append", default=[], metavar="NAME[:LEVEL[:MIN]]=CMD", help="replace the deferred checks"
    )
    p.set_defaults(fn=cmd_set)

    p = sub.add_parser("start", parents=[common], help="branch and worktree for a quest or one of its tasks")
    p.add_argument("id")
    p.add_argument("--task", help="a parallel writer inside the quest, branched from it")
    p.add_argument("--files", help="the task's file globs (default: the quest's)")
    p.add_argument("--force", metavar="REASON", help="start before its needs landed, or over an overlapping file set")
    p.set_defaults(fn=cmd_start)

    p = sub.add_parser(
        "status",
        parents=[common],
        help="what is ready, running, waiting and landable",
        description="Each quest and what it still needs to land. Progress counts the criteria in its quest "
        "file that its journals' Evidence tables name with evidence (`| A1 | job ID |`); `status ID` lists "
        "each criterion with the rows behind it. "
        "Colored on a terminal (NO_COLOR turns it off); CAMPAIGN_BAR sets the progress bar's two characters "
        "(default \u2588\u2591, e.g. \u25b0\u25b1).",
    )
    p.add_argument("id", nargs="?")
    p.add_argument("--json", action="store_true")
    p.add_argument("--reviews", action="store_true", help="verdicts and blocking findings per review file")
    p.add_argument("--all", action="store_true", help="every campaign on this machine")
    p.set_defaults(fn=cmd_status, brief=False)

    p = sub.add_parser(
        "brief",
        parents=[common],
        help="status for agents: one line per thing to act on",
        description="What a quest still needs, one line each: landing gaps, open notes, failed jobs no journal "
        "names yet, pending jobs. In a quest's worktree, that quest and its file set; elsewhere, every quest.",
    )
    p.add_argument("id", nargs="?")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status, brief=True, reviews=False, all=False)

    p = sub.add_parser(
        "usage", parents=[common], help="tokens, cost and time per quest, role and class (harness logs and jobs)"
    )
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_usage)

    p = sub.add_parser("check", parents=[common], help="contradictions between state, git, worktrees and jobs")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("verify", parents=[common], help="queue a quest's gate checks at its branch head")
    p.add_argument("id")
    p.add_argument("--commit", help="verify this commit instead of the head")
    p.add_argument("--force", action="store_true", help="rerun even if an identical job succeeded")
    p.add_argument("--fresh", action="store_true", help="a fresh checkout instead of the quest's build slot")
    p.add_argument("--all", action="store_true", help="the deferred checks too")
    p.add_argument(
        "--deferred", action="store_true", help="rerun a landed quest's deferred checks on main (or --commit)"
    )
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser(
        "accept", parents=[common], help="record a review lens as passed for the quest's current content"
    )
    p.add_argument("id")
    p.add_argument("lens", choices=LENSES)
    p.add_argument("--commit", help="the commit accepted (default: the branch head)")
    p.add_argument("--jobs", help="jobs that confirmed each blocking fix (needed after CHANGES)")
    p.add_argument("--waive", metavar="REASON", help="no review: low or normal risk with strong evidence")
    p.add_argument("--note")
    p.set_defaults(fn=cmd_accept)

    p = sub.add_parser("land", parents=[common], help="fast-forward (or cleanly merge) a quest onto main")
    p.add_argument("id")
    p.add_argument("--upto", metavar="COMMIT", help="land a prefix of the branch as an increment")
    p.add_argument("--reason", help="why the increment is safe to land alone")
    p.add_argument("--push", action="store_true")
    p.add_argument("--no-ci", action="store_true")
    p.add_argument("--record", metavar="COMMIT", help="record a landing that already happened")
    p.set_defaults(fn=cmd_land)

    p = sub.add_parser("migrate", parents=[common], help=f"move the campaign's files from {LEGACY_META} to {META}")
    p.set_defaults(fn=cmd_migrate)
    p = sub.add_parser(
        "note",
        parents=[common],
        help="pass a finding to a quest from anywhere on the machine",
        description="Leaves a note for a quest ([NAME:]ID; NAME from anywhere on the machine). It stays open, "
        "shown by status and blocking the landing, until one of the quest's journals names its id or the game "
        "master closes it (--close ID --reason R, in the home's main checkout).",
    )
    p.add_argument("quest", metavar="[NAME:]ID")
    p.add_argument("text", nargs="?", default="")
    p.add_argument("--from", dest="by", default=os.environ.get("USER", "someone"), help="who found it")
    p.add_argument("--close", metavar="NOTE", help="close an open note (the game master)")
    p.add_argument("--reason")
    p.set_defaults(fn=cmd_note)
    p = sub.add_parser("clean", parents=[common], help="remove a landed or dropped quest's worktrees and branches")
    p.add_argument("id")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_clean)

    p = sub.add_parser(
        "wait", parents=[common], help="block until jobs finish, or (--commits) until anything new happens; no tokens"
    )
    p.add_argument("jobs", nargs="*")
    p.add_argument("--quest")
    p.add_argument("--any", action="store_true", help="return when the first one finishes")
    p.add_argument(
        "--commits",
        action="store_true",
        help="return at the first quest-branch commit or job of this campaign not seen by an earlier wait",
    )
    p.add_argument("--timeout", type=float, default=0, help="seconds; exit 3 when it passes")
    p.set_defaults(fn=cmd_wait)

    p = sub.add_parser("worker", help="run queued jobs; exits when the queue is empty")
    p.set_defaults(fn=cmd_worker)

    p = sub.add_parser("_run")
    p.add_argument("jobdir")
    p.set_defaults(fn=cmd_run)

    job = sub.add_parser("job", help="submit and inspect machine jobs").add_subparsers(
        dest="job_command", required=True
    )
    p = job.add_parser("submit", parents=[common], help="queue a command at an exact commit")
    p.add_argument("--quest")
    p.add_argument("--repo", help="repository (default: this one, or the quest's)")
    p.add_argument("--commit", help="ref to run (default: HEAD, or the quest branch)")
    p.add_argument(
        "--ref",
        action="append",
        default=[],
        metavar="NAME=[PATH@]REF",
        help="another commit (of this or another repository) at $JOB_REF_NAME",
    )
    p.add_argument("--cwd", help="subdirectory to run in")
    p.add_argument("--level", choices=LEVELS, default="light")
    p.add_argument("--minutes", type=int, default=30)
    p.add_argument("--priority", type=int, default=0)
    p.add_argument("--after", help="jobs that must succeed first")
    p.add_argument("--env", action="append", default=[], metavar="K=V")
    p.add_argument("--slot", help="reuse a named checkout (incremental builds)")
    p.add_argument("--keep", action="store_true", help="keep the source checkout")
    p.add_argument("--retries", type=int, default=0, help="requeues after a lost runner (only for jobs safe to rerun)")
    p.add_argument("--what")
    p.add_argument("--unversioned", action="store_true", help="run in --cwd as it is (probes; never evidence)")
    p.add_argument("--force", action="store_true", help="run even if an identical job succeeded")
    p.add_argument("--sync", action="store_true", help="run now and wait (no worker); same records")
    p.add_argument("cmd", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_job_submit)
    p = job.add_parser("list", parents=[common])
    p.add_argument("--quest")
    p.add_argument("--state")
    p.set_defaults(fn=cmd_job_list)
    p = job.add_parser("show", parents=[common])
    p.add_argument("job")
    p.set_defaults(fn=cmd_job_show)
    p = job.add_parser("log", parents=[common])
    p.add_argument("job")
    p.add_argument("-n", "--lines", type=int, default=40)
    p.set_defaults(fn=cmd_job_log)
    p = job.add_parser("rerun", parents=[common], help="the same jobs at the quest's head (or --commit)")
    p.add_argument("jobs", nargs="+", metavar="JOB")
    p.add_argument("--commit", help="ref to run (default: the quest branch, else HEAD)")
    p.set_defaults(fn=cmd_job_rerun)
    p = job.add_parser("cancel", parents=[common])
    p.add_argument("job")
    p.set_defaults(fn=cmd_job_cancel)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = parser()
    args, extra = ap.parse_known_args(argv)
    if args.chdir:
        try:
            os.chdir(args.chdir)
        except OSError as e:
            ap.error(f"-C {args.chdir}: {e.strerror}")
    if getattr(args, "campaign", None):
        os.environ["CAMPAIGN"] = args.campaign  # job runners and the worker inherit it
    # older argparse leaves key=value assignments after an option (`set ID --task T files=...`) unparsed
    if extra and getattr(args, "fn", None) is cmd_set and all("=" in x and not x.startswith("-") for x in extra):
        args.assignments += extra
    elif extra:
        ap.error(f"unrecognized arguments: {' '.join(extra)}")
    try:
        return int(args.fn(args))
    except CampaignError as e:
        print(f"campaign: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
