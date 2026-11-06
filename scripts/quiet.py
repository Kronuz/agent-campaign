#!/usr/bin/env python3
"""quiet: share one machine between agents' builds, tests and measurements.

Holds are files in ~/.scratch/quiet/, one KEY=VALUE per line. Levels: light (builds, tests, fuzz;
a few at once, nice 19), strict (a measurement pinned to its own cores, light work beside it),
exclusive (a measurement alone on the machine). `quiet --help` is the manual. A hold lives while
its process runs and before its deadline; dead or expired holds are removed on sight.
"""

from __future__ import annotations

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

USAGE = """\
quiet: share one machine between agents' builds, tests and measurements.

Usage:
  quiet LEVEL NAME MINUTES WHAT -- CMD...   wait for a turn at LEVEL, then run CMD
  quiet run NAME MINUTES WHAT -- CMD...     the same, at $QUIET_LEVEL (default strict)
  quiet hold NAME MINUTES WHAT [PID]        hold the machine exclusively until released,
                                            the deadline, or PID exits
  quiet release NAME                        end a hold
  quiet list                                show the live holds: level, what, who, until
  quiet check                               exit 0 free; 2 busy, work can still start;
                                            1 blocked: exclusive hold or disk full
  quiet wait [--light]                      block until free (--light: until light
                                            work can start)

Levels:
  light      Builds, tests, fuzz. A few run at once (a quarter of the CPUs), at nice 19.
             While a strict hold runs they use only the cores it leaves free.
  strict     A measurement on its own cores (the upper half). One at a time; light
             work keeps running on the other half. Expect some noise from the shared
             cache and memory bandwidth.
  exclusive  A measurement with the machine to itself: waits for every hold and for
             the load to settle, and nothing else starts until it ends. Use it for
             cache-sensitive A/Bs and numbers you publish.

NAME names the hold, MINUTES bounds it (it also ends when CMD exits), WHAT says what
it is for. The level is recorded in the hold, so every agent's quiet honors it.
A strict or exclusive hold lasts at most QUIET_MAX_MIN minutes (30), and its command
is stopped at the deadline (exit 124): a measurement never runs on unheld. A light
command keeps running unheld past its deadline unless QUIET_KILL=1.

Platforms: Linux pins cores with CPU affinity, FreeBSD with cpuset. Macs and single-CPU
machines can't split cores: strict acts as exclusive, or with QUIET_PAUSE=1 stops light
holds (SIGSTOP) while it runs and resumes them after.
Local VMs: on Linux, strict also moves qemu VMs (QUIET_VM names the measured one), so a
measurement inside a VM can be strict; elsewhere VM threads can't be pinned, so use exclusive.

Examples:
  quiet light build 30 "cargo test" -- cargo test -j4
  quiet strict smoke 20 "detector smoke" -- ./smoke.sh
  quiet exclusive ab 20 "gate A/B" -- ./bench.sh

Environment:
  QUIET_LEVEL          level for `run` (light, strict, exclusive; default strict)
  QUIET_STRICT_CPUS    the strict core set (default: upper half of the physical cores,
                       SMT siblings together, e.g. 4-7)
  QUIET_PAUSE          1: on a host that can't split cores, strict pauses the light holds
                       instead of waiting for them (not exclusive, not VMs; light holds
                       started from a terminal are not pausable and are waited for)
  QUIET_VM             strict on Linux: the local VM being measured (pids or a pgrep -f
                       pattern); its threads join the strict set, other qemu VMs of this
                       user move to the light set, and both are restored after
  QUIET_LIGHT_SLOTS    light holds at once (default: CPUs / 4)
  QUIET_MAX_LOAD       no new light hold above this load (default: CPUs * 3 / 4)
  QUIET_LOAD_WAIT      minutes exclusive waits for the load to settle (default 10)
  QUIET_MAX_MIN        longest strict or exclusive hold, in minutes (default 30)
  QUIET_KILL           1: stop a light command at its deadline too (job runners set it)
  QUIET_STARVE_MIN     a light request waiting this long goes before new strict holds (10)
  QUIET_DISK_MIN_GB    no new hold below this free disk (default 5)
  QUIET_DISK_WARN_GB   `check` warns below this (default 20)
  QUIET_WHO            holder name in `list` (default: user, directory, parent pid)
  QUIET_NOTIFY_AFTER   notify (via `report`) when a run hold took this many seconds or
                       more (default 300); its sidebar pill shows queued, then running
  QUIET_DIR            state directory (default ~/.scratch/quiet)

quiet coordinates this machine only; a remote host runs its own quiet.
"""

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
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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


