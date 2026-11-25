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


def set_state(case: CampaignCase, wt: Path, pid: str, state: str) -> None:
    """Commits State STATE in the quest's journal, unless it already says so."""
    path = wt / f".campaign/demo/journal/{pid}.md"
    text = re.sub(r"^State:.*$", f"State: {state}", path.read_text(), count=1, flags=re.M)
    if text != path.read_text():
        case.commit(wt, {f".campaign/demo/journal/{pid}.md": text})


class Hold(CampaignCase):
    """The hold's lifecycle table (SKILL.md, "Run a quest"), a test per row, with the hold on."""

    def setUp(self) -> None:
        super().setUp()
        self.env.pop("CAMPAIGN_EVIDENCE_HOLD")

    def iterating(self) -> Path:
        wt = self.started("fix", files="app.py,lib/**")
        set_state(self, wt, "fix", "working")
        brief = self.home / ".campaign/demo/quests/fix.md"
        brief.write_text(brief.read_text().replace("None in this test.", "- A1 prints two: a regression test"))
        self.git(self.home, "commit", "-qam", "criteria")
        self.commit(wt, {"app.py": "print(2)\n"})
        return wt

    def jobs(self) -> int:
        return len(list((self.root / "jobs").glob("*/spec.json")))

    def queued(self, wt: Path, cmd: str = TEST) -> tuple[str, str]:
        """A contributor's `job submit --for` and the lead's `verify`, both queued: their job ids."""
        tagged = self.submit(wt, "--quest", "fix", "--for", "A1", "--", cmd)
        gate = re.search(r"(?m)^unit: (\d{6}-\d{6}-[0-9a-f]{6})", self.ok(self.home, "verify", "fix").out)
        assert gate, "verify queued no unit check"
        return tagged, gate[1]

    def held(self, wt: Path, why: str, rerun: str = "", *, verify: bool = True) -> None:
        """Submit --for, the lead's verify (unless its check is reused) and (given a tagged job) its
        rerun, all held for WHY."""
        refused = self.fails(wt, "job", "submit", "--quest", "fix", "--for", "A1", "--", f"{TEST} # held")
        self.assertIn(f"--for A1 held: {why}. Evidence runs once per round", refused.err)
        self.assertEqual(refused.out, "")  # no id: nothing was queued
        self.assertNotIn("command:", refused.err)  # nothing reads as queued before the hold
        out = self.ok(self.home, "verify", "fix").out
        self.assertIn(f"unit: held: {why}." if verify else "(reused)", out)
        if rerun:
            self.fails(self.home, "job", "rerun", rerun, match=f"held: {why}")

    def test_row_1_ready_for_review_or_waiting_on_jobs_set_at_the_head_queues(self) -> None:
        wt = self.iterating()
        set_state(self, wt, "fix", "ready for review")
        tagged, gate = self.queued(wt)
        self.ok(self.home, "wait", tagged, gate, "--timeout", "60")
        self.ok(self.home, "job", "rerun", tagged)
        set_state(self, wt, "fix", f"waiting on jobs {tagged}")
        again, _ = self.queued(wt, f"{TEST} && true")
        self.assertNotEqual(again, tagged)

    def test_row_2_a_state_set_before_the_head_holds_until_set_again(self) -> None:
        wt = self.iterating()
        set_state(self, wt, "fix", "ready for review")
        tagged, gate = self.queued(wt)
        self.ok(self.home, "wait", tagged, gate, "--timeout", "60")
        head = self.commit(wt, {"app.py": "print(2)  # new code\n"})[:12]
        self.held(wt, f"State 'ready for review' was set before the head {head}: set State at {head}", tagged)
        set_state(self, wt, "fix", f"ready for review at {head}")
        self.queued(wt)

    def test_row_3_any_other_state_holds(self) -> None:
        wt = self.iterating()
        self.held(wt, "the journal State is 'working'")
        self.assertEqual(self.jobs(), 0)
        set_state(self, wt, "fix", "ready for review")
        tagged, _ = self.queued(wt)
        set_state(self, wt, "fix", "fixing D1")
        self.held(wt, "the journal State is 'fixing D1'", tagged, verify=False)  # its unit check is reused
        self.assertEqual(self.submit(wt, "--quest", "fix", "--for", "A1", "--", TEST), tagged)  # reused: not held

    def test_row_3_a_bold_or_missing_state_line_holds(self) -> None:
        wt = self.iterating()
        rel = ".campaign/demo/journal/fix.md"
        self.commit(wt, {rel: (wt / rel).read_text().replace("State: working", "**State:** working")})
        self.held(wt, "the journal State is '(no State line)'")

    def test_row_8_rerun_of_a_mutant_that_still_holds_queues_nothing_at_any_state(self) -> None:
        wt = self.iterating()
        set_state(self, wt, "fix", "ready for review")
        args = ("--quest", "fix", "--for", "A1", "--fails-without", "--commit", "main")
        mutant = self.submit(wt, *args, "--", TEST)
        plain = self.submit(wt, "--quest", "fix", "--for", "A1", "--", TEST)
        self.ok(self.home, "wait", plain, mutant, "--timeout", "60")
        set_state(self, wt, "fix", "fixing")
        self.commit(wt, {"lib/util.py": "X = 5\n"})
        before = self.jobs()
        out = self.ok(self.home, "job", "rerun", mutant).out
        self.assertIn(f"{mutant} still holds at ", out)
        self.assertEqual(self.jobs(), before)

    def test_row_4_a_tasked_quest_s_verify_queues_once_every_task_merged(self) -> None:
        self.iterating()
        self.ok(self.home, "start", "fix", "--task", "lib", "--files", "lib/**")
        task = self.wt("fix--lib")
        self.commit(task, {"lib/util.py": "X = 2\n"})
        self.assertIn("unit: held: the journal State is 'working'", self.ok(self.home, "verify", "fix").out)
        own = self.submit(task, "--quest", "fix", "--task", "lib", "--", "grep -q 'X = 2' lib/util.py")
        self.ok(self.home, "wait", own, "--timeout", "60")
        self.ok(self.home, "task", "merge", "fix", "lib")
        self.assertRegex(self.ok(self.home, "verify", "fix").out, r"(?m)^unit: \d{6}-\d{6}-[0-9a-f]{6}")

    def test_row_5_a_quest_in_review_or_landing_never_holds(self) -> None:
        wt = self.iterating()
        self.ok(self.home, "set", "fix", "status=review")
        self.queued(wt)

    def test_row_6_a_probe_or_measurement_without_for_queues(self) -> None:
        wt = self.iterating()
        probe = self.submit(wt, "--quest", "fix", "--", TEST)
        self.assertEqual(self.jobs(), 1)
        self.ok(self.home, "wait", probe, "--timeout", "60")

    def test_row_7_force_queues(self) -> None:
        wt = self.iterating()
        self.submit(wt, "--quest", "fix", "--for", "A1", "--force", "--", TEST)
        self.assertRegex(self.ok(self.home, "verify", "fix", "--force").out, r"(?m)^unit: \d{6}-\d{6}-[0-9a-f]{6}$")
        self.assertEqual(self.jobs(), 2)


