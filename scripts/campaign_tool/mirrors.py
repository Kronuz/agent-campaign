"""Git mirrors: a job's git reaches GitHub through one bare mirror per repository in the job store."""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .core import ENTRY, jobs_dir
from .procs import mtime

# a job's git reaches GitHub through one bare mirror per repository, under the job store
MIRRORED = re.compile(r"^https://(github\.com)/([^/\s]+)/([^/\s]+)\.git$")
PINNED = re.compile(r'GIT_REPOSITORY\s+"?(https://github\.com/[^\s")]+\.git)"?\s+GIT_TAG\s+"?([0-9a-f]{40})\b')
GIT_SHIM = """#!/bin/sh
# git for campaign jobs: GitHub repositories through the job store's mirrors (references/host.md)
if [ -z "$CAMPAIGN_GIT" ]; then  # outside a job (a build directory's cached git): the next git on PATH
  self=$(dirname "$0")
  IFS=:
  for d in $PATH; do [ "$d" != "$self" ] && [ -x "$d/git" ] && exec "$d/git" "$@"; done
  echo "git: not found" >&2
  exit 127
fi
case " $* " in *" clone "*https://github.com/*) exec "$CAMPAIGN_PYTHON" "$CAMPAIGN_TOOL" _git 0 "$@" ;; esac
"$CAMPAIGN_GIT" "$@" && exit 0
code=$?
case " $* " in *" checkout "* | *" fetch "* | *" submodule "* | *" switch "*)
  exec "$CAMPAIGN_PYTHON" "$CAMPAIGN_TOOL" _git "$code" "$@" ;;
esac
exit "$code"
"""


def mirror_root() -> Path:
    return jobs_dir() / "git-mirrors"


def mirror_of(url: str) -> Path:
    """Where URL's mirror lives: git-mirrors/HOST/OWNER/REPO.git."""
    m = MIRRORED.match(url)
    assert m is not None
    return mirror_root() / m[1] / m[2] / f"{m[3]}.git"


def upstream_env() -> dict[str, str]:
    """This environment without GIT_CONFIG_COUNT's settings, whose rewrites point at the mirrors."""
    return {k: v for k, v in os.environ.items() if not re.match(r"GIT_CONFIG_(COUNT|KEY_|VALUE_)", k)}


def has_commit(real: str, mirror: Path, rev: str) -> bool:
    probe = [real, "-C", str(mirror), "cat-file", "-e", f"{rev}^{{commit}}"]
    return subprocess.run(probe, capture_output=True, check=False).returncode == 0


