#!/usr/bin/env python3
"""quiet: share one machine between agents' builds, tests and measurements.

Levels:
  light      Builds, tests, fuzz. A few run at once, at nice 19, on the cores a strict hold
             leaves free.
  strict     A measurement on its own cores, one at a time, beside light work. Macs can't
             split cores, so there it is exclusive.
  exclusive  A measurement with the machine to itself, for cache-sensitive A/Bs and numbers
             you publish.

`quiet LEVEL NAME MINUTES WHAT -- CMD` waits for a turn at that level, then runs CMD under a hold
every agent's quiet honors. NAME names the hold (one hold per name), MINUTES bounds it, WHAT says
what it is for. A hold ends with CMD or at its deadline; dead or expired holds are removed on sight.
`quiet help COMMAND` documents each command and its settings.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Collection, Iterator
from pathlib import Path
from types import FrameType

import report
from style import add_help, mark, paint, show_help

EPILOG = """examples:
  quiet light build 30 "cargo test" -- cargo test -j4
  quiet strict smoke 20 "detector smoke" -- ./smoke.sh
  quiet exclusive ab 20 "gate A/B" -- ./bench.sh

environment (every hold):
  QUIET_DIR            state directory (default ~/.scratch/quiet)
  QUIET_DISK_MIN_GB    no new hold below this free disk (default 5)
  QUIET_WHO            holder name in `list` (default: user, directory, parent pid)
  QUIET_NOTIFY_AFTER   notify when a run took this many seconds or more (default 300)

quiet coordinates this machine only; a remote host runs its own quiet."""

LEVEL_HELP = {
    "light": (
        "builds, tests, fuzz: wait for a light turn, then run CMD at nice 19",
        """Waits for a light turn, then runs CMD at nice 19. A few light holds run at once; while a strict
hold runs they use only the cores it leaves free. A light request waiting QUIET_STARVE_MIN minutes
goes before new strict requests, so builds are never starved. A light command keeps running past
its deadline unless QUIET_KILL=1. Inside another hold, CMD runs at once under it.""",
        """environment:
  QUIET_LIGHT_SLOTS    light holds at once (default: CPUs / 2 while the load is under CPUs / 2
                       and no strict hold runs, else CPUs / 4)
  QUIET_MAX_LOAD       no new light hold above this load (default: CPUs * 3 / 4)
  QUIET_STARVE_MIN     minutes before a waiting light request goes first (default 10)
  QUIET_KILL           1: stop the command at its deadline too (job runners set it)""",
    ),
    "strict": (
        "a measurement: wait for a strict turn, then run CMD on its own cores",
        """Waits for a strict turn, then runs CMD on its own cores (the upper half of the physical cores);
strict holds go one at a time, in arrival order, and light work keeps running on the other half.
Expect some noise from the shared cache and memory bandwidth. CMD is stopped at the deadline (exit
124, an invalid result), which is at most QUIET_MAX_MIN minutes away: a measurement never runs
unheld. Inside a strict or exclusive hold, CMD runs at once under it; inside a light one, it is
refused, since it would wait for its own parent.

