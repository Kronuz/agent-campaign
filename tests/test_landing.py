"""Landing a quest: the feature model, increments, the direct path, CI and push, and recovery from an interrupted landing."""

from __future__ import annotations

import json
import time

from harness import CampaignCase


class FeatureModel(CampaignCase):
    """Evidence across merges from main, `land --merge-main`, [ci] criteria and task merges."""

    def criteria(self, pid: str, text: str) -> None:
        brief = self.home / f".campaign/demo/quests/{pid}.md"
        brief.write_text(brief.read_text().replace("None in this test.", text))
        self.git(self.home, "commit", "-qam", f"{pid} criteria")

    def ci_green(self) -> None:
        state = self.state()
        state["ci"] = "github"
        (self.home / ".campaign/demo/state.json").write_text(json.dumps(state))
        self.git(self.home, "commit", "-q", "-am", "require CI")
        gh = self.root / "bin" / "gh"
        gh.write_text('#!/bin/sh\necho \'[{"name": "build", "status": "completed", "conclusion": "success"}]\'\n')
        gh.chmod(0o755)

    def test_evidence_carries_across_merges_from_main_that_touch_none_of_its_files(self) -> None:
        self.env["COLUMNS"] = "400"
        self.commit(self.home, {"other/o.txt": "0\n"})
        b = self.started("b", files="lib/**,docs/**")
        self.criteria("b", "- A1 the probe passes: a probe")
        tested = self.commit(b, {"lib/util.py": "X = 2\n"})
        check = "grep -q 2 lib/util.py"
        probe = self.submit(b, "--quest", "b", "--for", "A1", "--", check)
        self.ok(self.home, "wait", probe)
        for n in range(3):  # docs-only commits and merges from main, interleaved
            self.commit(b, {f"docs/b{n}.md": "notes\n"})
            self.commit(self.home, {"other/o.txt": f"{n + 1}\n"})
            self.git(b, "merge", "-q", "--no-edit", "main")
        out = self.ok(self.home, "status", "b").out
        self.assertIn("A1  met", out)
        self.assertIn(
            f"{probe} (passed at {tested[:12]}; 3 merges from main since, none touching its files; "
            "ignores docs-only docs/b0.md, docs/b1.md, docs/b2.md)",
            out,
        )
        jobs = len(list((self.root / "jobs").glob("2*")))
        self.assertEqual(self.submit(b, "--quest", "b", "--for", "A1", "--", check), probe)
        self.assertEqual(len(list((self.root / "jobs").glob("2*"))), jobs, "a merge from main reran evidence")
        self.commit(self.home, {"lib/new.py": "Y = 1\n"})  # main adds a file in b's set
        self.git(b, "merge", "-q", "--no-edit", "main")
        out = self.ok(self.home, "status", "b").out
        self.assertIn("A1  pending", out)
        self.assertIn(f"{probe} ran other code", out)

    def test_a_measurement_carries_across_main_only_through_its_inputs(self) -> None:
        self.env["COLUMNS"] = "400"
        self.commit(self.home, {"src/core.py": "1\n", "lib/util.py": "X = 1\n"})
        b = self.started("b", files="bench/**,docs/**")
        self.criteria("b", "- A1 the whole tree holds: a timing\n- A2 the library holds: a timing")
        self.commit(b, {"bench/run.sh": "cat lib/util.py src/core.py > /dev/null\n"})
        heavy = ("--quest", "b", "--level", "strict", "--minutes", "1")
        self.fails(b, "job", "submit", *heavy, "--inputs", "none/**", "--", "sh bench/run.sh", match="match no file")
        whole = self.submit(b, *heavy, "--for", "A1", "--", "sh bench/run.sh")
        scoped = self.submit(b, *heavy, "--for", "A2", "--inputs", "lib/**", "--", "sh bench/run.sh")
        self.ok(self.home, "wait", whole, scoped)
        self.commit(b, {"docs/notes.md": "the numbers\n"})
        out = self.ok(self.home, "status", "b").out
        self.assertIn("A1  met", out)
        self.assertIn(f"{whole} (measured at {self.git(b, 'rev-parse', 'HEAD~1')[:12]}; ignores docs-only", out)
        self.commit(self.home, {"src/core.py": "2\n"})  # main's source outside the quest's files
        self.git(b, "merge", "-q", "--no-edit", "main")
        out = self.ok(self.home, "status", "b").out
        self.assertIn(f"{whole} ran other code", out)  # a measurement without inputs: main's code voids it
        self.assertIn("A2  met", out)  # its inputs are unchanged
        self.commit(self.home, {"lib/util.py": "X = 2\n"})  # an input changes on main
        self.git(b, "merge", "-q", "--no-edit", "main")
        out = self.ok(self.home, "status", "b").out
        self.assertIn(f"{scoped} ran other code", out)

    def test_land_merge_main_merges_and_lands_when_the_evidence_carries(self) -> None:
        self.ci_green()
        self.commit(self.home, {"notes/c.txt": "1\n2\n3\n", "notes/d.txt": "1\n2\n3\n"})
        a = self.started("a", files="app.py")
        b = self.started("b", files="lib/**")
        c = self.started("c", files="notes/c.txt")
        d = self.started("d", files="notes/d.txt")
        self.commit(a, {"app.py": "print('a')\n"})
        self.commit(b, {"lib/util.py": "X = 2\n"})
        c_head = self.commit(c, {"notes/c.txt": "1 c\n2\n3\n"})
        self.commit(d, {"notes/d.txt": "1\n2\n3 d\n"})
        for pid in "abcd":
            self.verified(pid)
        self.ok(self.home, "land", "a")
        self.fails(self.home, "land", "b", match="merge main into it (`campaign land b --merge-main`)")
        out = self.ok(self.home, "land", "b", "--merge-main").out
        self.assertIn("merged main into quest/demo/b at ", out)
        (landing,) = self.quest("b")["landed"]
        self.assertEqual(len(self.git(self.home, "rev-list", "--parents", "-n1", landing["tested"]).split()), 3)
        self.commit(self.home, {"notes/c.txt": "1 main\n2\n3\n", "notes/d.txt": "1 main\n2\n3\n"})
        self.fails(self.home, "land", "c", "--merge-main", match="conflicts in notes/c.txt")
        self.assertEqual(self.git(c, "rev-parse", "HEAD"), c_head)  # a conflict leaves the branch as it was
        refused = self.fails(self.home, "land", "d", "--merge-main", match="merged main into quest/demo/d at ")
        self.assertIn("d cannot land yet", refused.text)
        self.assertIn("check unit is not green", refused.text)  # main changed d's own file: evidence voided
        self.assertEqual(self.git(d, "show", "HEAD:notes/d.txt"), "1 main\n2\n3 d")  # the merge is kept
        self.verified("d")
        self.ok(self.home, "land", "d", "--merge-main")

    def test_a_ci_criterion_is_met_by_the_land_gates_ci(self) -> None:
        self.env["COLUMNS"] = "400"
        one = self.started("one", files="app.py")
        self.criteria("one", "- A1 [ci] CI is green at the landed commit")
        self.commit(one, {"app.py": "print(11)\n"})
        self.verified("one")
        self.fails(one, "job", "submit", "--quest", "one", "--for", "A1", "--", "true", match="met by CI")
        out = self.ok(self.home, "status", "one").out
        self.assertIn("A1  pending  [ci] CI is green", out)
        self.assertIn("the campaign runs no CI", out)
        self.fails(self.home, "land", "one", match="criteria without evidence")
        self.ok(self.home, "land", "one", "--no-ci", "this test has no CI")  # the waiver covers the criterion
        self.assertEqual(self.quest("one")["landed"][0]["criteria"]["A1"], "CI waived by --no-ci")
        self.ci_green()
        two = self.started("two", files="app.py")
        self.criteria("two", "- A1 [ci] CI is green at the landed commit")
        head = self.commit(two, {"app.py": "print(22)\n"})
        self.verified("two")
        self.assertIn("A1  met      [ci] CI is green", self.ok(self.home, "status", "two").out)
        self.ok(self.home, "land", "two")
        self.assertEqual(self.quest("two")["landed"][0]["criteria"]["A1"], f"CI green at {head[:12]}")

    def test_a_task_merges_into_its_quest_once_its_own_jobs_pass_at_its_head(self) -> None:
        wt = self.started("fix", files="app.py,lib/**")
        self.ok(self.home, "start", "fix", "--task", "lib", "--files", "lib/**")
        task = self.wt("fix--lib")
        self.commit(task, {"lib/util.py": "X = 2\n"})
        self.fails(self.home, "task", "merge", "fix", "lib", match="no jobs of its own")
        self.assertIn("task lib: working; no jobs of its own", self.ok(wt, "brief").out)
        probe = self.submit(task, "--quest", "fix", "--task", "lib", "--", "test -f lib/ok")
        self.run_cli(self.home, "wait", probe, "--timeout", "60")
        self.fails(self.home, "task", "merge", "fix", "lib", match=f"{probe} failed at")
        head = self.commit(task, {"lib/ok": "\n"})
        rerun = self.ok(self.home, "job", "rerun", probe).out.split(":")[0]
        self.assertEqual(json.loads((self.root / "jobs" / rerun / "spec.json").read_text())["commit"], head)
        self.ok(self.home, "wait", rerun)
        timing = ("--level", "exclusive", "--minutes", "1", "--", "false")
        trial = self.submit(task, "--quest", "fix", "--task", "lib", *timing)
        self.run_cli(self.home, "wait", trial, "--timeout", "60")  # a measurement is the quest's evidence
        self.assertIn("ready to merge: `campaign task merge fix lib`", self.ok(self.home, "brief", "fix").out)
        self.ok(self.home, "task", "merge", "fix", "lib")
        self.git(wt, "merge-base", "--is-ancestor", head, "HEAD")
        self.assertFalse(task.exists())
        self.assertEqual(self.git(self.home, "branch", "--list", "quest/demo/fix--lib"), "")
        self.assertIn(f"lib: merged at {head[:12]}", self.ok(self.home, "status", "fix").out)
        self.assertIn(f"task lib: merged at {head[:12]}", self.ok(self.home, "brief", "fix").out)
        self.fails(self.home, "task", "merge", "fix", "lib", match="already")
        self.ok(self.home, "check")

    def test_a_fails_without_job_never_holds_a_task_merge(self) -> None:
        self.started("fix", files="app.py,lib/**")
        brief = self.home / ".campaign/demo/quests/fix.md"
        brief.write_text(brief.read_text().replace("None in this test.", "- A1 the value is two: a test"))
        self.git(self.home, "commit", "-qam", "criteria")
        self.ok(self.home, "start", "fix", "--task", "lib", "--files", "lib/**")
        task = self.wt("fix--lib")
        before = self.commit(task, {"lib/util.py": "X = 0\n"})
        self.commit(task, {"lib/util.py": "X = 2\n"})
        head = self.git(task, "rev-parse", "HEAD")
        test = ("--", "grep -q 'X = 2' lib/util.py")
        tags = ("--quest", "fix", "--task", "lib", "--for", "A1")
        probe = self.submit(task, *tags, *test)
        old = self.submit(task, *tags, "--fails-without", "--commit", before, *test)
        moved = self.ok(self.home, "job", "rerun", old, "--commit", head).out.split(":")[0]  # it passes there
        self.run_cli(self.home, "wait", probe, old, moved, "--timeout", "60")
        self.ok(self.home, "task", "merge", "fix", "lib")  # the head passed its test; must-fail runs are not its own

    def test_a_task_merge_skips_withdrawn_jobs_and_runs_at_other_code(self) -> None:
        self.started("fix", files="app.py,lib/**")
        self.ok(self.home, "start", "fix", "--task", "lib", "--files", "lib/**")
        task = self.wt("fix--lib")
        mutant = self.commit(task, {"lib/util.py": "X = 0\n"})
        self.git(task, "reset", "--hard", "HEAD~1")
        self.commit(task, {"lib/util.py": "X = 2\n"})
        side = self.submit(
            task, "--quest", "fix", "--task", "lib", "--commit", mutant, "--", "grep -q 'X = 0' lib/util.py"
        )
        slow = self.submit(task, "--quest", "fix", "--task", "lib", "--", "sleep 60")
        self.ok(self.home, "job", "cancel", slow)
        self.run_cli(self.home, "wait", side, slow, "--timeout", "60")  # a cancelled job ends it nonzero
        self.fails(self.home, "task", "merge", "fix", "lib", match="no jobs of its own at its head")
        probe = self.submit(task, "--quest", "fix", "--task", "lib", "--", "grep -q 'X = 2' lib/util.py")
        self.ok(self.home, "wait", probe, "--timeout", "60")
        later = self.submit(
            task, "--quest", "fix", "--task", "lib", "--commit", mutant, "--", "grep -q 'X = 2' lib/util.py"
        )
        self.run_cli(self.home, "wait", later, "--timeout", "60")  # the same test fails at the mutant, later
        self.ok(self.home, "task", "merge", "fix", "lib")

    def test_a_dropped_task_holds_no_files(self) -> None:
        self.started("fix", files="app.py,lib/**")
        self.ok(self.home, "start", "fix", "--task", "a", "--files", "lib/a*")
        self.ok(self.home, "start", "fix", "--task", "b", "--files", "lib/b*")
        self.fails(self.home, "set", "fix", "--task", "b", "files=lib/**", match="overlap tasks a")
        self.ok(self.home, "set", "fix", "--task", "a", "status=dropped", "reason=superseded by b")
        self.ok(self.home, "set", "fix", "--task", "b", "files=lib/**")

    def test_a_task_already_in_its_quest_branch_merges_without_jobs(self) -> None:
        wt = self.started("fix", files="app.py,lib/**")
        self.ok(self.home, "start", "fix", "--task", "lib", "--files", "lib/**")
        task = self.wt("fix--lib")
        head = self.commit(task, {"lib/util.py": "X = 2\n"})
        self.fails(self.home, "task", "merge", "fix", "lib", match="no jobs of its own")
        self.git(wt, "merge", "-q", "--no-ff", "-m", "merge lib by hand", "quest/demo/fix--lib")
        self.ok(self.home, "task", "merge", "fix", "lib")
        self.assertIn(f"lib: merged at {head[:12]}", self.ok(self.home, "status", "fix").out)


