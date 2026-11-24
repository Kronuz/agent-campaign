"""Gate checks queued by verify: ahead of other light evidence, and one identity with job submit.
The hold while a quest iterates is test_evidence_once's."""

from __future__ import annotations

import json
import re
from pathlib import Path

from harness import CampaignCase


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
