"""Criteria, the evidence jobs give them, and the gates that read it."""

from __future__ import annotations

import json
from pathlib import Path

from harness import CampaignCase, campaign_module


class Evidence(CampaignCase):
    def test_status_starts_no_git_process_per_job(self) -> None:
        wt = self.started("fix", files="app.py")

        def git_processes() -> int:
            trace = self.root / "git-trace.log"
            self.ok(self.home, "status")  # warms anything status computes once
            self.env["GIT_TRACE"] = str(trace)
            self.ok(self.home, "status")
            del self.env["GIT_TRACE"]
            count = trace.read_text().count("trace: built-in: git")
            trace.unlink()
            return count

        before = git_processes()
        for n in range(4):  # each job at its own commit, so each has its own code tree
            self.commit(wt, {"app.py": f"print({n})\n"})
            self.submit(wt, "--quest", "fix", "--sync", "--", "true", str(n))
        self.assertEqual(git_processes(), before)

    def test_code_trees_leave_out_only_the_campaigns_files(self) -> None:
        campaign = campaign_module()
        repo = self.repo("shapes", {"a.py": "1\n", ".campaignx/f": "y\n"})
        commits = [self.git(repo, "rev-parse", "HEAD")]
        for files in ({".campaign/demo/state.json": "{}\n"}, {"docs/guide.md": "g\n"}, {".campaign/x/PLAN.md": "p\n"}):
            commits.append(self.commit(repo, files))
        self.git(repo, "rm", "-q", "-r", "docs")
        commits.append(self.commit(repo, {"a.py": "1\n"}))
        trees = [campaign.code_tree(repo, c) for c in commits]
        self.assertEqual(trees[0], self.git(repo, "rev-parse", f"{commits[0]}^{{tree}}"))  # nothing to leave out
        self.assertEqual((trees[1], trees[3], trees[4]), (trees[0], trees[2], trees[0]))  # campaign files never count
        self.assertNotEqual(trees[0], trees[2])  # docs/guide.md does, and .campaignx is code

    def test_evidence_outlives_documentation_only_commits_its_command_does_not_name(self) -> None:
        wt = self.started("fix", files="app.py,README.md,docs/**")
        tested = self.commit(wt, {"app.py": "print(2)\n"})
        self.verified("fix")
        self.commit(wt, {"README.md": "app, documented\n"})
        self.commit(wt, {"docs/perf.md": "the numbers\n"})
        jobs = len(list((self.root / "jobs").glob("2*")))
        out = self.ok(self.home, "verify", "fix").out
        self.assertIn(f"(reused: passed at {tested[:12]}; ignores docs-only README.md, docs/perf.md)", out)
        self.assertEqual(len(list((self.root / "jobs").glob("2*"))), jobs, "verify reran a check for docs")
        brief = self.ok(self.home, "brief", "fix").out
        self.assertIn(f"passed at {tested[:12]}; ignores docs-only README.md, docs/perf.md", brief)
        self.assertNotIn("check unit is not green", brief)
        self.ok(self.home, "land", "fix")
        (landing,) = self.quest("fix")["landed"]
        self.assertEqual(landing["evidence"][0]["docs"], ["README.md", "docs/perf.md"])
        named = self.started("named", files="app.py,README.md", check="unit=python3 app.py && cat README.md")
        self.commit(named, {"app.py": "print(3)\n"})
        self.verified("named")
        self.commit(named, {"README.md": "what the check reads\n"})
        self.assertIn("check unit is not green", self.ok(self.home, "brief", "named").out)  # its command names it
        self.commit(named, {"app.py": "print(4)\n", "docs/later.md": "x\n"})
        self.assertIn("check unit is not green", self.ok(self.home, "brief", "named").out)

    def test_docs_that_tests_execute_void_the_evidence(self) -> None:
        self.ok(self.home, "config", "tested_docs=docs/site/**")
        wt = self.started("fix", files="app.py,docs/**")
        self.commit(wt, {"app.py": "print(2)\n"})
        self.verified("fix")
        self.commit(wt, {"docs/notes.md": "prose\n"})
        self.assertNotIn("check unit is not green", self.ok(self.home, "brief", "fix").out)  # plain docs keep it
        self.commit(wt, {"docs/site/api.md": "a request the e2e runs\n"})
        self.assertIn("check unit is not green", self.ok(self.home, "brief", "fix").out)
        self.assertIn("tested_docs=docs/site/**", self.ok(self.home, "config").out)

    def test_brief_warns_of_a_criterion_row_outside_the_evidence_section(self) -> None:
        wt = self.started("fix", files="app.py")
        brief = self.home / ".campaign/demo/quests/fix.md"
        brief.write_text(brief.read_text().replace("None in this test.", "- A1 [manual] reads well\n- A5 [manual] ok"))
        self.git(self.home, "commit", "-qam", "criteria")
        journal = "## Evidence\n\n| A1 | the lead read it |\n\n## Result\n\n| Row | Value |\n|---|---|\n| A5 | ok |\n"
        self.commit(wt, {".campaign/demo/journal/fix.md": journal})
        out = self.ok(wt, "brief").out
        self.assertIn("  - fix's journal has a row for A5 under `## Result`: landing reads only `## Evidence`\n", out)
        self.assertNotIn("row for A1", out)

    def test_each_criterion_needs_the_kind_of_evidence_it_names(self) -> None:
        wt = self.started("fix", files="app.py,docs/**")
        brief = self.home / ".campaign/demo/quests/fix.md"
        criteria = (
            "- A1 prints two: the unit check\n- A2 [artifact] the results note: docs/perf/fix.md\n"
            "- A3 [decision] the owner approves the name\n- A4 [manual] the output reads well: the lead reads it"
        )
        brief.write_text(brief.read_text().replace("None in this test.", criteria))
        self.git(self.home, "commit", "-qam", "criteria")
        self.commit(wt, {"app.py": "print(2)\n", "docs/perf/fix.md": "2x\n"})
        jid = self.ok(self.home, "verify", "fix").out.split()[1]
        self.ok(self.home, "wait", jid)
        journal = ".campaign/demo/journal/fix.md"
        rows = "| A1 | tested it, all green |\n| A2 | `docs/perf/gone.md` |\n| A3 | D1 |\n| A4 | |\n"
        self.commit(wt, {journal: f"## Evidence\n\n{rows}"})
        self.fails(self.home, "land", "fix", match="criteria without evidence at")
        out = self.ok(self.home, "status", "fix").out
        self.assertIn("names no job", out)
        self.assertIn("docs/perf/gone.md is not in", out)
        self.assertIn("names no decision in PLAN.md", out)
        rows = f"| A1 | {jid} |\n| A2 | `docs/perf/fix.md` |\n| A3 | D1 |\n| A4 | the lead read the output |\n"
        self.commit(wt, {journal: f"## Evidence\n\n{rows}"})
        self.fails(self.home, "land", "fix", match=": A3 (")
        plan = self.home / ".campaign/demo/PLAN.md"
        plan.write_text(plan.read_text() + "| D1 | 2026-11-08 | name it fix | owner | short |\n")
        self.git(self.home, "commit", "-qam", "decision D1")
        out = self.ok(self.home, "status", "fix").out
        self.assertIn("A4  met      [manual] the output reads well", out)
        self.ok(self.home, "land", "fix")

    def test_a_criterion_naming_one_gate_check_or_a_bare_path_needs_no_journal_row(self) -> None:
        checks = ("--check", "unit=python3 app.py", "--check", "lint=true")
        self.ok(self.home, "new", "fix", "--title", "fix", "--risk", "low", "--files", "app.py,docs/**", *checks)
        self.ok(self.home, "start", "fix")
        wt = self.wt("fix")
        brief = self.home / ".campaign/demo/quests/fix.md"
        criteria = (
            "- A1 prints two: the `unit`\n  check\n- A2 [artifact] the results note\n"
            "- A3 both pass: the `unit` check and the `lint` check\n- A4 holds: a regression test"
        )
        brief.write_text(brief.read_text().replace("None in this test.", criteria))
        self.git(self.home, "commit", "-qam", "criteria")
        self.commit(wt, {"app.py": "print(2)\n", "docs/perf/fix.md": "2x\n"})
        self.verified("fix")
        self.commit(wt, {".campaign/demo/journal/fix.md": "## Evidence\n\n| A2 | docs/perf/fix.md. |\n"})
        out = self.ok(self.home, "status", "fix").out
        self.assertIn("A1  met", out)  # the check its text names passed at the head
        self.assertIn("A2  met", out)  # a bare path that exists
        self.assertIn("a criterion names one gate check of its quest", out)
        refused = self.fails(self.home, "land", "fix", match="criteria without evidence at")
        self.assertIn(": A3-A4 (", refused.text)  # two checks named, and no evidence at all
        brief.write_text(brief.read_text().replace("the `unit` check and the `lint` check", "the `lint` check"))
        self.git(self.home, "commit", "-qam", "one check")
        self.submit(wt, "--quest", "fix", "--for", "A4", "--fails-without", "--", "true")  # passes: no failing test
        brief.write_text(brief.read_text().replace("a regression test", "the `unit` check"))
        self.git(self.home, "commit", "-qam", "named")
        self.run_cli(self.home, "wait", "--quest", "fix", "--timeout", "60")
        self.fails(self.home, "land", "fix", match=": A4 (")  # a passing check, but no failing job without the fix
        mutant = self.submit(wt, "--quest", "fix", "--for", "A4", "--fails-without", "--", "exit 1")
        self.ok(self.home, "wait", mutant, "--timeout", "60")
        self.ok(self.home, "land", "fix")

    def test_cited_jobs_count_only_when_they_passed_at_the_code_being_landed(self) -> None:
        wt = self.started("fix", files="app.py")
        brief = self.home / ".campaign/demo/quests/fix.md"
        brief.write_text(brief.read_text().replace("None in this test.", "- A1 prints two: a probe"))
        self.git(self.home, "commit", "-qam", "criteria")
        self.commit(wt, {"app.py": "print(2)\n"})
        probe = self.submit(wt, "--quest", "fix", "--", "python3 app.py | grep -q 2")
        broken = self.submit(wt, "--quest", "fix", "--", "exit 3")
        self.ok(self.home, "wait", probe)
        self.run_cli(self.home, "wait", broken, "--timeout", "60")
        journal = ".campaign/demo/journal/fix.md"
        self.commit(wt, {journal: f"## Evidence\n\n| A1 | {broken} |\n"})
        self.verified("fix")
        out = self.ok(self.home, "status", "fix").out
        self.assertIn("    A1  pending  prints two: a probe\n", out)
        self.assertIn(f"journal: {broken} ({broken} failed)\n", out)
        self.fails(self.home, "land", "fix", match="criteria without evidence")
        self.commit(wt, {journal: f"State: ready for review\n\n## Evidence\n\n| A1 | {probe} |\n"})
        self.assertIn("    A1  met      prints two: a probe\n", self.ok(self.home, "status", "fix").out)
        self.commit(wt, {"app.py": "print(2)  # the code moves on after the probe\n"})
        self.verified("fix")
        r = self.fails(self.home, "land", "fix")
        self.assertIn(f"evidence jobs ran other code than {self.git(wt, 'rev-parse', 'HEAD')[:12]}", r.text)
        self.assertIn(f"`campaign job rerun {probe}`", r.text)
        rerun = self.ok(self.home, "job", "rerun", probe).out.split(":")[0]
        self.ok(self.home, "wait", rerun)
        self.ok(self.home, "land", "fix")  # the row still names the probe; its rerun at this code counts

    def test_brief_names_the_newer_job_that_replaces_a_cited_one(self) -> None:
        wt = self.started("fix", files="app.py")
        brief = self.home / ".campaign/demo/quests/fix.md"
        brief.write_text(brief.read_text().replace("None in this test.", "- A1 prints two: a probe"))
        self.git(self.home, "commit", "-qam", "criteria")
        self.commit(wt, {"app.py": "print(2)\n"})
        probe = self.submit(wt, "--quest", "fix", "--sync", "--", "python3 app.py | grep -q 2")
        journal = ".campaign/demo/journal/fix.md"
        self.commit(wt, {journal: f"## Evidence\n\n| A1 | {probe} |\n"})
        self.assertNotIn("replaces it", self.ok(wt, "brief").out)
        self.commit(wt, {"app.py": "print(2)  # the code moves on\n"})
        rerun = self.ok(self.home, "job", "rerun", probe).out.split(":")[0]
        self.ok(self.home, "wait", rerun)
        out = self.ok(wt, "brief").out
        self.assertIn(f"  - A1 in fix cites {probe}; {rerun} replaces it (done at ", out)
        [quest] = json.loads(self.ok(wt, "brief", "--json").out)["quests"]
        self.assertEqual([(r["job"], r["by"]) for r in quest["replaced"]], [(probe, rerun)])
        self.commit(wt, {journal: f"## Evidence\n\n| A1 | {rerun} |\n"})
        self.assertNotIn("replaces it", self.ok(wt, "brief").out)

    def test_a_cited_job_that_ran_after_another_counts(self) -> None:
        wt = self.started("fix", files="app.py")
        brief = self.home / ".campaign/demo/quests/fix.md"
        brief.write_text(brief.read_text().replace("None in this test.", "- A1 prints two: a probe"))
        self.git(self.home, "commit", "-qam", "criteria")
        self.commit(wt, {"app.py": "print(2)\n"})
        build = self.submit(wt, "--quest", "fix", "--", "true")
        probe = self.submit(wt, "--quest", "fix", "--after", build, "--", "python3 app.py | grep -q 2")
        self.ok(self.home, "wait", probe)
        self.commit(wt, {".campaign/demo/journal/fix.md": f"## Evidence\n\n| A1 | {probe} |\n"})
        self.assertIn("    A1  met      prints two: a probe\n", self.ok(self.home, "status", "fix").out)

    def scoped(self) -> Path:
        """A quest with a whole-tree build-test (unit) and a bench check whose inputs are lib/**."""
        self.ok(self.home, "config", "tested_docs=docs/**")  # docs the tests run: whole-tree evidence ignores none
        wt = self.started("fast", files="app.py,lib/**,docs/**,CMakeLists.txt", check="unit:light:5=python3 app.py")
        self.fails(self.home, "set", "fast", "--inputs", "bench=lib/**", match="has no check bench")
        self.ok(
            self.home, "set", "fast", "--check", "unit:light:5=python3 app.py", "--check", "bench=test -f lib/util.py"
        )
        self.fails(self.home, "set", "fast", "--inputs", "bench=lib/**", match="inputs narrowed to lib/**")
        self.ok(self.home, "set", "fast", "--inputs", "bench=lib/**", "--weaken", "bench reads only lib/")
        self.assertEqual(self.ok(self.home, "get", "fast", "inputs").out, "bench=lib/**\n")
        brief = self.home / ".campaign/demo/quests/fast.md"
        brief.write_text(brief.read_text().replace("None in this test.", "- A1 the bench holds: the bench check"))
        self.git(self.home, "commit", "-qam", "criteria")
        return wt

    def test_input_scoped_evidence_outlives_commits_outside_its_inputs(self) -> None:
        wt = self.scoped()
        self.commit(wt, {"app.py": "print(2)\n"})
        self.verified("fast")
        bench = self.ok(self.home, "get", "fast", "check").out  # the checks still read as before
        self.assertIn("bench:light:30=test -f lib/util.py", bench)
        (jid,) = [
            j.name for j in (self.root / "jobs").glob("2*") if "check bench" in j.joinpath("spec.json").read_text()
        ]
        self.commit(wt, {".campaign/demo/journal/fast.md": f"## Evidence\n\n| A1 | {jid} |\n"})
        tested = self.commit(wt, {"docs/guide.md": "a page the e2e runs\n"})
        jobs = len(list((self.root / "jobs").glob("2*")))
        out = self.ok(self.home, "verify", "fast").out
        self.assertIn(f"bench: {jid} (reused: its inputs are unchanged since", out)
        self.assertEqual(len(list((self.root / "jobs").glob("2*"))), jobs + 1, "only the build-test reruns")
        self.ok(self.home, "wait", "--quest", "fast", "--timeout", "90", timeout=120)
        self.assertIn("    A1  met      the bench holds", self.ok(self.home, "status", "fast").out)
        self.commit(wt, {"app.py": "print(3)\n"})  # code outside the inputs: the bench holds, the build-test not
        brief = self.ok(self.home, "brief", "fast").out
        self.assertNotIn("check bench is not green", brief)
        self.assertIn("check unit is not green", brief)
        self.fails(self.home, "land", "fast", match="check unit is not green")
        self.verified("fast")
        self.ok(self.home, "land", "fast")
        (landing,) = self.quest("fast")["landed"]
        scoped = [e for e in landing["evidence"] if e.get("inputs")]
        self.assertEqual(
            [(e["job"], e["inputs"], e["commit"]) for e in scoped], [(jid, ["lib/**"], scoped[0]["commit"])]
        )
        self.assertNotEqual(scoped[0]["commit"], tested)

    def test_input_scoped_evidence_is_void_when_an_input_or_build_file_changes(self) -> None:
        wt = self.scoped()
        self.commit(wt, {"app.py": "print(2)\n"})
        self.verified("fast")
        for files in ({"lib/util.py": "X = 2\n"}, {"lib/new.py": "Y = 1\n"}, {"CMakeLists.txt": "project(x)\n"}):
            self.commit(wt, files)
            self.assertIn("check bench is not green", self.ok(self.home, "brief", "fast").out, files)
            self.verified("fast")
        named = self.started("named", files="tools/**", check="unit=python3 app.py")
        self.ok(self.home, "set", "named", "--check", "unit=python3 app.py", "--check", "probe=sh tools/probe.sh")
        self.ok(self.home, "set", "named", "--inputs", "probe=lib/**", "--weaken", "the probe reads lib/")
        self.commit(named, {"tools/probe.sh": "true\n"})
        self.verified("named")
        self.commit(named, {"tools/probe.sh": "true  # the script the command runs\n"})
        self.assertIn("check probe is not green", self.ok(self.home, "brief", "named").out)

    def test_input_scoped_evidence_is_void_when_a_sibling_of_its_script_changes(self) -> None:
        """The script a command names runs the files beside it: they are inputs, whatever the globs say."""
        wt = self.started("sib", files="tools/**", check="unit=python3 app.py")
        self.ok(self.home, "set", "sib", "--check", "unit=python3 app.py", "--check", "probe=sh tools/probe.sh")
        self.ok(self.home, "set", "sib", "--inputs", "probe=lib/**", "--weaken", "the probe reads lib/")
        self.commit(wt, {"tools/probe.sh": 'sh "$(dirname "$0")/helper.sh"\n', "tools/helper.sh": "true\n"})
        self.verified("sib")
        self.assertNotIn("check probe is not green", self.ok(self.home, "brief", "sib").out)
        self.commit(wt, {"tools/helper.sh": "exit 1\n"})  # only the sibling the script calls
        self.assertIn("check probe is not green", self.ok(self.home, "brief", "sib").out)

    def test_inputs_never_replace_the_whole_tree_build_test(self) -> None:
        self.ok(self.home, "new", "solo", "--title", "solo", "--check", "unit=true")
        self.fails(self.home, "set", "solo", "--inputs", "unit=lib/**", "--weaken", "x", match="keep one without them")
        self.assertIn(
            "a file it reads outside them is not checked", " ".join(self.ok(self.home, "set", "--help").out.split())
        )
        self.fails(
            self.home,
            "new",
            "ci",
            "--title",
            "ci",
            "--check",
            "unit=ci:build",
            "--check",
            "b=true",
            "--inputs",
            "unit=x",
        )
        self.ok(self.home, "set", "solo", "--later", "slow=true", "--inputs", "slow=lib/**")
        self.ok(self.home, "set", "solo", "--inputs", "slow=")  # cleared: whole-tree again, a stronger check
        self.assertNotIn("inputs", self.quest("solo")["checks"][1])

    def test_land_needs_green_checks_at_the_exact_head(self) -> None:
        wt = self.started("fix", files="app.py")
        self.commit(wt, {"app.py": "print(2)\n"})
        self.fails(self.home, "land", "fix", match="not green")
        self.verified("fix")
        self.commit(wt, {"app.py": "print(3)\n"})  # the evidence is now for an older commit
        self.fails(self.home, "land", "fix", match="not green")
        self.ready_for_review(wt, "fix")
        self.verified("fix")
        head = self.git(wt, "rev-parse", "HEAD")
        self.ok(self.home, "land", "fix")
        self.assertEqual(self.quest("fix")["status"], "landed")
        record = self.quest("fix")["landed"][0]
        self.assertEqual(record["tested"], head)
        self.assertEqual(record["evidence"][0]["state"], "done")

    def test_status_counts_failures_only_at_the_code_the_quest_has_now(self) -> None:
        wt = self.started("fix", files="app.py")
        self.commit(wt, {"app.py": "print(2)\n"})
        self.verified("fix")
        failed = self.submit(self.home, "--quest", "fix", "--", "exit 1")
        self.run_cli(self.home, "wait", failed, "--timeout", "60")
        out = self.ok(self.home, "status").out
        # the check passed, the probe did not, and no journal mentions it yet
        self.assertIn("jobs at this code: 1 passed, 1 failed; not in a journal yet (`campaign job log ID`):", out)
        self.assertIn(f"    {failed}  exit 1 at ", out)  # which job and what it ran
        self.assertNotIn("criterion with evidence", out)  # a quest with no criteria shows none
        self.commit(wt, {".campaign/demo/journal/fix.md": "a journal commit keeps the code\n"})
        self.assertIn(f"    {failed}  exit 1 at ", self.ok(self.home, "status").out)
        self.commit(wt, {".campaign/demo/journal/fix.md": f"{failed} ran the wrong command\n"})
        out = self.ok(self.home, "status").out
        self.assertIn("jobs at this code: 1 passed, 1 failed (noted in the journals)\n", out)
        self.assertNotIn(f"    {failed}", out)
        other = self.submit(self.home, "--quest", "fix", "--", "exit 2")
        self.run_cli(self.home, "wait", other, "--timeout", "60")
        out = self.ok(self.home, "status").out  # one noted, one not: only the unread one is listed
        self.assertIn("jobs at this code: 1 passed, 2 failed; 1 not in a journal yet (`campaign job log ID`):", out)
        self.assertIn(f"    {other}  exit 2 at ", out)
        self.assertNotIn(f"    {failed}", out)
        self.commit(wt, {"app.py": "print(3)\n"})
        self.assertNotIn("jobs at this code", self.ok(self.home, "status").out)  # history: `campaign job list`

    def test_status_shows_progress_from_the_criteria_and_the_journal(self) -> None:
        wt = self.started("fix", files="app.py")
        brief = self.home / ".campaign/demo/quests/fix.md"
        brief.write_text(
            "# fix\n\n## Acceptance criteria\n\n- A1 [manual] one: a test\n- A2 [manual] two: a test\n- A3 three: a test\n"
        )
        self.git(self.home, "commit", "-qam", "criteria")
        out = self.ok(self.home, "status").out  # the template's placeholder row lists nothing
        self.assertIn("0% 0 of 3 criteria with evidence\n", out)
        self.assertIn("no evidence yet: A1-A3", out)
        journal = "\n".join(
            [
                "# fix: journal",
                "",
                "State: working",
                "",
                "## Next",
                "",
                "1. wire the reader",
                "",
                "## Evidence",
                "",
                "| Criterion | Evidence |",
                "|---|---|",
                "| A1 | job 261106-1 |",
                "| A2 | <pending> |",
                "| A3 | job 261106-2 | Pending |",
                "",
            ]
        )
        self.commit(wt, {".campaign/demo/journal/fix.md": journal})
        out = self.ok(self.home, "status").out
        self.assertIn("33% 1 of 3 criteria with evidence (A1)", out)
        self.assertNotIn("no evidence yet", out)
        self.ok(self.home, "start", "fix", "--task", "two", "--files", "app.py")
        task = self.root / "app-wt/demo/fix--two"
        self.commit(task, {".campaign/demo/journal/fix--two.md": "## Evidence\n\n| A2 | test_two |\n"})
        out = self.ok(self.home, "status").out
        self.assertIn("67% 2 of 3 criteria with evidence (A1-A2)", out)  # a task's evidence counts for its quest
        self.assertNotIn("criteria:", out)  # the list is for one quest's view
        one = self.ok(self.home, "status", "fix").out
        for line in (
            "A1  met      [manual] one: a test",
            "             journal: job 261106-1",
            "A2  met      [manual] two: a test",
            "             journal: <pending>",
            "             two: test_two",
            "A3  pending  three: a test",
            "             journal: job 261106-2 | Pending",
        ):
            self.assertIn(f"    {line}\n", one)
        self.assertIn("two: (no State line)", one)
        self.assertIn("67% of the campaign", out)
        self.assertIn("next: wire the reader", out)
        self.env["CAMPAIGN_BAR"] = "#."
        self.assertIn("#############....... 67% 2 of 3", self.ok(self.home, "status").out)

    def test_campaign_metadata_commits_keep_the_evidence(self) -> None:
        wt = self.started("fix", files="app.py")
        self.commit(wt, {"app.py": "print(2)\n"})
        self.verified("fix")
        work = ".campaign/demo/journal/fix.md"
        self.commit(wt, {work: (wt / work).read_text() + "\nEvidence: the unit check.\n"})
        self.assertIn("reused", self.ok(self.home, "verify", "fix").out)
        landed = self.ok(self.home, "land", "fix").out  # main moved only in campaign files: a merge
        self.assertIn(f"(merge {self.quest('fix')['landed'][0]['commit'][:12]})", landed)
        self.commit(self.home, {"docs/notes.md": "not campaign metadata\n"})
        b = self.started("other", files="README.md")
        self.commit(b, {"README.md": "x\n"})
        self.verified("other")
        self.commit(b, {"other.txt": "outside the campaign's files and the docs counts as code\n"})
        self.assertNotIn("reused", self.ok(self.home, "verify", "other").out)

    def test_failing_check_blocks_landing(self) -> None:
        wt = self.started("fix", files="app.py", check="unit=python3 app.py")
        self.commit(wt, {"app.py": "raise SystemExit(3)\n"})
        self.ok(self.home, "verify", "fix")
        self.assertEqual(self.run_cli(self.home, "wait", "--quest", "fix", "--timeout", "60").code, 1)
        self.fails(self.home, "land", "fix", match="failed")

    def test_landed_code_equals_the_tested_code(self) -> None:
        wt = self.started("fix", files="app.py")
        self.commit(wt, {"app.py": "print(2)\n"})
        self.verified("fix")
        self.ok(self.home, "new", "other", "--title", "other", "--risk", "low")  # a state-only commit on main
        self.ok(self.home, "land", "fix")
        tested = self.quest("fix")["landed"][0]["tested"]
        diff = self.git(self.home, "diff", tested, "main", "--", ".", ":(exclude).campaign")
        self.assertEqual(diff, "")

    def test_main_moving_in_code_requires_a_rebase_and_new_evidence(self) -> None:
        a = self.started("a", files="app.py", check="unit=true")
        self.commit(a, {"app.py": "print('a')\n"})
        self.verified("a")
        b = self.started("b", files="lib/**", check="unit=true")
        self.commit(b, {"lib/util.py": "X = 2\n"})
        self.verified("b")
        self.ok(self.home, "land", "a")
        self.fails(self.home, "land", "b", match="merge main into it")
        self.git(b, "rebase", "-q", "main")
        self.fails(self.home, "land", "b", match="not green")
        self.verified("b")
        self.ok(self.home, "land", "b")

    def test_evidence_survives_merges_from_main_that_ci_tests(self) -> None:
        state = self.state()
        state["ci"] = "github"
        (self.home / ".campaign/demo/state.json").write_text(json.dumps(state))
        self.git(self.home, "commit", "-q", "-am", "require CI")
        gh = self.root / "bin" / "gh"
        gh.write_text('#!/bin/sh\necho \'[{"name": "build", "status": "completed", "conclusion": "success"}]\'\n')
        gh.chmod(0o755)
        self.commit(self.home, {"notes/n.txt": "1\n2\n3\n4\n5\n6\n"})
        a = self.started("a", files="app.py")
        self.commit(a, {"app.py": "print('a')\n"})
        self.verified("a")
        b = self.started("b", files="lib/**,notes/**")
        brief = self.home / ".campaign/demo/quests/b.md"
        brief.write_text(brief.read_text().replace("None in this test.", "- A1 the probe passes: a probe"))
        self.git(self.home, "commit", "-qam", "criteria")
        self.commit(b, {"lib/util.py": "X = 2\n", "notes/n.txt": "1\n2\n3\n4\n5\n6 b\n"})
        check = "grep -q 2 lib/util.py && grep -q 6 notes/n.txt"  # also reads the file main changes below
        probe = self.submit(b, "--quest", "b", "--", check)
        self.verified("b")
        self.commit(b, {".campaign/demo/journal/b.md": f"State: ready for review\n\n## Evidence\n\n| A1 | {probe} |\n"})
        self.ok(self.home, "land", "a")
        self.fails(self.home, "land", "b", match="merge main into it")
        self.git(b, "merge", "-q", "--no-edit", "main")
        jobs = len(list((self.root / "jobs").glob("2*")))
        out = self.ok(self.home, "verify", "b").out
        self.assertIn("(reused: passed at ", out)
        self.assertIn("only 1 merge from main since, none touching its files", out)
        self.assertEqual(self.submit(b, "--quest", "b", "--", check), probe)
        self.assertEqual(len(list((self.root / "jobs").glob("2*"))), jobs, "a merge from main reran evidence")
        brief_out = self.ok(self.home, "brief", "b").out
        self.assertNotIn("not green", brief_out)
        self.assertNotIn("criteria without evidence", brief_out)
        self.commit(self.home, {"notes/n.txt": "1 main\n2\n3\n4\n5\n6\n"})  # main changes a file b changes
        self.git(b, "merge", "-q", "--no-edit", "main")
        brief_out = self.ok(self.home, "brief", "b").out
        self.assertIn("check unit is not green", brief_out)
        self.assertIn("criteria without evidence", brief_out)
        self.verified("b")
        self.fails(self.home, "land", "b", match="criteria without evidence")
        rerun = self.ok(self.home, "job", "rerun", probe).out.split(":")[0]
        self.ok(self.home, "wait", rerun)
        self.ok(self.home, "land", "b")

    def test_without_ci_the_build_test_runs_at_the_merged_code(self) -> None:
        a = self.started("a", files="app.py")
        self.commit(a, {"app.py": "print('a')\n"})
        self.verified("a")
        b = self.started("b", files="lib/**")
        brief = self.home / ".campaign/demo/quests/b.md"
        brief.write_text(brief.read_text().replace("None in this test.", "- A1 the probe passes: a probe"))
        self.git(self.home, "commit", "-qam", "criteria")
        self.commit(b, {"lib/util.py": "X = 2\n"})
        probe = self.submit(b, "--quest", "b", "--", "grep -q 2 lib/util.py")
        self.verified("b")
        self.commit(b, {".campaign/demo/journal/b.md": f"State: ready for review\n\n## Evidence\n\n| A1 | {probe} |\n"})
        self.ok(self.home, "land", "a")
        self.git(b, "merge", "-q", "--no-edit", "main")
        brief_out = self.ok(self.home, "brief", "b").out
        self.assertIn("check unit is not green", brief_out)  # nothing else tests the merge
        self.assertNotIn("criteria without evidence", brief_out)  # the probe tested b's own code
        self.verified("b")
        self.ok(self.home, "land", "b")

    def test_a_tagged_job_at_new_code_cancels_its_queued_older_role(self) -> None:
        """A resubmit with the same label and criterion replaces the older queued or running job even
        when its command or environment changed; another label for the criterion is kept."""
        wt = self.started("fix", files="app.py")
        brief = self.home / ".campaign/demo/quests/fix.md"
        brief.write_text(brief.read_text().replace("None in this test.", "- A1 prints two: a probe"))
        self.git(self.home, "commit", "-qam", "criteria")
        go = self.root / "go"
        hold = f"while [ ! -e {go} ]; do sleep 0.1; done"
        gate = self.submit(wt, "--", hold)
        busy = self.submit(wt, "--quest", "fix", "--for", "A1", "--what", "A1 soak", "--", hold)
        self.wait_until(lambda: self.job(busy)["state"] == "running")
        tagged = ("--quest", "fix", "--for", "A1", "--after", gate)
        old = self.submit(wt, *tagged, "--what", "A1 probe", "--env", "X=1", "--", "python3 app.py")
        other = self.submit(wt, *tagged, "--what", "A1 oracle", "--", "true oracle")
        self.commit(wt, {"app.py": "print(2)\n"})
        r = self.ok(wt, "job", "submit", *tagged, "--what", "A1 probe", "--", "python3 app.py | cat")
        self.assertIn(f"superseded {old}", r.out)
        self.assertEqual(self.job(other)["state"], "queued", "another role for A1 was cancelled")
        r = self.ok(wt, "job", "submit", "--quest", "fix", "--for", "A1", "--what", "A1 soak", "--", hold + " ")
        self.assertIn(f"superseded {busy}: running", r.out)
        self.wait_until(lambda: self.job(busy)["state"] != "running")
        go.touch()
        self.ok(self.home, "wait", "--quest", "fix", "--timeout", "60")

    def test_tagged_jobs_prove_their_criteria_without_a_table(self) -> None:
        wt = self.started("fix", files="app.py")
        brief = self.home / ".campaign/demo/quests/fix.md"
        criteria = "- A1 prints two: a probe\n- A2 the suite passes\n- A3 [manual] reads well: the lead"
        brief.write_text(brief.read_text().replace("None in this test.", criteria))
        self.git(self.home, "commit", "-qam", "criteria")
        self.commit(wt, {"app.py": "print(2)\n"})
        self.fails(wt, "job", "submit", "--quest", "fix", "--for", "A3", "--", "true", match="[manual] criterion")
        self.fails(wt, "job", "submit", "--quest", "fix", "--for", "A9", "--", "true", match="no criterion A9")
        self.fails(wt, "job", "submit", "--for", "A1", "--", "true", match="add --quest")
        probe = self.submit(wt, "--quest", "fix", "--for", "A1", "--", "python3 app.py | grep -q 2")
        failing = self.submit(wt, "--quest", "fix", "--for", "A2", "--", "exit 4")
        self.ok(self.home, "verify", "fix", "--for", "A2")
        self.run_cli(self.home, "wait", "--quest", "fix", "--timeout", "90", timeout=120)
        journal = ".campaign/demo/journal/fix.md"
        self.commit(wt, {journal: f"## Evidence\n\n| A1 | {failing} |\n| A3 | the lead read the output |\n"})
        out = self.ok(self.home, "status", "fix").out
        self.assertIn("A1  met", out)  # its tagged probe, whatever the table cites
        self.assertIn(f"jobs: {probe}", out)
        self.assertIn("A2  met", out)  # the suite tagged by verify passed, though another tagged job failed
        table = self.ok(wt, "brief").out
        self.assertIn("evidence, for the journal's Evidence table:\n  | Criterion | Evidence |", table)
        self.assertIn(f"  | A1 | {probe} |", table)
        self.assertIn("  | A3 | the lead read the output |", table)
        self.commit(wt, {"app.py": "print(2)  # the code moves on\n"})
        self.ready_for_review(wt, "fix")
        self.verified("fix")
        refused = self.fails(self.home, "land", "fix", match="criteria without evidence at")
        self.assertIn(f"`campaign job rerun {probe}`", refused.text)
        self.assertIn(f"{probe} ran other code", self.ok(self.home, "status", "fix").out)
        rerun = self.ok(self.home, "job", "rerun", probe).out.split(":")[0]
        self.ok(self.home, "wait", rerun)
        self.ok(self.home, "land", "fix")  # the rerun carries the tag: same command, the head's code
        self.assertTrue(self.quest("fix")["landed"][0]["criteria"]["A1"].startswith(rerun))  # kept by prune

    def test_a_job_meant_to_fail_never_sends_a_failure_notice(self) -> None:
        calls = self.root / "cmux-calls"
        fake = self.root / "bin" / "cmux"
        fake.write_text(f'#!/bin/sh\nprintf "%s|" "$@" >> {calls}; echo >> {calls}\n')
        fake.chmod(0o755)
        self.env.update(CMUX_WORKSPACE_ID="ws-test", QUIET_NOTIFY_AFTER="0")
        wt = self.started("fix", files="app.py")
        brief = self.home / ".campaign/demo/quests/fix.md"
        brief.write_text(brief.read_text().replace("None in this test.", "- A1 prints two: a regression test"))
        self.git(self.home, "commit", "-qam", "criteria")
        mutant = self.submit(wt, "--quest", "fix", "--for", "A1", "--fails-without", "--", "exit 1")
        self.run_cli(wt, "wait", mutant, "--timeout", "60")  # one at a time: the fake cmux's writes interleave
        plain = self.submit(wt, "--quest", "fix", "--", "exit 2")
        self.run_cli(wt, "wait", plain, "--timeout", "60")
        notices = [x for x in calls.read_text().splitlines() if x.startswith("notify|")]
        self.assertTrue(any(f"job-{mutant}: done|" in x for x in notices), notices)
        self.assertTrue(any(f"job-{plain}: failed (exit 2)|" in x for x in notices), notices)
        self.assertFalse(any(f"job-{mutant}: failed" in x for x in notices), "a mutant failing as meant warned")

    def test_a_fails_without_job_must_fail_where_the_fix_is_missing(self) -> None:
        wt = self.started("fix", files="app.py")
        brief = self.home / ".campaign/demo/quests/fix.md"
        brief.write_text(brief.read_text().replace("None in this test.", "- A1 prints two: a regression test"))
        self.git(self.home, "commit", "-qam", "criteria")
        self.commit(wt, {"app.py": "print(2)\n"})
        self.fails(wt, "job", "submit", "--quest", "fix", "--fails-without", "--", "true", match="needs --for")
        test = "python3 app.py | grep -q 2"
        self.submit(wt, "--quest", "fix", "--for", "A1", "--", test)
        self.submit(wt, "--quest", "fix", "--for", "A1", "--fails-without", "--commit", "main", "--", test)
        self.submit(wt, "--quest", "fix", "--for", "A1", "--fails-without", "--", test)  # passes: the fix is in
        self.ok(self.home, "verify", "fix")
        self.run_cli(self.home, "wait", "--quest", "fix", "--timeout", "60")
        self.assertIn("A1  met", self.ok(self.home, "status", "fix").out)  # one fails-without job failed
        self.ok(self.home, "land", "fix")
        bad_only = self.started("other", files="lib/**")
        brief = self.home / ".campaign/demo/quests/other.md"
        brief.write_text(brief.read_text().replace("None in this test.", "- A1 holds: a regression test"))
        self.git(self.home, "commit", "-qam", "criteria")
        self.commit(bad_only, {"lib/util.py": "X = 2\n"})
        plain = self.submit(bad_only, "--quest", "other", "--for", "A1", "--", "grep -q 2 lib/util.py")
        args = ("--quest", "other", "--for", "A1", "--fails-without")
        inverted = self.submit(bad_only, *args, "--", "grep -q 2 lib/util.py")
        self.assertNotEqual(plain, inverted, "the flag is part of a job's identity")
        mutant = self.submit(bad_only, *args, "--commit", "main", "--", "grep -q 1 lib/util.py")
        self.ok(self.home, "verify", "other")
        self.run_cli(self.home, "wait", "--quest", "other", "--timeout", "60")
        self.assertIn(f"{mutant} failed: the test passed without the fix", self.ok(bad_only, "brief").out)
        self.assertEqual(self.job(mutant)["exit"], 0)  # the recorded exit is the command's own
        self.fails(self.home, "land", "other", match="criteria without evidence")
        good = self.submit(
            bad_only,
            "--quest",
            "other",
            "--for",
            "A1",
            "--fails-without",
            "--commit",
            "main",
            "--",
            "grep -q 2 lib/util.py",
        )
        self.ok(self.home, "wait", good)
        self.assertEqual(self.job(good)["state"], "done")
        self.assertIn("failed without the fix, as it must (exit 1)", self.job(good)["note"])
        self.ok(self.home, "land", "other")

    def test_a_fails_without_job_never_counts_a_command_that_did_not_run(self) -> None:
        wt = self.started("fix", files="app.py")
        brief = self.home / ".campaign/demo/quests/fix.md"
        brief.write_text(brief.read_text().replace("None in this test.", "- A1 holds: a regression test"))
        self.git(self.home, "commit", "-qam", "criteria")
        self.commit(wt, {"app.py": "print(2)\n"})
        args = ("--quest", "fix", "--for", "A1", "--fails-without", "--commit", "main")
        missing = self.submit(wt, *args, "--", "no-such-command-here")  # exit 127
        unrunnable = self.submit(wt, *args, "--", "/etc/hosts")  # exit 126
        broken = self.submit(wt, *args, "--build", "exit 3", "--", "exit 1")
        self.run_cli(self.home, "wait", missing, unrunnable, broken, "--timeout", "60")
        for jid, why in ((missing, "exit 127"), (unrunnable, "exit 126"), (broken, "its build failed")):
            self.assertEqual(self.job(jid)["state"], "failed", jid)
            self.assertIn(why, self.job(jid)["note"])
        self.fails(self.home, "land", "fix", match="criteria without evidence")
        self.assertIn(f"{missing} failed: exit 127", self.ok(wt, "brief").out)

    def test_a_fails_without_record_is_read_by_the_rule_it_was_submitted_under(self) -> None:
        campaign = campaign_module()

        def job(version: int, state: str, exit_code: int) -> dict[str, object]:
            spec: dict[str, object] = {"fails_without": version} if version else {}
            return {"id": "j", "spec": spec, "state": {"state": state, "exit": exit_code}}

        met, why = campaign.fails_without_met, campaign.fails_without_why
        self.assertTrue(met(job(0, "done", 0)))  # an older, self-inverted command that exited 0
        self.assertFalse(met(job(0, "failed", 1)))
        self.assertIn("had to exit 0", why(job(0, "failed", 1)))
        self.assertTrue(met(job(campaign.FAILS_WITHOUT, "done", 1)))  # the flag's own inversion
        for code in (0, 124, 126, 127):
            self.assertFalse(met(job(campaign.FAILS_WITHOUT, "done", code)), code)
        self.assertFalse(met(job(campaign.FAILS_WITHOUT, "timeout", 124)))
        spec = {"fails_without": campaign.FAILS_WITHOUT}
        self.assertEqual(campaign.outcome(spec, 124)[0], "timeout")
        self.assertEqual(campaign.outcome(spec, 5, cancelled=True)[0], "cancelled")
        self.assertEqual(campaign.outcome({}, 1), ("failed", ""))  # no flag, no inversion
        self.assertEqual(campaign.outcome({}, 0), ("done", ""))

    def test_a_ci_check_is_met_by_its_ci_job_and_queues_nothing(self) -> None:
        conclusion = self.root / "ci-conclusion"
        conclusion.write_text("failure")
        jobs = '{"jobs": [{"name": "AddressSanitizer + UBSan", "status": "completed", "conclusion": "%s"}]}'
        fake = self.root / "bin" / "gh"
        fake.write_text(
            "#!/bin/sh\n"
            'if [ "$2" = list ]; then echo \'[{"databaseId": 7}]\'; '
            f"else printf '{jobs}' \"$(cat {conclusion})\"; fi\n"
        )
        fake.chmod(0o755)
        self.started("san", check="asan=ci:AddressSanitizer")
        self.ok(self.home, "verify", "san")
        self.assertNotIn("san", self.ok(self.home, "job", "list", "--all").out, "a CI check queued a local job")
        refused = self.run_cli(self.home, "land", "san")
        self.assertIn("check asan is not green", refused.out + refused.err)
        self.assertIn("AddressSanitizer + UBSan failed", refused.out + refused.err)
        conclusion.write_text("cancelled")  # superseded by a newer push: not a failure
        superseded = self.run_cli(self.home, "land", "san")
        self.assertIn("AddressSanitizer + UBSan was cancelled, not failed", superseded.out + superseded.err)
        self.assertNotIn("UBSan failed", superseded.out + superseded.err)
        conclusion.write_text("success")
        later = self.run_cli(self.home, "land", "san")
        self.assertNotIn("check asan", later.out + later.err)

    def test_ci_at_the_same_code_speaks_for_a_journal_or_docs_commit(self) -> None:
        tested = self.root / "ci-commit"
        jobs = '{"jobs": [{"name": "AddressSanitizer + UBSan", "status": "completed", "conclusion": "success"}]}'
        fake = self.root / "bin" / "gh"
        listed = f'case "$*" in *"$(cat {tested})"*) echo \'[{{"databaseId": 7}}]\';; *) echo \'[]\';; esac'
        fake.write_text(f"#!/bin/sh\nif [ \"$2\" = list ]; then {listed}; else printf '%s' '{jobs}'; fi\n")
        fake.chmod(0o755)
        wt = self.started("san", check="asan=ci:AddressSanitizer")
        tested.write_text(self.git(wt, "rev-parse", "HEAD"))
        journal = next((wt / ".campaign").rglob("san.md"))
        journal.write_text(journal.read_text() + "\nA note.\n")
        self.git(wt, "commit", "-qam", "journal")
        self.assertNotIn("check asan", self.run_cli(self.home, "brief", "san").out)
        self.commit(wt, {"docs/note.md": "CI skips documentation-only pushes\n"})
        self.assertNotIn("check asan", self.run_cli(self.home, "brief", "san").out)
        (wt / "more.py").write_text("print(2)\n")
        self.git(wt, "add", "more.py")
        self.git(wt, "commit", "-qm", "code")
        self.assertIn("no CI job named", self.run_cli(self.home, "brief", "san").out)

    def test_a_passed_ci_run_is_fetched_once_for_every_command(self) -> None:
        calls = self.root / "gh-calls"
        jobs = '{"jobs": [{"name": "AddressSanitizer + UBSan", "status": "completed", "conclusion": "success"}]}'
        fake = self.root / "bin" / "gh"
        fake.write_text(
            f'#!/bin/sh\necho "$2" >> {calls}\n'
            'if [ "$2" = list ]; then echo \'[{"databaseId": 7}]\'; '
            f"else printf '%s' '{jobs}'; fi\n"
        )
        fake.chmod(0o755)
        self.started("san", check="asan=ci:AddressSanitizer")
        self.assertNotIn("check asan", self.run_cli(self.home, "brief", "san").out)
        self.assertIn("view", calls.read_text().split())
        calls.write_text("")
        self.assertNotIn("check asan", self.run_cli(self.home, "brief", "san").out)
        self.assertNotIn("view", calls.read_text().split(), "a passed run's jobs were fetched again")
