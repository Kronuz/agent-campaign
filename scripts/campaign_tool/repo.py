"""Git repositories: refs, history, objects and trees, read once per command where they cannot change."""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import json
import os
import re
import subprocess
from collections.abc import Callable, Collection, Generator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .core import LEAD_FILES, META, CampaignError, Json, die, git, git_ok, jobs_dir, pool, warm_fingerprint

if TYPE_CHECKING:  # imported when used: concurrent.futures imports logging, a sixth of a command's start
    from concurrent.futures import Future


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
_listed: list[list[Json]] = []  # the job store's jobs, while a command only reads


@contextlib.contextmanager
def frozen() -> Generator[None, None, None]:
    """Reads each repository's branch heads and the job store once, for a command that only reads
    (status asks about each branch and job many times); commands that move branches or change jobs
    always read them again."""
    _heads.append({})
    try:
        yield
    finally:
        _heads.pop()
        if not _heads:
            _listed.clear()
            _status.clear()


def branch_head(repo: Path, ref: str) -> str | None:
    if not _heads or SHA.fullmatch(ref):
        return None
    return repo_refs(repo).get(f"refs/heads/{ref}")


def repo_refs(repo: Path) -> dict[str, str]:
    """While frozen, a repository's refs and the commits they name (tags peeled), listed once."""
    snapshot = _heads[0]
    if str(repo) not in snapshot:
        listing = git(repo, "for-each-ref", "--format=%(objectname) %(*objectname) %(refname)", check=False)
        rows = (x.split(" ") for x in listing.splitlines())
        snapshot[str(repo)] = {r[2]: r[1] or r[0] for r in rows if len(r) == 3}
    return snapshot[str(repo)]


REF_NAME = re.compile(r"(?![0-9a-f]+$)(?![A-Z_]+$)[\w.-]+(/[\w.-]+)*")  # a ref's name: no id, HEAD or rev expression


def frozen_ref(repo: Path, ref: str) -> str | None:
    """While frozen, the commit a ref's name names (a branch first, then git's order), or "" when no
    ref has that name; None when only git can say (not frozen, a commit id, HEAD, an expression)."""
    if not _heads or not REF_NAME.fullmatch(ref):
        return None
    refs = repo_refs(repo)
    for name in (f"refs/heads/{ref}", ref, f"refs/{ref}", f"refs/tags/{ref}", f"refs/remotes/{ref}"):
        if name.startswith("refs/") and name in refs:
            return refs[name]
    return refs.get(f"refs/remotes/{ref}/HEAD", "")


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
    return frozen_ref(repo, ref) or git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")


def has_ref(repo: Path, ref: str) -> bool:
    known = frozen_ref(repo, ref)
    return bool(known) if known is not None else git_ok(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")


def ancestor(repo: Path, a: str, b: str) -> bool:
    a, b = branch_head(repo, a) or a, branch_head(repo, b) or b  # while frozen, a branch is its commit
    return bool(on_commits(("ancestor", str(repo), a, b), lambda: git_ok(repo, "merge-base", "--is-ancestor", a, b)))


_reachable: dict[tuple[str, str], set[str]] = {}


def off_branch(repo: Path, commits: list[str], ref: str) -> set[str]:
    """Which of COMMITS are not in REF's history: one listing of that history (Xapiand's 15k
    commits list in about 0.3 s), and none while REF stays at the commit it was asked at."""
    head = rev(repo, ref)
    shas = sorted({x for x in commits if SHA.fullmatch(x)})
    off: list[str] = on_commits(("off-branch", str(repo), head, *shas), lambda: sorted(set(shas) - history(repo, head)))
    return {*off, *(x for x in commits if not SHA.fullmatch(x) and not ancestor(repo, x, ref))}


def history(repo: Path, ref: str) -> set[str]:
    key = (str(repo), ref)
    if key not in _reachable:
        _reachable[key] = set(git(repo, "rev-list", ref, check=False).split())
    return _reachable[key]


_status: dict[Path, Future[list[str]]] = {}


def changed_files(place: Path) -> list[str]:
    """A checkout's uncommitted changes (`git status --porcelain`), read once while frozen."""
    if not _heads:
        return git(place, "status", "--porcelain").splitlines()
    return warm_status(place).result()


def warm_status(place: Path) -> Future[list[str]]:
    if place not in _status:
        _status[place] = pool().submit(lambda: git(place, "status", "--porcelain").splitlines())
    return _status[place]


def warm_repos(repos: Collection[Path]) -> None:
    """Reads, side by side, what a command asks of each repository one at a time: the repository
    it belongs to and, while frozen, its branch heads; and the host's toolchain meanwhile."""
    warm_fingerprint()

    def read(repo: Path) -> None:
        with contextlib.suppress(CampaignError):  # asked again in order, and reported there
            if common_dir(repo) and _heads:
                main_branch(repo)

    list(pool().map(read, dict.fromkeys(repos)))


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


_maps: dict[tuple[str, str], dict[str, str]] = {}


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