class Mutants(CampaignCase):
    def fixed(self) -> Path:
        """A quest whose fix makes app.py print 2, at State ready for review."""
        wt = self.started("fix", files="app.py,lib/**")
        brief = self.home / ".campaign/demo/quests/fix.md"
        brief.write_text(brief.read_text().replace("None in this test.", "- A1 prints two: a regression test"))
        self.git(self.home, "commit", "-qam", "criteria")
        self.commit(wt, {"app.py": "print(2)\nprint('tail')\n"})
        set_state(self, wt, "fix", "ready for review")
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
        rerun = (
            self.ok(self.home, "job", "rerun", plain).out.split()[0].rstrip(":")
        )  # new, or reused where inputs are traced
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
        self.assertIn(
            "the code its mutation reverts, or its test, changed (app.py): submit a mutant", self.ok(wt, "brief").out
        )

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


CHECK = (
    "import subprocess, sys\nout = subprocess.run([sys.executable, 'app.py'], capture_output=True, text=True).stdout\n"
    "assert out.split()[0] == '2', out\n"
)
MUTATE = "from pathlib import Path\np = Path('app.py')\np.write_text(p.read_text().replace('print(2)', 'print(1)'))\n"


class MutantFootprint(CampaignCase):
    """What a fails-without result answers for: the correctness review's C3, C4 and C5."""

    def fixed(self) -> Path:
        """Quest fix: app.py prints 2 (the fix), check.py tests it, at State ready for review."""
        wt = self.started("fix", files="app.py,check.py,mutate.py,lib/**")
        brief = self.home / ".campaign/demo/quests/fix.md"
        brief.write_text(brief.read_text().replace("None in this test.", "- A1 prints two: a regression test"))
        self.git(self.home, "commit", "-qam", "criteria")
        self.commit(wt, {"app.py": "print(2)\nprint('tail')\n", "check.py": CHECK, "mutate.py": MUTATE})
        set_state(self, wt, "fix", "ready for review")
        return wt

    def met(self) -> bool:
        return bool(re.search(r"(?m)^ +A1  met ", self.ok(self.home, "status", "fix").out))

    def proven(self, wt: Path, mutant: str) -> None:
        plain = self.submit(wt, "--quest", "fix", "--for", "A1", "--", "python3 check.py")
        self.ok(self.home, "wait", plain, mutant, "--timeout", "90", timeout=120)
        self.assertTrue(self.met())

    def moved(self, wt: Path, files: dict[str, str]) -> bool:
        """Commits FILES, proves the plain test at the new head, and says whether A1 is still met."""
        self.commit(wt, files)
        plain = self.submit(wt, "--quest", "fix", "--for", "A1", "--", "python3 check.py")
        self.ok(self.home, "wait", plain, "--timeout", "90", timeout=120)
        return self.met()

    def test_a_mutation_made_by_a_script_moves_with_the_quest_s_files(self) -> None:
        wt = self.fixed()
        cmd = "python3 mutate.py && python3 check.py"  # names mutate.py and check.py, not app.py
        self.proven(wt, self.submit(wt, "--quest", "fix", "--for", "A1", "--fails-without", "--", cmd))
        self.assertFalse(self.moved(wt, {"app.py": "print(1 + 1)\nprint('tail')\n"}))

    def test_an_increment_leaves_the_quest_s_own_commit_the_quest_s_code(self) -> None:
        wt = self.fixed()
        sed = "sed -i.bak 's/print(2)/print(1)/' app.py"
        mutant = self.submit(wt, "--quest", "fix", "--for", "A1", "--fails-without", "--", f"{sed} && python3 check.py")
        self.proven(wt, mutant)
        at = self.git(wt, "rev-parse", "HEAD")
        self.commit(wt, {"lib/util.py": "X = 2\n"}, "more work")
        self.ok(self.home, "verify", "fix", "--commit", at)
        self.ok(self.home, "wait", "--quest", "fix", "--timeout", "90", timeout=120)
        self.ok(self.home, "land", "fix", "--upto", at, "--reason", "the fix alone is safe")
        self.assertFalse(self.moved(wt, {"app.py": "print(1 + 1)\nprint('tail')\n"}))

    def test_a_mutant_commit_moves_with_the_test_it_ran(self) -> None:
        wt = self.fixed()
        self.commit(wt, {"app.py": "print(1)\nprint('tail')\n"}, "mutant: revert the fix")
        mutant_commit = self.git(wt, "rev-parse", "HEAD")
        self.git(wt, "reset", "-q", "--hard", "HEAD~1")
        self.git(self.home, "update-ref", "refs/campaign/demo/fix-a1", mutant_commit)
        args = ("--quest", "fix", "--for", "A1", "--fails-without", "--commit", "refs/campaign/demo/fix-a1")
        self.proven(wt, self.submit(wt, *args, "--", "python3 check.py"))
        self.assertTrue(self.moved(wt, {"lib/util.py": "X = 5\n"}))  # another file: it holds
        self.assertFalse(self.moved(wt, {"check.py": "print('checked')\n"}))  # the test it ran changed

    def test_a_mutant_commit_of_the_new_test_on_the_base_holds(self) -> None:
        """The quest's new test applied to its base: the head adds the same test, so nothing moved."""
        wt = self.fixed()
        self.git(wt, "checkout", "-q", "--detach", "HEAD~2")  # the base: before the fix and its State
        self.commit(wt, {"check.py": CHECK}, "mutant: the test without the fix")
        mutant_commit = self.git(wt, "rev-parse", "HEAD")
        self.git(wt, "checkout", "-q", "quest/demo/fix")
        self.git(self.home, "update-ref", "refs/campaign/demo/fix-a1", mutant_commit)
        args = ("--quest", "fix", "--for", "A1", "--fails-without", "--commit", "refs/campaign/demo/fix-a1")
        self.proven(wt, self.submit(wt, *args, "--", "python3 check.py"))
        self.assertTrue(self.moved(wt, {"lib/util.py": "X = 5\n"}))


