"""Quests, ownership, evidence, reviews, landing and state recovery."""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from harness import CAMPAIGN, CampaignCase, campaign_module


def at(seconds: float) -> str:
    """An ISO timestamp `seconds` after now, as Claude Code writes them."""
    return datetime.fromtimestamp(time.time() + 600 + seconds, timezone.utc).isoformat().replace("+00:00", "Z")


class Quests(CampaignCase):
    def test_wait_returns_when_an_contributor_commits_its_journal(self) -> None:
        wt = self.started("fix", files="app.py")
        waiting = subprocess.Popen(
            [sys.executable, str(CAMPAIGN), "wait", "--commits", "--timeout", "60"],
            cwd=self.home,
            env=self.env,
            stdout=subprocess.PIPE,
            text=True,
        )
        time.sleep(2)
        self.assertIsNone(waiting.poll())  # nothing has happened yet
        journal = ".campaign/demo/journal/fix.md"
        state = "State: blocked on the lead: the publication code\n"
        self.commit(wt, {journal: state + (wt / journal).read_text().split("\n", 3)[-1]})
        out, _ = waiting.communicate(timeout=30)
        self.assertEqual(waiting.returncode, 0)
        self.assertIn("fix: quest/demo/fix moved to", out)
        self.assertIn("journal: blocked on the lead: the publication code", out)
        self.ok(self.home, "set", "fix", "status=dropped", "reason=done here")
        self.assertIn("no quest in flight", self.ok(self.home, "wait", "--commits").out)

    def test_wait_misses_nothing_that_happened_between_two_waits(self) -> None:
        a = self.started("one", files="app.py")
        b = self.started("two", files="README.md")
        self.assertEqual(self.run_cli(self.home, "wait", "--commits", "--timeout", "1").code, 3)  # starts from now
        self.commit(a, {"app.py": "print(2)\n"})  # both contributors finish while the lead is busy
        self.commit(b, {"README.md": "two\n"})
        out = self.ok(self.home, "wait", "--commits", "--timeout", "30").out
        self.assertIn("one: quest/demo/one moved to", out)
        self.assertIn("two: quest/demo/two moved to", out)
        self.assertIn("still in flight: one (working), two (working)", out)
        self.commit(b, {"README.md": "two again\n"})  # lands while the lead handles the first two
        out = self.ok(self.home, "wait", "--commits", "--timeout", "30").out
        self.assertIn("two: quest/demo/two moved to", out)
        self.assertNotIn("one: quest/demo/one moved", out)
        job = self.submit(self.home, "--quest", "one", "--", "true")
        self.ok(self.home, "wait", job)  # ends before the next event wait starts
        self.assertIn(f"{job}  done exit 0", self.ok(self.home, "wait", "--commits", "--timeout", "30").out)
        self.assertEqual(self.run_cli(self.home, "wait", "--commits", "--timeout", "1").code, 3)

    def test_mistakes_get_messages_that_say_what_to_do(self) -> None:
        self.fails(self.home, "new", "Bad_ID", "--title", "x", match="quest id 'Bad_ID': use lowercase letters")
        self.started("fix", files="app.py")
        self.fails(self.home, "set", "fix", "risk=extreme", match="risk is one of low, normal, high, critical")
        self.fails(self.home, "set", "fix", "colour=red", match="no field colour to set; the fields are title")
        self.assertIn("wait for them with `campaign wait --quest fix`", self.ok(self.home, "verify", "fix").out)

    def test_job_list_shows_this_campaigns_jobs(self) -> None:
        self.started("fix", files="app.py")
        mine = self.submit(self.home, "--quest", "fix", "--sync", "--", "true")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        theirs = self.submit(elsewhere, "--unversioned", "--sync", "--", "true")
        here = self.ok(self.home, "job", "list").out
        self.assertTrue(here.startswith("JOB "), here)
        self.assertIn(mine, here)
        self.assertNotIn(theirs, here)
        for everything in (self.ok(self.home, "job", "list", "--all").out, self.ok(elsewhere, "job", "list").out):
            self.assertIn(mine, everything)
            self.assertIn(theirs, everything)

    def test_an_empty_campaign_says_how_to_begin(self) -> None:
        self.assertIn("  no quests yet (`campaign new ID --title T` plans one)\n", self.ok(self.home, "status").out)
        self.assertIn("nothing in flight or planned (`campaign new ID --title T`", self.ok(self.home, "brief").out)
        self.assertEqual(
            self.ok(self.home, "job", "list").out, "no jobs of this campaign (--all lists every campaign's)\n"
        )

    def test_get_prints_what_set_takes_and_config_reads_a_key(self) -> None:
        self.started("fix", files="app.py", check="unit=python3 app.py")
        self.ok(self.home, "set", "fix", "--later", "soak::5=true")
        out = self.ok(self.home, "get", "fix").out
        self.assertTrue(out.startswith("title=fix\nstatus=active\nrisk=low\nreason=\n"), out)
        for line in (
            "files=app.py",
            "check=unit:light:30=python3 app.py",
            "later=soak:light:5=true",
            "branch=quest/demo/fix",
        ):
            self.assertIn(f"\n{line}\n", out)
        self.assertEqual(self.ok(self.home, "get", "fix", "status", "files").out, "active\napp.py\n")
        self.fails(self.home, "get", "fix", "nope", match="no field nope; the fields are title, status")
        self.ok(self.home, "start", "fix", "--task", "docs", "--files", "app.py")
        self.assertEqual(self.ok(self.home, "get", "fix", "--task", "docs", "files", "status").out, "app.py\nactive\n")
        self.assertEqual(self.ok(self.home, "config", "ci").out, "\n")
        self.fails(self.home, "config", "ci", "ci=github", match="either KEYs to read or KEY=VALUE")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        self.assertEqual(self.ok(elsewhere, "--campaign", "demo", "get", "fix", "status").out, "active\n")
        self.assertEqual(self.ok(elsewhere, "get", "fix", "status", "--campaign", "demo").out, "active\n")

    def test_task_file_scope_can_be_corrected_without_overlapping_a_peer(self) -> None:
        self.started("fix", files="app.py,lib/**,README.md")
        self.ok(self.home, "start", "fix", "--task", "one", "--files", "lib/**")
        self.ok(self.home, "start", "fix", "--task", "two", "--files", "app.py")
        self.ok(self.home, "set", "fix", "--task", "one", "files=lib/**,README.md")
        self.assertEqual(self.quest("fix")["tasks"]["one"]["files"], ["lib/**", "README.md"])
        self.fails(self.home, "set", "fix", "--task", "one", "files=app.py", match="overlap")
        self.assertEqual(self.quest("fix")["tasks"]["one"]["files"], ["lib/**", "README.md"])
        self.fails(self.home, "set", "fix", "--task", "missing", "files=lib/**", match="no task")
        self.fails(self.home, "set", "fix", "--task", "one", "status=review", match="dropped or active")

    def test_start_records_branch_worktree_base_and_work_file(self) -> None:
        wt = self.started("fix", files="app.py")
        p = self.quest("fix")
        self.assertEqual(p["status"], "active")
        self.assertEqual(p["branch"], "quest/demo/fix")
        self.assertTrue((wt / ".campaign/demo/journal/fix.md").exists())
        self.assertEqual(self.git(self.home, "merge-base", "main", "quest/demo/fix"), p["base"])
        self.assertTrue((self.home / ".campaign/demo/quests/fix.md").exists())

    def test_needs_block_start_and_after_blocks_landing(self) -> None:
        self.ok(self.home, "new", "lib", "--title", "lib", "--risk", "low", "--files", "lib/**")
        self.ok(self.home, "new", "use", "--title", "use", "--risk", "low", "--files", "app.py", "--needs", "lib")
        self.fails(self.home, "start", "use", match="needs lib")
        self.ok(self.home, "start", "use", "--force", "contract fixed in lib's brief")
        self.assertIn("started by force", self.quest("use")["reason"])
        self.ok(
            self.home,
            "new",
            "late",
            "--title",
            "late",
            "--risk",
            "low",
            "--files",
            "README.md",
            "--after",
            "lib",
            "--check",
            "unit=true",
        )
        self.ok(self.home, "start", "late")
        self.commit(self.wt("late"), {"README.md": "late\n"})
        self.verified("late")
        self.fails(self.home, "land", "late", match="land lib first")

    def test_dependency_cycles_are_rejected(self) -> None:
        self.ok(self.home, "new", "a", "--title", "a", "--risk", "low")
        self.ok(self.home, "new", "b", "--title", "b", "--risk", "low", "--needs", "a")
        self.fails(self.home, "set", "a", "needs=b", match="dependency cycle")

    def test_state_is_written_only_from_the_main_checkout(self) -> None:
        wt = self.started("fix", files="app.py")
        self.fails(wt, "set", "fix", "status=review", match="main checkout")
        self.ok(wt, "status")  # reading works anywhere

    def test_status_transitions_are_validated(self) -> None:
        self.started("fix", files="app.py")
        self.fails(self.home, "set", "fix", "status=landed", match="cannot become landed")
        self.fails(self.home, "set", "fix", "status=blocked", match="needs a reason")
        self.ok(self.home, "set", "fix", "status=blocked", "reason=waiting on the owner")
        self.ok(self.home, "set", "fix", "status=active")
        self.assertNotIn("reason", self.quest("fix"))


