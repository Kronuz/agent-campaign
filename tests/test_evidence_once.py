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


ID = r"\d{6}-\d{6}-[0-9a-f]{6}"


class Hold(CampaignCase):
    """The hold's lifecycle table (references/review.md, "When evidence runs"), with the hold on: a test
    per row, each asking every entry point that queues evidence (`evidence_hold`)."""

    n = itertools.count()

    def setUp(self) -> None:
        super().setUp()
        self.env.pop("CAMPAIGN_EVIDENCE_HOLD")
        self.env.pop("CAMPAIGN", None)

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
        gate = re.search(rf"(?m)^unit: ({ID})", self.ok(self.home, "verify", "fix").out)
        assert gate, "verify queued no unit check"
        return tagged, gate[1]

    def round(self) -> tuple[Path, str, str, str]:
        """Quest fix after a round at State ready for review (its tagged job and gate check passed), then
        new code: the worktree, the tagged job, the gate check's job, and the new head."""
        wt = self.iterating()
        set_state(self, wt, "fix", "ready for review")
        tagged, unit = self.queued(wt)
        self.ok(self.home, "wait", tagged, unit, "--timeout", "60")
        return wt, tagged, unit, self.commit(wt, {"app.py": "print(2)  # round 2\n"})[:12]

    def entries(self, wt: Path, tagged: str, unit: str, where: Path | None = None) -> dict[str, tuple[int, str]]:
        """Every entry point that queues evidence, asked once at the head: (exit code, what it said)
        for the contributor's submit --for, a rerun of its tagged job (and with --commit), a rerun of a
        gate check, and the lead's verify. WHERE runs the reruns (default: the home)."""
        lead = where or self.home
        asked = {
            "submit --for": self.run_cli(
                wt, "job", "submit", "--quest", "fix", "--for", "A1", "--", f"{TEST} # {next(self.n)}"
            ),
            "rerun": self.run_cli(lead, "job", "rerun", tagged),
            "rerun --commit": self.run_cli(lead, "job", "rerun", tagged, "--commit", "quest/demo/fix"),
            "rerun of a gate check": self.run_cli(lead, "job", "rerun", unit),
            "verify": self.run_cli(self.home, "verify", "fix"),
        }
        return {k: (r.code, r.text) for k, r in asked.items()}

    def all_held(self, said: dict[str, tuple[int, str]], why: str, before: int) -> None:
        for entry, (code, text) in said.items():
            if entry == "verify":
                self.assertIn(f"unit: held: {why}. Evidence runs once per round", text, entry)
            else:
                self.assertNotEqual(code, 0, f"{entry} queued: {text}")
                self.assertIn(f"held: {why}. Evidence runs once per round", text, entry)
        self.assertEqual(self.jobs(), before, "a held entry point queued a job")

    def all_queued(self, said: dict[str, tuple[int, str]]) -> None:
        for entry, (code, text) in said.items():
            self.assertEqual(code, 0, f"{entry}: {text}")
            self.assertNotIn("held:", text, entry)
            self.assertRegex(text, rf"(?m)^(?:unit: )?{ID}", entry)

    def test_row_1_ready_for_review_or_waiting_on_jobs_set_at_the_head_queues(self) -> None:
        wt, tagged, unit, head = self.round()
        set_state(self, wt, "fix", f"ready for review at {head}")
        said = self.entries(wt, tagged, unit)
        self.all_queued(said)
        set_state(self, wt, "fix", f"waiting on jobs {said['submit --for'][1].split()[0]}")
        self.all_queued(self.entries(wt, tagged, unit))

    def test_row_1_the_hold_reads_the_state_as_the_to_do_does(self) -> None:
        """One reader for a State that ends a round (todo.ready): its case and wording are free."""
        wt, tagged, unit, head = self.round()
        set_state(self, wt, "fix", f"Ready for review at {head}")
        self.all_queued(self.entries(wt, tagged, unit))

    def test_row_1_code_and_state_in_one_commit_queue(self) -> None:
        wt = self.iterating()
        rel = ".campaign/demo/journal/fix.md"
        text = re.sub(r"^State:.*$", "State: ready for review", (wt / rel).read_text(), count=1, flags=re.M)
        self.commit(wt, {"app.py": "print(2)  # with its State\n", rel: text})
        self.queued(wt)

    def test_row_2_a_state_set_before_the_head_holds_until_set_again(self) -> None:
        wt, tagged, unit, head = self.round()
        why = f"State 'ready for review' was set before the head {head}: set State at {head}"
        self.all_held(self.entries(wt, tagged, unit), why, self.jobs())
        set_state(self, wt, "fix", f"ready for review at {head}")
        self.all_queued(self.entries(wt, tagged, unit))

    def test_row_2_a_note_line_starting_with_state_sets_nothing(self) -> None:
        """Only the journal's own State line, the first, sets the State (correctness C13)."""
        wt, tagged, unit, head = self.round()
        rel = ".campaign/demo/journal/fix.md"
        note = (wt / rel).read_text() + "\n## Session 2\n\nState: noted while fixing; the line above is unchanged.\n"
        self.commit(wt, {rel: note})
        why = f"State 'ready for review' was set before the head {head}: set State at {head}"
        self.all_held(self.entries(wt, tagged, unit), why, self.jobs())

    def test_row_3_any_other_state_holds(self) -> None:
        wt, tagged, unit, _ = self.round()
        set_state(self, wt, "fix", "fixing D4")
        self.all_held(self.entries(wt, tagged, unit), "the journal State is 'fixing D4'", self.jobs())

    def test_row_3_a_bold_or_missing_state_line_holds(self) -> None:
        wt = self.iterating()
        rel = ".campaign/demo/journal/fix.md"
        self.commit(wt, {rel: (wt / rel).read_text().replace("State: working", "**State:** working")})
        refused = self.fails(wt, "job", "submit", "--quest", "fix", "--for", "A1", "--", TEST)
        self.assertIn(
            "--for A1 held: the journal State is '(no State line)'. Evidence runs once per round", refused.err
        )
        self.assertEqual(refused.out, "")  # no id: nothing was queued
        self.assertNotIn("command:", refused.err)  # nothing reads as queued before the hold
        self.assertEqual(self.jobs(), 0)

    def test_row_3_a_reran_job_from_outside_any_campaign_is_held(self) -> None:
        """The rerun's tables come from the job's tags, not the directory it runs in (correctness C20)."""
        wt, tagged, unit, _ = self.round()
        set_state(self, wt, "fix", "working")
        said = self.entries(wt, tagged, unit, where=self.root)
        self.all_held(
            {k: v for k, v in said.items() if k.startswith("rerun")}, "the journal State is 'working'", self.jobs()
        )

    def test_reused_or_still_valid_first_at_any_state(self) -> None:
        """What an earlier job answers queues nothing, so no State holds it (design D4a): after a
        docs-only commit at State working, each entry point hands back the round's job."""
        wt, tagged, unit, _ = self.round()
        self.git(wt, "reset", "-q", "--hard", "HEAD~1")  # back to the round's code
        set_state(self, wt, "fix", "working")
        self.commit(wt, {"README.md": "app, documented\n"})
        before = self.jobs()
        self.assertEqual(self.submit(wt, "--quest", "fix", "--for", "A1", "--", TEST), tagged)
        for job in (tagged, unit):
            for extra in ((), ("--commit", "quest/demo/fix")):
                out = self.ok(self.home, "job", "rerun", job, *extra).out
                self.assertIn(f"{job} (reused): {job} at ", out)
        self.assertIn(f"unit: {unit} (reused", self.ok(self.home, "verify", "fix").out)
        self.assertEqual(self.jobs(), before)

    def test_reused_means_what_submit_reuses(self) -> None:
        """A twin queued after another job is a different request to submit, so the hold holds it
        (correctness C19)."""
        wt = self.iterating()
        set_state(self, wt, "fix", "ready for review")
        blocker = self.submit(wt, "--quest", "fix", "--", "sleep 60")
        behind = self.submit(wt, "--quest", "fix", "--for", "A1", "--after", blocker, "--", TEST)
        set_state(self, wt, "fix", "working")  # the journal only: the code, and the job's key, are the same
        before = self.jobs()
        refused = self.fails(wt, "job", "submit", "--quest", "fix", "--for", "A1", "--", TEST)
        self.assertIn("held: the journal State is 'working'", refused.err)
        self.ok(self.home, "job", "cancel", blocker)
        self.ok(self.home, "job", "cancel", behind)
        self.assertEqual(self.jobs(), before)

    def test_row_4_a_tasked_quest_s_verify_queues_once_every_task_merged_or_dropped(self) -> None:
        """Correctness C12: a dropped task never merges, and counts as done."""
        wt, _, unit, _ = self.round()
        self.ok(self.home, "start", "fix", "--task", "lib", "--files", "lib/**")
        self.ok(self.home, "start", "fix", "--task", "app", "--files", "app.py")
        task = self.wt("fix--lib")
        self.commit(task, {"lib/util.py": "X = 2\n"})
        self.assertIn("unit: held: ", self.ok(self.home, "verify", "fix").out)
        own = self.submit(task, "--quest", "fix", "--task", "lib", "--", "grep -q 'X = 2' lib/util.py")  # row 7
        self.ok(self.home, "wait", own, "--timeout", "60")
        self.ok(self.home, "task", "merge", "fix", "lib")
        self.assertIn("unit: held: ", self.ok(self.home, "verify", "fix").out)  # task app is still open
        self.ok(self.home, "set", "fix", "--task", "app", "status=dropped", "reason=folded into lib")
        self.assertRegex(self.ok(self.home, "job", "rerun", unit).out, rf"(?m)^{ID}: {unit} at ")
        self.assertRegex(self.ok(self.home, "verify", "fix").out, rf"(?m)^unit: {ID}")
        self.fails(wt, "job", "submit", "--quest", "fix", "--for", "A1", "--", f"{TEST} # 4", match="held: ")

    def test_row_5_a_quest_in_review_holds_as_an_active_one(self) -> None:
        """Design D6: review fix rounds' evidence is held by the journal State too."""
        wt, tagged, unit, head = self.round()
        self.ok(self.home, "set", "fix", "status=review")
        why = f"State 'ready for review' was set before the head {head}: set State at {head}"
        self.all_held(self.entries(wt, tagged, unit), why, self.jobs())
        set_state(self, wt, "fix", f"ready for review at {head}")
        self.all_queued(self.entries(wt, tagged, unit))

    def test_row_6_land_queues_its_own_landing_checks_with_no_hint_for_them(self) -> None:
        """Design D4b: after its merge from main, `land` queues the voided evidence past the hold, and
        names those jobs once, as queued, not as commands to run."""
        wt, tagged, unit, head = self.round()
        set_state(self, wt, "fix", f"ready for review at {head}")
        tagged, unit = self.queued(wt)
        self.ok(self.home, "wait", tagged, unit, "--timeout", "60")
        self.commit(self.home, {"lib/main.py": "Y = 1\n"})  # main changes a file in the quest's set
        refused = self.fails(self.home, "land", "fix", "--merge-main", match="merged main into quest/demo/fix at ")
        queued = re.search(rf"queued at landing priority 3: reruns ({ID}); landing checks ({ID})", refused.text)
        assert queued is not None, refused.text
        self.assertNotIn("campaign job rerun", refused.text)
        self.assertNotIn("campaign verify", refused.text)
        verify = self.ok(self.home, "verify", "fix").out  # what land queued answers the lead's verify
        self.assertIn(f"unit: {queued[2]} (reused)", verify)
        self.fails(wt, "job", "submit", "--quest", "fix", "--for", "A1", "--", f"{TEST} # 6", match="set State at ")
        self.ok(self.home, "wait", *queued.groups(), "--timeout", "60")
        self.ok(self.home, "land", "fix")

    def test_row_7_a_probe_or_measurement_without_for_queues(self) -> None:
        wt = self.iterating()
        probe = self.submit(wt, "--quest", "fix", "--", TEST)
        self.assertEqual(self.jobs(), 1)
        self.ok(self.home, "wait", probe, "--timeout", "60")
        self.assertRegex(self.ok(self.home, "job", "rerun", probe, "--commit", "quest/demo/fix").out, rf"^{ID}")

    def test_row_8_force_queues(self) -> None:
        wt = self.iterating()
        self.submit(wt, "--quest", "fix", "--for", "A1", "--force", "--", TEST)
        self.assertRegex(self.ok(self.home, "verify", "fix", "--force").out, rf"(?m)^unit: {ID}$")
        self.assertEqual(self.jobs(), 2)

    def test_row_9_rerun_of_a_mutant_that_still_holds_queues_nothing_at_any_state_or_commit(self) -> None:
        """Correctness C10: with --commit too, a mutant that still holds is still valid."""
        wt = self.iterating()
        set_state(self, wt, "fix", "ready for review")
        args = ("--quest", "fix", "--for", "A1", "--fails-without", "--commit", "main")
        mutant = self.submit(wt, *args, "--", TEST)
        plain = self.submit(wt, "--quest", "fix", "--for", "A1", "--", TEST)
        self.ok(self.home, "wait", plain, mutant, "--timeout", "60")
        for state in ("fixing", "ready for review at {head}"):
            head = self.commit(wt, {"lib/util.py": f"X = {next(self.n)}\n"})[:12]
            set_state(self, wt, "fix", state.format(head=head))
            before = self.jobs()
            for extra in ((), ("--commit", "main"), ("--commit", "quest/demo/fix")):
                out = self.ok(self.home, "job", "rerun", mutant, *extra).out
                self.assertIn(f"{mutant} still holds at ", out)
            self.assertEqual(self.jobs(), before)


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
        # where inputs are traced (Linux) the rerun reuses the pass, and brief says why it still counts
        self.assertRegex(brief, rf"\| A1 \| {rerun}(?: \([^)]*\))?; fails without: {mutant} \|")
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
LONG_CHECK = "".join(f"# the regression test, line {n}: app.py prints two\n" for n in range(12)) + CHECK
MUTATE = "from pathlib import Path\np = Path('app.py')\np.write_text(p.read_text().replace('print(2)', 'print(1)'))\n"