class Increments(CampaignCase):
    def test_a_safe_prefix_lands_before_the_quest(self) -> None:
        wt = self.started("big", files="lib/**,app.py", risk="normal", check="unit=true")
        first = self.commit(wt, {"lib/util.py": "X = 5\n"}, "refactor")
        self.commit(wt, {"app.py": "print('feature')\n"}, "feature")
        self.ok(self.home, "verify", "big", "--commit", first)
        self.ok(self.home, "wait", "--quest", "big", "--timeout", "60")
        self.fails(self.home, "land", "big", "--upto", first, match="--reason")
        why = "behavior-preserving refactor"
        brief = self.home / ".campaign/demo/quests/big.md"
        text = brief.read_text()
        start = text.index("## May land early")
        brief.write_text(text[:start] + "## May land early\n\nNone.\n")
        self.git(self.home, "commit", "-qam", "nothing lands early")
        self.fails(self.home, "land", "big", "--upto", first, "--reason", why, match="says None under 'May land early'")
        brief.write_text(text)  # the template's placeholder: internal code behind tests may land early
        self.git(self.home, "commit", "-qam", "the default")
        self.verified("big")
        self.fails(self.home, "land", "big", "--upto", "quest/demo/big", "--reason", why, match="the whole quest")
        self.ok(self.home, "land", "big", "--upto", first, "--reason", why)
        p = self.quest("big")
        self.assertEqual(p["status"], "active")
        self.assertFalse(p["landed"][0]["final"])
        self.assertTrue(self.git(self.home, "merge-base", "--is-ancestor", first, "main") == "")