class Ownership(CampaignCase):
    def test_overlapping_file_sets_cannot_start_together(self) -> None:
        self.started("one", files="lib/**")
        self.ok(self.home, "new", "two", "--title", "two", "--risk", "low", "--files", "lib/util.py")
        self.fails(self.home, "start", "two", match="overlap one")
        self.ok(self.home, "new", "three", "--title", "three", "--risk", "low", "--files", "app.py")
        self.ok(self.home, "start", "three")
        self.started("dense", files="tests/*dense*")  # a pattern and a file it does not match
        self.started("fix", files="tests/test_collection.py")
        self.ok(self.home, "new", "more", "--title", "more", "--risk", "low", "--files", "tests/test_dense_codec.py")
        self.fails(self.home, "start", "more", match="overlap dense")

    def test_status_lists_launchable_quests_critical_path_first(self) -> None:
        self.started("one", files="lib/**")
        new = ("--title", "t", "--risk", "low", "--files")
        self.ok(self.home, "new", "clash", *new, "lib/util.py")  # needs met, but one holds its files
        self.ok(self.home, "new", "leaf", *new, "docs.md")
        self.ok(self.home, "new", "root", *new, "app.py")
        self.ok(self.home, "new", "tail", *new, "tail.py", "--needs", "root")  # waits on root
        self.ok(self.home, "new", "parked", *new, "parked.py")
        self.ok(self.home, "set", "parked", "status=blocked", "reason=owner")
        out = self.ok(self.home, "status").out
        self.assertIn("  launchable now: root, leaf\n  held by file sets: one holds clash\n", out)
        self.assertIn("  hunt list: 0 open (.campaign/demo/hunt.md)\n", out)  # init wrote it
        o = json.loads(self.ok(self.home, "status", "--json").out)["overview"]
        self.assertEqual((o["launchable"], o["held"]), (["root", "leaf"], {"clash": ["one"]}))
        brief = self.ok(self.home, "brief").out
        self.assertIn("clash planned low\n  - files may overlap one: start once it lands, or narrow files\n", brief)
        self.assertIn("root planned low\n  ready to start: `campaign start root`\n", brief)
        self.fails(self.home, "start", "clash", match="overlap one")  # the same rule start enforces
        hunt = self.home / ".campaign/demo/hunt.md"
        rows = "| slow merge | job J1 row 3 | lib/merge.py | . | open |\n| x | y | z | . | closed: fixed |\n"
        hunt.write_text(hunt.read_text() + rows)
        self.assertIn("  hunt list: 1 open", self.ok(self.home, "status").out)

    def test_landing_refuses_changes_outside_the_file_set(self) -> None:
        wt = self.started("fix", files="app.py", check="unit=true")
        self.commit(wt, {"app.py": "print(2)\n", "lib/util.py": "X = 2\n"})
        self.verified("fix")
        self.fails(self.home, "land", "fix", match="outside its file set")

    def test_landing_refuses_edits_to_the_leads_files(self) -> None:
        wt = self.started("fix", files="app.py", check="unit=true")
        self.commit(wt, {".campaign/demo/quests/fix.md": "weakened criteria\n"})
        self.verified("fix")
        self.fails(self.home, "land", "fix", match="lead's files")
        self.assertIn("lead's files", self.ok(self.home, "check").out)

    def test_check_reports_a_task_writing_outside_its_files(self) -> None:
        self.started("fix", files="app.py,lib/**")
        self.ok(self.home, "start", "fix", "--task", "lib", "--files", "lib/**")
        self.commit(self.wt("fix--lib"), {"app.py": "print('not this task's')\n"})
        self.assertIn("fix--lib: changes files outside its scope: app.py", self.ok(self.home, "check").out)

    def test_check_reports_two_branches_changing_one_file(self) -> None:
        a = self.started("a", files="app.py")
        self.ok(self.home, "new", "b", "--title", "b", "--risk", "low", "--files", "app.py")
        self.ok(self.home, "start", "b", "--force", "owner asked for both")
        self.commit(a, {"app.py": "print('a')\n"})
        self.commit(self.wt("b"), {"app.py": "print('b')\n"})
        self.assertIn("a and b both change app.py", self.ok(self.home, "check").out)


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
        self.fails(self.home, "land", "fix", match="criteria without evidence in a journal's Evidence table at")
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
        self.commit(wt, {journal: f"## Evidence\n\n| A1 | {probe} |\n"})
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
        wt = self.started("fast", files="app.py,lib/**,docs/**,CMakeLists.txt", check="unit=python3 app.py")
        self.fails(self.home, "set", "fast", "--inputs", "bench=lib/**", match="has no check bench")
        self.ok(self.home, "set", "fast", "--check", "unit=python3 app.py", "--check", "bench=test -f lib/util.py")
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

    def test_inputs_never_replace_the_whole_tree_build_test(self) -> None:
        self.ok(self.home, "new", "solo", "--title", "solo", "--check", "unit=true")
        self.fails(self.home, "set", "solo", "--inputs", "unit=lib/**", "--weaken", "x", match="keep one without them")
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
        self.verified("fix")
        head = self.git(wt, "rev-parse", "HEAD")
        self.ok(self.home, "land", "fix")
        self.assertEqual(self.quest("fix")["status"], "landed")
        record = self.quest("fix")["landed"][0]
        self.assertEqual(record["tested"], head)
        self.assertEqual(record["evidence"][0]["state"], "done")

    def test_status_counts_failures_only_at_the_code_the_quest_has_now(self) -> None:
        self.keep_criteria = True
        wt = self.started("fix", files="app.py")
        self.commit(wt, {"app.py": "print(2)\n"})
        self.verified("fix")
        failed = self.submit(self.home, "--quest", "fix", "--", "exit 1")
        self.run_cli(self.home, "wait", failed, "--timeout", "60")
        out = self.ok(self.home, "status").out
        # the check passed, the probe did not, and no journal mentions it yet
        self.assertIn("jobs at this code: 1 passed, 1 failed; not in a journal yet (`campaign job log ID`):", out)
        self.assertIn(f"    {failed}  exit 1 at ", out)  # which job and what it ran
        self.assertNotIn("criterion with evidence", out)  # the template's <criterion> is not one
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
        self.assertNotIn("failed", self.ok(self.home, "status").out)  # history: `campaign job list`

    def test_status_shows_progress_from_the_criteria_and_the_journal(self) -> None:
        wt = self.started("fix", files="app.py")
        brief = self.home / ".campaign/demo/quests/fix.md"
        brief.write_text(
            "# fix\n\n## Acceptance criteria\n\n- A1 [manual] one: a test\n- A2 [manual] two: a test\n- A3 three: a test\n"
        )
        self.git(self.home, "commit", "-qam", "criteria")
        out = self.ok(self.home, "status").out  # the template's placeholder row lists nothing
        self.assertIn("0% 0 of 3 criteria with evidence\n", out)
        self.assertIn("in no journal's Evidence table yet: A1-A3", out)
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
        self.assertNotIn("in no journal's Evidence table", out)
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
        self.fails(self.home, "land", "b", match="rebase onto main")
        self.git(b, "rebase", "-q", "main")
        self.fails(self.home, "land", "b", match="not green")
        self.verified("b")
        self.ok(self.home, "land", "b")

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
        conclusion.write_text("success")
        later = self.run_cli(self.home, "land", "san")
        self.assertNotIn("check asan", later.out + later.err)

    def test_ci_at_the_same_code_speaks_for_a_journal_commit(self) -> None:
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


class Reviews(CampaignCase):
    def normal(self) -> str:
        wt = self.started("feat", files="app.py", risk="normal")
        self.commit(wt, {"app.py": "print('feature')\n"})
        self.verified("feat")
        return self.git(wt, "rev-parse", "HEAD")

    def test_normal_risk_needs_an_accepted_review(self) -> None:
        self.normal()
        self.fails(self.home, "land", "feat", match="correctness review not accepted")
        self.fails(self.home, "accept", "feat", "correctness", match="no review")
        self.review("feat", "correctness", "APPROVE")
        self.ok(self.home, "accept", "feat", "correctness", "--note", "read the error paths twice")
        self.assertEqual(self.quest("feat")["accepted"]["correctness"]["note"], "read the error paths twice")
        self.ok(self.home, "land", "feat")

    def test_changes_need_confirming_jobs_and_reject_cannot_be_accepted(self) -> None:
        self.normal()
        self.review("feat", "correctness", "CHANGES", findings=(("C1", "yes", "defect"),))
        self.fails(self.home, "accept", "feat", "correctness", match="fix them, or the reviewer withdraws them")
        self.commit(self.wt("feat"), {"app.py": "print('feature, fixed')\n"})
        self.fails(self.home, "accept", "feat", "correctness", match="--confirm")
        jid = self.submit(self.wt("feat"), "--quest", "feat", "--", "python3 app.py")
        self.ok(self.home, "wait", jid)
        self.ok(self.home, "accept", "feat", "correctness", "--confirm", f"C1={jid}")
        self.assertEqual(self.quest("feat")["accepted"]["correctness"]["findings"]["C1"]["jobs"][0]["job"], jid)
        self.review("feat", "correctness", "REJECT")
        self.fails(self.home, "accept", "feat", "correctness", match="re-plan")

    def test_waivers_are_recorded_and_refused_for_high_risk(self) -> None:
        self.normal()
        self.ok(self.home, "accept", "feat", "correctness", "--waive", "three-line change with a revert-checked test")
        self.assertEqual(self.quest("feat")["accepted"]["correctness"]["by"], "waived")
        self.ok(self.home, "land", "feat")
        self.started("risky", files="lib/**", risk="high")
        self.fails(self.home, "accept", "risky", "correctness", "--waive", "trust me", match="cannot be waived")

    def test_changes_after_acceptance_void_it(self) -> None:
        self.normal()
        self.review("feat", "correctness", "APPROVE")
        self.ok(self.home, "accept", "feat", "correctness")
        self.commit(self.wt("feat"), {"app.py": "print('sneaky')\n"})
        self.verified("feat")
        self.fails(self.home, "land", "feat", match="changed since")

    def test_main_moving_elsewhere_asks_for_a_rebase_not_a_new_review(self) -> None:
        self.normal()
        self.review("feat", "correctness", "APPROVE")
        self.ok(self.home, "accept", "feat", "correctness")
        self.commit(self.home, {"notes.md": "a handoff note on main\n"})
        status = self.ok(self.home, "status", "feat").out
        self.assertIn("rebase onto main", status)
        self.assertNotIn("changed since", status)  # the reviewed code did not change
        self.started("other", files="lib.py", risk="normal")
        self.assertIn("not reviewed yet; launch a correctness reviewer", self.ok(self.home, "status", "other").out)
        self.review("other", "correctness", "CHANGES")
        status = self.ok(self.home, "status", "other").out
        self.assertIn("correctness review not accepted: verdict CHANGES; fix what blocks", status)

    def test_a_changed_brief_voids_acceptance(self) -> None:
        self.normal()
        self.review("feat", "correctness", "APPROVE")
        self.ok(self.home, "accept", "feat", "correctness")
        self.commit(self.home, {".campaign/demo/quests/feat.md": "new criteria\n"})
        self.fails(self.home, "land", "feat", match="quest file changed")

    def test_upstream_code_changes_void_high_risk_acceptance_only(self) -> None:
        wt = self.started("dur", files="lib/**", risk="high", check="unit=true")
        self.commit(wt, {"lib/util.py": "X = 3\n"})
        self.review("dur", "correctness", "APPROVE")
        self.ok(self.home, "accept", "dur", "correctness")
        other = self.started("other", files="app.py", check="unit=true")
        self.commit(other, {"app.py": "print('other')\n"})
        self.verified("other")
        self.ok(self.home, "land", "other")
        self.git(wt, "rebase", "-q", "main")
        self.verified("dur")
        self.fails(self.home, "land", "dur", match="re-accept")
        self.ok(self.home, "accept", "dur", "correctness")  # the lead read the upstream diff
        self.ok(self.home, "land", "dur")

    def test_review_counts_are_derived_from_review_files(self) -> None:
        self.normal()
        folder = self.home / ".campaign/demo/reviews"
        folder.mkdir(parents=True)
        (folder / "feat-correctness.md").write_text(
            "Verdict: CHANGES\n\n| ID | Blocking | Class | Finding | Evidence | Fix |\n|---|---|---|---|---|---|\n"
            "| C1 | yes | defect | leak | probe | free |\n| C2 | no | nit | name | - | - |\n"
        )
        data = json.loads(self.ok(self.home, "status", "--json", "--reviews").out)
        self.assertEqual(data["reviews"][0]["blocking_defects"], 1)
        self.assertEqual(data["reviews"][0]["findings"], 2)