class MutantFootprint(CampaignCase):
    """What a fails-without result answers for: the correctness review's C3, C4 and C5."""

    def fixed(self) -> Path:
        """Quest fix: app.py prints 2 (the fix), check.py tests it, at State ready for review."""
        wt = self.started("fix", files="app.py,check.py,check2.py,test_check.py,mutate.py,lib/**")
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

    def at_head(self, wt: Path, cmd: str) -> bool:
        """Proves the head's own test, CMD, at the head; says whether A1 is still met."""
        plain = self.submit(wt, "--quest", "fix", "--for", "A1", "--", cmd)
        self.ok(self.home, "wait", plain, "--timeout", "90", timeout=120)
        return self.met()

    def mutant_ref(self, commit: str) -> tuple[str, ...]:
        self.git(self.home, "update-ref", "refs/campaign/demo/fix-a1", commit)
        return ("--quest", "fix", "--for", "A1", "--fails-without", "--commit", "refs/campaign/demo/fix-a1")

    def test_a_mutant_commit_goes_stale_when_its_test_is_renamed(self) -> None:
        """Correctness C14: the test it ran, moved and weakened in one commit, is a change to it."""
        wt = self.fixed()
        self.commit(wt, {"check.py": LONG_CHECK})
        self.commit(wt, {"app.py": "print(1)\nprint('tail')\n"}, "mutant: revert the fix")
        mutant_commit = self.git(wt, "rev-parse", "HEAD")
        self.git(wt, "reset", "-q", "--hard", "HEAD~1")
        self.proven(wt, self.submit(wt, *self.mutant_ref(mutant_commit), "--", "python3 check.py"))
        self.git(wt, "mv", "check.py", "test_check.py")
        self.commit(wt, {"test_check.py": LONG_CHECK.replace("assert out.split()[0] == '2', out", "print(out)")})
        self.assertIn("R0", self.git(wt, "diff", "--name-status", "-M", "HEAD~1", "HEAD"))  # git reads a rename
        self.assertFalse(self.at_head(wt, "python3 test_check.py"))

    def on_the_base(self, change: dict[str, str], cmd: str = "python3 check.py") -> bool:
        """The quest's new test applied to its base, proven; then CHANGE at the head: whether A1, by the
        head's test CMD, is still met. It holds while the quest's files are as it ran them (correctness
        C15, by C4's rule: any change to them since stales it)."""
        wt = self.fixed()
        self.git(wt, "checkout", "-q", "--detach", "HEAD~2")  # the base: before the fix and its State
        self.commit(wt, {"check.py": CHECK}, "mutant: the test without the fix")
        mutant_commit = self.git(wt, "rev-parse", "HEAD")
        self.git(wt, "checkout", "-q", "quest/demo/fix")
        self.proven(wt, self.submit(wt, *self.mutant_ref(mutant_commit), "--", "python3 check.py"))
        time.sleep(1.1)  # the head it ran at is read by commit time, to the second
        if "check.py" not in cmd:
            self.git(wt, "rm", "-q", "check.py")
        self.commit(wt, change)
        return self.at_head(wt, cmd)

    def test_a_mutant_commit_of_the_new_test_on_the_base_holds(self) -> None:
        self.assertTrue(self.on_the_base({"README.md": "docs\n"}))

    def test_a_mutant_commit_of_the_new_test_on_the_base_moves_with_the_quest_s_files(self) -> None:
        self.assertFalse(self.on_the_base({"lib/util.py": "X = 5\n"}))

    def test_a_mutant_commit_of_the_new_test_on_the_base_moves_when_its_test_is_replaced(self) -> None:
        self.assertFalse(self.on_the_base({"check2.py": "print('checks nothing')\n"}, "python3 check2.py"))

    def test_a_run_at_main_with_the_test_from_a_ref_moves_with_the_quest_s_files(self) -> None:
        """Correctness C16: main's code with the quest's test copied from a ref of its head."""
        wt = self.fixed()
        head = self.git(wt, "rev-parse", "HEAD")
        cmd = 'cp "$JOB_REF_NEW/check.py" . && python3 check.py'
        args = ("--quest", "fix", "--for", "A1", "--fails-without", "--commit", "main", "--ref", f"NEW={head}")
        self.proven(wt, self.submit(wt, *args, "--", cmd))
        self.assertTrue(self.moved(wt, {"README.md": "docs\n"}))
        self.assertFalse(self.moved(wt, {"check.py": CHECK.replace("assert out.split()[0] == '2', out", "print(out)")}))


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
                st: dict[str, object] = {"state": state}
                if state != "running":
                    st["ended"] = submitted + 7200
                if started:
                    st["started"] = submitted + 3600
                (d / "heartbeat").write_text("")  # a running one's: fresh, so no reaper ends it
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
            "and newly met no criterion, 0 criteria newly met; 24 job-hours waiting. Lead: tell its contributor"
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

    def test_mutants_cut_from_one_head_with_no_plain_job_are_one_round(self) -> None:
        """Design D5(a): keyed by their parent, not each its own commit."""
        self.started("fix", files="app.py")
        head = self.git(self.home, "rev-parse", "main")
        self.fake_jobs("fix", self.heads(5, head), 1, "done", started=True, mutant=True)
        self.assertNotIn("fix queued evidence", self.run_cli(self.home, "status").out)

    def test_a_round_still_in_flight_is_undecided(self) -> None:
        """Design D5(b): a round with a job queued or running is neither barren nor the round before."""
        self.started("fix", files="app.py")
        self.fake_jobs("fix", self.heads(1), 1, "done", started=True, age_h=8)  # A1 met
        for k in range(3):  # three rounds that re-prove A1: barren
            self.fake_jobs("fix", self.heads(1), 1, "done", started=True, age_h=7 - k)
        self.fake_jobs("fix", self.heads(1), 1, "running", started=True, age_h=0.5)
        self.assertNotIn("fix queued evidence", self.run_cli(self.home, "status").out)
        self.fake_jobs("fix", self.heads(1), 1, "done", started=True, age_h=0.2)  # a fourth barren round
        self.assertIn("fix queued evidence at 6 heads", self.run_cli(self.home, "status").out)


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