class DirectPath(CampaignCase):
    def test_a_new_quest_gates_on_the_suite_unless_a_check_names_another_gate(self) -> None:
        def checks(pid: str) -> list[tuple[str, bool]]:
            return [(x["name"], x.get("later", False)) for x in self.quest(pid)["checks"]]

        self.ok(self.home, "new", "plain", "--title", "plain")
        self.assertEqual(self.quest("plain")["checks"][0]["cmd"], "python3 app.py")
        self.ok(self.home, "new", "own", "--title", "own", "--check", "unit=true")
        self.assertEqual(checks("own"), [("unit", False)])
        self.repo("lib", {"lib.py": "X = 1\n"})  # another repository gets its own suite, never the home's
        self.fails(self.home, "new", "lib", "--title", "lib", "--repo", "../lib", match="config suite.lib=CMD")
        self.ok(self.home, "new", "libown", "--title", "l", "--repo", "../lib", "--check", "unit=true")
        self.assertEqual(checks("libown"), [("unit", False)])
        self.ok(self.home, "config", "suite.lib=python3 lib.py")
        self.assertEqual(self.ok(self.home, "config", "suite.lib").out, "python3 lib.py\n")
        self.ok(self.home, "new", "lib", "--title", "lib", "--repo", "../lib")
        self.assertEqual(self.quest("lib")["checks"][0]["cmd"], "python3 lib.py")
        self.ok(self.home, "config", "suite=")
        self.fails(self.home, "new", "none", "--title", "none", match="no gate")
        self.ok(self.home, "config", "suite.lib=")
        self.assertNotIn("suite.lib", self.ok(self.home, "config").out)

    def test_sanitizer_crash_fuzz_and_mutant_checks_are_milestone_checks(self) -> None:
        def checks(pid: str) -> list[tuple[str, bool]]:
            return [(x["name"], x.get("later", False)) for x in self.quest(pid)["checks"]]

        runs = ["--check", "asan=make asan", "--check", "crash=./crash-matrix", "--check", "tsan=make tsan"]
        runs += ["--check", "unit=./fuzz_decoder -runs=100", "--check", "mutants=true", "--check", "ci=ci:UBSan"]
        r = self.ok(self.home, "new", "norm", "--title", "norm", *runs)
        self.assertIn("asan: a milestone check", r.out)
        later = [("asan", True), ("crash", True), ("tsan", True), ("unit", True), ("mutants", True)]
        self.assertEqual(checks("norm"), [*later, ("ci", False)])  # a CI check gates, so no suite
        self.ok(self.home, "new", "high", "--title", "high", "--risk", "high", "--lenses", "durability", *runs)
        gated = [("asan", True), ("crash", False), ("tsan", False), ("unit", True), ("mutants", True), ("ci", False)]
        self.assertEqual(checks("high"), gated)  # high risk keeps the crash model and TSan as gates
        self.ok(self.home, "new", "only", "--title", "only", "--check", "asan=make asan")
        self.assertEqual(checks("only"), [("suite", False), ("asan", True)])

    def test_deferred_checks_never_block_landing_and_report_on_the_landed_code(self) -> None:
        self.ok(
            self.home,
            "new",
            "fast",
            "--title",
            "fast",
            "--risk",
            "low",
            "--files",
            "app.py",
            "--check",
            "unit=true",
            "--later",
            "soak=sleep 1; grep -q fixed app.py",
            "--later",
            "other-platform=echo other",
        )
        self.fails(
            self.home, "new", "x", "--title", "x", "--later", "bench:exclusive:20=true", match="at most 10 minutes"
        )
        self.ok(self.home, "start", "fast")
        self.commit(self.wt("fast"), {"app.py": "print(2)\n"})
        self.verified("fast")  # gates only: the deferred ones are not run yet
        self.assertEqual(len(list((self.root / "jobs").glob("*/spec.json"))), 1)
        self.ok(self.home, "land", "fast")
        record = self.quest("fast")["landed"][0]
        self.assertEqual(len(record["later"]), 2)
        for jid in record["later"]:
            spec = json.loads((self.root / "jobs" / jid / "spec.json").read_text())
            self.assertEqual((spec["priority"], spec["commit"]), (-1, record["commit"]))
        self.run_cli(self.home, "wait", *record["later"], "--timeout", "60")
        r = self.run_cli(self.home, "check")
        self.assertEqual(r.code, 1)
        self.assertIn("deferred check", r.out)
        self.assertIn("fix forward or revert", r.out)
        status = self.ok(self.home, "status").out
        self.assertIn("deferred checks: 1 done, 1 failed", status)
        self.assertNotIn("not in a journal yet", status)  # a landed quest's failures are history
        # removing the red deferred check on the record drops its run too; the other one stays
        self.ok(self.home, "set", "fast", "--later", "other-platform=echo other", "--weaken", "CI runs the soak")
        self.assertEqual(len(self.quest("fast")["landed"][0]["later"]), 1)
        self.ok(self.home, "check")
        self.ok(
            self.home,
            "set",
            "fast",
            "--later",
            "soak=sleep 1; grep -q fixed app.py",
            "--later",
            "other-platform=echo other",
        )
        # the fix lands (here straight on main); rerunning the deferred checks on main clears the error
        self.commit(self.home, {"app.py": "print(2)  # fixed\n"})
        self.ok(self.home, "verify", "fast", "--deferred")
        rerun = self.quest("fast")["landed"][0]["later"]
        self.assertNotEqual(rerun, record["later"])
        self.ok(self.home, "wait", *rerun, "--timeout", "60")
        self.ok(self.home, "check")

    def test_slow_ci_workflows_can_be_named_non_gating(self) -> None:
        self.assertEqual(
            self.ok(self.home, "config").out,
            "ci=\nci_later=\nsuite=python3 app.py\npeers=\noutbox=\ndisk_warn=100\ntested_docs=\npin_check=\nenv=\n",
        )
        self.ok(self.home, "config", "ci=github", "ci_later=macos")
        self.assertEqual(self.ok(self.home, "config", "ci", "ci_later").out, "github\nmacos\n")
        wt = self.started("fix", files="app.py", check="unit=true")
        self.assertEqual(self.ok(wt, "config", "ci").out, "github\n")  # readable from a worktree
        self.commit(wt, {"app.py": "print(2)\n"})
        self.verified("fix")
        gh = self.root / "bin" / "gh"
        runs = '[{"name": "linux", "status": "completed", "conclusion": "success"}, {"name": "macos", "status": "queued", "conclusion": ""}]'
        gh.write_text(f"#!/bin/sh\necho '{runs}'\n")
        gh.chmod(0o755)
        self.ok(self.home, "land", "fix")

    def test_brief_lists_the_ci_a_landing_waits_for(self) -> None:
        self.ok(self.home, "config", "ci=github")
        wt = self.started("fix", files="app.py", check="unit=true")
        self.commit(wt, {"app.py": "print(2)\n"})
        self.verified("fix")
        gh = self.root / "bin" / "gh"
        gh.write_text('#!/bin/sh\necho \'[{"name": "linux", "status": "queued", "conclusion": ""}]\'\n')
        gh.chmod(0o755)
        self.assertIn("CI at", self.ok(wt, "brief").out)
        self.assertIn("running: linux", self.ok(self.home, "status", "fix").out)
        gh.write_text('#!/bin/sh\necho \'[{"name": "linux", "status": "completed", "conclusion": "success"}]\'\n')
        self.assertNotIn("CI at", self.ok(wt, "brief").out)
        self.ok(self.home, "land", "fix")