class ReviewBinding(CampaignCase):
    """An acceptance covers the code its review saw and the review's own content; after CHANGES, each
    blocking finding is confirmed by jobs that passed at the fixed code."""

    def feature(self) -> tuple[Path, str]:
        wt = self.started("feat", files="app.py,tests/**", risk="normal")
        self.commit(wt, {"app.py": "print('feature')\n"})
        self.verified("feat")
        return wt, self.git(wt, "rev-parse", "HEAD")

    def probe(self, wt: Path, cmd: str = "python3 app.py", commit: str = "") -> str:
        jid = self.submit(wt, "--quest", "feat", *(["--commit", commit] if commit else []), "--", cmd)
        self.run_cli(self.home, "wait", jid, "--timeout", "60")
        return jid

    def test_an_approval_covers_only_the_commit_it_reviewed(self) -> None:
        wt, reviewed = self.feature()
        self.review("feat", "correctness", "APPROVE")
        self.commit(wt, {"app.py": "print('unreviewed')\n"})
        self.verified("feat")
        self.fails(self.home, "accept", "feat", "correctness", match=f"the review approved {reviewed[:12]}")
        self.ok(self.home, "accept", "feat", "correctness", "--commit", reviewed)
        self.fails(self.home, "land", "feat", match="changed since")

    def test_a_review_may_name_its_commit_with_the_repository(self) -> None:
        _, reviewed = self.feature()
        self.review("feat", "correctness", "APPROVE", commit=f"app:{reviewed}")
        self.ok(self.home, "accept", "feat", "correctness")
        self.assertEqual(self.quest("feat")["accepted"]["correctness"]["reviewed"], reviewed)

    def test_metadata_commits_after_a_review_keep_it(self) -> None:
        wt, _ = self.feature()
        self.review("feat", "correctness", "APPROVE")
        self.commit(wt, {".campaign/demo/journal/feat.md": "State: ready for review\n"})
        self.ok(self.home, "accept", "feat", "correctness")
        self.commit(wt, {".campaign/demo/journal/feat.md": "State: landing\n"})
        self.ok(self.home, "land", "feat")

    def test_a_review_edited_after_acceptance_voids_it(self) -> None:
        self.feature()
        self.review("feat", "correctness", "APPROVE")
        review = self.home / ".campaign/demo/reviews/feat-correctness.md"
        review.write_text(review.read_text() + "\nThe lead's disposition: fine.\n")
        self.ok(self.home, "accept", "feat", "correctness")  # commits the review with its acceptance
        self.assertEqual(self.git(self.home, "status", "--porcelain", "--", str(review)), "")
        accepted = self.quest("feat")["accepted"]["correctness"]["review"]
        self.assertEqual(accepted, self.git(self.home, "rev-parse", f"HEAD:{review.relative_to(self.home)}"))
        review.write_text(
            review.read_text().replace("Verdict: APPROVE", "Verdict: CHANGES")
            + "| C1 | yes | defect | leak | p | f |\n"
        )
        self.git(self.home, "commit", "-qam", "the reviewer changed its ruling")
        self.fails(self.home, "land", "feat", match="correctness review changed after it was accepted")

    def test_an_acceptance_without_review_content_reads_as_void(self) -> None:
        self.feature()
        self.review("feat", "correctness", "CHANGES", findings=(("C1", "yes", "gap"),))
        state = self.home / ".campaign/demo/state.json"
        data = json.loads(state.read_text())
        head = self.git(self.home, "rev-parse", "quest/demo/feat")
        data["quests"]["feat"]["accepted"] = {"correctness": {"by": "review", "commit": head, "at": "2026-11-06"}}
        state.write_text(json.dumps(data, indent=2))
        self.git(self.home, "commit", "-q", "-am", "an earlier tool's acceptance")
        (view,) = json.loads(self.ok(self.home, "status", "feat", "--json").out)["quests"]
        for out in (view["reviews"]["correctness"], self.ok(self.home, "map").out):
            self.assertIn("so it is void: accept again", out)
            self.assertNotIn("accepted at", out)

    def test_an_acceptance_names_the_review_it_read(self) -> None:
        """A review rewritten while `accept` runs is not accepted under the verdict read before it."""
        self.feature()
        self.review("feat", "correctness", "APPROVE")
        path = self.home / ".campaign/demo/reviews/feat-correctness.md"
        campaign = campaign_module()
        parse = campaign.read_review

        def read_then_rewrite(text: str) -> Any:
            parsed = parse(text)
            path.write_text(text.replace("Verdict: APPROVE", "Verdict: REJECT"))
            return parsed

        cwd = os.getcwd()
        try:
            os.chdir(self.home)
            with (
                mock.patch.dict(os.environ, self.env, clear=True),
                mock.patch.object(campaign, "read_review", read_then_rewrite),
            ):
                self.assertEqual(campaign.main(["accept", "feat", "correctness"]), 0)
        finally:
            os.chdir(cwd)
        self.fails(self.home, "land", "feat", match="correctness review changed after it was accepted")

    def test_a_waiver_never_overrides_a_review(self) -> None:
        self.feature()
        self.ok(self.home, "accept", "feat", "correctness", "--waive", "a one-line change")
        self.review("feat", "correctness", "CHANGES", findings=(("C1", "yes", "defect"),))
        self.fails(self.home, "land", "feat", match="correctness review changed after it was accepted")
        self.fails(self.home, "accept", "feat", "correctness", "--waive", "small", match="a review exists")

    def test_a_verdict_agrees_with_its_findings(self) -> None:
        self.feature()
        self.review("feat", "correctness", "APPROVE", findings=(("C1", "yes", "defect"),))
        self.fails(self.home, "accept", "feat", "correctness", match="APPROVE but lists blocking findings C1")
        self.review("feat", "correctness", "CHANGES", findings=(("C1", "no", "nit"),))
        self.fails(self.home, "accept", "feat", "correctness", match="CHANGES but lists no blocking finding")
        for conditional in ("APPROVE WITH CONDITIONS", "**APPROVE WITH REQUIRED CHANGES.**"):
            self.review("feat", "correctness", conditional)
            self.fails(self.home, "accept", "feat", "correctness", match="a conditional approval is CHANGES")
        self.review("feat", "correctness", "**APPROVE.**")
        self.ok(self.home, "accept", "feat", "correctness")

    def test_an_appended_recheck_is_the_verdict(self) -> None:
        self.feature()
        reviewed = self.review("feat", "correctness", "CHANGES", findings=(("C1", "yes", "defect"),))
        path = self.home / ".campaign/demo/reviews/feat-correctness.md"
        path.write_text(path.read_text() + f"\n## Re-check\n\nCommit: {reviewed}\nVerdict: APPROVE\n\nC1: closed.\n")
        self.ok(self.home, "accept", "feat", "correctness")
        self.assertEqual(self.quest("feat")["accepted"]["correctness"]["reviewed"], reviewed)

    def test_only_jobs_that_passed_at_the_fixed_code_confirm_each_finding(self) -> None:
        wt, reviewed = self.feature()
        self.review("feat", "correctness", "CHANGES", findings=(("C1", "yes", "defect"), ("C2", "yes", "gap")))
        self.commit(wt, {"app.py": "print('feature, fixed')\n"})
        self.verified("feat")
        other = self.repo("other", {"x.py": "print(1)\n"})
        unrelated = self.submit(other, "--sync", "--", "true")
        old = self.probe(wt, "python3 app.py && true", commit=reviewed)  # passed, but only before the fix
        failed = self.probe(wt, "exit 1")
        good = self.probe(wt)
        for jid, why in ((unrelated, "not the quest's repository"), (old, "ran other code"), (failed, "failed")):
            self.fails(
                self.home,
                "accept",
                "feat",
                "correctness",
                "--confirm",
                f"C1={jid}",
                "--confirm",
                f"C2={good}",
                match=why,
            )
        self.fails(self.home, "accept", "feat", "correctness", "--confirm", f"C1={good}", match="C2 has no confirming")
        self.fails(self.home, "accept", "feat", "correctness", "--confirm", f"C9={good}", match="no finding C9")
        self.ok(self.home, "accept", "feat", "correctness", "--confirm", f"C1={good}", "--confirm", f"C2={good}")
        record = self.quest("feat")["accepted"]["correctness"]
        self.assertEqual(record["reviewed"], reviewed)
        self.assertEqual(
            {k: [j["job"] for j in v["jobs"]] for k, v in record["findings"].items()}, {"C1": [good], "C2": [good]}
        )
        self.assertFalse(record["findings"]["C1"]["reproduced"])  # no failing run of its probe before the fix
        self.ok(self.home, "land", "feat")

    def test_a_confirmation_records_the_failure_it_reproduced(self) -> None:
        wt, _ = self.feature()
        probe = self.probe(wt, "python3 app.py | grep -q fixed")  # the reviewer's probe fails at the reviewed code
        self.review("feat", "correctness", "CHANGES", findings=(("C1", "yes", "defect"),))
        self.commit(wt, {"app.py": "print('feature, fixed')\n"})
        self.verified("feat")
        rerun = self.ok(self.home, "job", "rerun", probe).out.split(":")[0]
        self.ok(self.home, "wait", rerun)
        self.ok(self.home, "accept", "feat", "correctness", "--confirm", f"C1={rerun}")
        self.assertTrue(self.quest("feat")["accepted"]["correctness"]["findings"]["C1"]["reproduced"])

    def test_a_fix_beyond_the_reviewed_files_needs_a_review_or_a_reason(self) -> None:
        wt, _ = self.feature()
        self.review("feat", "correctness", "CHANGES", findings=(("C1", "yes", "defect"),))
        self.commit(wt, {"app.py": "print('fixed')\n", "tests/test_app.py": "assert True\n"})
        self.verified("feat")
        good = self.probe(wt)
        self.fails(
            self.home, "accept", "feat", "correctness", "--confirm", f"C1={good}", match="tests/test_app.py, which the"
        )
        why = "tests/test_app.py is C1's regression test"
        self.ok(self.home, "accept", "feat", "correctness", "--confirm", f"C1={good}", "--fix-scope", why)
        self.assertEqual(
            self.quest("feat")["accepted"]["correctness"]["fix_scope"], {"paths": ["tests/test_app.py"], "reason": why}
        )
        self.review("feat", "correctness", "APPROVE")  # or the reviewer reviews the fix at its commit
        self.ok(self.home, "accept", "feat", "correctness")
        self.ok(self.home, "land", "feat")


class RiskPolicy(CampaignCase):
    """What a quest must pass is never weakened silently: lowering its risk, removing a lens, or removing,
    replacing or deferring a check needs --weaken REASON, is recorded, and voids its acceptances."""

    def test_a_downgrade_after_review_needs_a_reason_and_voids_acceptance(self) -> None:
        wt = self.started("dur", files="lib/**", risk="high", check="unit=true", lenses="durability,correctness")
        self.commit(wt, {"lib/util.py": "X = 3\n"})
        self.verified("dur")
        for lens in ("durability", "correctness"):
            self.review("dur", lens, "APPROVE")
            self.ok(self.home, "accept", "dur", lens)
        self.fails(self.home, "set", "dur", "risk=normal", match="--weaken")
        self.fails(self.home, "set", "dur", "lenses=correctness", match="lens durability removed")
        self.assertEqual(self.quest("dur")["risk"], "high")  # refused without writing
        why = "durability moved to the wal quest"
        self.ok(self.home, "set", "dur", "risk=normal", "lenses=correctness", "--weaken", why)
        q = self.quest("dur")
        self.assertEqual(q["accepted"], {})
        self.assertEqual(q["history"][-1]["reason"], why)
        self.assertIn("risk high -> normal", q["history"][-1]["weakened"])
        self.assertIn(why, self.ok(self.home, "status", "dur").out)
        self.fails(self.home, "land", "dur", match="correctness review not accepted")

    def test_the_review_a_risk_needs_cannot_be_removed(self) -> None:
        self.fails(
            self.home, "new", "x", "--title", "x", "--risk", "normal", "--lenses", "", match="needs a code review"
        )
        self.fails(
            self.home, "new", "y", "--title", "y", "--risk", "critical", "--lenses", "correctness", match="design"
        )
        self.started("z", files="app.py", risk="normal", check="unit=true")
        self.fails(self.home, "set", "z", "lenses=design", "--weaken", "why", match="needs a code review")
        state = self.home / ".campaign/demo/state.json"
        data = json.loads(state.read_text())
        data["quests"]["z"]["lenses"] = []  # a hand edit
        state.write_text(json.dumps(data))
        self.git(self.home, "commit", "-qam", "hand edit")
        self.commit(self.wt("z"), {"app.py": "print(2)\n"})
        self.verified("z")
        self.fails(self.home, "land", "z", match="needs a code review")

    def test_a_failing_gate_is_neither_replaced_nor_deferred_silently(self) -> None:
        wt = self.started("fix", files="app.py", check="unit=python3 app.py")
        self.commit(wt, {"app.py": "raise SystemExit(3)\n"})
        self.ok(self.home, "verify", "fix")
        self.run_cli(self.home, "wait", "--quest", "fix", "--timeout", "60")
        self.fails(self.home, "set", "fix", "--check", "unit=true", match="gate check unit changed")
        self.fails(
            self.home, "set", "fix", "--check", "other=true", "--later", "unit=python3 app.py", match="unit deferred"
        )
        self.fails(self.home, "land", "fix", match="not green")
        self.ok(self.home, "set", "fix", "--check", "unit=true", "--weaken", "the unit check was the wrong command")
        self.assertEqual(self.quest("fix")["history"][-1]["weakened"], ["gate check unit changed"])

    def test_strengthening_needs_no_reason(self) -> None:
        self.started("fix", files="app.py", risk="normal", check="unit=true")
        self.ok(self.home, "set", "fix", "risk=critical")
        self.assertEqual(sorted(self.quest("fix")["lenses"]), ["correctness", "design"])
        self.ok(self.home, "set", "fix", "--check", "unit=true", "--check", "lint=true")
        self.ok(self.home, "set", "fix", "lenses=correctness,design,security")