def try_hold(name: str, level: str, exclusive: bool, start: float) -> tuple[str, str] | None:
    """The hold's cores and pause flag once its level's rule allows it, else None."""
    if free_gb() < int(env("QUIET_DISK_MIN_GB", "5")):
        return None
    if level == "strict":
        if count("strict", name) or older_pending(name) or starving(name):
            return None
        cpus = strict_set(exclusive)
        if cpus:  # only light holds on other cores may overlap
            return None if unpinned_light(name, complement(cpus)) else (cpus, "")
        if env("QUIET_PAUSE", "") == "1" and not exclusive:  # light holds are paused instead of waited out
            return None if unpausable_light(name) else ("", "1")
        if count("light", name):
            return None
        # let the load settle, but not forever: other sessions' work isn't ours to wait out
        if load1() >= QUARTER and time.time() - start < 60 * float(env("QUIET_LOAD_WAIT", "10")):
            return None
        return "", ""
    beside = strict_beside(name, (DIR / f"{name}.starving").exists())
    if beside is None:
        return None
    if count("light", name) >= int(env("QUIET_LIGHT_SLOTS", str(QUARTER))) or load1() >= max_load():
        return None
    return (complement(beside) if beside else ""), ""


def acquire(name: str, minutes: int, what: str, level: str, exclusive: bool) -> bool:
    """Waits for the hold; False when a signal ended the wait."""
    me = os.getpid()
    if level == "strict":  # queue ahead of new light holds
        until = int(time.time()) + 60 * (minutes + 120)
        put(
            DIR / f"{name}.pending",
            [
                ("pid", me),
                ("until", until),
                ("cpus", strict_set(exclusive)),
                ("what", what),
                ("since", time.strftime("%H:%M:%S")),
                ("level", "strict"),
            ],
        )
    marker = DIR / f"{name}.starving"
    start = time.time()
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
            got = try_hold(name, level, exclusive, start)
            if got is not None:
                write_hold(name, minutes, what, me, level, cpus=got[0], pause=got[1])
                for path in (DIR / f"{name}.pending", marker):
                    path.unlink(missing_ok=True)
                return True
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


# Sidebar pills and notifications through report (cmux, else the desktop); a no-op without it.
# Each pill has a marker in pills/ naming its run's pid, so a pill whose run died without its
# cleanup (SIGKILL) is cleared on sight, like a dead hold.


def report(*args: str, ws: str | None = None) -> None:
    exe = shutil.which("report")
    if not exe:
        return
    environ = dict(os.environ) if ws is None else {**os.environ, "CMUX_WORKSPACE_ID": ws}
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run([exe, *args], env=environ, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)


def pill(name: str, minutes: int, value: str, icon: str) -> None:
    until = int(time.time()) + 60 * (minutes + 120)
    put(DIR / "pills" / name, [("pid", os.getpid()), ("until", until), ("ws", os.environ.get("CMUX_WORKSPACE_ID", ""))])
    report("status", f"quiet-{name}", value, icon)


def unpill(name: str) -> None:
    report("unstatus", f"quiet-{name}")
    (DIR / "pills" / name).unlink(missing_ok=True)


def reap_pills() -> None:
    """Clears the pills of dead runs, in the workspace each was set in."""
    with contextlib.suppress(FileNotFoundError):
        for path in sorted((DIR / "pills").iterdir()):
            if path.name.endswith(".tmp") or not path.is_file():
                continue
            info = read(path)
            if not live(info):
                report("unstatus", f"quiet-{path.name}", ws=get(info, "ws"))
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
        report("notify", f"quiet {name}: {result}", f"{what} after {secs // 60}m{secs % 60}s")


