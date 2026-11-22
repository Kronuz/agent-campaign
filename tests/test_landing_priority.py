"""Evidence a merge from main voids runs at landing priority, for a quest that lacks nothing else."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from harness import CampaignCase

PROBE = "grep -q 2 b/util.py"


class LandingPriority(CampaignCase):
    def criteria(self, pid: str, text: str) -> None:
        brief = self.home / f".campaign/demo/quests/{pid}.md"
        brief.write_text(brief.read_text().replace("None in this test.", text))
        self.git(self.home, "commit", "-qam", f"{pid} criteria")

    def priority(self, jid: str) -> int:
        return int(self.spec(jid)["priority"])

    def spec(self, jid: str) -> dict[str, Any]:
        result: dict[str, Any] = json.loads((self.root / "jobs" / jid / "spec.json").read_text())
        return result

    def ready(self, pid: str = "b", criteria: str = "", **extra: str) -> tuple[Path, str]:
        """Quest PID with A1 proven by a probe and its check green at its head; then main adds a file in
        its set, so a merge from main voids both."""
        wt = self.started(pid, files=f"{pid}/**", check="unit=true", **extra)
        self.criteria(pid, "- A1 the probe passes: a probe" + criteria)
        self.commit(wt, {f"{pid}/util.py": "X = 2\n"})
        probe = self.submit(wt, "--quest", pid, "--for", "A1", "--", f"grep -q 2 {pid}/util.py")
        self.ok(self.home, "wait", probe)
        self.assertEqual(self.priority(probe), 1)
        self.verified(pid)
        self.commit(self.home, {f"{pid}/main.py": "Y = 1\n"})
        return wt, probe

    def rerun(self, *jobs: str) -> list[str]:
        out = self.ok(self.home, "job", "rerun", *jobs).out
        return [line.split()[0].rstrip(":") for line in out.splitlines() if re.match(r"\d{6}-\d{6}-", line)]

    def test_land_merge_main_queues_the_voided_evidence_at_landing_priority(self) -> None:
        self.ready()
        refused = self.fails(self.home, "land", "b", "--merge-main", match="merged main into quest/demo/b at ")
        queued = re.search(r"queued its reruns at landing priority 3: (.+)", refused.text)
        assert queued is not None, refused.text
        ids = queued.group(1).split()
        self.assertEqual(sorted(str(self.spec(j)["cmd"]) for j in ids), sorted([PROBE, "true"]))
        self.assertEqual([self.priority(j) for j in ids], [3, 3])
        self.ok(self.home, "wait", *ids, "--timeout", "60")
        self.ok(self.home, "land", "b")  # the reruns are the evidence: it lands on them

    def test_job_rerun_at_a_lead_merge_runs_at_landing_priority(self) -> None:
        self.env["CAMPAIGN_LIGHT_JOBS"] = "1"
        wt, probe = self.ready()
        self.git(wt, "merge", "-q", "--no-edit", "main")  # the lead's merge, not --merge-main
        sent = self.ok(self.home, "message", "b", "a finding to answer", "--from", "reviewer").text
        msg = re.search(r"msg-\d+", sent)
        assert msg is not None, sent
        go = self.root / "go"  # the blocker holds the only slot, so the reruns stay queued
        blocker = self.submit(self.home, "--", f"while [ ! -e {go} ]; do sleep 0.1; done")
        self.wait_until(lambda: self.job(blocker)["state"] == "running")
        (early,) = self.rerun(probe)  # an open message: not landing yet
        self.assertEqual(self.priority(early), 1)
        self.ok(self.home, "message", "b", "--close", msg.group(0), "--reason", "answered")
        (again,) = self.rerun(probe)
        self.assertEqual(again, early)  # the queued rerun, reused, moves up with the quest
        self.assertEqual(self.priority(early), 3)
        go.touch()
        self.ok(self.home, "wait", blocker, early, "--timeout", "60")

    def test_a_quest_with_other_gaps_keeps_its_priority(self) -> None:
        wt, probe = self.ready(criteria="\n- A2 the docs say so: a probe")  # A2 never met
        refused = self.fails(self.home, "land", "b", "--merge-main", match="merged main into quest/demo/b at ")
        self.assertNotIn("queued its reruns", refused.text)
        (rerun,) = self.rerun(probe)
        self.assertEqual(self.priority(rerun), 1)
        wt, probe = self.ready("c", risk="normal", lenses="correctness")  # its review is pending
        self.git(wt, "merge", "-q", "--no-edit", "main")
        (rerun,) = self.rerun(probe)
        self.assertEqual(self.priority(rerun), 1)

    def test_landing_priority_is_bounded_to_the_quests_own_evidence(self) -> None:
        wt, probe = self.ready()
        timing = self.submit(wt, "--quest", "b", "--level", "strict", "--minutes", "1", "--", "cat b/util.py")
        other = self.started("c", files="src/**", check="unit=true")
        theirs = self.submit(other, "--quest", "c", "--", "true")
        loose = self.submit(self.home, "--", "echo loose")
        self.ok(self.home, "wait", timing, theirs, loose)
        self.git(wt, "merge", "-q", "--no-edit", "main")
        self.commit(other, {"src/c.py": "C = 1\n"})  # each rerun is new code, never a reuse
        self.commit(self.home, {"other/o.txt": "1\n"})
        ran = dict(zip([probe, timing, theirs, loose], self.rerun(probe, timing, theirs, loose)))
        self.assertEqual(len(set(ran.values()) & {probe, timing, theirs, loose}), 0)
        self.assertEqual({j: self.priority(r) for j, r in ran.items()}, {probe: 3, timing: 0, theirs: 1, loose: 0})
        self.ok(self.home, "wait", *ran.values(), "--timeout", "60")