class Increments(CampaignCase):
    def test_a_safe_prefix_lands_before_the_quest(self) -> None:
        wt = self.started("big", files="lib/**,app.py", risk="normal", check="unit=true")
        first = self.commit(wt, {"lib/util.py": "X = 5\n"}, "refactor")
        self.commit(wt, {"app.py": "print('feature')\n"}, "feature")
        self.ok(self.home, "verify", "big", "--commit", first)
        self.ok(self.home, "wait", "--quest", "big", "--timeout", "60")
        self.fails(self.home, "land", "big", "--upto", first, match="--reason")
        why = "behavior-preserving refactor"
        self.fails(self.home, "land", "big", "--upto", first, "--reason", why, match="May land early")
        brief = self.home / ".campaign/demo/quests/big.md"
        text = brief.read_text()
        start = text.index("## May land early")
        brief.write_text(text[:start] + "## May land early\n\nThe lib/ refactor.\n")
        self.git(self.home, "commit", "-qam", "may land early")
        self.verified("big")
        self.fails(self.home, "land", "big", "--upto", "quest/demo/big", "--reason", why, match="the whole quest")
        self.ok(self.home, "land", "big", "--upto", first, "--reason", why)
        p = self.quest("big")
        self.assertEqual(p["status"], "active")
        self.assertFalse(p["landed"][0]["final"])
        self.assertTrue(self.git(self.home, "merge-base", "--is-ancestor", first, "main") == "")


class DirectPath(CampaignCase):
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
            "ci=\nci_later=\npeers=\noutbox=\ndisk_warn=100\nslot_days=7\ntested_docs=\n",
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


