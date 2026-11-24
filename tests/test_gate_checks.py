"""Gate checks queued by verify: ahead of other light evidence, one identity with job submit, and a
passed slow one held while the quest iterates."""

from __future__ import annotations

import json
import re
from pathlib import Path

from harness import CampaignCase

SLOW = "python3 lib/util.py"


class GateChecks(CampaignCase):
    def spec(self, jid: str) -> dict[str, object]:
        spec: dict[str, object] = json.loads((self.root / "jobs" / jid / "spec.json").read_text())
        return spec

    def verify(self, pid: str, *args: str) -> str:
        """The id of the one check verify queued, and whether it said (reused)."""
        out = self.ok(self.home, "verify", pid, *args).out
        found = re.search(r"^unit: (\S+)", out, re.M)
        assert found, out
        return found.group(1) + (" reused" if "(reused" in out else "")

    def gate_script(self, body: str = "") -> Path:
        script = self.home / "tools" / "gate.py"
        self.commit(self.home, {"tools/gate.py": f"# {body}\n"})
        return script

    def test_a_queued_gate_check_starts_before_the_quest_s_other_light_evidence(self) -> None:
        self.env["CAMPAIGN_LIGHT_JOBS"] = "1"
        wt = self.started("fix", files="app.py")
        self.commit(wt, {"app.py": "print(2)\n"})
        go = self.root / "go"  # the blocker holds the only slot until both are queued
        blocker = self.submit(self.home, "--", f"while [ ! -e {go} ]; do sleep 0.1; done")
        evidence = self.submit(wt, "--quest", "fix", "--", "echo evidence")
        gate = self.verify("fix")
        self.assertEqual([self.spec(j)["priority"] for j in (evidence, gate)], [1, 2])
        go.touch()
        self.ok(self.home, "wait", blocker, evidence, gate, "--timeout", "60")
        self.assertLess(self.job(gate)["started"], self.job(evidence)["started"])

    def test_a_gate_check_carries_the_digest_of_its_script_and_a_changed_script_is_a_new_job(self) -> None:
        script = self.gate_script("one")
        wt = self.started("fix", files="app.py", check=f"unit=python3 {script}")
        self.commit(wt, {"app.py": "print(2)\n"})
        first = self.verify("fix")
        self.assertEqual(list(self.spec(first)["scripts"]), [str(script)])  # type: ignore[call-overload]
        self.ok(self.home, "wait", first, "--timeout", "60")
        self.assertEqual(self.verify("fix"), first + " reused")
        script.write_text("# two\n")
        second = self.verify("fix")
        self.assertNotEqual(second, first + " reused")
        self.assertNotEqual(second.split()[0], first)

    def test_job_submit_of_the_same_command_reuses_the_verify_job(self) -> None:
        script = self.gate_script("one")
        wt = self.started("fix", files="app.py", check=f"unit=python3 {script}")
        self.commit(wt, {"app.py": "print(2)\n"})
        gate = self.verify("fix")
        self.ok(self.home, "wait", gate, "--timeout", "60")
        r = self.ok(wt, "job", "submit", "--quest", "fix", "--", "python3", str(script))
        self.assertEqual(r.out.split(), [gate])  # stdout is only the id
        self.assertIn(f"reused {gate}", r.err)