class Guard(CampaignCase):
    ids = itertools.count()

    def fake_jobs(
        self,
        pid: str,
        heads: list[str],
        n: int,
        state: str,
        *,
        started: bool,
        tag: str = "A1",
        age_h: float = 3,
        mutant: bool = False,
    ) -> None:
        """Writes N ended job records of quest PID at each of HEADS into the job store, submitted AGE_H
        hours ago, each waiting an hour in the queue (two when cancelled before it started)."""
        campaign = str(self.home / ".campaign/demo")
        for head in heads:
            submitted = time.time() - age_h * 3600
            for _ in range(n):
                d = self.root / "jobs" / f"261124-000000-{next(self.ids):06x}"
                d.mkdir(parents=True)
                spec = {
                    "cmd": "true",
                    "quest": pid,
                    "campaign": campaign,
                    "submitted": submitted,
                    "level": "light",
                    "commit": head,
                    "repo": str(self.home),
                    **({"fails_without": 2} if mutant else {}),
                }
                st: dict[str, object] = {"state": state, "ended": submitted + 7200}
                if started:
                    st["started"] = submitted + 3600
                (d / "spec.json").write_text(json.dumps(spec))
                (d / "state.json").write_text(json.dumps(st))
                if tag:
                    tags = [{"campaign": campaign, "quest": pid, "fails_without": mutant, "for": [tag]}]
                    (d / "for.json").write_text(json.dumps({"tags": tags}))

    def heads(self, n: int, parent: str = "") -> list[str]:
        """N commits of the home's repository: children of PARENT (mutants cut from it), else fake heads."""
        if not parent:
            return [f"{next(self.ids):040x}" for _ in range(n)]
        tree = self.git(self.home, "rev-parse", f"{parent}^{{tree}}")
        return [self.git(self.home, "commit-tree", tree, "-p", parent, "-m", f"mutant {i}") for i in range(n)]

    def test_the_guard_counts_rounds_and_names_the_lead_s_step(self) -> None:
        for pid in ("spin", "churn", "fine", "proving"):
            self.started(pid, files=f"{pid}/**")
        self.fake_jobs("spin", self.heads(3), 4, "cancelled", started=False)  # 3 rounds never started
        self.fake_jobs("churn", self.heads(4), 5, "failed", started=True)  # 4 rounds ran, none met anything
        self.fake_jobs("fine", self.heads(2), 9, "cancelled", started=False)  # below both thresholds
        self.fake_jobs("fine", self.heads(3), 9, "failed", started=True)
        self.fake_jobs("fine", self.heads(5), 9, "cancelled", started=False, age_h=30)  # outside the window
        self.fake_jobs("proving", self.heads(3), 5, "failed", started=True)
        self.fake_jobs("proving", self.heads(1), 5, "done", started=True)  # A1 met at this head, not the last
        self.fake_jobs("proving", self.heads(5), 5, "cancelled", started=False, tag="")  # probes: never counted
        spin = (
            "spin queued evidence at 3 heads in the last 24 h: 3 rounds were cancelled before any job started, 0 ran "
            "and newly met no criterion, 0 newly met; 24 job-hours waiting. Lead: tell its contributor"
        )
        churn = (
            "churn queued evidence at 4 heads in the last 24 h: 0 rounds were cancelled before any job started, 4 ran"
        )
        for args in (("status",), ("check",)):
            out = self.run_cli(self.home, *args).out
            self.assertIn(spin, out)
            self.assertIn(churn, out)
            self.assertNotIn("fine queued", out)
            self.assertNotIn("proving queued", out)
        self.assertIn(f"  - {spin}", self.ok(self.wt("spin"), "brief").out)
        self.assertNotIn("queued evidence at", self.ok(self.wt("fine"), "brief").out)

    def test_one_round_superseded_once_is_not_flagged(self) -> None:
        """A round at one head, with mutants cut from it, cancelled before it started by a fix."""
        self.started("fix", files="app.py")
        head = self.git(self.home, "rev-parse", "main")
        self.fake_jobs("fix", [head], 4, "cancelled", started=False, tag="A1")
        self.fake_jobs("fix", self.heads(3, head), 1, "cancelled", started=False, tag="A2", mutant=True)
        self.fake_jobs("fix", self.heads(1), 4, "done", started=True, tag="A1", age_h=1)  # the fixed round
        self.assertNotIn("fix queued evidence", self.run_cli(self.home, "status").out)
        self.fake_jobs("fix", self.heads(2), 3, "cancelled", started=False)  # 2 more rounds never ran: 3 in all
        self.assertIn("fix queued evidence at 4 heads", self.run_cli(self.home, "status").out)


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
        old = self.submit(wt, "--quest", "fix", "--", "python3 app.py | grep -q .")  # not the unit check
        self.ok(self.home, "wait", old, "--timeout", "60")
        self.commit(wt, {"app.py": "print(2)  # moved on\n"})
        passed = self.submit(wt, "--quest", "fix", "--", TEST)
        failed = self.submit(wt, "--quest", "fix", "--", "exit 3")
        self.run_cli(self.home, "wait", passed, failed, "--timeout", "60")
        rel = ".campaign/demo/journal/fix.md"
        text = re.sub(r"^State:.*$", "State: ready for review", journal.read_text(), count=1, flags=re.M)
        row = f"| A1 | {passed}, {failed}, {old} |"
        self.commit(wt, {rel: text.replace("| <A1> | <from `campaign brief`> |", row)})
        self.verified("fix")
        why = f"A1: {failed} failed, {old} ran other code"
        self.assertIn(why, self.fails(self.home, "land", "fix", match="criteria without evidence").text)
        self.assertIn(why, self.ok(self.home, "brief").out)
        examined = f"| A1 | {passed} |\n\nExamined, not proof: {failed} (failed), {old} (older code).\n"
        self.commit(wt, {rel: text.replace("| <A1> | <from `campaign brief`> |", examined)})
        self.ok(self.home, "verify", "fix")
        self.run_cli(self.home, "wait", "--quest", "fix", "--timeout", "90")  # exits 1: the examined job failed
        self.ok(self.home, "land", "fix")