class Campaigns(CampaignCase):
    """Several campaigns on one machine, sharing a library."""

    def setUp(self) -> None:
        super().setUp()
        self.lib = self.repo("lib", {"lib.py": "X = 1\n"})
        self.other = self.repo("other", {"o.py": "print(1)\n"})
        self.ok(self.other, "init", "--name", "other")

    def lib_quest(self, home: Path, pid: str, *extra: str) -> Path:
        self.ok(home, "new", pid, "--title", pid, "--risk", "low", "--repo", "../lib", "--check", "unit=true", *extra)
        self.ok(home, "start", pid)
        return self.root / "lib-wt" / ("demo" if home == self.home else "other") / pid

    def landed_from(self, home: Path, pid: str) -> None:
        self.ok(home, "verify", pid)
        self.ok(home, "wait", "--quest", pid, "--timeout", "90", timeout=120)
        self.ok(home, "land", pid)

    def test_a_rerun_moves_the_ref_that_holds_the_quests_code(self) -> None:
        """A job in another repository that names the quest's code by --ref reruns with that ref at the
        quest's head and its own commit unchanged."""
        wt = self.lib_quest(self.home, "pin")
        first = self.git(wt, "rev-parse", "HEAD")
        own = self.git(self.home, "rev-parse", "HEAD")
        jid = self.submit(
            self.home,
            "--quest",
            "pin",
            "--repo",
            str(self.home),
            "--commit",
            own,
            "--ref",
            f"lib={self.lib}@{first}",
            "--",
            'cat "$JOB_REF_LIB/lib.py" > "$JOB_OUT/seen"',
        )
        self.ok(self.home, "wait", jid)
        head = self.commit(wt, {"lib.py": "X = 2\n"})
        out = self.ok(self.home, "job", "rerun", jid).out
        new = out.split()[0].rstrip(":")
        self.ok(self.home, "wait", new)
        spec = json.loads((self.root / "jobs" / new / "spec.json").read_text())
        self.assertEqual((spec["commit"], spec["refs"]["lib"]["commit"]), (own, head))
        self.assertEqual((self.root / "jobs" / new / "a1/out/seen").read_text(), "X = 2\n")

    def test_campaigns_share_the_light_slots(self) -> None:
        """A campaign that queued first does not take every slot from one that queued after it."""
        self.env["CAMPAIGN_LIGHT_JOBS"] = "2"
        # long enough that "theirs" is queued before ours free a slot, even when submits are slow
        ours = [self.submit(self.home, "--level", "light", "--force", "--", "sleep 15") for _ in range(4)]
        theirs = self.submit(self.other, "--level", "light", "--", "true")
        self.ok(self.other, "wait", theirs, "--timeout", "60", timeout=90)
        self.ok(self.home, "wait", ours[3], "--timeout", "60", timeout=90)
        self.assertLess(self.job(theirs)["started"], self.job(ours[3])["started"], "the later campaign waited out ours")

    def test_a_draining_job_waits_for_queued_light_work(self) -> None:
        """An exclusive hold blocks new light holds while it waits, so it starts after queued light work."""
        self.env["CAMPAIGN_LIGHT_JOBS"] = "1"
        ours = self.submit(self.home, "--level", "light", "--force", "--", "sleep 4")
        theirs = self.submit(self.other, "--level", "light", "--", "true")
        ab = self.submit(self.home, "--level", "exclusive", "--minutes", "1", "--", "true")
        self.ok(self.home, "wait", ours, ab, "--timeout", "60", timeout=90)
        self.ok(self.other, "wait", theirs, "--timeout", "60", timeout=90)
        self.assertLessEqual(self.job(theirs)["started"], self.job(ab)["started"], "the A/B drained the queue")

    def test_an_overdue_drain_starts_no_new_light_job(self) -> None:
        """Past its wait, a draining job keeps new light jobs out, so only the running ones delay it."""
        self.env.update(CAMPAIGN_LIGHT_JOBS="2", CAMPAIGN_DRAIN_WAIT="0", CAMPAIGN_DRAIN_SLACK="0")
        ours = self.submit(self.home, "--level", "light", "--force", "--", "sleep 4")
        self.wait_until(lambda: self.job(ours)["state"] == "running")
        ab = self.submit(self.home, "--level", "exclusive", "--minutes", "1", "--", "true")
        theirs = self.submit(self.other, "--level", "light", "--", "true")
        self.ok(self.home, "wait", ours, ab, "--timeout", "60", timeout=90)
        self.ok(self.other, "wait", theirs, "--timeout", "60", timeout=90)
        self.assertLessEqual(self.job(ab)["started"], self.job(theirs)["started"], "light work lengthened the drain")

    def test_drains_and_light_work_take_turns(self) -> None:
        """With no drain turn, queued light work runs after a drain before the next drain's wait is up."""
        self.env.update(CAMPAIGN_LIGHT_JOBS="1", CAMPAIGN_DRAIN_WAIT="5", CAMPAIGN_DRAIN_TURN="0")
        ours = self.submit(self.home, "--level", "light", "--force", "--", "sleep 8")
        self.wait_until(lambda: self.job(ours)["state"] == "running")
        first = self.submit(self.home, "--level", "exclusive", "--minutes", "1", "--", "true")
        second = self.submit(self.home, "--level", "exclusive", "--minutes", "1", "--force", "--", "true")
        theirs = self.submit(self.other, "--level", "light", "--", "true")
        self.ok(self.home, "wait", ours, first, second, "--timeout", "90", timeout=120)
        self.ok(self.other, "wait", theirs, "--timeout", "90", timeout=120)
        self.assertLess(self.job(first)["started"], self.job(theirs)["started"], "the overdue drain went first")
        self.assertLess(self.job(theirs)["started"], self.job(second)["started"], "light work got its turn")

    def test_an_overdue_drain_opens_a_turn_for_the_queued_drains(self) -> None:
        """An overdue drain opens a turn: the drains queued behind it run before light work resumes."""
        self.env.update(CAMPAIGN_LIGHT_JOBS="1", CAMPAIGN_DRAIN_WAIT="5", CAMPAIGN_DRAIN_TURN="60")
        ours = self.submit(self.home, "--level", "light", "--force", "--", "sleep 8")
        self.wait_until(lambda: self.job(ours)["state"] == "running")
        first = self.submit(self.home, "--level", "exclusive", "--minutes", "1", "--", "true")
        second = self.submit(self.home, "--level", "exclusive", "--minutes", "1", "--force", "--", "true")
        theirs = self.submit(self.other, "--level", "light", "--", "true")
        self.ok(self.home, "wait", ours, first, second, "--timeout", "90", timeout=120)
        self.ok(self.other, "wait", theirs, "--timeout", "90", timeout=120)
        self.assertLess(self.job(second)["started"], self.job(theirs)["started"], "light work cut into the turn")
        self.assertIn("window open until", self.ok(self.home, "window").out)

    def test_direct_path_work_that_waited_a_turn_ends_it_after_the_running_drain(self) -> None:
        """A priority-1 light job queued longer than a drain turn ends the turn once the running drain
        ends, so it runs before the drains queued behind; it never starts beside a drain."""
        self.env.update(CAMPAIGN_LIGHT_JOBS="1", CAMPAIGN_DRAIN_WAIT="8", CAMPAIGN_DRAIN_TURN="6")
        go = self.root / "go"  # ours holds the only light slot until the turn opens, however slow submits are
        ours = self.submit(self.home, "--level", "light", "--force", "--", f"while [ ! -e {go} ]; do sleep 0.1; done")
        self.wait_until(lambda: self.job(ours)["state"] == "running")
        gate = self.submit(self.other, "--level", "light", "--priority", "1", "--", "true")
        first = self.submit(self.home, "--level", "exclusive", "--minutes", "1", "--", "sleep 3")
        second = self.submit(self.home, "--level", "exclusive", "--minutes", "1", "--force", "--", "sleep 3")
        self.wait_until((self.root / "jobs" / ".window").exists)
        go.touch()
        self.ok(self.home, "wait", ours, first, second, "--timeout", "120", timeout=150)
        self.ok(self.other, "wait", gate, "--timeout", "120", timeout=150)
        self.assertLess(self.job(first)["started"], self.job(gate)["started"], "the running turn went first")
        self.assertGreaterEqual(self.job(gate)["started"], self.job(first)["ended"], "light work beside a drain")
        self.assertLess(self.job(gate)["started"], self.job(second)["started"], "the turn held direct-path work")

    def test_a_window_runs_drains_back_to_back(self) -> None:
        """Inside a window, queued drains go before light work; without one, light work goes first."""
        self.env["CAMPAIGN_LIGHT_JOBS"] = "1"
        self.assertIn("window open until", self.ok(self.home, "window", "--minutes", "5", "--for", "A9").out)
        first = self.submit(self.home, "--level", "exclusive", "--minutes", "1", "--", "sleep 1")
        second = self.submit(self.home, "--level", "exclusive", "--minutes", "1", "--force", "--", "sleep 1")
        theirs = self.submit(self.other, "--level", "light", "--", "true")
        self.ok(self.home, "wait", first, second, "--timeout", "90", timeout=120)
        self.ok(self.other, "wait", theirs, "--timeout", "90", timeout=120)
        self.assertLess(self.job(second)["started"], self.job(theirs)["started"], "light work cut into the window")
        self.assertIn("window closed", self.ok(self.home, "window", "--close").out)
        self.assertIn("no window open", self.ok(self.home, "window").out)

    def test_a_message_wakes_the_other_campaigns_lead_in_cmux(self) -> None:
        calls = self.root / "cmux-calls"
        fake = self.root / "bin" / "cmux"
        fake.write_text(f'#!/bin/sh\nprintf "%s|" "$@" >> {calls}; echo >> {calls}\n')
        fake.chmod(0o755)
        self.env["CMUX_SURFACE_ID"] = "surface-other"
        self.ok(self.other, "status")  # other's lead is seen in its surface
        self.env["CMUX_SURFACE_ID"] = "surface-demo"
        self.ok(self.home, "status")
        sent = self.ok(self.home, "message", "other:", "journal pins moved", "--from", "demo-lead").out
        self.assertIn("msg-1 for the lead", sent)
        self.assertIn(
            "agent|message|surface-other|--from|demo-lead|--|campaign other: msg-1 for the lead, from demo-lead: "
            "journal pins moved",
            calls.read_text(),
        )
        calls.write_text("")
        self.ok(self.home, "message", "demo:", "a note to myself", "--from", "demo-lead")
        self.assertNotIn("agent|message", calls.read_text(), "the sender's own surface is not woken")
        self.env.pop("CMUX_SURFACE_ID")
        self.ok(self.home, "message", "other:", "from outside cmux", "--from", "owner")
        self.assertIn("agent|message|surface-other|", calls.read_text(), "the recorded lead is woken from anywhere")
        fake.write_text("#!/bin/sh\nexit 1\n")
        self.assertIn("msg-3 for the lead", self.ok(self.home, "message", "other:", "cmux down", "--from", "x").out)

    def test_a_library_quest_works_from_its_own_repository(self) -> None:
        api = self.lib_quest(self.home, "api", "--files", "lib.py")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        self.ok(elsewhere, "message", "demo:api", "the parser rejects tabs", "--from", "explorer")
        self.ok(api, "message", "api", "and spaces", "--from", "me")  # from its worktree in the library
        brief = self.ok(api, "brief").out
        self.assertTrue(brief.startswith("api active low: quest/demo/api at "), brief)
        self.assertIn("  files: lib.py\n", brief)
        self.assertIn("  - msg-1 from explorer: the parser rejects tabs\n", brief)
        self.assertIn("  - msg-2 from me: and spaces\n", brief)
        self.assertEqual(self.ok(api, "get", "api", "repo").out, "../lib\n")
        job = self.submit(api, "--quest", "api", "--", "true")
        self.ok(api, "wait", job)
        self.assertIn(job, self.ok(api, "job", "list").out)
        journal = self.root / "app-wt" / "demo" / "api"  # a library quest keeps its journal in the home
        self.assertEqual(self.ok(self.home, "get", "api", "journal").out, "../app-wt/demo/api\n")
        self.assertEqual(sorted(x.name for x in journal.iterdir() if x.name != ".git"), [".campaign"])
        self.assertIn(job, self.ok(journal, "job", "list").out)  # the journal's worktree finds the campaign too
        self.commit(api, {"lib.py": "X = 2\n", ".campaign/demo/journal/api.md": "in the library\n"})
        self.fails(self.home, "land", "api", match="adds campaign files to lib")
        self.git(api, "reset", "-q", "--hard", "HEAD~1")
        self.commit(api, {"lib.py": "X = 2\n"})
        self.commit(journal, {".campaign/demo/journal/api.md": "msg-1, msg-2: both pass now\n"})
        self.landed_from(self.home, "api")
        self.assertEqual([m["id"] for m in self.quest("api")["messages"]], ["msg-1", "msg-2"])
        self.assertEqual(self.git(self.lib, "ls-tree", "-r", "--name-only", "main", "--", ".campaign"), "")
        self.assertIn("both pass now", self.git(self.home, "show", "main:.campaign/demo/journal/api.md"))
        self.assertFalse(journal.exists(), "the journal's worktree outlived the landing")
        self.assertEqual(self.git(self.home, "branch", "--list", "quest/demo/api"), "")

    def test_a_planned_contract_change_warns_once_it_starts(self) -> None:
        self.ok(
            self.home,
            "new",
            "api",
            "--title",
            "api",
            "--risk",
            "low",
            "--repo",
            "../lib",
            "--files",
            "lib.py",
            "--provides",
            "lib-api",
            "--check",
            "unit=true",
        )
        self.ok(
            self.other,
            "new",
            "use",
            "--title",
            "use",
            "--risk",
            "low",
            "--repo",
            "../lib",
            "--files",
            "use.py",
            "--consumes",
            "lib-api",
            "--check",
            "unit=true",
        )
        self.assertNotIn(
            "changes contract lib-api", self.run_cli(self.home, "check").out, "a planned quest changes nothing"
        )
        self.assertNotIn("is changing in demo:api", self.run_cli(self.other, "check").out)
        self.ok(self.home, "start", "api")
        self.assertIn("api: changes contract lib-api, which other:use consumes", self.run_cli(self.home, "check").out)
        self.assertIn("use: contract lib-api is changing in demo:api", self.run_cli(self.other, "check").out)

    def test_campaigns_see_each_other_and_land_a_shared_library_in_order(self) -> None:
        self.fails(self.repo("third", {"t.py": "1\n"}), "init", "--name", "demo", match="already runs from")
        api = self.lib_quest(self.home, "api", "--files", "lib.py", "--provides", "lib-api")
        self.ok(
            self.other,
            "new",
            "use",
            "--title",
            "use",
            "--risk",
            "low",
            "--repo",
            "../lib",
            "--files",
            "lib.py,use.py",
            "--consumes",
            "lib-api",
            "--after",
            "demo:api",
            "--check",
            "unit=true",
        )
        started = self.ok(self.other, "start", "use")
        self.assertIn("may overlap demo:api", started.err)
        self.assertIn("told campaign demo's lead that use overlaps its work", started.out)
        told = self.ok(self.home, "status").out  # demo's lead hears it from the tool
        self.assertIn("msg-1 for the lead, from campaign other: other:use started and may change files", told)
        self.assertIn("msg-2 for the lead, from campaign other: other:use started and builds against", told)
        mine, theirs = self.run_cli(self.home, "check").out, self.run_cli(self.other, "check").out
        self.assertIn("api: changes contract lib-api, which other:use consumes", mine)
        self.assertIn("api: files may overlap other:use", mine)
        self.assertIn("use: contract lib-api is changing in demo:api", theirs)
        machine = self.ok(self.other, "status", "--all").out
        self.assertIn("demo", machine)
        self.assertIn("in flight: use", machine)
        use = self.root / "lib-wt" / "other" / "use"
        self.commit(use, {"use.py": "from lib import X\n"})
        self.ok(self.other, "verify", "use")
        self.ok(self.other, "wait", "--quest", "use", "--timeout", "90", timeout=120)
        self.fails(self.other, "land", "use", match="demo:api")
        self.commit(api, {"lib.py": "X = 2\n"})
        self.landed_from(self.home, "api")
        self.git(use, "rebase", "-q", "main")
        self.landed_from(self.other, "use")
        self.assertEqual((self.lib / "lib.py").read_text(), "X = 2\n")
        self.assertTrue((self.lib / "use.py").exists())

    def test_a_campaign_registers_again_on_its_next_write(self) -> None:
        (self.root / "campaigns" / "other").unlink()  # one from before the registry, or a wiped ~/.scratch
        self.assertNotIn("other", self.ok(self.home, "status", "--all").out)
        self.ok(self.other, "new", "x", "--title", "x", "--risk", "low")
        self.assertIn("other", self.ok(self.home, "status", "--all").out)

    def test_landing_waits_for_a_repository_another_campaign_is_writing(self) -> None:
        self.env["CAMPAIGN_LOCK_WAIT"] = "1"
        api = self.lib_quest(self.home, "api", "--files", "lib.py")
        self.commit(api, {"lib.py": "X = 2\n"})
        self.ok(self.home, "verify", "api")
        self.ok(self.home, "wait", "--quest", "api", "--timeout", "90", timeout=120)
        with open(self.lib / ".git" / "campaign.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            self.fails(self.home, "land", "api", match="busy")
        self.assertEqual((self.lib / "lib.py").read_text(), "X = 1\n")
        self.ok(self.home, "land", "api")
        self.assertEqual((self.lib / "lib.py").read_text(), "X = 2\n")


class Usage(CampaignCase):
    def codex_log(self, fixture: str, name: str) -> Path:
        source = Path(__file__).parent / "fixtures" / f"codex-{fixture}.jsonl"
        log = self.root / "codex/2026/11/05" / f"rollout-{name}.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(source.read_text().replace("HOME", str(self.home)) + "not json\n")
        return log

    def test_codex_real_shapes_attribute_tokens_without_counting_snapshots_twice(self) -> None:
        self.codex_log("lead", "lead")
        self.codex_log("agent", "agent")
        u = json.loads(self.ok(self.home, "usage", "--json").out)
        (run,) = u["runs"]
        self.assertEqual((run["role"], run["assignment"], run["quest"]), ("contributor", "performance", "fix"))
        self.assertEqual(
            (run["model"], run["effort"], run["tokens"], run["seconds"]), ("gpt-6-astra", "high", 1200, 120)
        )
        self.assertEqual((run["cached_input_tokens"], run["output_tokens"], run["cost"]), (800, 200, 0))
        (lead,) = u["lead"]
        self.assertEqual(
            (lead["tokens"], lead["seconds"], lead["model"], lead["effort"]), (550, 180, "gpt-6.1-sol", "medium")
        )
        out = self.ok(self.home, "usage").out
        self.assertIn("Claude Code and Codex record none", out)
        self.assertIn("cost n/a;", out)  # unavailable, never reported as zero
        self.assertNotIn("cost 0", out)

    def test_codex_encrypted_launch_is_unattributed_and_unrelated_leads_are_excluded(self) -> None:
        self.codex_log("agent", "opaque")
        other = self.codex_log("lead", "other")
        events = []
        for line in other.read_text().splitlines():
            if not line.startswith("{"):
                continue
            event = json.loads(line)
            payload = event["payload"]
            if payload.get("type") == "function_call":
                arguments = json.loads(payload["arguments"])
                arguments["message"] = "SCRUBBED_ENCRYPTED_PROMPT"
                payload["arguments"] = json.dumps(arguments)
            events.append(event)
        other.write_text(
            "".join(json.dumps(e) + "\n" for e in events).replace(str(self.home), str(self.root / "elsewhere"))
        )
        u = json.loads(self.ok(self.home, "usage", "--json").out)
        (run,) = u["runs"]
        self.assertEqual((run["role"], run["quest"], run["tokens"]), ("unattributed", "-", 1200))
        # This parent is related by thread id even though its cwd differs.
        self.assertEqual(len(u["lead"]), 1)
        other.write_text(other.read_text().replace('"lead"', '"unrelated"'))
        self.assertEqual(json.loads(self.ok(self.home, "usage", "--json").out)["lead"], [])

    def test_codex_runs_are_attributed_by_task_name_when_the_prompt_is_encrypted(self) -> None:
        self.ok(self.home, "new", "fix", "--title", "fix", "--risk", "low")
        names = (
            "contributor_fix_performance",
            "reviewer_fix_security",
            "fix_security",
            "night_fix_v2",
            "night_security",
        )
        names += ("fixer", "builder")
        for task in names:
            log = self.codex_log("agent", task)  # no parent log: the launch prompt is unreadable
            log.write_text(log.read_text().replace("/root/builder", f"/root/{task}").replace('"builder"', f'"{task}"'))
        u = json.loads(self.ok(self.home, "usage", "--json").out)
        got = {r["task"]: (r["role"], r["quest"], r["assignment"], r["lens"]) for r in u["runs"]}
        self.assertEqual(
            got,
            {
                "contributor_fix_performance": ("contributor", "fix", "performance", ""),
                "reviewer_fix_security": ("reviewer", "fix", "", "security"),
                "fix_security": ("reviewer", "fix", "", "security"),  # a lens marks a reviewer
                "night_fix_v2": ("contributor", "fix", "implementation", ""),  # the quest's id as a whole word
                "night_security": ("reviewer", "-", "", "security"),  # a role, but no quest named
                "fixer": ("unattributed", "-", "", ""),  # part of a word is not guessed at
                "builder": ("unattributed", "-", "", ""),
            },
        )
        out = self.ok(self.home, "usage").out
        self.assertIn("(unattributed)", out)
        self.assertFalse([x for x in out.splitlines() if x.startswith("- ")], out)  # one label for no quest
        self.assertIn("unattributed builder gpt-6-astra high", out)

    def test_codex_legacy_cumulative_snapshots_and_old_logs(self) -> None:
        log = self.codex_log("agent", "fallback")
        lines = [line for line in log.read_text().splitlines() if '"token_usage_record"' not in line]
        log.write_text("\n".join(lines) + "\n{truncated")
        (run,) = json.loads(self.ok(self.home, "usage", "--json").out)["runs"]
        self.assertEqual(run["tokens"], 1200)
        os.utime(log, (0, 0))
        self.assertEqual(json.loads(self.ok(self.home, "usage", "--json").out)["runs"], [])

    def test_codex_model_effort_changes_keep_separate_usage_and_active_time(self) -> None:
        log = self.codex_log("agent", "models")
        events = [json.loads(line) for line in log.read_text().splitlines() if line.startswith("{")]
        events += [
            {
                "type": "event_msg",
                "timestamp": "2026-11-05T12:00:00Z",
                "payload": {"type": "task_started", "turn_id": "next"},
            },
            {"type": "turn_context", "payload": {"turn_id": "next", "model": "gpt-6.1-sol", "effort": "medium"}},
            {
                "type": "token_usage_record",
                "timestamp": "2026-11-05T12:01:00Z",
                "payload": {
                    "turn_id": "next",
                    "response_id": "resp-next",
                    "usage": {"input_tokens": 20, "output_tokens": 5},
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-11-05T12:01:00Z",
                "payload": {"type": "task_complete", "turn_id": "next"},
            },
        ]
        log.write_text("".join(json.dumps(e) + "\n" for e in events))
        runs = json.loads(self.ok(self.home, "usage", "--json").out)["runs"]
        self.assertEqual(
            sorted((r["model"], r["tokens"], r["seconds"]) for r in runs),
            [("gpt-6-astra", 1200, 120), ("gpt-6.1-sol", 25, 60)],
        )

    def copilot_session(self, name: str, launches: list[tuple[str, str, float]], lead_cost: float) -> Path:
        """A Copilot session log in its real event shapes: (prompt, agent id, cost) per launch."""
        events: list[Any] = []
        for n, (prompt, aid, cost) in enumerate(launches):
            call = f"call-{aid}"
            events += [
                {
                    "type": "tool.execution_start",
                    "data": {"toolName": "task", "toolCallId": call, "arguments": {"prompt": prompt}},
                },
                {
                    "type": "subagent.started",
                    "agentId": aid,
                    "timestamp": f"2026-11-05T10:0{n}:00Z",
                    "data": {"toolCallId": call, "agentName": "general-purpose", "model": "claude-opus-5.5"},
                },
                {
                    "type": "subagent.configured",
                    "agentId": aid,
                    "data": {"model": "claude-opus-5.5", "reasoningEffort": "high"},
                },
                {
                    "type": "session.usage_record",
                    "agentId": aid,
                    "data": {
                        "usage": {
                            "cost": cost,
                            "inputTokens": 1000,
                            "outputTokens": 200,
                            "cacheReadTokens": 900,
                            "reasoningTokens": 50,
                        }
                    },
                },
                {
                    "type": "subagent.completed",
                    "agentId": aid,
                    "data": {"toolCallId": call, "durationMs": 120000, "totalToolCalls": 7},
                },
            ]
        events.append(
            {
                "type": "session.usage_record",
                "data": {"usage": {"cost": lead_cost, "inputTokens": 500, "outputTokens": 50}},
            }
        )
        log = self.root / "copilot" / name / "events.jsonl"
        log.parent.mkdir(parents=True)
        log.write_text("".join(json.dumps(e) + "\n" for e in events) + "not json\n")
        return log

    def test_usage_attributes_agent_runs_and_jobs_to_quests_roles_and_classes(self) -> None:
        self.env["CAMPAIGN_COPILOT_LOGS"] = str(self.root / "copilot")
        self.started("fix", files="app.py")
        jid = self.submit(self.home, "--quest", "fix", "--level", "none", "--", "sleep 1")
        self.ok(self.home, "wait", jid, "--timeout", "60")
        home = str(self.home)
        self.copilot_session(
            "s1",
            [
                (
                    f"---\nname: campaign-contributor\n---\nYou are the contributor for quest fix of campaign demo ({home}).\nAssignment: performance; for any",
                    "a1",
                    10,
                ),
                (f"You resume quest fix of campaign demo ({home}) after the previous contributor stopped.", "a2", 4),
                (f"You are the correctness reviewer for quest fix of campaign demo ({home}). You have not", "p1", 6),
                ("You are researching orchestration systems for a campaign skill.", "x1", 99),
                ("You are the contributor for quest fix of campaign other (/elsewhere).", "o1", 50),
            ],
            lead_cost=5,
        )
        self.copilot_session("unrelated", [("Explore the repository.", "u1", 70)], lead_cost=77)
        old = self.copilot_session(
            "before-init", [(f"You are the contributor for quest fix of campaign demo ({home}).", "z1", 33)], 0
        )
        os.utime(old, (0, 0))
        u = json.loads(self.ok(self.home, "usage", "--json").out)
        runs = sorted((r["role"], r["lens"], r["assignment"], r["quest"], r["cost"]) for r in u["runs"])
        self.assertEqual(
            runs,
            [
                ("contributor", "", "implementation", "fix", 4),
                ("contributor", "", "performance", "fix", 10),
                ("reviewer", "correctness", "", "fix", 6),
            ],
        )
        first = next(r for r in u["runs"] if r["agent"] == "a1")
        self.assertEqual(
            (first["effort"], first["tokens"], first["seconds"], first["tool_calls"]), ("high", 1200, 120, 7)
        )
        self.assertEqual([(s["session"], s["cost"]) for s in u["lead"]], [("s1", 5)])
        (job,) = u["jobs"]
        self.assertEqual((job["id"], job["quest"], job["state"]), (jid, "fix", "done"))
        self.assertGreaterEqual(job["ran"], 1)
        self.assertEqual(
            (first["cached_input_tokens"], first["output_tokens"], first["reasoning_tokens"]), (900, 200, 50)
        )
        self.assertEqual([r["agent"] for r in u["runs"] if r["resume"]], ["a2"])
        text = self.ok(self.home, "usage").out
        self.assertIn("recovery: 1 fresh resume, 1,200 tokens", text)
        self.assertIn("contributor performance claude-opus-5.5 high", text)
        self.assertIn("reviewer correctness claude-opus-5.5 high", text)

    def claude_agent(self, session: Path, aid: str, prompt: str, usages: list[tuple[str, int, int, int]]) -> Path:
        """A Claude Code subagent transcript in its real shapes: (message id, cache read, cache write,
        output) per logged block; blocks of one response repeat its message id."""
        lines: list[Any] = [
            {
                "type": "user",
                "agentId": aid,
                "timestamp": at(0),
                "message": {"role": "user", "content": prompt},
            },
            {
                "type": "api-request",
                "timestamp": at(1),
                "params": {"output_config": {"effort": "high"}},
            },
        ]
        for n, (mid, read, write, out) in enumerate(usages):
            usage = {
                "input_tokens": 2,
                "cache_read_input_tokens": read,
                "cache_creation_input_tokens": write,
                "output_tokens": out,
                "output_tokens_details": {"thinking_tokens": 1},
            }
            block = {"type": "tool_use", "name": "Bash"} if n % 2 else {"type": "text", "text": "ok"}
            lines.append(
                {
                    "type": "assistant",
                    "timestamp": at(60 * (n + 1)),
                    "message": {"id": mid, "model": "claude-opus-5-5", "usage": usage, "content": [block]},
                }
            )
        log = session / "subagents" / f"agent-{aid}.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("".join(json.dumps(x) + "\n" for x in lines) + "not json\n")
        log.with_suffix(".meta.json").write_text(json.dumps({"agentType": "campaign-contributor", "model": "opus"}))
        return log

    def test_usage_reads_claude_code_agents_once_per_response(self) -> None:
        self.started("fix", files="app.py")
        home = str(self.home)
        project = self.root / "claude" / "-app"
        project.mkdir(parents=True)
        lead = project / "s1.jsonl"
        lead.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "cwd": home,
                    "timestamp": at(0),
                    "message": {
                        "id": "m0",
                        "model": "claude-opus-5-5",
                        "usage": {"input_tokens": 7, "output_tokens": 3},
                    },
                }
            )
            + "\n"
        )
        prompt = f"You are the contributor for quest fix of campaign demo ({home}).\nAssignment: performance;"
        # m1 is logged twice (two content blocks of one response): counted once.
        self.claude_agent(project / "s1", "a1", prompt, [("m1", 100, 10, 5), ("m1", 100, 10, 5), ("m2", 200, 0, 7)])
        self.claude_agent(
            project / "s1",
            "o1",
            "You are the contributor for quest fix of campaign other (/elsewhere).",
            [("m3", 999, 0, 1)],
        )
        old = self.claude_agent(project / "s1", "z1", prompt, [("m4", 50, 0, 1)])
        os.utime(old, (0, 0))
        u = json.loads(self.ok(self.home, "usage", "--json").out)
        (run,) = u["runs"]
        self.assertEqual(
            (run["harness"], run["agent"], run["role"], run["assignment"], run["quest"], run["model"], run["effort"]),
            ("claude", "a1", "contributor", "performance", "fix", "claude-opus-5-5", "high"),
        )
        self.assertEqual(
            (
                run["tokens"],
                run["cached_input_tokens"],
                run["cache_write_tokens"],
                run["output_tokens"],
                run["requests"],
            ),
            (2 + 100 + 10 + 5 + 2 + 200 + 7, 300, 10, 12, 2),
        )
        self.assertEqual((round(run["seconds"]), run["tool_calls"], run["cost_available"]), (180, 1, False))
        self.assertEqual([(s["harness"], s["session"], s["tokens"]) for s in u["lead"]], [("claude", "s1", 10)])
        self.assertIn("read: claude, copilot, codex", self.ok(self.home, "usage").out)


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