class Integration(CampaignCase):
    def fake_gh(self, runs: str) -> None:
        gh = self.root / "bin" / "gh"
        gh.write_text(f"#!/bin/sh\necho '{runs}'\n")
        gh.chmod(0o755)

    def test_github_ci_must_be_green_at_the_target(self) -> None:
        state = self.state()
        state["ci"] = "github"
        (self.home / ".campaign/demo/state.json").write_text(json.dumps(state))
        self.git(self.home, "commit", "-q", "-am", "require CI")
        wt = self.started("fix", files="app.py", check="unit=true")
        self.commit(wt, {"app.py": "print(2)\n"})
        self.verified("fix")
        self.fake_gh("[]")
        self.fails(self.home, "land", "fix", match="no CI runs")
        self.fake_gh('[{"name": "build", "status": "completed", "conclusion": "failure"}]')
        self.fails(self.home, "land", "fix", match="failed: build")
        self.fake_gh('[{"name": "build", "status": "in_progress", "conclusion": ""}]')
        self.fails(self.home, "land", "fix", match="running: build")
        self.fake_gh('[{"name": "build", "status": "completed", "conclusion": "success"}]')
        self.ok(self.home, "land", "fix")

    def test_landing_without_ci_needs_a_reason_and_is_recorded(self) -> None:
        state = self.state()
        state["ci"] = "github"
        (self.home / ".campaign/demo/state.json").write_text(json.dumps(state))
        self.git(self.home, "commit", "-q", "-am", "require CI")
        wt = self.started("fix", files="app.py", check="unit=true")
        self.commit(wt, {"app.py": "print(2)\n"})
        self.verified("fix")
        self.fake_gh('[{"name": "build", "status": "completed", "conclusion": "failure"}]')
        self.fails(self.home, "land", "fix", "--no-ci")
        for blank in ("", "  "):
            self.fails(self.home, "land", "fix", "--no-ci", blank, match="--no-ci needs a reason")
            self.fails(self.home, "set", "fix", "risk=low", "--weaken", blank, match="--weaken needs a reason")
        self.ok(self.home, "land", "fix", "--no-ci", "the runner pool is down; the build passed locally as a job")
        self.assertIn("runner pool is down", self.quest("fix")["landed"][0]["ci_skipped"])

    def test_push_sends_the_landing_and_the_state(self) -> None:
        remote = self.root / "remote.git"
        self.git(self.root, "init", "-q", "--bare", "-b", "main", str(remote))
        self.git(self.home, "remote", "add", "origin", str(remote))
        wt = self.started("fix", files="app.py", check="unit=true")
        self.commit(wt, {"app.py": "print(2)\n"})
        self.verified("fix")
        self.ok(self.home, "land", "fix", "--push")
        self.assertEqual(
            self.git(self.root, "--git-dir", str(remote), "rev-parse", "main"), self.git(self.home, "rev-parse", "main")
        )

    def test_push_records_only_a_landing_origin_took(self) -> None:
        """Two clones of one origin: a push origin rejects records nothing, origin's campaign-only
        commits are merged first, and its code commits refuse the landing."""
        remote = self.root / "remote.git"
        self.git(self.root, "init", "-q", "--bare", "-b", "main", str(remote))
        self.git(self.home, "remote", "add", "origin", str(remote))
        self.git(self.home, "push", "-q", "origin", "main")
        other = self.root / "other"
        self.git(self.root, "clone", "-q", str(remote), str(other))
        for pid, path in (("fix", "app.py"), ("fix2", "lib/util.py")):
            self.commit(self.started(pid, files=path, check="unit=true"), {path: "X = 2\n"})
            self.verified(pid)
        # the other clone pushes between this landing's fetch and its push, so origin rejects it
        hooks = self.root / "hooks"
        hooks.mkdir()
        (hooks / "pre-push").write_text(
            '#!/bin/sh\nrm -f "$0"\nunset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE\n'
            f"mkdir -p {other}/.campaign/peer && echo note > {other}/.campaign/peer/note.md\n"
            f"git -C {other} add .campaign && git -C {other} commit -q -m race && git -C {other} push -q origin main\n"
        )
        (hooks / "pre-push").chmod(0o755)
        self.git(self.home, "config", "core.hooksPath", str(hooks))
        main = self.git(self.home, "rev-parse", "main")
        self.fails(self.home, "land", "fix", "--push", match="did not land")
        self.assertEqual(self.git(self.home, "rev-parse", "main"), main)
        self.assertEqual((self.quest("fix")["status"], self.quest("fix")["landed"]), ("active", []))
        self.assertNotIn("landing", self.state())
        self.assertTrue(self.wt("fix").exists())
        # landing again merges origin's campaign-only commit, and origin takes the result
        self.ok(self.home, "land", "fix", "--push")
        self.assertEqual(
            self.git(self.root, "--git-dir", str(remote), "rev-parse", "main"), self.git(self.home, "rev-parse", "main")
        )
        self.assertEqual(self.git(self.home, "show", "main:.campaign/peer/note.md"), "note")
        self.assertEqual(self.quest("fix")["status"], "landed")
        # origin's code the home lacks refuses the landing before anything moves
        self.git(other, "pull", "-q", "--no-rebase", "origin", "main")
        self.commit(other, {"README.md": "changed elsewhere\n"})
        self.git(other, "push", "-q", "origin", "main")
        main = self.git(self.home, "rev-parse", "main")
        self.fails(self.home, "land", "fix2", "--push", match="outside the campaign's files or its state")
        self.assertEqual(self.git(self.home, "rev-parse", "main"), main)
        self.assertEqual(self.quest("fix2")["status"], "active")

    def test_the_lead_lands_a_pin_bump_on_main_after_its_pin_check(self) -> None:
        remote = self.root / "remote.git"
        self.git(self.root, "init", "-q", "--bare", "-b", "main", str(remote))
        self.git(self.home, "remote", "add", "origin", str(remote))
        pins = 'FetchContent_Declare(fmt GIT_TAG v1.2.3)\nset(ZLIB_SHA "0a1b2c3d4e5f")\nproject(app)\n'
        self.commit(self.home, {"CMakeLists.txt": pins}, "build file")
        bumped = self.commit(
            self.home, {"CMakeLists.txt": pins.replace("v1.2.3", "v1.3.0").replace("0a1b2c3", "9f8e7d6")}
        )
        self.ok(self.home, "config", "pin_check=grep -q 'GIT_TAG v1.3.0' CMakeLists.txt")
        out = self.ok(self.home, "pin", "--push").out  # its newest code commit, past the config commit
        self.assertIn(f"pin bump {bumped[:12]}: recorded, pin check", out)
        (record,) = self.state()["pins"]
        self.assertEqual((record["commit"], record["repo"]), (bumped, "."))
        self.assertEqual(
            self.git(self.root, "--git-dir", str(remote), "rev-parse", "main"), self.git(self.home, "rev-parse", "main")
        )
        more = self.commit(self.home, {"CMakeLists.txt": pins.replace("project(app)", "project(app2)")})
        self.fails(self.home, "pin", more, match="CMakeLists.txt changes more than version strings")
        both = self.commit(self.home, {"CMakeLists.txt": pins, "app.py": "print(3)\n"})
        self.fails(self.home, "pin", both, match="app.py changes more than version strings")
        failing = self.commit(self.home, {"CMakeLists.txt": pins.replace("v1.2.3", "v1.4.0")})
        self.fails(self.home, "pin", failing, match="the pin check")
        self.assertEqual(len(self.state()["pins"]), 1)


