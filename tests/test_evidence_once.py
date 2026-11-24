"""Evidence once per round: criterion evidence and gate checks held while a quest iterates, fails-without
results that hold until their mutated code changes, and the guard against evidence queued at every commit."""

from __future__ import annotations

import itertools
import json
import re
import time
from pathlib import Path

from harness import CampaignCase

TEST = "python3 app.py | grep -q 2"


class Hold(CampaignCase):
    def iterating(self) -> Path:
        wt = self.started("fix", files="app.py,lib/**")
        brief = self.home / ".campaign/demo/quests/fix.md"
        brief.write_text(brief.read_text().replace("None in this test.", "- A1 prints two: a regression test"))
        self.git(self.home, "commit", "-qam", "criteria")
        self.commit(wt, {"app.py": "print(2)\n"})
        return wt

    def jobs(self) -> int:
        return len(list((self.root / "jobs").glob("*/spec.json")))

    def test_while_iterating_evidence_and_gate_checks_are_held_and_probes_queue(self) -> None:
        wt = self.iterating()
        held = "held, the journal State is 'working': evidence runs once per round."
        refused = self.fails(wt, "job", "submit", "--quest", "fix", "--for", "A1", "--", TEST)
        self.assertIn(f"--for A1 {held}", refused.err)
        self.assertIn("commit State ready for review to run it, or pass --force", refused.err)
        self.assertEqual(refused.out, "")  # no id: nothing was queued
        out = self.ok(self.home, "verify", "fix", "--for", "A1").out
        self.assertIn(f"unit: {held}", out)
        self.assertNotIn("wait for them", out)
        self.assertEqual(self.jobs(), 0)
        probe = self.submit(wt, "--quest", "fix", "--", TEST)  # no --for: a probe queues either way
        self.assertEqual(self.jobs(), 1)
        self.ok(self.home, "wait", probe, "--timeout", "60")
        forced = self.submit(wt, "--quest", "fix", "--for", "A1", "--force", "--", TEST)
        self.assertNotEqual(forced, probe)
        self.assertRegex(self.ok(self.home, "verify", "fix", "--force").out, r"(?m)^unit: \d{6}-\d{6}-[0-9a-f]{6}$")
        self.assertEqual(self.jobs(), 3)

    def test_at_ready_for_review_both_queue_and_a_reused_result_passes_a_hold(self) -> None:
        wt = self.iterating()
        self.ready_for_review(wt, "fix")
        tagged = self.submit(wt, "--quest", "fix", "--for", "A1", "--", TEST)
        gate = re.search(r"(?m)^unit: (\S+)$", self.ok(self.home, "verify", "fix").out)
        assert gate, "verify queued no unit check at ready for review"
        self.ok(self.home, "wait", tagged, gate[1], "--timeout", "60")
        journal = ".campaign/demo/journal/fix.md"
        text = (wt / journal).read_text().replace("State: ready for review", "State: working")
        self.commit(wt, {journal: text})  # iterating again, at the same code
        self.assertEqual(self.submit(wt, "--quest", "fix", "--for", "A1", "--", TEST), tagged)
        self.assertIn(f"unit: {gate[1]} (reused)", self.ok(self.home, "verify", "fix").out)
        self.commit(wt, {"app.py": "print(2)  # new code\n"})
        self.fails(wt, "job", "submit", "--quest", "fix", "--for", "A1", "--", TEST, match="held")
        self.fails(self.home, "job", "rerun", tagged, match="submit it again with `job submit --force`")