class SharedHome(CampaignCase):
    def test_one_home_holds_two_campaigns_apart(self) -> None:
        self.ok(self.home, "init", "--name", "perf")
        self.fails(self.home, "status", match="pass --campaign NAME")
        for name in ("demo", "perf"):
            self.ok(self.home, "new", "fix", "--title", name, "--risk", "low", "--files", "app.py", "--campaign", name)
            started = self.ok(self.home, "start", "fix", "--campaign", name)
        self.assertIn("may overlap demo:fix", started.err)  # neighbors in one home still see each other
        self.assertEqual(self.quest("fix")["branch"], "quest/demo/fix")
        perf = self.root / "app-wt" / "perf" / "fix"
        self.assertTrue((perf / ".campaign/perf/journal/fix.md").exists())
        # a quest's worktree knows its campaign; the main checkout needs the name
        self.assertIn("fix  active  low  perf", self.ok(perf, "status").out)
        self.env["CAMPAIGN"] = "perf"
        self.assertIn("campaign perf", self.ok(self.home, "status").out)
        self.env.pop("CAMPAIGN")
        # each lead's wait sees only its campaign's events
        for name in ("demo", "perf"):
            self.run_cli(self.home, "wait", "--commits", "--timeout", "1", "--campaign", name)
        self.commit(perf, {"app.py": "print('perf')\n"})
        self.assertEqual(self.run_cli(self.home, "wait", "--commits", "--timeout", "1", "--campaign", "demo").code, 3)
        self.assertIn("quest/perf/fix moved", self.ok(self.home, "wait", "--commits", "--campaign", "perf").out)


