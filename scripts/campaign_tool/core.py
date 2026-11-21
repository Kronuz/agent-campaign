"""The campaign tool's vocabulary and small utilities: constants, errors, processes, files and text."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import socket
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:  # imported when used: concurrent.futures imports logging, a sixth of a command's start
    from concurrent.futures import Future, ThreadPoolExecutor


SKILL = Path(__file__).resolve().parent.parent.parent
ENTRY = SKILL / "scripts" / "campaign.py"  # the command, which imports this package
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
SHORT_RUN = 10  # minutes: a measurement (strict, exclusive) that does not say --long
LIGHT_RUN = 30  # minutes: a light fuzz or soak, its build included, that does not say --long
LONG_RUN = re.compile(r"fuzz|soak", re.I)  # runs that grow to fill any time they are given
STALE = float(os.environ.get("CAMPAIGN_STALE_SECONDS", "60"))
UNSTARTED_ATTEMPTS = 3  # a runner that dies before the command starts is requeued, up to this many tries
BEAT = float(os.environ.get("CAMPAIGN_HEARTBEAT_SECONDS", "15"))
KILL_GRACE = 20  # seconds a stopped job's processes get between TERM and KILL
HOST = socket.gethostname().split(".")[0]

Json = dict[str, Any]


class CampaignError(Exception):
    pass


def die(message: str) -> NoReturn:
    raise CampaignError(message)


def sh(args: list[str], cwd: Path | None = None, check: bool = True) -> str:
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, errors="replace")
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


_parsed: dict[tuple[Path, Callable[..., Any]], tuple[tuple[int, int, int], Any]] = {}


def shared(path: Path, parse: Callable[[Path], Any]) -> Any:
    """parse(PATH), once per process while the file keeps its inode, mtime and size; None when it
    does not exist. Every caller gets the same object, so none may change it."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    ident = (st.st_ino, st.st_mtime_ns, st.st_size)
    hit = _parsed.get((path, parse))
    if hit is None or hit[0] != ident:
        hit = _parsed[(path, parse)] = (ident, parse(path))
    return hit[1]


def read_json_shared(path: Path) -> Json | None:
    """read_json, parsed once per process while the file is the same (shared)."""
    data: Json | None = shared(path, read_json)
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
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(stat) and not stat.startswith("Z")  # "": reaped since the kill


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
_fingerprint: list[Future[str]] = []


def fingerprint() -> str:
    """The host's toolchain, so a cached result is reused only on the toolchain that produced it."""
    warm_fingerprint()
    return _fingerprint[0].result()


def warm_fingerprint() -> None:
    """Starts reading the host's toolchain (a quarter second of compilers' --version) once per process."""
    if not _fingerprint:
        _fingerprint.append(pool().submit(toolchain))


def toolchain() -> str:
    out = sh(["bash", "-c", FINGERPRINT_CMD + os.environ.get("CAMPAIGN_FINGERPRINT", "")], check=False)
    return hashlib.sha256(out.encode()).hexdigest()[:16]


_pool: list[ThreadPoolExecutor] = []


def pool() -> ThreadPoolExecutor:
    """This process's threads for waiting on other processes (git, gh) side by side: enough for a
    campaign's repositories and GitHub questions at once, since they mostly wait."""
    if not _pool:
        from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

        _pool.append(ThreadPoolExecutor(max_workers=32))
    return _pool[0]


def stamp_time(at: str) -> float:
    """The epoch time of a stamp()."""
    return time.mktime(time.strptime(at, "%Y-%m-%dT%H:%M:%S"))


CI_CHECK = "ci:"  # a check whose command is ci:JOB is met by that GitHub CI job, not by a local one
CI_CITE = re.compile(r"`ci:([^`]+)`")  # an Evidence row citing a CI job: `ci:ThreadSanitizer`


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def table_cells(row: str) -> list[str]:
    """A Markdown table row's cells; `\\|` is a pipe inside a cell, as GitHub writes it."""
    text = row.strip()
    text = text[1:] if text.startswith("|") else text
    text = text[:-1] if text.endswith("|") and not text.endswith("\\|") else text
    return [x.strip().replace("\\|", "|") for x in re.split(r"(?<!\\)\|", text)]


def section(text: str, title: str) -> str:
    m = re.search(rf"^## {title}\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1) if m else ""


def span(seconds: float) -> str:
    m = round(seconds / 60)
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def jobs_dir() -> Path:
    d = Path(os.environ.get("CAMPAIGN_JOBS") or Path.home() / ".scratch" / "campaign" / "jobs")
    if d not in _made:
        d.mkdir(parents=True, exist_ok=True)
        _made.add(d)
    return d


_made: set[Path] = set()  # job stores this process made sure exist


def mtime(path: Path) -> float | None:
    """A file's modification time, or None when it is gone (its owner may remove it at any moment)."""
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return None


def size_text(n: int) -> str:
    return f"{n / 2**30:.1f} GiB" if n >= 2**30 else f"{n / 2**20:.0f} MiB"