class Mutants(CampaignCase):
    def fixed(self) -> Path:
        """A quest whose fix makes app.py print 2, at State ready for review."""
        wt = self.started("fix", files="app.py,lib/**")
        brief = self.home / ".campaign/demo/quests/fix.md"
        brief.write_text(brief.read_text().replace("None in this test.", "- A1 prints two: a regression test"))
        self.git(self.home, "commit", "-qam", "criteria")
        self.commit(wt, {"app.py": "print(2)\nprint('tail')\n"})
        self.ready_for_review(wt, "fix")
        return wt

    def met(self) -> bool:
        return bool(re.search(r"(?m)^ +A1  met ", self.ok(self.home, "status", "fix").out))

    def proven(self, wt: Path, mutant: str) -> str:
        """Runs the plain test at the head beside MUTANT; returns the plain job."""
        plain = self.submit(wt, "--quest", "fix", "--for", "A1", "--", TEST)
        self.ok(self.home, "wait", plain, mutant, "--timeout", "60")
        self.assertTrue(self.met())
        return plain

    def moved_on(self, wt: Path, plain: str, mutant: str, files: dict[str, str]) -> None:
        """Commits FILES and reruns the plain test there: the mutant still holds, and nothing asks for it."""
        self.commit(wt, files)
        rerun = self.ok(self.home, "job", "rerun", plain).out.split(":")[0]
        self.ok(self.home, "wait", rerun, "--timeout", "60")
        self.assertTrue(self.met(), "a fails-without result went stale though its mutated code is unchanged")
        brief = self.ok(wt, "brief").out
        self.assertNotIn(mutant, brief.split("evidence, for")[0], "brief asks about the mutant")
        self.assertIn(f"| A1 | {rerun}; fails without: {mutant} |", brief)
        before = len(list((self.root / "jobs").glob("*/spec.json")))
        out = self.ok(self.home, "job", "rerun", mutant).out
        self.assertIn(f"{mutant} still holds at ", out)
        self.assertIn("the code its mutation reverts is unchanged, so it stays evidence", out)
        self.assertEqual(len(list((self.root / "jobs").glob("*/spec.json"))), before)

    def stale(self, wt: Path, files: dict[str, str]) -> None:
        """Commits FILES, which change the mutated code: the mutant no longer holds, and brief says why."""
        self.commit(wt, files)
        plain = self.submit(wt, "--quest", "fix", "--for", "A1", "--", TEST)
        self.ok(self.home, "wait", plain, "--timeout", "60")
        self.assertFalse(self.met(), "a fails-without result held though the code it mutates changed")
        self.assertIn("the code its mutation reverts changed (app.py): submit a mutant", self.ok(wt, "brief").out)

    def test_a_mutant_commit_holds_until_the_lines_it_reverts_change(self) -> None:
        wt = self.fixed()
        self.commit(wt, {"app.py": "print(1)\nprint('tail')\n"}, "mutant: revert the fix")
        mutant_commit = self.git(wt, "rev-parse", "HEAD")
        self.git(wt, "reset", "-q", "--hard", "HEAD~1")
        self.git(self.home, "update-ref", "refs/campaign/demo/fix-a1", mutant_commit)
        args = ("--quest", "fix", "--for", "A1", "--fails-without", "--commit", "refs/campaign/demo/fix-a1")
        mutant = self.submit(wt, *args, "--", TEST)
        plain = self.proven(wt, mutant)
        self.moved_on(wt, plain, mutant, {"lib/util.py": "X = 5\n"})  # another file
        self.moved_on(wt, plain, mutant, {"app.py": "print(2)\nprint('end')\n"})  # another line of the file
        self.stale(wt, {"app.py": "print(1 + 1)\nprint('end')\n"})  # the line the mutant reverts

    def test_a_mutation_in_the_command_holds_until_a_file_it_names_changes(self) -> None:
        wt = self.fixed()
        sed = "sed -i.bak 's/print(2)/print(1)/' app.py"
        mutant = self.submit(wt, "--quest", "fix", "--for", "A1", "--fails-without", "--", f"{sed} && {TEST}")
        plain = self.proven(wt, mutant)
        self.moved_on(wt, plain, mutant, {"lib/util.py": "X = 5\n"})
        self.stale(wt, {"app.py": "print(2)\nprint('end')\n"})

    def test_a_run_at_main_before_the_fix_holds_at_every_head(self) -> None:
        wt = self.fixed()
        args = ("--quest", "fix", "--for", "A1", "--fails-without", "--commit", "main")
        mutant = self.submit(wt, *args, "--", TEST)
        plain = self.proven(wt, mutant)
        self.moved_on(wt, plain, mutant, {"app.py": "print(1 + 1)\n"})