class SharedHomeMessages(CampaignCase):
    def test_messages_and_outboxes_stay_apart_in_a_shared_home(self) -> None:
        self.ok(self.home, "init", "--name", "perf")
        for name in ("demo", "perf"):
            self.ok(self.home, "new", "fix", "--title", name, "--risk", "low", "--files", "app.py", "--campaign", name)
            self.ok(self.home, "start", "fix", "--campaign", name)
        demo, perf = self.wt("fix"), self.root / "app-wt" / "perf" / "fix"
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        self.ok(elsewhere, "message", "demo:fix", "for demo", "--from", "a")
        self.ok(elsewhere, "message", "perf:fix", "for perf", "--from", "b")
        self.ok(perf, "message", "fix", "from its own worktree", "--from", "c")  # the worktree knows its campaign
        self.fails(self.home, "message", "fix", "which?", match="pass --campaign NAME or set CAMPAIGN; or address")
        self.assertIn("msg-1 from a: for demo", self.ok(demo, "brief").out)
        self.assertNotIn("for perf", self.ok(demo, "brief").out)
        seen = self.ok(perf, "brief").out
        self.assertIn("msg-1 from b: for perf", seen)
        self.assertIn("msg-2 from c: from its own worktree", seen)
        self.assertNotIn("for demo", seen)
        # starting perf:fix told demo's lead, whose work it overlaps, and nobody else
        self.assertIn(
            "for the lead, from campaign perf: perf:fix started",
            self.ok(self.home, "status", "--campaign", "demo").out,
        )
        self.assertNotIn("for the lead", self.ok(self.home, "status", "--campaign", "perf").out)
        # each campaign's outbox is its own ref
        bare = self.root / "remote.git"
        self.git(self.root, "init", "-q", "--bare", str(bare))
        self.git(self.home, "remote", "add", "origin", str(bare))
        for name in ("demo", "perf"):
            self.ok(self.home, "config", f"peers=far={bare}", "--campaign", name)
            self.ok(self.home, "message", "far:x", f"from {name}", "--campaign", name)
        remote = ("--git-dir", str(bare))
        refs = self.git(self.root, *remote, "for-each-ref", "--format=%(refname)", "refs/campaign").split()
        self.assertEqual(refs, ["refs/campaign/outbox/demo", "refs/campaign/outbox/perf"])
        for name in ("demo", "perf"):
            files = self.git(self.root, *remote, "ls-tree", "-r", "--name-only", f"refs/campaign/outbox/{name}").split()
            self.assertEqual(len(files), 1)


class Messages(CampaignCase):
    def test_an_agent_outside_the_home_names_it(self) -> None:
        wt = self.started("fix", files="app.py")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        unrelated = self.repo("unrelated", {"x.txt": "x\n"})  # a repository that holds no campaign
        self.assertTrue(self.ok(elsewhere, "-C", str(self.home), "brief").out.startswith("campaign demo:"))
        self.assertIn("fix active low", self.ok(unrelated, "brief", "--campaign", "demo").out)
        self.ok(elsewhere, "-C", str(self.home), "set", "fix", "title=renamed")  # writes too
        self.assertEqual(self.quest("fix")["title"], "renamed")
        self.env["CAMPAIGN_HOME"] = str(self.home)
        self.ok(elsewhere, "set", "fix", "risk=normal")
        self.assertEqual(self.quest("fix")["risk"], "normal")
        del self.env["CAMPAIGN_HOME"]
        self.fails(wt, "set", "fix", "risk=high", match="run this there, or with -C")  # an contributor never writes
        self.fails(elsewhere, "brief", "--campaign", "nope", match="no campaign nope here or on this machine")
        self.fails(elsewhere, "brief", match="pass -C DIR, --campaign NAME, or set CAMPAIGN_HOME")

    def test_a_leads_message_shows_until_it_is_closed(self) -> None:
        wt = self.started("fix", files="app.py")
        self.run_cli(self.home, "wait", "--commits", "--timeout", "1")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        sent = self.ok(elsewhere, "message", "demo:", "the CI runner moved", "--from", "owner").out
        self.assertIn("msg-1 for the lead", sent)
        woke = self.ok(self.home, "wait", "--commits", "--timeout", "30").out
        self.assertIn("demo: msg-1 for the lead, from owner: the CI runner moved", woke)
        self.assertIn("msg-1 for the lead, from owner: the CI runner moved", self.ok(self.home, "status").out)
        self.assertIn("- msg-1 for the lead, from owner: the CI runner moved", self.ok(self.home, "brief").out)
        self.assertNotIn("CI runner", self.ok(wt, "brief").out)  # an contributor's brief is its quest's
        self.commit(wt, {"app.py": "print(2)\n"})
        self.verified("fix")
        self.ok(self.home, "land", "fix")  # a lead's message blocks no quest
        self.assertIn("msg-1 for the lead", self.ok(self.home, "brief").out)
        self.fails(self.home, "message", ":", "--close", "msg-1", match="needs --reason")
        self.ok(self.home, "message", ":", "--close", "msg-1", "--reason", "updated the runbook")
        self.assertNotIn("CI runner", self.ok(self.home, "status").out)
        self.assertEqual(self.state()["messages"][0]["closed"], "updated the runbook")

    def test_messages_cross_machines_through_outboxes(self) -> None:
        self.started("fix", files="app.py")
        self.run_cli(self.home, "wait", "--commits", "--timeout", "1")
        # another machine: its own job queue and registry, and a repository its outbox is pushed to
        bare = self.root / "far.git"
        self.git(self.root, "init", "-q", "--bare", str(bare))
        far = self.repo("far", {"f.py": "1\n"})
        self.git(far, "remote", "add", "origin", str(bare))
        here = self.env["CAMPAIGN_JOBS"]
        self.env["CAMPAIGN_JOBS"] = str(self.root / "far-machine" / "jobs")  # its registry is beside it
        self.ok(far, "init", "--name", "far")
        self.ok(far, "config", f"peers=demo={self.root / 'demo.git'}")
        sent = self.ok(far, "message", "demo:fix", "the encoder drops the last byte", "--from", "explorer").out
        self.assertIn("queued in far's outbox", sent)
        self.fails(far, "message", "nowhere:x", "text", match="no campaign nowhere on this machine or among the peers")
        self.env["CAMPAIGN_JOBS"] = here
        self.ok(self.home, "config", f"peers=far={bare}")
        self.env["CAMPAIGN_PEER_SECONDS"] = "0"
        woke = self.ok(self.home, "wait", "--commits", "--timeout", "30").out
        self.assertIn("fix: msg-1 from far: explorer: the encoder drops the last byte", woke)
        self.assertIn("unanswered messages: msg-1", self.ok(self.home, "brief").out)  # it gates like a local note
        self.assertEqual(self.run_cli(self.home, "wait", "--commits", "--timeout", "2").code, 3)  # fetched once

    def test_a_message_from_outside_wakes_the_game_master_and_must_be_answered_to_land(self) -> None:
        wt = self.started("fix", files="app.py")
        self.commit(wt, {"app.py": "print(2)\n"})
        self.verified("fix")
        self.run_cli(self.home, "wait", "--commits", "--timeout", "1")  # the lead has seen it all
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        r = self.ok(elsewhere, "message", "demo:fix", "util rounds wrong past 2^53", "--from", "explorer hunt-lib")
        self.assertIn("msg-1 for fix", r.out)
        woke = self.ok(self.home, "wait", "--commits", "--timeout", "30").out
        self.assertIn("fix: msg-1 from explorer hunt-lib: util rounds wrong past 2^53", woke)
        self.assertIn("msg-1 from explorer hunt-lib", self.ok(self.home, "status").out)
        self.fails(self.home, "land", "fix", match="unanswered messages: msg-1")
        self.commit(wt, {".campaign/demo/journal/fix.md": "msg-1: util.py is outside this quest; no change\n"})
        self.assertNotIn("msg-1 from", self.ok(self.home, "status").out)  # answered
        self.ok(self.home, "land", "fix")
        filed = self.quest("fix")["messages"]
        self.assertEqual([(n["id"], n["by"]) for n in filed], [("msg-1", "explorer hunt-lib")])
        self.assertTrue(filed[0]["answered"])
        self.fails(elsewhere, "message", "demo:fix", "late", match="fix is landed")

    def test_the_game_master_closes_a_message_with_a_reason(self) -> None:
        self.started("fix", files="app.py")
        self.ok(self.home, "message", "fix", "cache the parse", "--from", "owner")
        self.fails(self.home, "message", "fix", "--close", "msg-1", match="needs --reason")
        self.ok(self.home, "message", "fix", "--close", "msg-1", "--reason", "measured: no gain")
        self.assertNotIn("msg-1", self.ok(self.home, "status").out)
        self.assertEqual(self.quest("fix")["messages"][0]["closed"], "measured: no gain")
        self.assertIn("msg-2 for fix", self.ok(self.home, "message", "fix", "another").out)  # ids are not reused


