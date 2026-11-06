"""quiet: measurements end at their deadline, holds outlive the stop, and builds are not starved."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

QUIET = Path(__file__).resolve().parent.parent / "scripts" / "quiet.sh"


class Quiet(unittest.TestCase):
    def setUp(self) -> None:
        base = os.environ.get("CAMPAIGN_TEST_TMP")
        if base:
            Path(base).mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=base)
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
            ["bash", str(QUIET), *args],
            env={**self.env, **env},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def mark(self, name: str, seconds: float) -> str:
        return f"echo {name}-start >> {self.log}; sleep {seconds}; echo {name}-end >> {self.log}"

    def events(self) -> list[str]:
        return self.log.read_text().split()

    def test_a_measurement_is_stopped_at_its_deadline(self) -> None:
        # a starved shell may outlive its TERM until the KILL 10 s later, so `&&`: a run that was never
        # stopped prints "late" at 30 s, and how soon a stopped one ends depends on the CPU it gets
        p = self.quiet("exclusive", "m", "1", "measure", "--", "bash", "-c", "sleep 30 && echo late")
        out, err = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 124)
        self.assertNotIn("late", out)
        self.assertIn("invalid", err)

    def test_bounded_commands_finish_normally_under_cpu_load(self) -> None:
        """Starved commands must not be reported as past their deadline (a timer race seen under load)."""
        env = dict(self.env, QUIET_KILL="1", QUIET_LIGHT_SLOTS="8", QUIET_MINUTE="60")
        burn = "import time\nend = time.time() + 60\nwhile time.time() < end: pass"
        burners = [subprocess.Popen([sys.executable, "-c", burn]) for _ in range(os.cpu_count() or 4)]
        runs: list[subprocess.Popen[bytes]] = []
        try:
            runs = [
                subprocess.Popen(["bash", str(QUIET), "light", f"b{i}", "30", "load", "--", "true"], env=env)
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
        listed = subprocess.run(["bash", str(QUIET), "list"], env=self.env, text=True, capture_output=True).stdout
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


if __name__ == "__main__":
    unittest.main()