def minutes_arg(text: str) -> int | None:
    if re.fullmatch(r"[0-9]+", text) and int(text) > 0:
        return int(text)
    print(f"quiet: MINUTES must be a positive integer, not '{text}'", file=sys.stderr)
    return None


def run(level: str, exclusive: bool, args: list[str]) -> int:
    if len(args) < 3:
        print(USAGE, end="", file=sys.stderr)
        return 2
    name, text, what, *cmd = args
    cmd = cmd[1:] if cmd[:1] == ["--"] else cmd
    minutes = minutes_arg(text)
    if minutes is None:
        return 2
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
            print(
                f"{path.name} [{label}{on}]: {get(info, 'what')} (since {get(info, 'since')}, until {clock(get(info, 'until'))}{by})"
            )
        if level != "light" and not cpus:  # needs the whole machine
            state = 0
        elif state:  # light, or strict on its own cores, which leaves the rest to light holds
            state = 2
    return state


def show_waiting() -> None:
    for path in [*entries(".pending"), *entries(".starving")]:
        info = read(path)
        pid = get(info, "pid")
        if pid.isdigit() and alive(int(pid)):
            print(f"{path.name} [waiting, {get(info, 'level')}]: {get(info, 'what')} (since {get(info, 'since')})")


def check() -> int:
    code = {1: 0, 2: 2}.get(show_holds(True), 1)
    # free GiB on $HOME's volume: below QUIET_DISK_MIN_GB builds fail oddly, so it blocks
    free, low, warn, home = (
        free_gb(),
        int(env("QUIET_DISK_MIN_GB", "5")),
        int(env("QUIET_DISK_WARN_GB", "20")),
        Path.home(),
    )
    if free < low:
        print(f"disk: {free} GiB free on {home}'s volume, below {low}: free space first")
        code = 1
    elif free < warn:
        print(f"disk: {free} GiB free on {home}'s volume, below {warn}: delete build dirs you no longer need")
        code = code or 2
    load, most = load1(), max_load()
    if load >= most:
        print(f"load: {load} on {NCPU} CPUs, above {most}: start no new build or test until it drops")
        code = code or 2
    return code


def main(argv: list[str]) -> int:
    (DIR / "pills").mkdir(parents=True, exist_ok=True)
    verb, args = (argv[0], argv[1:]) if argv else ("", [])
    if verb in ("light", "strict", "exclusive"):  # quiet LEVEL NAME ...: whatever the environment says
        return run("light" if verb == "light" else "strict", verb == "exclusive", args)
    level = env("QUIET_LEVEL", "strict")
    if verb in ("run", "hold") and level not in ("light", "strict", "exclusive"):
        print(f"quiet: QUIET_LEVEL must be light, strict or exclusive, not '{level}'", file=sys.stderr)
        return 2
    if verb == "run":
        return run("light" if level == "light" else "strict", level == "exclusive", args)
    if verb == "hold" and len(args) >= 3:
        minutes = minutes_arg(args[1])
        if minutes is None:
            return 2
        with locked():
            write_hold(
                args[0],
                minutes,
                args[2],
                int(args[3]) if len(args) > 3 else 0,
                "light" if level == "light" else "strict",
            )
        return 0
    if verb == "release" and args:
        drop(DIR / args[0])
        return 0
    if verb == "_watch" and len(args) == 3:  # the pause watchdog, started by run
        return watch(Path(args[0]), int(args[1]), int(args[2]))
    if verb == "check":
        return check()
    if verb == "list":
        show_holds(True)
        show_waiting()
        return 0
    if verb == "wait":
        while True:
            state = show_holds(False)
            if state == 1 or (state == 2 and args[:1] == ["--light"]):
                return 0
            time.sleep(30)
    if verb in ("-h", "--help", "help"):
        print(USAGE, end="")
        return 0
    print(USAGE, end="", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