Platforms: Linux pins cores with CPU affinity, FreeBSD with cpuset. Macs and single-CPU machines
can't split cores: strict acts as exclusive, or with QUIET_PAUSE=1 stops light holds (SIGSTOP)
while it runs and resumes them after. On Linux strict also moves qemu VMs (QUIET_VM names the
measured one), so a measurement inside a VM can be strict; elsewhere use exclusive.""",
        """environment:
  QUIET_STRICT_CPUS    the strict core set (default: upper half of the physical cores,
                       SMT siblings together, e.g. 4-7)
  QUIET_PAUSE          1: where cores can't be split, pause light holds instead of waiting
                       (not exclusive, not VMs; light holds started from a terminal can't pause)
  QUIET_VM             Linux: the local VM being measured (pids or a pgrep -f pattern); its
                       threads join the strict set, other qemu VMs of this user move to the
                       light set, and both are restored after
  QUIET_LOAD_WAIT      minutes to wait for the load to settle where cores can't be split (10)
  QUIET_MAX_MIN        longest strict or exclusive hold, in minutes (default 30)""",
    ),
    "exclusive": (
        "a measurement alone: wait until the machine is quiet, then run CMD",
        """Waits for every hold to end and for the load to settle (at most QUIET_LOAD_WAIT minutes), then
runs CMD with the machine to itself; nothing else starts until it ends. Use it for cache-sensitive
A/Bs and numbers you publish. CMD is stopped at its deadline (exit 124), at most QUIET_MAX_MIN
minutes away. Inside an exclusive hold, CMD runs at once under it.""",
        """environment:
  QUIET_LOAD_WAIT      minutes to wait for the load to settle (default 10)
  QUIET_MAX_MIN        longest strict or exclusive hold, in minutes (default 30)""",
    ),
    "run": (
        "the same at $QUIET_LEVEL (default strict)",
        """Runs CMD as `quiet light`, `strict` or `exclusive` would, by $QUIET_LEVEL.""",
        """environment:
  QUIET_LEVEL          light, strict or exclusive (default strict)""",
    ),
}

Hold = dict[str, list[str]]


def env(name: str, default: str) -> str:
    return os.environ.get(name) or default


DIR = Path(env("QUIET_DIR", str(Path.home() / ".scratch" / "quiet")))
NCPU = os.cpu_count() or 4
QUARTER = max(NCPU // 4, 1)
MINUTE = float(env("QUIET_MINUTE", "60"))  # seconds per minute; tests shorten it
KILL = env("QUIET_KILL", "0") == "1"
SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
TERM_GRACE = 10  # seconds a stopped command gets between TERM and KILL
SUFFIXES = (".tmp", ".pending", ".starving")
# Core partitioning: a strict hold is pinned to QUIET_STRICT_CPUS and light holds started while
# one is live or queued take the other cores. The halves still share L3 and memory bandwidth (a
# 32 MB random walk slowed 58% beside a 4-job build on an 8-core VM), which is why cache-sensitive
# numbers take exclusive. macOS has no affinity: beside a measurement, 4 build-like loads cost
# 7-8% (Intel) and 5-10% (Apple Silicon) of CPU-bound timing at nice 19 or background QoS alike,
# and nothing when SIGSTOPped, so a strict hold there is exclusive or pauses light holds.
PIN = (
    "affinity"
    if sys.platform == "linux"
    else "cpuset"
    if sys.platform.startswith("freebsd") and shutil.which("cpuset")
    else ""
)


# CPU lists ("0-3,6") and the affinity masks recorded for VM threads ("4f", as taskset prints them)


def expand(cpus: str) -> list[int]:
    out: list[int] = []
    for part in cpus.split(","):
        lo, _, hi = part.strip().partition("-")
        if lo:
            out.extend(range(int(lo), int(hi or lo) + 1))
    return out


def complement(cpus: str) -> str:
    """The CPUs of this machine not in the list, as a list: the light set beside a strict one."""
    taken = set(expand(cpus))
    return ",".join(str(i) for i in range(NCPU) if i not in taken)


def to_mask(cpus: Collection[int]) -> str:
    return format(sum(1 << c for c in cpus), "x")


def from_mask(mask: str) -> set[int]:
    n = int(mask.replace(",", ""), 16)
    return {i for i in range(n.bit_length()) if n >> i & 1}


def get_affinity(pid: int) -> set[int]:
    if sys.platform == "linux":
        return os.sched_getaffinity(pid)
    return set()


def set_affinity(pid: int, cpus: Collection[int]) -> None:
    if sys.platform == "linux":
        os.sched_setaffinity(pid, cpus)


def core_set() -> str:
    """The default strict set: the upper half of the physical cores, SMT siblings together."""
    p_cores: set[int] | None = None
    with contextlib.suppress(OSError):
        p_cores = set(expand(Path("/sys/devices/cpu_core/cpus").read_text().strip()))  # hybrid: P-cores only
    groups: set[str] = set()
    for f in Path("/sys/devices/system/cpu").glob("cpu[0-9]*/topology/thread_siblings_list"):
        with contextlib.suppress(OSError):
            groups.add(f.read_text().strip())
    ordered = sorted(groups, key=lambda g: expand(g)[0])
    if p_cores is not None:
        ordered = [g for g in ordered if expand(g)[0] in p_cores]
    if len(ordered) < 2:
        return f"{NCPU // 2}-{NCPU - 1}"
    return ",".join(ordered[len(ordered) // 2 :])


def strict_set(exclusive: bool) -> str:
    """The cores a strict hold would use; empty when not partitioning."""
    if exclusive or not PIN or NCPU < 2:
        return ""
    s = env("QUIET_STRICT_CPUS", "") or core_set()
    rest = complement(s)
    return s if rest and rest != complement("") else ""


# Hold files


def read(path: Path) -> Hold:
    """A hold's fields; a hold dropped by another call reads as empty."""
    info: Hold = {}
    try:
        text = path.read_text()
    except OSError:
        return info
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            info.setdefault(key, []).append(value)
    return info


def get(info: Hold, key: str) -> str:
    values = info.get(key)
    return values[0] if values else ""


def put(path: Path, fields: list[tuple[str, object]]) -> None:
    """Writes a hold or marker whole, so a reader never sees half of one."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("".join(f"{k}={str(v).replace(chr(10), ' ')}\n" for k, v in fields))
    tmp.replace(path)


def append(path: Path, line: str) -> None:
    with path.open("a") as fh:
        fh.write(line + "\n")


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


def live(info: Hold) -> bool:
    """True when its pid runs (0: no pid) and its deadline is ahead."""
    try:
        until, pid = int(get(info, "until")), int(get(info, "pid") or "0")
    except ValueError:
        return False
    return int(time.time()) < until and (pid == 0 or alive(pid))


def drop(path: Path) -> None:
    """Removes a hold after undoing what it did to other processes."""
    info = read(path)
    resume(info)
    for entry in info.get("vm", []):
        tid, _, mask = entry.partition(":")
        if Path(f"/proc/{tid}").is_dir():
            with contextlib.suppress(OSError, ValueError):
                set_affinity(int(tid), from_mask(mask))
    path.unlink(missing_ok=True)


def resume(info: Hold) -> None:
    for pid in info.get("paused", []):
        with contextlib.suppress(OSError, ValueError):
            os.kill(int(pid), signal.SIGCONT)


def entries(suffix: str = "") -> list[Path]:
    """The hold files (suffix "") or markers (".pending", ".starving") in the directory."""
    try:
        names = sorted(os.listdir(DIR))
    except FileNotFoundError:
        return []
    if suffix:
        return [DIR / n for n in names if n.endswith(suffix) and not n.startswith(".")]
    return [DIR / n for n in names if not n.startswith(".") and not n.endswith(SUFFIXES) and (DIR / n).is_file()]


def live_holds(skip: str = "") -> Iterator[tuple[Path, Hold]]:
    """The live holds other than NAME skip; the rest are dropped on sight."""
    for path in entries():
        if path.name == skip:
            continue
        info = read(path)
        if live(info):
            yield path, info
        else:
            drop(path)


def count(level: str, skip: str) -> int:
    return sum(1 for _, info in live_holds(skip) if get(info, "level") == level)


def unpinned_light(skip: str, cpus: str) -> int:
    """Live light holds other than NAME skip not pinned to the light set cpus."""
    return sum(1 for _, info in live_holds(skip) if get(info, "level") == "light" and get(info, "cpus") != cpus)


def unpausable_light(skip: str) -> int:
    return sum(1 for _, info in live_holds(skip) if get(info, "level") == "light" and get(info, "pausable") != "1")


def strict_beside(name: str, starving: bool) -> str | None:
    """The cores a live or waiting strict hold other than NAME runs on: None when one needs the
    whole machine, "" when there is none. A starving light request ignores waiting strict ones."""
    found = ""
    for path in [*entries(), *([] if starving else entries(".pending"))]:
        pending = path.name.endswith(".pending")
        if path.name.removesuffix(".pending") == name:
            continue
        info = read(path)
        if not live(info):
            drop(path)
            continue
        if not pending and get(info, "level") == "light":
            continue
        cpus = get(info, "cpus")
        if not cpus:
            return None
        found = cpus
    return found


def starving(name: str) -> bool:
    """True when a light request other than NAME has waited past QUIET_STARVE_MIN."""
    found = False
    for path in entries(".starving"):
        if not live(read(path)):
            path.unlink(missing_ok=True)
        elif path.name != f"{name}.starving":
            found = True
    return found


def older_pending(name: str) -> bool:
    """True when a live strict request queued before NAME's still waits: strict holds go in order."""
    mine = mtime(DIR / f"{name}.pending")
    if mine is None:
        return False
    for path in entries(".pending"):
        if path.name != f"{name}.pending" and live(read(path)):
            t = mtime(path)
            if t is not None and t < mine:
                return True
    return False


def mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def load1() -> int:
    return int(os.getloadavg()[0])


def light_slots(beside: str) -> int:
    """Light holds at once: twice the quarter while half the machine idles, since tests leave builds' cores free."""
    fixed = env("QUIET_LIGHT_SLOTS", "")
    if fixed:
        return int(fixed)
    return QUARTER if beside or load1() >= NCPU // 2 else max(NCPU // 2, QUARTER)


def max_load() -> int:
    return int(env("QUIET_MAX_LOAD", str(max(NCPU * 3 // 4, 1))))


def free_gb() -> int:
    st = os.statvfs(Path.home())
    return st.f_bavail * st.f_frsize // 1024**3


def clock(until: str) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(int(until)))
    except ValueError:
        return until


def grace(level: str) -> int:
    """Seconds a bounded command's hold outlives its deadline, covering TERM then KILL."""
    return TERM_GRACE + 5 if level == "strict" or KILL else 0


def write_hold(name: str, minutes: int, what: str, pid: int, level: str, *, cpus: str = "", pause: str = "") -> None:
    who = os.environ.get("QUIET_WHO") or f"{os.environ.get('USER', '')} in {Path.cwd().name} (ppid {os.getppid()})"
    put(
        DIR / name,
        [
            ("what", what),
            ("since", time.strftime("%H:%M:%S")),
            ("until", int(time.time()) + int(MINUTE * minutes) + grace(level)),
            ("pid", pid),
            ("host", socket.gethostname().split(".")[0]),
            ("level", level),
            ("who", who),
            ("cpus", cpus),
            ("pause", pause),
        ],
    )


@contextlib.contextmanager
def locked() -> Iterator[None]:
    """Holds the directory's lock, so check-then-hold is atomic; the kernel frees it if we die."""
    with (DIR / ".flock").open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


# Taking a hold


def try_hold(name: str, level: str, exclusive: bool, start: float) -> tuple[tuple[str, str] | None, str]:
    """The hold's cores and pause flag once its level's rule allows it, else None and why not."""
    if free_gb() < int(env("QUIET_DISK_MIN_GB", "5")):
        return None, "low disk"
    if level == "strict":
        if count("strict", name):
            return None, "behind a strict hold"
        if older_pending(name):
            return None, "behind an earlier strict request"
        if starving(name):
            return None, "a long-waiting light request goes first"
        cpus = strict_set(exclusive)
        if cpus:  # only light holds on other cores may overlap
            return (None, "light holds on its cores") if unpinned_light(name, complement(cpus)) else ((cpus, ""), "")
        if env("QUIET_PAUSE", "") == "1" and not exclusive:  # light holds are paused instead of waited out
            return (None, "a light hold that can't pause") if unpausable_light(name) else (("", "1"), "")
        lights = count("light", name)
        if lights:
            return None, f"behind {lights} light hold{'s' if lights > 1 else ''}"
        # let the load settle, but not forever: other sessions' work isn't ours to wait out
        load = load1()
        if load >= QUARTER and time.time() - start < 60 * float(env("QUIET_LOAD_WAIT", "10")):
            return None, f"load {load:g} settling"
        return ("", ""), ""
    beside = strict_beside(name, (DIR / f"{name}.starving").exists())
    if beside is None:
        return None, "a strict hold has the machine"
    if count("light", name) >= light_slots(beside):
        return None, "light slots full"
    load = load1()
    if load >= max_load():
        return None, f"load {load:g}"
    return ((complement(beside) if beside else ""), ""), ""


def claim_name(name: str, minutes: int, what: str, level: str, exclusive: bool) -> str:
    """Who else holds or waits under NAME, else "" after recording a strict request, which queues
    ahead of new light holds: one name is one hold, so a second can't take it or its sidebar pill."""
    with locked():
        for path in (DIR / name, DIR / f"{name}.pending", DIR / f"{name}.starving"):
            info = read(path) if path.exists() else {}
            if info and live(info) and get(info, "pid") != str(os.getpid()):
                return f"pid {get(info, 'pid')} ({get(info, 'what')})"
        if level == "strict":
            put(
                DIR / f"{name}.pending",
                [
                    ("pid", os.getpid()),
                    ("until", int(time.time()) + 60 * (minutes + 120)),
                    ("cpus", strict_set(exclusive)),
                    ("what", what),
                    ("since", time.strftime("%H:%M:%S")),
                    ("level", "strict"),
                ],
            )
    return ""


RANK = {"light": 0, "strict": 1, "exclusive": 2}


def nested(level: str, exclusive: bool, cmd: list[str]) -> int | None:
    """Inside a live hold ($QUIET_HELD, set for its command), a request it covers runs right away
    under it; a stronger one would wait for its own parent, so it is refused. None outside a hold."""
    held = os.environ.get("QUIET_HELD", "").split()
    if len(held) != 3:
        return None
    name, kind, pid = held
    info = read(DIR / name) if (DIR / name).exists() else {}
    if not info or not live(info) or get(info, "pid") != pid:
        return None
    want = "exclusive" if exclusive else level
    if RANK[want] > RANK.get(kind, 0):
        print(
            f"quiet: this runs inside {name}'s {kind} hold, so a {want} request would wait for its own parent; "
            "run it as its own hold or job",
            file=sys.stderr,
        )
        return 2
    try:
        os.execvp(cmd[0], cmd)
    except OSError as e:
        print(f"quiet: cannot run {cmd[0]}: {e}", file=sys.stderr)
        return 127 if isinstance(e, FileNotFoundError) else 126


def acquire(name: str, minutes: int, what: str, level: str, exclusive: bool) -> bool:
    """Waits for the hold; False when a signal ended the wait."""
    me = os.getpid()
    marker = DIR / f"{name}.starving"
    start = time.time()
    shown = ""
    while not STOPPING:
        # a light request that waited long enough goes before new strict holds (bounded service);
        # it says so before taking the lock, which busy pollers may hold most of the time
        if (
            level == "light"
            and not marker.exists()
            and time.time() - start >= MINUTE * float(env("QUIET_STARVE_MIN", "10"))
        ):
            until = int(time.time()) + 86400
            put(
                marker,
                [
                    ("pid", me),
                    ("until", until),
                    ("what", what),
                    ("since", time.strftime("%H:%M:%S")),
                    ("level", "light"),
                ],
            )
        with locked():
            got, why = try_hold(name, level, exclusive, start)
            if got is not None:
                write_hold(name, minutes, what, me, level, cpus=got[0], pause=got[1])
                for path in (DIR / f"{name}.pending", marker):
                    path.unlink(missing_ok=True)
                return True
        if why != shown:  # the pill says what it waits for
            pill(name, minutes, f"queued ({why}): {what}", "hourglass")
            shown = why
        nap(float(env("QUIET_POLL", "5")))
    return False


# Pausing light holds (hosts that can't split cores, QUIET_PAUSE=1)


def stop_tree(hold: Path, pid: int) -> None:
    """SIGSTOPs pid's descendants top-down, recording each first; a stopped parent can't fork."""
    found = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout.split()
    for child in map(int, found):
        append(hold, f"paused={child}")
        try:
            os.kill(child, signal.SIGSTOP)
        except OSError:
            continue
        stop_tree(hold, child)


def pause_light(name: str) -> None:
    for _, info in live_holds(name):
        if get(info, "level") == "light" and get(info, "pausable") == "1":
            stop_tree(DIR / name, int(get(info, "pid")))


def watchdog(hold: Path, until: int) -> subprocess.Popen[bytes]:
    """Starts `quiet _watch`, which resumes the paused jobs if this run dies without its cleanup, or
    at its deadline. It has its own session, so a job runner's sweep of this one can't stop it first."""
    return subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "_watch", str(hold), str(os.getpid()), str(until)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def watch(hold: Path, pid: int, until: int) -> int:
    while alive(pid) and time.time() < until:
        time.sleep(2)
    resume(read(hold))
    return 0


# Local VMs: pinning a command never reaches a VM's vCPU threads, so a strict hold on Linux moves
# the VMs themselves. QUIET_VM names the measured VM (pids or a pgrep -f pattern): it goes to the
# strict set; every other qemu process of this user goes to the light set; each thread's mask is
# saved in the hold and restored when it is dropped.


def qemu_pids(pattern: str) -> list[int]:
    """This user's processes whose command line matches qemu-system.*PATTERN, as pgrep -f does."""
    try:
        rx = re.compile("qemu-system.*" + pattern)
    except re.error as e:
        print(f"quiet: QUIET_VM is not a valid pattern: {e}", file=sys.stderr)
        return []
    found: list[int] = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or int(proc.name) == os.getpid():
            continue
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            cmdline = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if rx.search(cmdline):
            found.append(int(proc.name))
    return sorted(found)


def vm_pids(spec: str) -> list[int]:
    if not spec:
        return []
    if re.fullmatch(r"[0-9, ]+", spec):
        return [int(p) for p in re.split(r"[, ]+", spec) if p]
    return qemu_pids(spec)


def vm_move(hold: Path, cpus: str, pids: list[int]) -> None:
    """Records each thread's mask in the hold, then pins the thread to cpus."""
    recorded = {e.partition(":")[0] for e in read(hold).get("vm", [])}
    for pid in pids:
        try:
            tids = os.listdir(f"/proc/{pid}/task")
        except OSError:
            continue
        for tid in tids:
            if tid in recorded:
                continue
            try:
                mask = get_affinity(int(tid))
            except OSError:
                continue
            append(hold, f"vm={tid}:{to_mask(mask)}")
            recorded.add(tid)
            try:
                set_affinity(int(tid), expand(cpus))
            except OSError as e:
                print(f"quiet: cannot pin thread {tid} to {cpus}: {e}", file=sys.stderr)


def pin_vms(hold: Path, cpus: str) -> None:
    measured = vm_pids(env("QUIET_VM", ""))
    others = [p for p in qemu_pids("") if p not in measured]
    if not measured and not others:
        return
    have = len(expand(cpus))
    for pid in measured:
        vcpus = 0
        for comm in Path(f"/proc/{pid}/task").glob("*/comm"):
            with contextlib.suppress(OSError):
                vcpus += comm.read_text().startswith("CPU ")
        if vcpus > have:
            print(
                f"quiet: VM {pid} has {vcpus} vCPUs on {have} strict CPUs; expect lock-holder preemption in the guest",
                file=sys.stderr,
            )
    for again in (False, True):  # a second pass catches threads started meanwhile
        if again:
            time.sleep(1)
        vm_move(hold, cpus, measured)
        vm_move(hold, complement(cpus), others)


# Sidebar pills and notifications through report (cmux, else the desktop).
# Each pill has a marker in pills/ naming its run's pid, so a pill whose run died without its
# cleanup (SIGKILL) is cleared on sight, like a dead hold.


def pill(name: str, minutes: int, value: str, icon: str) -> None:
    until = int(time.time()) + 60 * (minutes + 120)
    put(DIR / "pills" / name, [("pid", os.getpid()), ("until", until), ("ws", os.environ.get("CMUX_WORKSPACE_ID", ""))])
    report.status(f"quiet-{name}", value, icon)


def unpill(name: str) -> None:
    report.unstatus(f"quiet-{name}")
    (DIR / "pills" / name).unlink(missing_ok=True)


def reap_pills() -> None:
    """Clears the pills of dead runs, in the workspace each was set in."""
    with contextlib.suppress(FileNotFoundError):
        for path in sorted((DIR / "pills").iterdir()):
            if path.name.endswith(".tmp") or not path.is_file():
                continue
            info = read(path)
            if not live(info):
                report.unstatus(f"quiet-{path.name}", get(info, "ws"))
                path.unlink(missing_ok=True)


# Running a command under a hold


STOPPING: list[int] = []  # the first TERM, INT or HUP that reached quiet


def on_signal(signum: int, _frame: FrameType | None) -> None:
    if not STOPPING:
        STOPPING.append(signum)


def nap(seconds: float) -> None:
    """Sleeps, waking early for a signal."""
    end = time.time() + seconds
    while not STOPPING and time.time() < end:
        time.sleep(min(0.1, max(end - time.time(), 0)))


def spawn(cmd: list[str], group: bool) -> subprocess.Popen[bytes]:
    if not group:
        return subprocess.Popen(cmd)
    if sys.version_info >= (3, 11):
        return subprocess.Popen(cmd, process_group=0)
    return subprocess.Popen(cmd, preexec_fn=os.setpgrp)


def group_alive(child: subprocess.Popen[bytes]) -> bool:
    child.poll()  # a zombie leader still counts as a member
    try:
        os.killpg(child.pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def stop_group(child: subprocess.Popen[bytes]) -> None:
    """TERMs the command's process group and KILLs what is left TERM_GRACE seconds later."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(child.pid, signal.SIGTERM)
    end = time.time() + TERM_GRACE
    while group_alive(child) and time.time() < end:
        time.sleep(0.1)
    if group_alive(child):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(child.pid, signal.SIGKILL)
    child.wait()


def execute(cmd: list[str], *, name: str, minutes: int, light: bool, cpus: str, deadline: float | None) -> int:
    """Runs CMD at the hold's priority and on its cores, which quiet takes first so CMD inherits
    them. With a deadline, CMD runs in its own process group (still in the caller's session, which
    job runners sweep) and is stopped there: exit 124."""
    if light:
        os.nice(19)
    if cpus and PIN == "cpuset":
        cmd = ["cpuset", "-l", cpus, *cmd]
    elif cpus:
        set_affinity(0, expand(cpus))
    group = deadline is not None
    try:
        child = spawn(cmd, group)
    except OSError as e:
        print(f"quiet: cannot run {cmd[0]}: {e}", file=sys.stderr)
        return 127 if isinstance(e, FileNotFoundError) else 126
    while True:
        with contextlib.suppress(subprocess.TimeoutExpired):
            code = child.wait(timeout=0.1)
            return code if code >= 0 else 128 - code
        if STOPPING:
            if group:
                stop_group(child)
            else:  # it shares quiet's process group, so a terminal's INT reached it already
                if STOPPING[0] != signal.SIGINT:
                    with contextlib.suppress(ProcessLookupError):
                        child.send_signal(STOPPING[0])
                child.wait()
            return 128 + STOPPING[0]
        if deadline is not None and time.time() >= deadline:
            stop_group(child)
            print(
                f"quiet: {name} reached its {minutes}-minute deadline and was stopped; its result is invalid (split longer work)",
                file=sys.stderr,
            )
            return 124


def finished(name: str, what: str, code: int, started: float) -> None:
    """Clears the run's pill; notifies when it ran QUIET_NOTIFY_AFTER seconds or more."""
    secs = int(time.time() - started)
    unpill(name)
    if secs >= int(env("QUIET_NOTIFY_AFTER", "300")):
        result = "done" if code == 0 else f"failed (exit {code})"
        report.notify(f"quiet {name}: {result}", f"{what} after {secs // 60}m{secs % 60}s")


def positive(text: str) -> int:
    if re.fullmatch(r"[0-9]+", text) and int(text) > 0:
        return int(text)
    raise argparse.ArgumentTypeError(f"must be a positive integer, not '{text}'")


def run(level: str, exclusive: bool, a: argparse.Namespace) -> int:
    name, minutes, what = a.name, a.minutes, a.what
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    longest = int(env("QUIET_MAX_MIN", "30"))
    if level == "strict" and minutes > longest:
        print(
            f"quiet: a strict or exclusive hold lasts at most {longest} minutes; split the run into shorter pieces",
            file=sys.stderr,
        )
        return 2
    if not cmd:
        print("quiet: no command to run after --", file=sys.stderr)
        return 2
    inside = nested(level, exclusive, cmd)
    if inside is not None:
        return inside
    clash = claim_name(name, minutes, what, level, exclusive)
    if clash:
        print(f"quiet: {name} is already held or awaited by {clash}; use another name", file=sys.stderr)
        return 2
    hold = DIR / name
    started, code = 0.0, 1
    guard: subprocess.Popen[bytes] | None = None
    for s in SIGNALS:
        if signal.getsignal(s) != signal.SIG_IGN:  # one ignored on entry stays ignored, as in a shell
            signal.signal(s, on_signal)
    try:
        reap_pills()
        pill(name, minutes, f"queued: {what}", "hourglass")
        if not acquire(name, minutes, what, level, exclusive):
            return 128 + STOPPING[0]
        started = time.time()
        pill(name, minutes, f"{level}: {what}", "hammer")
        info = read(hold)
        cpus, until = get(info, "cpus"), int(get(info, "until"))
        kind = "exclusive" if exclusive or (level == "strict" and not cpus) else level
        os.environ["QUIET_HELD"] = f"{name} {kind} {os.getpid()}"  # quiet inside the command runs under this hold
        if level == "strict" and get(info, "pause") == "1":
            pause_light(name)
            guard = watchdog(hold, until)
        if level == "strict" and cpus and PIN == "affinity":
            pin_vms(hold, cpus)
        if level == "light" and not os.isatty(0):
            append(hold, "pausable=1")  # a job reading the terminal can't be stopped under its shell
        deadline = until - grace(level) if level == "strict" or KILL else None
        if STOPPING:
            code = 128 + STOPPING[0]
        else:
            code = execute(cmd, name=name, minutes=minutes, light=level == "light", cpus=cpus, deadline=deadline)
        return code
    finally:
        if started:
            finished(name, what, code, started)
        else:
            unpill(name)
        if guard is not None:
            guard.terminate()
            guard.wait()
        if get(read(hold), "pid") == str(os.getpid()):
            drop(hold)
        for path in (DIR / f"{name}.pending", DIR / f"{name}.starving"):
            path.unlink(missing_ok=True)


# Reporting


LEVEL_MARKS = {"light": ("green", "🟢"), "strict": ("yellow", "🟡"), "exclusive": ("red", "🔴")}


def show_holds(out: bool) -> int:
    """Prints the live holds: 1 for none, 2 for light only (or strict on its own cores), 0 for a
    hold that needs the whole machine."""
    reap_pills()
    state = 1
    for path, info in live_holds():
        level, cpus, who = get(info, "level") or "strict", get(info, "cpus"), get(info, "who")
        label = "exclusive" if level == "strict" and not cpus else level
        if get(info, "pause") == "1":
            label = "strict, pausing light holds"
        if out:
            on = f" cpus {cpus}" if cpus else ""
            by = f", by {who}" if who else ""
            color, emoji = LEVEL_MARKS.get(label.split(",")[0], ("dim", "⚪"))
            print(
                f"{mark(emoji)}{path.name} [{paint(label, color)}{on}]: {get(info, 'what')} (since {get(info, 'since')}, until {clock(get(info, 'until'))}{by})"
            )
        if level != "light" and not cpus:  # needs the whole machine
            state = 0
        elif state:  # light, or strict on its own cores, which leaves the rest to light holds
            state = 2
    return state


def show_waiting() -> int:
    shown = 0
    for path in [*entries(".pending"), *entries(".starving")]:
        info = read(path)
        pid = get(info, "pid")
        if pid.isdigit() and alive(int(pid)):
            print(
                f"{mark('⏳')}{path.name} [waiting, {get(info, 'level')}]: {get(info, 'what')} (since {get(info, 'since')})"
            )
            shown += 1
    return shown


def check() -> int:
    held = show_holds(True)
    code = {1: 0, 2: 2}.get(held, 1)
    warned = False
    # free GiB on $HOME's volume: below QUIET_DISK_MIN_GB builds fail oddly, so it blocks
    free, low, warn, home = (
        free_gb(),
        int(env("QUIET_DISK_MIN_GB", "5")),
        int(env("QUIET_DISK_WARN_GB", "20")),
        Path.home(),
    )
    if free < low:
        print(f"{mark('❌')}{paint('disk', 'red')}: {free} GiB free on {home}'s volume, below {low}: free space first")
        code, warned = 1, True
    elif free < warn:
        print(
            f"{mark('❗')}{paint('disk', 'yellow')}: {free} GiB free on {home}'s volume, below {warn}: delete build dirs you no longer need"
        )
        code, warned = code or 2, True
    load, most = load1(), max_load()
    if load >= most:
        print(
            f"{mark('❗')}{paint('load', 'yellow')}: {load} on {NCPU} CPUs, above {most}: start no new build or test until it drops"
        )
        code, warned = code or 2, True
    if held == 1 and not warned:
        print(f"{mark('✅')}free")
    return code


def level_from_env() -> tuple[str, bool]:
    level = env("QUIET_LEVEL", "strict")
    if level not in ("light", "strict", "exclusive"):
        print(f"quiet: QUIET_LEVEL must be light, strict or exclusive, not '{level}'", file=sys.stderr)
        sys.exit(2)
    return ("light" if level == "light" else "strict"), level == "exclusive"


def cmd_run(a: argparse.Namespace) -> int:
    if a.command == "run":
        return run(*level_from_env(), a)
    return run("light" if a.command == "light" else "strict", a.command == "exclusive", a)


def cmd_hold(a: argparse.Namespace) -> int:
    with locked():
        info = read(DIR / a.name) if (DIR / a.name).exists() else {}
        if info and live(info):
            print(f"quiet: {a.name} is already held by pid {get(info, 'pid')} ({get(info, 'what')})", file=sys.stderr)
            return 2
        write_hold(a.name, a.minutes, a.what, a.pid, level_from_env()[0])
    return 0


def cmd_release(a: argparse.Namespace) -> int:
    drop(DIR / a.name)
    return 0


def cmd_list(_a: argparse.Namespace) -> int:
    held, waiting = show_holds(True), show_waiting()
    if held == 1 and not waiting:
        print("no holds and no waiting requests")
    return 0


def cmd_check(_a: argparse.Namespace) -> int:
    return check()


def cmd_watch(a: argparse.Namespace) -> int:
    return watch(a.hold, a.pid, a.until)


def cmd_wait(a: argparse.Namespace) -> int:
    while True:
        state = show_holds(False)
        if state == 1 or (state == 2 and a.light):
            return 0
        time.sleep(30)


def parser() -> argparse.ArgumentParser:
    raw = argparse.RawDescriptionHelpFormatter
    ap = argparse.ArgumentParser(prog="quiet", description=__doc__, epilog=EPILOG, formatter_class=raw)
    sub = ap.add_subparsers(dest="command", metavar="COMMAND", title="commands", required=True)
    for verb, (summary, text, settings) in LEVEL_HELP.items():
        p = sub.add_parser(verb, help=summary, description=text, epilog=settings, formatter_class=raw)
        p.add_argument("name", metavar="NAME", help="the hold's name, one hold per name")
        p.add_argument("minutes", metavar="MINUTES", type=positive, help="its bound")
        p.add_argument("what", metavar="WHAT", help="what it is for, shown by list and the sidebar")
        p.add_argument("cmd", metavar="-- CMD", nargs=argparse.REMAINDER, help="the command and its arguments")
        p.usage = "%(prog)s NAME MINUTES WHAT -- CMD [ARG ...]"
        p.set_defaults(fn=cmd_run)
    p = sub.add_parser(
        "hold",
        help="hold the machine until released, the deadline, or PID exits",
        description="Records a hold at $QUIET_LEVEL (default strict) at once, without waiting for a turn or "
        "running anything, for work started another way. It lasts until `quiet release NAME`, the deadline, or "
        "PID's exit; check first that the machine is free (`quiet check`).",
    )
    p.add_argument("name", metavar="NAME")
    p.add_argument("minutes", metavar="MINUTES", type=positive)
    p.add_argument("what", metavar="WHAT")
    p.add_argument("pid", metavar="PID", type=int, nargs="?", default=0, help="the process the hold follows")
    p.set_defaults(fn=cmd_hold)
    p = sub.add_parser("release", help="end a hold", description="Ends a hold taken with `quiet hold`.")
    p.add_argument("name", metavar="NAME")
    p.set_defaults(fn=cmd_release)
    p = sub.add_parser(
        "list",
        help="the live holds and waiting requests",
        description="Prints each live hold (level, cores, what, since, until, who) and each waiting request.",
    )
    p.set_defaults(fn=cmd_list)
    p = sub.add_parser(
        "check",
        help="exit 0 free; 2 busy, work can still start; 1 blocked",
        description="""Prints the holds and any disk or load warning ("free" when there is none), and exits 0 when
the machine is free, 2 when it is busy but light work can still start, and 1 when it is blocked (an
exclusive hold, or too little disk). Run it before starting builds, tests or measurements.""",
        epilog="""environment:
  QUIET_DISK_MIN_GB    blocked below this free disk (default 5)
  QUIET_DISK_WARN_GB   warn below this free disk (default 20)
  QUIET_MAX_LOAD       busy above this load (default: CPUs * 3 / 4)""",
        formatter_class=raw,
    )
    p.set_defaults(fn=cmd_check)
    p = sub.add_parser(
        "wait",
        help="block until free",
        description="Blocks until no hold is live, or with --light until light work can start.",
    )
    p.add_argument("--light", action="store_true", help="until light work can start")
    p.set_defaults(fn=cmd_wait)
    p = sub.add_parser("_watch")  # the pause watchdog, started by run
    p.add_argument("hold", type=Path)
    p.add_argument("pid", type=int)
    p.add_argument("until", type=int)
    p.set_defaults(fn=cmd_watch)
    add_help(sub, "quiet")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = parser()
    args = ap.parse_args(argv)
    if args.command == "help":
        return show_help(ap, args.topic)
    (DIR / "pills").mkdir(parents=True, exist_ok=True)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