class Recovery(CampaignCase):
    def test_an_interrupted_landing_is_completed_by_the_next_writer(self) -> None:
        wt = self.started("fix", files="app.py", check="unit=true")
        head = self.commit(wt, {"app.py": "print(2)\n"})
        state = self.state()
        state["landing"] = {
            "id": "fix",
            "target": head,
            "final": True,
            "reason": "",
            "mode": "merge",
            "at": "now",
        }
        (self.home / ".campaign/demo/state.json").write_text(json.dumps(state))
        self.git(self.home, "merge", "-q", "--no-ff", "-m", "crash after this", head)  # main moved, then the lead died
        self.assertIn("interrupted", self.ok(self.home, "check").out)
        r = self.ok(self.home, "new", "next", "--title", "next")
        self.assertIn("completed the interrupted landing", r.err)
        self.assertEqual(self.quest("fix")["status"], "landed")
        self.assertNotIn("landing", self.state())

    def test_an_interrupted_landing_that_never_moved_main_is_cleared(self) -> None:
        wt = self.started("fix", files="app.py", check="unit=true")
        head = self.commit(wt, {"app.py": "print(2)\n"})
        state = self.state()
        state["landing"] = {"id": "fix", "target": head, "final": True, "mode": "ff", "at": "now"}
        (self.home / ".campaign/demo/state.json").write_text(json.dumps(state))
        self.git(self.home, "commit", "-q", "-am", "intent on disk")
        r = self.ok(self.home, "new", "next", "--title", "next")
        self.assertIn("main never moved", r.err)
        self.assertEqual(self.quest("fix")["status"], "active")

    def test_check_finds_deleted_and_unrecorded_branches(self) -> None:
        wt = self.started("gone", files="app.py")
        self.git(self.home, "worktree", "remove", "--force", str(wt))
        self.git(self.home, "branch", "-D", "quest/demo/gone")
        r = self.run_cli(self.home, "check")
        self.assertEqual(r.code, 1)
        self.assertIn("branch quest/demo/gone is gone", r.out)
        self.ok(self.home, "set", "gone", "status=dropped", "reason=superseded")
        wt = self.started("done", files="README.md")
        head = self.commit(wt, {"README.md": "done\n"})
        self.git(self.home, "merge", "-q", "--no-ff", "-m", "by hand", head)
        self.assertIn("not recorded", self.ok(self.home, "check").out)
        self.fails(self.home, "land", "done", "--record", head, match="--reason")
        self.ok(self.home, "land", "done", "--record", head, "--reason", "merged by hand before the tool existed")
        self.assertEqual(self.quest("done")["status"], "landed")
        record = self.quest("done")["landed"][0]
        self.assertEqual(record["unverified"], ["unit: no job"])  # what the landing never checked
        self.assertIn("unverified: unit: no job", self.ok(self.home, "status", "done").out)
        wt = self.started("rec", files="lib/**", risk="normal", check="unit=true")
        head = self.commit(wt, {"lib/util.py": "X = 9\n"})
        self.verified("rec")
        self.review("rec", "correctness", "APPROVE")
        self.ok(self.home, "accept", "rec", "correctness")
        self.review("rec", "correctness", "REJECT")  # after acceptance, which no longer holds
        self.git(self.home, "merge", "-q", "--no-ff", "-m", "by hand", head)
        self.ok(self.home, "land", "rec", "--record", head, "--reason", "merged by hand")
        unverified = self.quest("rec")["landed"][0]["unverified"]
        self.assertEqual(len(unverified), 1, unverified)
        self.assertIn("correctness review changed after it was accepted", unverified[0])

    def test_contracts_with_two_providers_are_reported(self) -> None:
        self.ok(self.home, "new", "a", "--title", "a", "--provides", "api:store")
        self.ok(self.home, "new", "b", "--title", "b", "--provides", "api:store")
        self.ok(self.home, "new", "c", "--title", "c", "--consumes", "api:store", "--files", "README.md")
        out = self.ok(self.home, "check").out
        self.assertIn("contract api:store is provided by a, b", out)

    def test_clean_removes_worktrees_branches_and_slots(self) -> None:
        wt = self.started("fix", files="app.py")
        self.commit(wt, {"app.py": "print(2)\n"})
        self.verified("fix")
        self.ok(self.home, "land", "fix", "--keep")
        self.assertTrue(wt.exists(), "--keep removed the worktree")
        (wt / "probe.txt").write_text("unsaved probe\n")
        self.fails(self.home, "clean", "fix", match="untracked")
        (wt / "probe.txt").unlink()
        self.ok(self.home, "clean", "fix")
        self.assertFalse(wt.exists())
        self.assertNotIn("quest/demo/fix", self.git(self.home, "branch", "--list"))
        self.assertNotIn("slots", self.git(self.home, "worktree", "list"))

    def test_the_last_job_in_a_landed_quests_slot_removes_it(self) -> None:
        wt = self.started("fix", files="app.py")
        self.commit(wt, {"app.py": "print(2)\n"})
        self.verified("fix")
        go = self.root / "go"
        probe = self.submit(wt, "--quest", "fix", "--", f"while [ ! -e {go} ]; do sleep 0.1; done")
        self.wait_until(lambda: self.job(probe)["state"] == "running")
        slot = self.root / "jobs" / "slots" / "demo-fix"
        self.assertEqual(json.loads((self.root / "jobs" / probe / "spec.json").read_text())["slot"], "demo-fix")
        out = self.ok(self.home, "land", "fix").out
        self.assertIn(f"kept its build slot demo-fix: job {probe} uses it", out)
        self.assertTrue(slot.exists())
        go.touch()
        self.ok(self.home, "wait", probe)
        self.wait_until(lambda: not slot.exists() and "slots" not in self.git(self.home, "worktree", "list"))

    def test_landing_cleans_what_is_safe_and_keeps_the_rest(self) -> None:
        wt = self.started("fix", files="app.py")
        self.commit(wt, {"app.py": "print(2)\n"})
        self.verified("fix")
        self.ok(self.home, "land", "fix")
        self.assertFalse(wt.exists(), "the landing left the worktree")
        self.assertNotIn("quest/demo/fix", self.git(self.home, "branch", "--list"))
        self.assertNotIn("slots", self.git(self.home, "worktree", "list"))
        self.assertEqual(self.quest("fix")["worktree"], "")
        dirty = self.started("dirty", files="lib/util.py")
        self.commit(dirty, {"lib/util.py": "X = 2\n"})
        self.verified("dirty")
        (dirty / "notes.txt").write_text("not committed\n")
        out = self.ok(self.home, "land", "dirty").out
        self.assertIn("kept", out)
        self.assertTrue((dirty / "notes.txt").exists(), "the landing deleted an untracked file")
        self.assertEqual(self.quest("dirty")["status"], "landed")
        (dirty / "notes.txt").unlink()
        self.ok(self.home, "clean", "dirty")
        self.assertFalse(dirty.exists())

    def test_landing_keeps_a_worktree_a_job_reads_from(self) -> None:
        wt = self.started("data", files="README.md,.gitignore")
        self.commit(wt, {".gitignore": "corpus.bin\n", "README.md": "app 2\n"})
        (wt / "corpus.bin").write_text("input\n")  # ignored: git calls the worktree clean
        self.ok(wt, "job", "submit", "--sync", "--", f"cat {wt / 'corpus.bin'}")
        self.verified("data")
        out = self.ok(self.home, "land", "data").out
        self.assertIn("reads from it", out)
        self.assertTrue((wt / "corpus.bin").exists(), "the landing deleted an input a job reads")

    def test_check_reports_housekeeping_without_failing(self) -> None:
        wt = self.started("fix", files="app.py")
        self.commit(wt, {"app.py": "print(2)\n"})
        self.verified("fix")
        self.ok(self.home, "land", "fix", "--keep")
        loose = self.submit(self.home, "--sync", "--", "true loose")
        state = self.root / "jobs" / loose / "state.json"
        data = json.loads(state.read_text())
        data["ended"] = time.time() - 30 * 86400
        state.write_text(json.dumps(data))
        out = self.ok(self.home, "check").out
        self.assertIn("still have worktrees or branches: fix", out)
        self.assertIn("would delete 1 job", out)
        self.env.pop("CAMPAIGN_DISK_WARN_GB")  # the campaign's disk_warn decides
        self.env["CCACHE_DIR"] = str(self.root / "ccache")
        (self.root / "ccache").mkdir()
        (self.root / "ccache" / "blob").write_bytes(b"x" * 3_000_000)
        self.ok(self.home, "config", "disk_warn=100000")
        self.assertEqual(self.ok(self.home, "config", "disk_warn").out.strip(), "100000")
        out = self.ok(self.home, "check").out
        self.assertIn("GiB free", out)
        self.assertIn("under 100000 GiB", out)
        self.assertIn("largest: ccache 3 MiB (`ccache --evict-older-than 7d`), the job store's jobs", out)
        self.ok(self.home, "config", "disk_warn=0")
        self.assertNotIn("GiB free", self.ok(self.home, "check").out)
