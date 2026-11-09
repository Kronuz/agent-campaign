"""quiet: measurements end at their deadline, holds outlive the stop, builds are not starved, and
strict holds get their cores (Linux) or pause light work (QUIET_PAUSE)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable
from pathlib import Path

import harness  # noqa: F401  # first: it puts the tools' directory on sys.path
import quiet
from harness import alone, scratch

QUIET = Path(__file__).resolve().parent.parent / "scripts" / "quiet.py"
LINUX = sys.platform == "linux"
NCPU = os.cpu_count() or 1


def affinity(pid: int) -> list[int]:
    if sys.platform == "linux":
        return sorted(os.sched_getaffinity(pid))
    return []


def vcpus(pid: int) -> int:
    return sum(1 for t in Path(f"/proc/{pid}/task").glob("*/comm") if t.read_text().startswith("CPU "))


class Quiet(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir=scratch())
        self.dir = Path(self._tmp.name)
        self.log = self.dir / "log"
        self.env = dict(
            os.environ,
            QUIET_DIR=str(self.dir / "q"),
            QUIET_MINUTE="2",
            QUIET_POLL="0.2",
            QUIET_LOAD_WAIT="0",
            QUIET_MAX_LOAD="1000",
            QUIET_DISK_MIN_GB="0",
            QUIET_STARVE_MIN="2",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def quiet(self, *args: str, **env: str) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [sys.executable, str(QUIET), *args],
            env={**self.env, **env},
            stdin=subprocess.DEVNULL,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def mark(self, name: str, seconds: float) -> str:
        return f"echo {name}-start >> {self.log}; sleep {seconds}; echo {name}-end >> {self.log}"

    def events(self) -> list[str]:
        return self.log.read_text().split()

    def listed(self) -> str:
        return subprocess.run([sys.executable, str(QUIET), "list"], env=self.env, text=True, capture_output=True).stdout

    def until(self, test: Callable[[], object], seconds: float = 20) -> None:
        end = time.time() + seconds
        while not test():
            self.assertLess(time.time(), end, "timed out waiting")
            time.sleep(0.05)

    def test_check_and_list_say_when_the_machine_is_free(self) -> None:
        p = self.quiet("check", QUIET_DISK_WARN_GB="0")
        out, _ = p.communicate(timeout=30)
        self.assertEqual((p.returncode, out), (0, "free\n"))
        self.assertEqual(self.listed(), "no holds and no waiting requests\n")

    def test_a_name_is_one_hold(self) -> None:
        first = self.quiet("strict", "bench", "10", "first", "--", "sleep", "4")
        self.until(lambda: "bench [" in self.listed())
        second = self.quiet("strict", "bench", "1", "second", "--", "true")
        _, err = second.communicate(timeout=20)
        self.assertEqual(second.returncode, 2)  # refused at once, not run beside the first
        self.assertIn(f"bench is already held or awaited by pid {first.pid} (first)", err)
        first.communicate(timeout=30)
        self.assertEqual(first.returncode, 0)

    def test_quiet_inside_a_hold_runs_under_it_or_refuses(self) -> None:
        inner = f"{sys.executable} {QUIET} strict inner 1 inner -- echo inner-ran"
        p = self.quiet("strict", "outer", "1", "outer", "--", "bash", "-c", inner)
        out, err = p.communicate(timeout=30)  # would wait behind its own parent until the deadline
        self.assertEqual((p.returncode, out), (0, "inner-ran\n"), err)
        p = self.quiet("light", "build", "1", "build", "--", "bash", "-c", inner)
        out, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 2)
        self.assertIn("runs inside build's light hold, so a strict request would wait for its own parent", err)

    def test_a_waiting_holds_pill_says_what_it_waits_for(self) -> None:
        stub = self.dir / "bin"
        stub.mkdir()
        (stub / "cmux").write_text(f'#!/bin/sh\necho "$*" >> {self.dir / "cmux.log"}\n')
        (stub / "cmux").chmod(0o755)
        env = {"PATH": f"{stub}:{self.env['PATH']}", "CMUX_WORKSPACE_ID": "ws"}
        first = self.quiet("strict", "a", "10", "first", "--", "sleep", "3", **env)
        self.until(lambda: "a [" in self.listed())
        second = self.quiet("strict", "b", "10", "second", "--", "true", **env)
        second.communicate(timeout=30)
        first.communicate(timeout=30)
        pills = (self.dir / "cmux.log").read_text()
        self.assertIn("set-status quiet-b queued (behind a strict hold): second --icon hourglass", pills)
        self.assertIn("set-status quiet-b strict: second --icon hammer", pills)

    def test_a_measurement_is_stopped_at_its_deadline(self) -> None:
        # a starved shell may outlive its TERM until the KILL 10 s later, so `&&`: a run that was never
        # stopped prints "late" at 30 s, and how soon a stopped one ends depends on the CPU it gets
        p = self.quiet("exclusive", "m", "1", "measure", "--", "bash", "-c", "sleep 30 && echo late")
        out, err = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 124)
        self.assertNotIn("late", out)
        self.assertIn("invalid", err)

    @alone
    def test_bounded_commands_finish_normally_under_cpu_load(self) -> None:
        """Starved commands must not be reported as past their deadline (a timer race seen under load)."""
        env = dict(self.env, QUIET_KILL="1", QUIET_LIGHT_SLOTS="8", QUIET_MINUTE="60")
        burn = "import time\nend = time.time() + 60\nwhile time.time() < end: pass"
        burners = [subprocess.Popen([sys.executable, "-c", burn]) for _ in range(os.cpu_count() or 4)]
        runs: list[subprocess.Popen[bytes]] = []
        try:
            runs = [
                subprocess.Popen([sys.executable, str(QUIET), "light", f"b{i}", "30", "load", "--", "true"], env=env)
                for i in range(8)
            ]
            codes = [p.wait(timeout=120) for p in runs]  # the burners stop at 60 s; other sessions may not
        finally:
            for p in [*burners, *runs]:  # never leave a request polling a deleted hold directory
                if p.poll() is None:
                    p.kill()
            for p in [*burners, *runs]:
                p.wait()
        self.assertEqual(codes, [0] * 8)

    def test_exit_codes_pass_through(self) -> None:
        p = self.quiet("strict", "s", "1", "ok", "--", "bash", "-c", "exit 3")
        p.communicate(timeout=30)
        self.assertEqual(p.returncode, 3)

    def test_the_command_keeps_its_own_options(self) -> None:
        p = self.quiet("light", "o", "1", "options", "--", "sh", "-c", 'echo "[$*]"', "sh", "-h", "--light", "--", "x")
        out, err = p.communicate(timeout=30)
        self.assertEqual((p.returncode, out), (0, "[-h --light -- x]\n"), err)

    def test_long_strict_holds_are_refused(self) -> None:
        p = self.quiet("strict", "s", "31", "too long", "--", "true")
        _, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 2)
        self.assertIn("at most 30", err)

    def test_the_hold_outlives_a_command_that_ignores_term(self) -> None:
        stubborn = f"trap 'echo T-term >> {self.log}; sleep 3; echo T-exit >> {self.log}; exit 1' TERM; while :; do sleep 0.1; done"
        first = self.quiet("exclusive", "t", "1", "stubborn", "--", "bash", "-c", stubborn)
        time.sleep(0.5)
        second = self.quiet("exclusive", "u", "1", "next", "--", "bash", "-c", f"echo U-start >> {self.log}")
        first.communicate(timeout=60)
        second.communicate(timeout=60)
        self.assertEqual(self.events(), ["T-term", "T-exit", "U-start"])

    def test_list_shows_requests_waiting_for_a_hold(self) -> None:
        a = self.quiet("exclusive", "A", "10", "A", "--", "sleep", "4")
        time.sleep(1)
        b = self.quiet("exclusive", "B", "10", "the benchmark", "--", "true")
        time.sleep(1.5)
        listed = self.listed()
        for p in (a, b):
            p.communicate(timeout=60)
        self.assertIn("B.pending [waiting, strict]: the benchmark", listed)

    def test_a_starving_build_goes_before_a_queued_measurement(self) -> None:
        # A outlasts the 4 s it takes L to become starving (QUIET_STARVE_MIN 2 x QUIET_MINUTE 2), plus a poll
        a = self.quiet("exclusive", "A", "10", "A", "--", "bash", "-c", self.mark("A", 9))
        time.sleep(1)
        # the measurement polls faster, so only the fairness rule can let the build in first
        light = self.quiet("light", "L", "10", "L", "--", "bash", "-c", self.mark("L", 1), QUIET_POLL="1")
        time.sleep(1)
        b = self.quiet("exclusive", "B", "10", "B", "--", "bash", "-c", self.mark("B", 1), QUIET_POLL="0.05")
        for p in (a, light, b):
            p.communicate(timeout=60)
        self.assertEqual(self.events(), ["A-start", "A-end", "L-start", "L-end", "B-start", "B-end"])

    def test_a_stopped_run_stops_its_command_and_frees_the_hold(self) -> None:
        p = self.quiet(
            "exclusive", "t", "10", "stopped", "--", "bash", "-c", f"echo $$ > {self.log}; sleep 30 && echo late"
        )
        self.until(lambda: self.log.exists() and self.log.read_text().strip())
        group = int(self.log.read_text())
        p.send_signal(signal.SIGTERM)
        out, _ = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 128 + signal.SIGTERM)
        self.assertNotIn("late", out)
        with self.assertRaises(ProcessLookupError):
            os.killpg(group, 0)
        self.assertEqual(self.listed(), "no holds and no waiting requests\n")

    def test_a_pausing_strict_hold_stops_light_work_and_resumes_it(self) -> None:
        ticks = self.dir / "ticks"
        ticker = f"import time\nwhile True:\n    open({str(ticks)!r}, 'a').write('.')\n    time.sleep(0.05)"
        light = self.quiet("light", "L", "10", "ticker", "--", sys.executable, "-c", ticker)
        self.until(ticks.exists)
        sizes = f"import os, time\na = os.path.getsize({str(ticks)!r})\ntime.sleep(1.5)\nprint(a, os.path.getsize({str(ticks)!r}))"
        # all the CPUs as the strict set: no partition, so on Linux too strict pauses instead
        strict = self.quiet(
            "strict",
            "S",
            "10",
            "measure",
            "--",
            sys.executable,
            "-c",
            sizes,
            QUIET_PAUSE="1",
            QUIET_STRICT_CPUS=f"0-{NCPU - 1}",
        )
        out, err = strict.communicate(timeout=60)
        after = ticks.stat().st_size
        time.sleep(0.5)
        resumed = ticks.stat().st_size
        light.terminate()
        light.communicate(timeout=30)
        self.assertEqual(strict.returncode, 0, err)
        before, during = map(int, out.split())
        self.assertEqual(before, during)
        self.assertGreater(resumed, after)

    @unittest.skipUnless(LINUX and NCPU >= 2, "cores are partitioned on Linux")
    def test_strict_and_light_holds_split_the_cores(self) -> None:
        upper = f"{NCPU // 2}-{NCPU - 1}"
        show = (
            "import os, sys, time\nprint(sorted(os.sched_getaffinity(0)), flush=True)\ntime.sleep(float(sys.argv[1]))"
        )
        strict = self.quiet(
            "strict", "S", "10", "measure", "--", sys.executable, "-c", show, "3", QUIET_STRICT_CPUS=upper
        )
        self.until(lambda: "[strict cpus" in self.listed())
        light = self.quiet("light", "L", "1", "build", "--", sys.executable, "-c", show, "0", QUIET_STRICT_CPUS=upper)
        light_out, _ = light.communicate(timeout=30)
        strict_out, _ = strict.communicate(timeout=30)
        self.assertEqual(json.loads(strict_out), list(range(NCPU // 2, NCPU)))
        self.assertEqual(json.loads(light_out), list(range(NCPU // 2)))

    @alone  # any strict hold beside it would move its fake VMs too
    @unittest.skipUnless(LINUX and NCPU >= 4, "VM threads are pinned on Linux")
    def test_strict_moves_vm_threads_and_restores_them(self) -> None:
        mine = [p for p in Path("/proc").iterdir() if p.name.isdigit() and p.stat().st_uid == os.getuid()]
        if any(b"qemu-system" in (p / "cmdline").read_bytes() for p in mine if (p / "cmdline").exists()):
            self.skipTest("a real qemu VM of this user would be moved")
        vm = (
            "import ctypes, threading, time\n"
            "def vcpu(i):\n"
            "    ctypes.CDLL(None).prctl(15, f'CPU {i}/KVM'.encode())\n"
            "    time.sleep(60)\n"
            "for i in range(3):\n"
            "    threading.Thread(target=vcpu, args=(i,), daemon=True).start()\n"
            "time.sleep(60)"
        )
        fakes = [
            subprocess.Popen([sys.executable, "-c", vm, "qemu-system-x86_64", "-name", f"fakevm-{n}"])
            for n in ("measured", "other")
        ]
        try:
            for f in fakes:
                self.until(lambda f=f: vcpus(f.pid) == 3)
            show = (
                "import json, os, sys\n"
                "print(json.dumps({p: sorted({tuple(sorted(os.sched_getaffinity(int(t)))) for t in os.listdir(f'/proc/{p}/task')}) for p in sys.argv[1:]}))"
            )
            pids = [str(f.pid) for f in fakes]
            strict_cpus = f"{NCPU - 2}-{NCPU - 1}"
            p = self.quiet(
                "strict",
                "S",
                "1",
                "vm",
                "--",
                sys.executable,
                "-c",
                show,
                *pids,
                QUIET_STRICT_CPUS=strict_cpus,
                QUIET_VM="fakevm-measured",
            )
            out, err = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, err)
            during = json.loads(out)
            self.assertEqual(during[pids[0]], [[NCPU - 2, NCPU - 1]])
            self.assertEqual(during[pids[1]], [list(range(NCPU - 2))])
            self.assertIn("has 3 vCPUs on 2 strict CPUs", err)
            original = affinity(0)
            for f in fakes:
                for t in Path(f"/proc/{f.pid}/task").iterdir():
                    self.assertEqual(affinity(int(t.name)), original)
        finally:
            for f in fakes:
                f.kill()
                f.wait()

    def test_a_light_hold_waits_for_memory(self) -> None:
        p = self.quiet("light", "mem", "1", "build", "--", "true", QUIET_MIN_MEM_GB="100000", QUIET_LIGHT_WAIT="0")
        time.sleep(3)
        self.assertIsNone(p.poll(), "a light hold started without the memory it needs")
        p.kill()
        p.communicate()
        done = self.quiet("light", "mem2", "1", "build", "--", "true", QUIET_MIN_MEM_GB="0")
        self.assertEqual(done.wait(timeout=30), 0)


class LightSlots(unittest.TestCase):
    """Light holds are half the CPUs; free memory, not the load, stops new ones."""

    def test_half_the_cpus_or_a_set_count(self) -> None:
        saved = (quiet.NCPU, os.environ.pop("QUIET_LIGHT_SLOTS", None))
        try:
            quiet.NCPU = 16
            self.assertEqual(quiet.light_slots(), 8)
            os.environ["QUIET_LIGHT_SLOTS"] = "3"
            self.assertEqual(quiet.light_slots(), 3)
        finally:
            quiet.NCPU, fixed = saved
            os.environ.pop("QUIET_LIGHT_SLOTS", None)
            if fixed is not None:
                os.environ["QUIET_LIGHT_SLOTS"] = fixed

    def test_free_memory_is_measured(self) -> None:
        self.assertGreater(quiet.free_mem_gb(), 0)


if __name__ == "__main__":
    unittest.main()