class SlowOnce(CampaignCase):
    def iterating(self, slow_passes: bool = True) -> tuple[Path, str]:
        """A quest with a quick and a slow gate check, verified at X, then a code change: (worktree, X)."""
        wt = self.started("q", files="app.py,lib/**", check="unit:light:5=python3 app.py")
        self.ok(self.home, "set", "q", "--check", f"slow:light:20={SLOW}")
        x = self.commit(wt, {"lib/util.py": "X = 2\n" if slow_passes else "raise SystemExit(1)\n"})
        self.ok(self.home, "verify", "q")
        self.run_cli(self.home, "wait", "--quest", "q", "--timeout", "90", timeout=120)
        self.commit(wt, {"app.py": "print(2)\n", "lib/util.py": "X = 3\n"})
        return wt, x

    def queued(self, out: str) -> dict[str, str]:
        """Each check's first word after its name in verify's output: a job id, or "held,"."""
        return dict(re.findall(r"^(unit|slow): (\S+)", out, re.M))

    def set_state(self, wt: Path, state: str) -> None:
        rel = ".campaign/demo/journal/q.md"
        self.commit(wt, {rel: re.sub(r"^State: .*$", f"State: {state}", (wt / rel).read_text(), flags=re.M)})

    def test_a_slow_check_that_passed_earlier_is_held_while_iterating(self) -> None:
        _, x = self.iterating()
        out = self.ok(self.home, "verify", "q").out
        found = self.queued(out)
        self.assertRegex(found["unit"], r"^\d{6}-\d{6}-[0-9a-f]{6}$")
        self.assertEqual(found["slow"], "held,")
        line = next(t for t in out.splitlines() if t.startswith("slow:"))
        self.assertEqual(
            line,
            f"slow: held, slow and passed at {x[:12]}: it reruns at the round's end (State ready for review, or land)",
        )
        slow_jobs = [j for j in (self.root / "jobs").glob("*/spec.json") if f'"cmd": "{SLOW}"' in j.read_text()]
        self.assertEqual(len(slow_jobs), 1)  # only the run at X

    def test_at_ready_for_review_the_slow_check_runs(self) -> None:
        wt, _ = self.iterating()
        self.set_state(wt, "ready for review")
        found = self.queued(self.ok(self.home, "verify", "q").out)
        self.assertRegex(found["slow"], r"^\d{6}-\d{6}-[0-9a-f]{6}$")
        self.assertNotEqual(found["slow"], found["unit"])

    def test_force_runs_the_slow_check(self) -> None:
        self.iterating()
        found = self.queued(self.ok(self.home, "verify", "q", "--force").out)
        self.assertRegex(found["slow"], r"^\d{6}-\d{6}-[0-9a-f]{6}$")

    def test_a_slow_check_that_failed_is_not_held(self) -> None:
        self.iterating(slow_passes=False)
        found = self.queued(self.ok(self.home, "verify", "q").out)
        self.assertRegex(found["slow"], r"^\d{6}-\d{6}-[0-9a-f]{6}$")

    def test_a_slow_check_that_never_ran_is_not_held(self) -> None:
        wt = self.started("q", files="app.py,lib/**", check="unit:light:5=python3 app.py")
        self.ok(self.home, "set", "q", "--check", f"slow:light:20={SLOW}")
        self.commit(wt, {"app.py": "print(2)\n"})
        found = self.queued(self.ok(self.home, "verify", "q").out)
        self.assertRegex(found["slow"], r"^\d{6}-\d{6}-[0-9a-f]{6}$")

    def test_a_slow_check_whose_last_run_failed_is_not_held_though_an_earlier_one_passed(self) -> None:
        wt, _ = self.iterating()
        self.commit(wt, {"lib/util.py": "raise SystemExit(1)\n"})
        self.ok(self.home, "verify", "q", "--force")
        self.run_cli(self.home, "wait", "--quest", "q", "--timeout", "90", timeout=120)
        self.commit(wt, {"lib/util.py": "X = 4\n"})
        found = self.queued(self.ok(self.home, "verify", "q").out)
        self.assertRegex(found["slow"], r"^\d{6}-\d{6}-[0-9a-f]{6}$")

    def test_a_check_under_twenty_minutes_is_never_held(self) -> None:
        wt = self.started("q", files="app.py,lib/**", check="unit:light:19=python3 app.py")
        self.commit(wt, {"app.py": "print(2)\n"})
        self.verified("q")
        self.commit(wt, {"app.py": "print(3)\n"})
        found = self.queued(self.ok(self.home, "verify", "q").out)
        self.assertRegex(found["unit"], r"^\d{6}-\d{6}-[0-9a-f]{6}$")