class Guard(CampaignCase):
    ids = itertools.count()

    def fake_jobs(self, pid: str, n: int, state: str, *, started: bool, tag: str = "", age_h: float = 3) -> None:
        """Writes N ended job records of quest PID into the job store, submitted AGE_H hours ago, each
        waiting an hour in the queue (two when cancelled before it started)."""
        campaign = str(self.home / ".campaign/demo")
        submitted = time.time() - age_h * 3600
        for _ in range(n):
            d = self.root / "jobs" / f"261124-000000-{next(self.ids):06x}"
            d.mkdir(parents=True)
            spec = {"cmd": "true", "quest": pid, "campaign": campaign, "submitted": submitted, "level": "light"}
            st: dict[str, object] = {"state": state, "ended": submitted + 7200}
            if started:
                st["started"] = submitted + 3600
            (d / "spec.json").write_text(json.dumps(spec))
            (d / "state.json").write_text(json.dumps(st))
            if tag:
                tags = [{"campaign": campaign, "quest": pid, "fails_without": False, "for": [tag]}]
                (d / "for.json").write_text(json.dumps({"tags": tags}))

    def test_the_guard_names_a_quest_queuing_evidence_at_every_commit(self) -> None:
        for pid in ("spin", "churn", "fine", "proving"):
            self.started(pid, files=f"{pid}/**")
        self.fake_jobs("spin", 10, "cancelled", started=False)  # cancelled before starting
        self.fake_jobs("churn", 20, "done", started=True)  # ran, and no criterion newly met
        self.fake_jobs("fine", 9, "cancelled", started=False)  # below both thresholds
        self.fake_jobs("fine", 19, "done", started=True)
        self.fake_jobs("fine", 30, "cancelled", started=False, age_h=30)  # outside the window
        self.fake_jobs("proving", 25, "done", started=True, tag="A1")  # A1 first proven in the window
        self.fake_jobs("proving", 1, "done", started=True, tag="A2", age_h=40)
        spin = (
            "spin queues evidence at every commit: in the last 24 h, 10 of its 10 jobs were cancelled before "
            "starting and 0 ran with no criterion newly met, 20 h waiting in the queue."
        )
        churn = (
            "churn queues evidence at every commit: in the last 24 h, 0 of its 20 jobs were cancelled before "
            "starting and 20 ran with no criterion newly met, 20 h waiting in the queue."
        )
        for args in (("status",), ("check",)):
            out = self.run_cli(self.home, *args).out
            self.assertIn(spin, out)
            self.assertIn(churn, out)
            self.assertNotIn("fine queues", out)
            self.assertNotIn("proving queues", out)
        self.assertIn(f"  - {spin}", self.ok(self.wt("spin"), "brief").out)
        self.assertNotIn("queues evidence", self.ok(self.wt("fine"), "brief").out)


class ExaminedJobs(CampaignCase):
    def test_a_row_cites_only_its_proof_and_land_names_what_keeps_it_unmet(self) -> None:
        """lead-driver-measured's A1 row: a passed job beside a failed one and one at other code."""
        wt = self.started("fix", files="app.py")
        journal = wt / ".campaign/demo/journal/fix.md"
        self.assertIn("A row cites only what proves its criterion", journal.read_text())
        self.assertNotIn("name every job you examined", journal.read_text())
        brief = self.home / ".campaign/demo/quests/fix.md"
        brief.write_text(brief.read_text().replace("None in this test.", "- A1 prints two: a probe"))
        self.git(self.home, "commit", "-qam", "criteria")
        self.commit(wt, {"app.py": "print(2)\n"})
        old = self.submit(wt, "--quest", "fix", "--", "python3 app.py")
        self.ok(self.home, "wait", old, "--timeout", "60")
        self.commit(wt, {"app.py": "print(2)  # moved on\n"})
        passed = self.submit(wt, "--quest", "fix", "--", TEST)
        failed = self.submit(wt, "--quest", "fix", "--", "exit 3")
        self.run_cli(self.home, "wait", passed, failed, "--timeout", "60")
        rel = ".campaign/demo/journal/fix.md"
        text = journal.read_text().replace("State: working", "State: ready for review")
        row = f"| A1 | {passed}, {failed}, {old} |"
        self.commit(wt, {rel: text.replace("| <A1> | <from `campaign brief`> |", row)})
        self.verified("fix")
        why = f"A1: {failed} failed, {old} ran other code"
        self.assertIn(why, self.fails(self.home, "land", "fix", match="criteria without evidence").text)
        self.assertIn(why, self.ok(self.home, "brief").out)
        examined = f"| A1 | {passed} |\n\nExamined, not proof: {failed} (failed), {old} (older code).\n"
        self.commit(wt, {rel: text.replace("| <A1> | <from `campaign brief`> |", examined)})
        self.verified("fix")
        self.ok(self.home, "land", "fix")
