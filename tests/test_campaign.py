"""Quests, ownership, evidence, reviews, landing and state recovery."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from typing import Any

from harness import CAMPAIGN, CampaignCase, campaign_module


class Quests(CampaignCase):
    def test_wait_returns_when_an_adventurer_commits_its_journal(self) -> None:
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
        state = "State: blocked on the game master: the publication code\n"
        self.commit(wt, {journal: state + (wt / journal).read_text().split("\n", 3)[-1]})
        out, _ = waiting.communicate(timeout=30)
        self.assertEqual(waiting.returncode, 0)
        self.assertIn("fix: quest/demo/fix moved to", out)
        self.assertIn("journal: blocked on the game master: the publication code", out)
        self.ok(self.home, "set", "fix", "status=dropped", "reason=done here")
        self.assertIn("no quest in flight", self.ok(self.home, "wait", "--commits").out)

    def test_wait_misses_nothing_that_happened_between_two_waits(self) -> None:
        a = self.started("one", files="app.py")
        b = self.started("two", files="README.md")
        self.assertEqual(self.run_cli(self.home, "wait", "--commits", "--timeout", "1").code, 3)  # starts from now
        self.commit(a, {"app.py": "print(2)\n"})  # both adventurers finish while the game master is busy
        self.commit(b, {"README.md": "two\n"})
        out = self.ok(self.home, "wait", "--commits", "--timeout", "30").out
        self.assertIn("one: quest/demo/one moved to", out)
        self.assertIn("two: quest/demo/two moved to", out)
        self.assertIn("still in flight: one (working), two (working)", out)
        self.commit(b, {"README.md": "two again\n"})  # lands while the game master handles the first two
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

    def test_landing_refuses_changes_outside_the_file_set(self) -> None:
        wt = self.started("fix", files="app.py", check="unit=true")
        self.commit(wt, {"app.py": "print(2)\n", "lib/util.py": "X = 2\n"})
        self.verified("fix")
        self.fails(self.home, "land", "fix", match="outside its file set")

    def test_landing_refuses_edits_to_the_game_masters_files(self) -> None:
        wt = self.started("fix", files="app.py", check="unit=true")
        self.commit(wt, {".campaign/demo/quests/fix.md": "weakened criteria\n"})
        self.verified("fix")
        self.fails(self.home, "land", "fix", match="game master's files")
        self.assertIn("game master's files", self.ok(self.home, "check").out)

    def test_check_reports_two_branches_changing_one_file(self) -> None:
        a = self.started("a", files="app.py")
        self.ok(self.home, "new", "b", "--title", "b", "--risk", "low", "--files", "app.py")
        self.ok(self.home, "start", "b", "--force", "owner asked for both")
        self.commit(a, {"app.py": "print('a')\n"})
        self.commit(self.wt("b"), {"app.py": "print('b')\n"})
        self.assertIn("a and b both change app.py", self.ok(self.home, "check").out)


class Evidence(CampaignCase):
    def reference_code_tree(self, repo: Path, commit: str) -> str:
        """The code tree as `git ls-tree` and `git mktree` build it, which job keys have always used."""

        def ls(tree: str) -> list[str]:
            return [e for e in self.git(repo, "ls-tree", "-z", tree).split("\0") if e]

        def mktree(entries: list[str]) -> str:
            p = subprocess.run(
                ["git", "-C", str(repo), "mktree", "-z"],
                input="".join(e + "\0" for e in entries),
                capture_output=True,
                text=True,
                check=True,
            )
            return p.stdout.strip()

        kept = []
        for e in ls(commit):
            name = e.split("\t", 1)[1]
            inner = [x for x in ls(e.split()[2]) if x.split("\t", 1)[1] != "campaign"] if " tree " in e else []
            if name == "docs" and inner:
                kept.append(f"040000 tree {mktree(inner)}\tdocs")
            elif name not in (".campaign", "docs") or " tree " not in e:
                kept.append(e)
        return mktree(kept)

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

    def test_code_trees_match_what_git_mktree_builds(self) -> None:
        campaign = campaign_module()
        repo = self.repo("shapes", {"a.py": "1\n", "docs.md": "x\n", "docs-x/f": "y\n", "z/f": "z\n"})
        commits = [self.git(repo, "rev-parse", "HEAD")]
        for files in (
            {".campaign/demo/state.json": "{}\n"},
            {"docs/guide.md": "g\n"},
            {"docs/campaign/state.json": "{}\n"},
            {".campaign/demo/PLAN.md": "p\n"},
        ):
            commits.append(self.commit(repo, files))
        self.git(repo, "rm", "-q", "docs/guide.md")  # docs/ now holds only the campaign
        commits.append(self.commit(repo, {"a.py": "1\n"}))
        commits.append(self.commit(self.repo("flat", {"docs": "a file\n", "b.py": "2\n"}), {"b.py": "3\n"}))
        for commit in commits:
            where = repo if commit != commits[-1] else self.root / "flat"
            self.assertEqual(campaign.code_tree(where, commit), self.reference_code_tree(where, commit))
        trees = [campaign.code_tree(repo, c) for c in commits[:-1]]
        self.assertEqual(len(set(trees)), 2)  # campaign files never change it; docs/guide.md does
        self.assertEqual(trees[0], trees[-1])

    def test_land_needs_every_criterion_proven_and_every_task_merged_or_dropped(self) -> None:
        wt = self.started("fix", files="app.py,lib/**,README.md")
        brief = self.home / ".campaign/demo/quests/fix.md"
        brief.write_text(brief.read_text().replace("None in this test.", "- A1 prints two: unit\n- A2 helper: unit"))
        self.git(self.home, "commit", "-qam", "criteria")
        self.ok(self.home, "start", "fix", "--task", "lib", "--files", "lib/**")
        self.ok(self.home, "start", "fix", "--task", "docs", "--files", "README.md")
        self.commit(self.wt("fix--lib"), {"lib/util.py": "X = 2\n"})  # work that never reaches the quest branch
        self.commit(self.wt("fix--docs"), {"README.md": "app, documented\n"})
        self.git(wt, "merge", "-q", "--no-edit", "quest/demo/fix--docs")
        journal = "## Evidence\n\n| Criterion | Evidence |\n|---|---|\n| A1 | job 1 |\n| A2 | pending |\n"
        self.commit(wt, {"app.py": "print(2)\n", ".campaign/demo/journal/fix.md": journal})
        self.verified("fix")
        r = self.fails(self.home, "land", "fix")
        self.assertIn("task lib is not merged into", r.text)
        self.assertNotIn("task docs", r.text)  # merged
        self.assertIn("criteria without evidence in a journal's Evidence table at", r.text)
        self.assertIn(": A2 (`campaign status fix` lists them)", r.text)
        self.fails(self.home, "set", "fix", "--task", "lib", "status=dropped", match="needs reason=R")
        self.ok(self.home, "set", "fix", "--task", "lib", "status=dropped", "reason=superseded by the docs task")
        self.assertIn("lib: dropped: superseded by the docs task", self.ok(self.home, "status").out)
        self.commit(wt, {".campaign/demo/journal/fix.md": journal.replace("| A2 | pending |", "| A2 | job 2 |")})
        self.ok(self.home, "land", "fix")  # the journal commit keeps the checks' evidence

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

    def test_land_needs_green_checks_at_the_exact_head(self) -> None:
        wt = self.started("fix", files="app.py")
        self.commit(wt, {"app.py": "print(2)\n"})
        self.fails(self.home, "land", "fix", match="not green")
        self.verified("fix")
        self.commit(wt, {"app.py": "print(3)\n"})  # the evidence is now for an older commit
        self.fails(self.home, "land", "fix", match="not green")
        self.verified("fix")
        self.ok(self.home, "land", "fix")
        self.assertEqual(self.quest("fix")["status"], "landed")
        record = self.quest("fix")["landed"][0]
        self.assertEqual(record["tested"], self.git(wt, "rev-parse", "HEAD"))
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
        self.assertIn("0 of 1 criterion with evidence", out)  # the template's <criterion> is not one
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
        brief.write_text("# fix\n\n## Acceptance criteria\n\n- A1 one: a test\n- A2 two: a test\n- A3 three: a test\n")
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
            "A1  met      one: a test",
            "             journal: job 261106-1",
            "A2  met      two: a test",
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
        self.commit(b, {"docs/other.md": "outside the campaign's files counts as code\n"})
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
        self.review("feat", "correctness", "CHANGES")
        self.fails(self.home, "accept", "feat", "correctness", match="--jobs")
        jid = self.submit(self.wt("feat"), "--quest", "feat", "--", "python3 app.py")
        self.ok(self.home, "wait", jid)
        self.ok(self.home, "accept", "feat", "correctness", "--jobs", jid)
        self.assertEqual(self.quest("feat")["accepted"]["correctness"]["jobs"][0]["job"], jid)
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
        self.assertIn("not reviewed yet; launch a correctness paladin", self.ok(self.home, "status", "other").out)
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
        self.ok(self.home, "accept", "dur", "correctness")  # the game master read the upstream diff
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


class Increments(CampaignCase):
    def test_a_safe_prefix_lands_before_the_quest(self) -> None:
        wt = self.started("big", files="lib/**,app.py", risk="normal", check="unit=true")
        first = self.commit(wt, {"lib/util.py": "X = 5\n"}, "refactor")
        self.commit(wt, {"app.py": "print('feature')\n"}, "feature")
        self.ok(self.home, "verify", "big", "--commit", first)
        self.ok(self.home, "wait", "--quest", "big", "--timeout", "60")
        self.fails(self.home, "land", "big", "--upto", first, match="--reason")
        self.ok(self.home, "land", "big", "--upto", first, "--reason", "behavior-preserving refactor")
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
            "soak=sleep 1; exit 3",
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
        # the fix lands like any quest; rerunning the deferred checks on main clears the error
        self.ok(self.home, "set", "fast", "--later", "soak=true", "--later", "other-platform=echo other")
        self.ok(self.home, "verify", "fast", "--deferred")
        rerun = self.quest("fast")["landed"][0]["later"]
        self.assertNotEqual(rerun, record["later"])
        self.ok(self.home, "wait", *rerun, "--timeout", "60")
        self.ok(self.home, "check")

    def test_slow_ci_workflows_can_be_named_non_gating(self) -> None:
        self.assertEqual(self.ok(self.home, "config").out, "ci=\nci_later=\npeers=\noutbox=\n")
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

    def test_a_library_quest_works_from_its_own_repository(self) -> None:
        api = self.lib_quest(self.home, "api", "--files", "lib.py")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        self.ok(elsewhere, "message", "demo:api", "the parser rejects tabs", "--from", "scout")
        self.ok(api, "message", "api", "and spaces", "--from", "me")  # from its worktree in the library
        brief = self.ok(api, "brief").out
        self.assertTrue(brief.startswith("api active low: quest/demo/api at "), brief)
        self.assertIn("  files: lib.py\n", brief)
        self.assertIn("  - msg-1 from scout: the parser rejects tabs\n", brief)
        self.assertIn("  - msg-2 from me: and spaces\n", brief)
        self.assertEqual(self.ok(api, "get", "api", "repo").out, "../lib\n")
        job = self.submit(api, "--quest", "api", "--", "true")
        self.ok(api, "wait", job)
        self.assertIn(job, self.ok(api, "job", "list").out)
        self.commit(api, {"lib.py": "X = 2\n", ".campaign/demo/journal/api.md": "msg-1, msg-2: both pass now\n"})
        self.landed_from(self.home, "api")
        self.assertEqual([m["id"] for m in self.quest("api")["messages"]], ["msg-1", "msg-2"])

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
        self.assertIn("told campaign demo's game master that use overlaps its work", started.out)
        told = self.ok(self.home, "status").out  # demo's game master hears it from the tool
        self.assertIn("msg-1 for the game master, from campaign other: other:use started and may change files", told)
        self.assertIn("msg-2 for the game master, from campaign other: other:use started and builds against", told)
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
        self.assertEqual((run["role"], run["class"], run["quest"]), ("adventurer", "Artificer", "fix"))
        self.assertEqual(
            (run["model"], run["effort"], run["tokens"], run["seconds"]), ("gpt-6-astra", "high", 1200, 120)
        )
        self.assertEqual((run["cached_input_tokens"], run["output_tokens"], run["cost"]), (800, 200, 0))
        (lead,) = u["game_master"]
        self.assertEqual(
            (lead["tokens"], lead["seconds"], lead["model"], lead["effort"]), (550, 180, "gpt-6.1-sol", "medium")
        )
        self.assertIn("Codex cost unavailable", self.ok(self.home, "usage").out)

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
        self.assertEqual(len(u["game_master"]), 1)
        other.write_text(other.read_text().replace('"lead"', '"unrelated"'))
        self.assertEqual(json.loads(self.ok(self.home, "usage", "--json").out)["game_master"], [])

    def test_codex_runs_are_attributed_by_task_name_when_the_prompt_is_encrypted(self) -> None:
        self.ok(self.home, "new", "fix", "--title", "fix", "--risk", "low")
        names = ("adventurer_fix_artificer", "paladin_fix_security", "fix_security", "night_fix_v2", "night_security")
        names += ("fixer", "builder")
        for task in names:
            log = self.codex_log("agent", task)  # no parent log: the launch prompt is unreadable
            log.write_text(log.read_text().replace("/root/builder", f"/root/{task}").replace('"builder"', f'"{task}"'))
        u = json.loads(self.ok(self.home, "usage", "--json").out)
        got = {r["task"]: (r["role"], r["quest"], r["class"], r["lens"]) for r in u["runs"]}
        self.assertEqual(
            got,
            {
                "adventurer_fix_artificer": ("adventurer", "fix", "Artificer", ""),
                "paladin_fix_security": ("paladin", "fix", "", "security"),
                "fix_security": ("paladin", "fix", "", "security"),  # a lens marks a paladin
                "night_fix_v2": ("adventurer", "fix", "", ""),  # the quest's id as a whole word
                "night_security": ("paladin", "-", "", "security"),  # a role, but no quest named
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
                    "data": {"usage": {"cost": cost, "inputTokens": 1000, "outputTokens": 200}},
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
                    f"---\nname: adventurer\n---\nYou are the adventurer for quest fix of campaign demo ({home}).\nClass: Artificer; your method",
                    "a1",
                    10,
                ),
                (f"You resume quest fix of campaign demo ({home}) after the previous adventurer stopped.", "a2", 4),
                (f"You are the correctness paladin for quest fix of campaign demo ({home}). You have not", "p1", 6),
                ("You are researching orchestration systems for a campaign skill.", "x1", 99),
                ("You are the adventurer for quest fix of campaign other (/elsewhere).", "o1", 50),
            ],
            lead_cost=5,
        )
        self.copilot_session("unrelated", [("Explore the repository.", "u1", 70)], lead_cost=77)
        old = self.copilot_session(
            "before-init", [(f"You are the adventurer for quest fix of campaign demo ({home}).", "z1", 33)], 0
        )
        os.utime(old, (0, 0))
        u = json.loads(self.ok(self.home, "usage", "--json").out)
        runs = sorted((r["role"], r["lens"], r["class"], r["quest"], r["cost"]) for r in u["runs"])
        self.assertEqual(
            runs,
            [
                ("adventurer", "", "", "fix", 4),
                ("adventurer", "", "Artificer", "fix", 10),
                ("paladin", "correctness", "", "fix", 6),
            ],
        )
        first = next(r for r in u["runs"] if r["agent"] == "a1")
        self.assertEqual(
            (first["effort"], first["tokens"], first["seconds"], first["tool_calls"]), ("high", 1200, 120, 7)
        )
        self.assertEqual([(s["session"], s["cost"]) for s in u["game_master"]], [("s1", 5)])
        (job,) = u["jobs"]
        self.assertEqual((job["id"], job["quest"], job["state"]), (jid, "fix", "done"))
        self.assertGreaterEqual(job["ran"], 1)
        text = self.ok(self.home, "usage").out
        self.assertIn("adventurer Artificer claude-opus-5.5 high", text)
        self.assertIn("paladin correctness claude-opus-5.5 high", text)


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


class Migration(CampaignCase):
    def test_an_older_campaign_runs_in_place_and_moves_once_nothing_is_in_flight(self) -> None:
        (self.home / "docs").mkdir()
        self.git(self.home, "mv", ".campaign/demo", "docs/campaign")  # a campaign started before .campaign
        self.git(self.home, "commit", "-q", "-m", "an older campaign")
        wt = self.started("fix", files="app.py", check="unit=true")
        self.assertTrue((wt / "docs/campaign/journal/fix.md").exists())
        self.assertIn("run `campaign migrate` once no quest is in flight (now: fix)", self.ok(self.home, "status").out)
        self.fails(self.home, "migrate", match="land or drop fix")
        self.commit(wt, {"app.py": "print(2)\n"})
        self.verified("fix")
        self.ok(self.home, "message", "fix", "the older layout takes messages too", "--from", "scout")
        self.fails(self.home, "land", "fix", match="unanswered messages: msg-1")
        self.commit(wt, {"docs/campaign/journal/fix.md": "msg-1: noted, nothing to change\n"})
        self.ok(self.home, "land", "fix")
        jid = self.submit(self.home, "--", "sleep 300")  # a job of this campaign, tagged the old way
        self.fails(self.home, "migrate", match=f"jobs {jid} are queued or running")
        self.assertIn("run `campaign migrate` once its jobs finish", self.ok(self.home, "status").out)
        self.ok(self.home, "job", "cancel", jid)
        self.run_cli(self.home, "wait", jid, "--timeout", "30")
        self.assertIn("docs/campaign: run `campaign migrate`\n", self.ok(self.home, "status").out)
        self.ok(self.home, "migrate")
        self.assertEqual(self.git(self.home, "status", "--porcelain"), "")
        self.assertTrue((self.home / ".campaign/demo/quests/fix.md").exists())
        self.assertFalse((self.home / "docs").exists())
        self.assertNotIn("campaign migrate", self.ok(self.home, "status").out)
        self.started("next", files="app.py", check="unit=true")
        # the move changed no code, so the evidence from before it still applies
        self.assertIn("reused", self.ok(self.home, "verify", "next").out)


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
        # each game master's wait sees only its campaign's events
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
        # starting perf:fix told demo's game master, whose work it overlaps, and nobody else
        self.assertIn(
            "for the game master, from campaign perf: perf:fix started",
            self.ok(self.home, "status", "--campaign", "demo").out,
        )
        self.assertNotIn("for the game master", self.ok(self.home, "status", "--campaign", "perf").out)
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
        self.fails(wt, "set", "fix", "risk=high", match="run this there, or with -C")  # an adventurer never writes
        self.fails(elsewhere, "brief", "--campaign", "nope", match="no campaign nope here or on this machine")
        self.fails(elsewhere, "brief", match="pass -C DIR, --campaign NAME, or set CAMPAIGN_HOME")

    def test_a_game_masters_message_shows_until_it_is_closed(self) -> None:
        wt = self.started("fix", files="app.py")
        self.run_cli(self.home, "wait", "--commits", "--timeout", "1")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        sent = self.ok(elsewhere, "message", "demo:", "the CI runner moved", "--from", "patron").out
        self.assertIn("msg-1 for the game master", sent)
        woke = self.ok(self.home, "wait", "--commits", "--timeout", "30").out
        self.assertIn("demo: msg-1 for the game master, from patron: the CI runner moved", woke)
        self.assertIn("msg-1 for the game master, from patron: the CI runner moved", self.ok(self.home, "status").out)
        self.assertIn("- msg-1 for the game master, from patron: the CI runner moved", self.ok(self.home, "brief").out)
        self.assertNotIn("CI runner", self.ok(wt, "brief").out)  # an adventurer's brief is its quest's
        self.commit(wt, {"app.py": "print(2)\n"})
        self.verified("fix")
        self.ok(self.home, "land", "fix")  # a game master's message blocks no quest
        self.assertIn("msg-1 for the game master", self.ok(self.home, "brief").out)
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
        sent = self.ok(far, "message", "demo:fix", "the encoder drops the last byte", "--from", "scout").out
        self.assertIn("queued in far's outbox", sent)
        self.fails(far, "message", "nowhere:x", "text", match="no campaign nowhere on this machine or among the peers")
        self.env["CAMPAIGN_JOBS"] = here
        self.ok(self.home, "config", f"peers=far={bare}")
        self.env["CAMPAIGN_PEER_SECONDS"] = "0"
        woke = self.ok(self.home, "wait", "--commits", "--timeout", "30").out
        self.assertIn("fix: msg-1 from far: scout: the encoder drops the last byte", woke)
        self.assertIn("unanswered messages: msg-1", self.ok(self.home, "brief").out)  # it gates like a local note
        self.assertEqual(self.run_cli(self.home, "wait", "--commits", "--timeout", "2").code, 3)  # fetched once

    def test_a_message_from_outside_wakes_the_game_master_and_must_be_answered_to_land(self) -> None:
        wt = self.started("fix", files="app.py")
        self.commit(wt, {"app.py": "print(2)\n"})
        self.verified("fix")
        self.run_cli(self.home, "wait", "--commits", "--timeout", "1")  # the game master has seen it all
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        r = self.ok(elsewhere, "message", "demo:fix", "util rounds wrong past 2^53", "--from", "scout hunt-lib")
        self.assertIn("msg-1 for fix", r.out)
        woke = self.ok(self.home, "wait", "--commits", "--timeout", "30").out
        self.assertIn("fix: msg-1 from scout hunt-lib: util rounds wrong past 2^53", woke)
        self.assertIn("msg-1 from scout hunt-lib", self.ok(self.home, "status").out)
        self.fails(self.home, "land", "fix", match="unanswered messages: msg-1")
        self.commit(wt, {".campaign/demo/journal/fix.md": "msg-1: util.py is outside this quest; no change\n"})
        self.assertNotIn("msg-1 from", self.ok(self.home, "status").out)  # answered
        self.ok(self.home, "land", "fix")
        filed = self.quest("fix")["messages"]
        self.assertEqual([(n["id"], n["by"]) for n in filed], [("msg-1", "scout hunt-lib")])
        self.assertTrue(filed[0]["answered"])
        self.fails(elsewhere, "message", "demo:fix", "late", match="fix is landed")

    def test_the_game_master_closes_a_message_with_a_reason(self) -> None:
        self.started("fix", files="app.py")
        self.ok(self.home, "message", "fix", "cache the parse", "--from", "patron")
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
        self.ok(self.home, "message", "fix", "check the overflow", "--from", "scout")
        brief = self.ok(wt, "brief").out  # in the quest's worktree: that quest only
        self.assertTrue(brief.startswith("fix active low: quest/demo/fix at "), brief)
        self.assertIn("  files: app.py\n", brief)
        self.assertIn("  - check unit is not green at", brief)
        self.assertIn("  - unanswered messages: msg-1:", brief)
        self.assertIn("  - msg-1 from scout: check the overflow\n", brief)
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
        self.assertEqual([(n["id"], n["by"]) for n in quest["messages"]], [("msg-1", "scout")])
        self.assertEqual([f["job"] for f in quest["failed"]], [failed])
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
        self.ok(self.home, "new", "b", "--title", "b", "--risk", "low", "--files", "README.md", "--milestone", "M2")
        self.ok(self.home, "start", "a")
        log = calls.read_text()
        self.assertIn("set-progress|0.0000|--label|demo M1: 0 of 1 quests landed|", log)
        self.assertIn("set-status|campaign-demo-a|a: active|--icon|hammer|", log)
        calls.write_text("")
        self.ok(self.home, "status")
        self.assertEqual(calls.read_text(), "", "an unchanged view sends nothing")
        self.commit(self.wt("a"), {"app.py": "print(2)\n"})
        self.verified("a")
        self.ok(self.home, "land", "a")
        log = calls.read_text()
        self.assertIn("notify|--title|Landed a|--body|", log)
        self.assertIn("clear-status|campaign-demo-a|", log)
        self.assertIn("set-progress|0.0000|--label|demo M2: 0 of 1 quests landed · ready: b|", log)
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
        self.git(
            self.home, "merge", "-q", "--no-ff", "-m", "crash after this", head
        )  # main moved, then the game master died
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
        self.ok(self.home, "land", "done", "--record", head)
        self.assertEqual(self.quest("done")["status"], "landed")

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
        self.ok(self.home, "land", "fix")
        (wt / "probe.txt").write_text("unsaved probe\n")
        self.fails(self.home, "clean", "fix", match="untracked")
        (wt / "probe.txt").unlink()
        self.ok(self.home, "clean", "fix")
        self.assertFalse(wt.exists())
        self.assertNotIn("quest/demo/fix", self.git(self.home, "branch", "--list"))
        self.assertNotIn("slots", self.git(self.home, "worktree", "list"))


if __name__ == "__main__":
    unittest.main()