class Style(CampaignCase):
    def test_brief_status_shows_what_to_act_on_and_a_worktree_sees_its_quest(self) -> None:
        wt = self.started("fix", files="app.py")
        self.ok(self.home, "new", "other", "--title", "other", "--risk", "low", "--files", "README.md")
        self.commit(wt, {"app.py": "print(2)\n"})
        failed = self.submit(self.home, "--quest", "fix", "--", "exit 1")
        self.run_cli(self.home, "wait", failed, "--timeout", "60")
        self.ok(self.home, "message", "fix", "check the overflow", "--from", "explorer")
        brief = self.ok(wt, "brief").out  # in the quest's worktree: that quest only
        self.assertTrue(brief.startswith("fix active low: quest/demo/fix at "), brief)
        self.assertIn("  files: app.py\n", brief)
        self.assertIn("  - check unit is not green at", brief)
        self.assertIn("  - unanswered messages: msg-1:", brief)
        self.assertIn("  - msg-1 from explorer: check the overflow\n", brief)
        self.assertIn(f"  - {failed} failed, in no journal yet: exit 1 at ", brief)
        self.assertNotIn("other", brief)
        self.assertNotIn("criteria", brief)
        self.assertNotIn("\nother  planned", self.ok(wt, "status").out)  # the full view lists only this quest too
        home = self.ok(self.home, "brief").out
        self.assertIn("campaign demo: 1 in flight (1 active), 1 planned, 0 landed; jobs 0 queued, 0 running\n", home)
        self.assertIn("other planned low\n  ready to start: `campaign start other`\n", home)
        self.assertNotIn("files:", home)
        data = json.loads(self.ok(wt, "brief", "--json").out)  # the same, for parsing
        self.assertNotIn("counts", data)
        [quest] = data["quests"]
        self.assertEqual((quest["id"], quest["files"], quest["next"]), ("fix", ["app.py"], ""))
        self.assertTrue(any(g.startswith("check unit is not green") for g in quest["to_land"]), quest)
        self.assertEqual([(n["id"], n["by"]) for n in quest["messages"]], [("msg-1", "explorer")])
        self.assertEqual([f["job"] for f in quest["failed"]], [failed])
        out = self.ok(self.home, "map").out
        self.assertIn("- 🔥 **fix**", out)
        self.assertIn(f"Failing: {failed} failed", out)
        journal = wt / ".campaign/demo/journal/fix.md"
        journal.write_text(journal.read_text() + f"\n{failed} failed on purpose: the overflow check.\n")
        self.assertIn("- 🔨 **fix**", self.ok(self.home, "map").out, "a failure its journal names still burns")
        data = json.loads(self.ok(self.home, "brief", "--json").out)
        self.assertEqual(data["counts"]["planned"], 1)
        other = next(q for q in data["quests"] if q["id"] == "other")
        self.assertEqual(other["next"], "campaign start other")
        self.assertNotIn("files", other)

    def test_color_and_marks_only_on_a_terminal(self) -> None:
        wt = self.started("fix", files="app.py")
        self.commit(wt, {"app.py": "print(2)\n"})
        self.verified("fix")
        plain = self.ok(self.home, "status", "fix").out + self.ok(self.home, "job", "list").out
        self.assertNotIn("\033[", plain)  # agents read through pipes: words only
        self.assertFalse([c for c in plain if ord(c) >= 0x1F300], plain)
        self.env["FORCE_COLOR"] = "1"
        styled = self.ok(self.home, "status", "fix").out
        self.assertIn("\033[", styled)
        self.assertIn("🔨 \033[1mfix\033[0m  \033[36mactive\033[0m", styled)
        self.assertIn("  🌿 branch quest/demo/fix", styled)
        self.assertIn("  ✅ \033[32mready to land", styled)
        self.assertIn("✅ ", self.ok(self.home, "job", "list").out)
        self.env["NO_COLOR"] = "1"  # wins over FORCE_COLOR
        out = self.ok(self.home, "status", "fix").out
        self.assertNotIn("\033[", out)
        self.assertNotIn("🌿", out)


class Sidebar(CampaignCase):
    def test_the_sidebar_follows_the_state_and_sends_only_changes(self) -> None:
        calls = self.root / "cmux-calls"
        fake = self.root / "bin" / "cmux"
        fake.write_text(f'#!/bin/sh\nprintf "%s|" "$@" >> {calls}; echo >> {calls}\n')
        fake.chmod(0o755)
        self.env["CMUX_WORKSPACE_ID"] = "ws-test"
        self.ok(
            self.home,
            "new",
            "a",
            "--title",
            "a",
            "--risk",
            "low",
            "--files",
            "app.py",
            "--milestone",
            "M1",
            "--check",
            "unit=true",
        )
        plan = self.home / ".campaign/demo/PLAN.md"  # PLAN.md orders M2 first, unlike the quests
        plan.write_text(plan.read_text().replace("| <M1> | <ids> | <command> |", "| M2 | b | |\n| M1 | a | |"))
        self.git(self.home, "commit", "-q", "-am", "milestones")
        self.ok(self.home, "new", "b", "--title", "b", "--risk", "low", "--files", "README.md", "--milestone", "M2")
        self.assertIn("set-progress|0.0000|--label|demo M2: 0 of 1 quests landed · launchable: b|", calls.read_text())
        self.ok(self.home, "start", "a")
        log = calls.read_text()
        self.assertIn("set-progress|0.0000|--label|demo M1: 0 of 1 quests landed|", log)  # the active one
        self.assertIn("set-status|campaign-demo-a|a: active|--icon|hammer|", log)
        calls.write_text("")
        self.ok(self.home, "status")
        self.assertEqual(calls.read_text(), "", "an unchanged view sends nothing")
        failed = self.submit(self.home, "--quest", "a", "--", "exit 1")
        self.run_cli(self.home, "wait", failed, "--timeout", "60")
        self.ok(self.home, "status")
        self.assertIn("set-status|campaign-demo-a|a: active · 🔥 1 failed job|", calls.read_text())
        calls.write_text("")
        self.ok(self.home, "set", "a", "status=blocked", "reason=owner")
        self.assertIn("clear-status|campaign-demo-a|", calls.read_text(), "a blocked quest keeps no pill")
        self.ok(self.home, "set", "a", "status=active")
        self.commit(self.wt("a"), {"app.py": "print(2)\n"})
        self.verified("a")
        self.ok(self.home, "land", "a")
        log = calls.read_text()
        self.assertIn("notify|--title|Landed a|--body|", log)
        self.assertIn("clear-status|campaign-demo-a|", log)
        self.assertIn("set-progress|0.0000|--label|demo M2: 0 of 1 quests landed · launchable: b|", log)
        self.env.pop("CMUX_WORKSPACE_ID")
        calls.write_text("")
        self.ok(self.home, "set", "b", "status=blocked", "reason=owner")
        self.assertEqual(calls.read_text(), "", "outside cmux nothing is drawn")


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


class Map(CampaignCase):
    def test_the_map_shows_intent_result_documents_and_progress(self) -> None:
        plan = self.home / ".campaign/demo/PLAN.md"
        text = plan.read_text().replace(
            "- <what success means, measurably>", "- Totals print right,\n  in every locale"
        )
        text = text.replace("| <M1> | <ids> | <command> |", "| M1 | fix, open | python3 app.py |")
        plan.write_text(text)
        self.git(self.home, "commit", "-q", "-am", "plan")
        wt = self.started("fix", files="app.py,docs/**", milestone="M1")
        brief = self.home / ".campaign/demo/quests/fix.md"
        brief.write_text(brief.read_text().replace("## Outcome\n", "## Outcome\n\nPrints two instead of one.\n", 1))
        self.git(self.home, "commit", "-q", "-am", "fix outcome")
        journal = ".campaign/demo/journal/fix.md"
        result = (
            (wt / journal)
            .read_text()
            .replace(
                "## Result\n", "## Result\n\nPrints 2; 1.0 ms per call (job x). How: docs/mechanisms/print.md.\n", 1
            )
        )
        self.commit(wt, {"app.py": "print(2)\n", "docs/mechanisms/print.md": "# Print\n", journal: result})
        self.verified("fix")
        self.ok(self.home, "map", "--write")
        self.ok(self.home, "new", "open", "--title", "Open work", "--files", "README.md", "--milestone", "M1")
        self.ok(self.home, "land", "fix")
        written = (self.home / ".campaign/demo/MAP.md").read_text()
        self.assertIn("- ✅ **fix**", written, "the landing did not refresh MAP.md")
        out = self.ok(self.home, "map").out
        self.assertIn("- 🎯 Totals print right, in every locale\n", out)
        self.assertIn("## 🔨 M1: 1 of 2 landed", out)
        self.assertIn("- ✅ **fix**: fix", out)
        self.assertIn("⚠️ landed, criteria left open", out)  # the legend
        self.assertIn("Outcome: Prints two instead of one.", out)
        self.assertIn("Result: Prints 2; 1.0 ms per call", out)
        self.assertIn("Docs: docs/mechanisms/print.md", out)
        self.assertIn("- 📋 **open**: Open work", out)
        self.assertNotIn("**open**", self.ok(self.home, "map", "fix").out)
        data = json.loads(self.ok(self.home, "map", "--json").out)
        self.assertEqual([q["id"] for q in data["milestones"][0]["quests"]], ["fix", "open"])
        self.env["FORCE_COLOR"] = "1"
        colored = self.ok(self.home, "map").out
        self.assertIn("\033[", colored)
        self.assertEqual(re.sub(r"\033\[[0-9;]*m", "", colored), out, "color changed the map's text")
        self.ok(self.home, "map", "--write")
        self.assertNotIn("\033", (self.home / ".campaign/demo/MAP.md").read_text(), "MAP.md holds color codes")

    def test_work_done_before_the_campaign_is_recorded_on_the_map(self) -> None:
        self.env.update(GIT_AUTHOR_DATE="2026-10-01T12:00:00", GIT_COMMITTER_DATE="2026-10-01T12:00:00")
        old = self.commit(self.home, {"docs/status/pins.md": "1. [x] K1 pins at head\n"}, message="Pins at head")
        for key in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"):
            self.env.pop(key)
        self.ok(self.home, "new", "pins", "--title", "Pins at head", "--risk", "normal")
        (self.home / ".campaign/demo/quests/pins.md").write_text(
            "# pins: Pins at head\n\n## Outcome\n\nEvery library at its head.\n\n## Acceptance criteria\n\n"
            "- A1 [artifact] Pins at head: `docs/status/pins.md`\n"
            "- A2 [artifact] Pins checked in CI: `docs/status/pins.md`\n"
            "- A3 [artifact] Pins listed: `docs/status/pins.md`\n"
        )
        journal = self.home / ".campaign/demo/journal/pins.md"
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_text(
            "# pins: journal\n\nState: landed\n\n## Evidence\n\n| Criterion | Evidence |\n|---|---|\n"
            '| A1 | `docs/status/pins.md`: "[x] K1 pins  at head" |\n| A2 | open: left unticked in `docs/status/pins.md` |\n'
            '| A3 | `docs/status/pins.md`: "[x] K9 pins listed" |\n\n'
            "## Result\n\nAll 12 pins at head; CI green.\n"
        )
        self.git(self.home, "add", ".campaign")
        self.git(self.home, "commit", "-q", "-m", "history: pins")
        self.fails(self.home, "land", "pins", "--earlier", match="--record")
        reason = "docs/status/pins.md; reviewed in docs/reviews/pins.md, APPROVE; CI run 1"
        self.ok(self.home, "land", "pins", "--record", old, "--earlier", "--reason", reason)
        record = self.quest("pins")["landed"][0]
        self.assertTrue(record["at"].startswith("2026-10-01"), record)
        self.assertEqual(record["unverified"], [], "work verified before the campaign is listed as unverified")
        self.assertIn("(before the campaign)", self.ok(self.home, "status", "pins").out)
        out = self.ok(self.home, "map").out
        self.assertIn("- ⚠️ **pins**: Pins at head (normal risk, landed before the campaign 2026-10-01 at app:", out)
        self.assertIn("; left open: A2, A3)", out)  # A3 quotes what the ledger never says
        self.assertIn("Result: All 12 pins at head; CI green.", out)
        self.assertNotIn("none yet", out)  # reviewed by the process of its time
        plan = self.home / ".campaign/demo/PLAN.md"
        plan.write_text(plan.read_text().replace("| <M1> | <ids> | <command> |", "| M9 | | grep -E 'a\\|b' x |"))
        self.git(self.home, "commit", "-q", "-am", "an empty milestone")
        out = self.ok(self.home, "map").out
        self.assertIn("## ⬜ M9: no quests yet", out)
        self.assertIn("Validation on main: grep -E 'a|b' x\n", out, "an escaped pipe split the cell")
        (pins,) = [q for q in json.loads(self.ok(self.home, "map", "--json").out)["other"] if q["id"] == "pins"]
        self.assertEqual(
            [x["met"] for x in pins["criteria"]], [True, False, False], "an unticked criterion reads as met"
        )


if __name__ == "__main__":
    unittest.main()