class CitedInFlight(CampaignCase):
    def test_land_waits_for_a_cited_job_still_queued_or_running(self) -> None:
        """review-sweeps' A3: tags proved it while its row cited a shard still running."""
        wt = self.started("fix", files="app.py")
        brief = self.home / ".campaign/demo/quests/fix.md"
        brief.write_text(brief.read_text().replace("None in this test.", "- A1 prints two: a probe"))
        self.git(self.home, "commit", "-qam", "criteria")
        self.commit(wt, {"app.py": "print(2)\n"})
        passed = self.submit(wt, "--quest", "fix", "--for", "A1", "--", TEST)
        self.ok(self.home, "wait", passed, "--timeout", "60")
        shard = self.submit(wt, "--", "sleep 120")  # no quest: verify's wait leaves it running
        journal = wt / ".campaign/demo/journal/fix.md"
        text = re.sub(r"^State:.*$", "State: ready for review", journal.read_text(), count=1, flags=re.M)
        row = f"| A1 | {passed}; shard {shard}: pending |"
        self.commit(wt, {".campaign/demo/journal/fix.md": text.replace("| <A1> | <from `campaign brief`> |", row)})
        self.verified("fix")
        state = self.job(shard)["state"]
        self.assertIn(state, ("queued", "running"))
        why = f"A1: {shard} {state}: land waits for its result"
        self.assertIn(why, self.fails(self.home, "land", "fix", match="criteria without evidence").text)
        self.assertIn(why, self.ok(self.home, "brief").out)
        self.ok(self.home, "job", "cancel", shard)
        deadline = time.time() + 60
        while self.job(shard)["state"] in ("queued", "running") and time.time() < deadline:
            time.sleep(0.5)  # a running job stops when its runner sees the cancel
        self.ok(self.home, "land", "fix")


class RoundOrder(CampaignCase):
    def test_every_text_gives_one_round_order(self) -> None:
        """Commit the code, State ready for review, submit the round, then State waiting on jobs."""
        order = re.compile(
            r"commit (?:it|the code), commit State .?ready for review.?, submit (?:its evidence|the round), "
            r"then (?:set|commit) State .?waiting on jobs"
        )
        skill = Path(__file__).resolve().parent.parent
        for rel in ("SKILL.md", "agents/campaign-contributor.md", "references/prompts.md", "references/review.md"):
            text = " ".join((skill / rel).read_text().split())
            self.assertRegex(text, order, rel)