def mirror_fetch(real: str, url: str, want: str = "", since: float = 0.0) -> bool:
    """Fills URL's mirror from its remote under the mirror's lock: makes it when missing, else fetches
    unless it holds WANT or was fetched after SINCE (another job's fetch while this one waited). Readers
    need no lock: a mirror appears by rename, and git writes objects before the refs naming them."""
    mirror = mirror_of(url)
    mirror.parent.mkdir(parents=True, exist_ok=True)
    fetched = mirror / "campaign-fetched"
    with open(mirror.with_name(mirror.name + ".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if mirror.exists():
            if (want and has_commit(real, mirror, want)) or (since and (mtime(fetched) or 0) >= since):
                return True
            ok = subprocess.run([real, "-C", str(mirror), "fetch", "--quiet", "origin"], env=upstream_env()).returncode
        else:
            new = mirror.with_name(f"{mirror.name}.new-{os.getpid()}")
            shutil.rmtree(new, ignore_errors=True)
            ok = subprocess.run([real, "clone", "--quiet", "--mirror", url, str(new)], env=upstream_env()).returncode
            if ok == 0:  # no automatic gc: a repack would delete packs a clone is linking
                subprocess.run([real, "-C", str(new), "config", "gc.auto", "0"], check=True)
                new.rename(mirror)
            shutil.rmtree(new, ignore_errors=True)
        if ok == 0:
            fetched.touch()
        return ok == 0


def mirror_env(env: dict[str, str]) -> dict[str, str]:
    """What a job's environment adds for the mirrors: the shim first on PATH (it fills and refreshes
    them) and, for a git that bypasses it (a build directory's cached one), each mirror's rewrite."""
    real = shutil.which("git", path=env.get("PATH"))
    if not real:
        return {}
    shim = mirror_root() / "bin" / "git"
    if not shim.exists() or shim.read_text() != GIT_SHIM:
        new = shim.with_name(f".git.{os.getpid()}")
        shim.parent.mkdir(parents=True, exist_ok=True)
        new.write_text(GIT_SHIM)
        new.chmod(0o755)
        os.replace(new, shim)
    out = {
        "PATH": f"{shim.parent}:{env.get('PATH', '')}",
        "CAMPAIGN_GIT": real,
        "CAMPAIGN_PYTHON": sys.executable,
        "CAMPAIGN_TOOL": str(ENTRY),
        "CAMPAIGN_JOBS": str(jobs_dir()),  # the shim's mirrors, whatever HOME the command sets
    }
    n = int(env.get("GIT_CONFIG_COUNT") or 0)
    for mirror in sorted(mirror_root().glob("*/*/*.git")):
        out[f"GIT_CONFIG_KEY_{n}"] = f"url.{mirror}.insteadOf"
        out[f"GIT_CONFIG_VALUE_{n}"] = f"https://{mirror.parent.parent.name}/{mirror.parent.name}/{mirror.name}"
        n += 1
    out["GIT_CONFIG_COUNT"] = str(n)
    return out


def mirror_pins(real: str, root: Path) -> None:
    """Before a job runs: each FetchContent pin its CMake files name (a GitHub URL and a commit) in its
    mirror, fetched only when the commit is missing, so a cached git that bypasses the shim finds it."""
    listed = subprocess.run(
        [real, "-C", str(root), "grep", "-l", "GIT_REPOSITORY", "--", "CMakeLists.txt", "*/CMakeLists.txt", "*.cmake"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split("\n")
    for name in filter(None, listed):
        for url, commit in PINNED.findall((root / name).read_text(errors="replace")):
            if not (mirror_of(url).exists() and has_commit(real, mirror_of(url), commit)):
                mirror_fetch(real, url, want=commit)


def git_mirrored(code: int, args: list[str]) -> int:
    """The git shim's slow path. A clone of a GitHub URL makes its mirror first; a command that failed
    (CODE) where GitHub remotes are involved refreshes their mirrors once, fetches, and runs again."""
    real, start = os.environ["CAMPAIGN_GIT"], time.time()
    where, i = Path.cwd(), 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] == "-C" and i + 1 < len(args):
            where = where / args[i + 1]
        i += 2 if args[i] in ("-C", "-c") else 1
    sub = args[i] if i < len(args) else ""
    urls = [x for x in args if MIRRORED.match(x)]
    remotes: dict[str, str] = {}
    if sub != "clone":
        for query in (["config"], ["config", "-f", ".gitmodules"]):
            found = subprocess.run(
                [real, "-C", str(where), *query, "--get-regexp", r"^(remote|submodule)\..*\.url$"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.splitlines()
            for line in found:
                key, _, url = line.partition(" ")
                if MIRRORED.match(url):
                    urls.append(url)
                    if key.startswith("remote."):
                        remotes[key[len("remote.") : -len(".url")]] = url
    urls = list(dict.fromkeys(urls))

    def rewrites() -> list[str]:
        return [y for u in urls if mirror_of(u).exists() for y in ("-c", f"url.{mirror_of(u)}.insteadOf={u}")]

    if not code:
        for url in urls:
            if not mirror_of(url).exists():
                mirror_fetch(real, url, since=start)  # a job that made it while this one waited is enough
        code = subprocess.run([real, *rewrites(), *args], check=False).returncode
    if not code or not urls:
        return code
    for url in urls:
        mirror_fetch(real, url, since=start)
    for name in remotes:
        subprocess.run([real, "-C", str(where), *rewrites(), "fetch", "--quiet", name], check=False)
    return subprocess.run([real, *rewrites(), *args], check=False).returncode
