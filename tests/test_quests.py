"""Creating, starting and owning quests."""

from __future__ import annotations

import json
import subprocess
import sys
import time

from harness import CAMPAIGN, CampaignCase


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
        seen = self.home / ".git" / "campaign-seen.json"  # the first wait records where it starts
        deadline = time.time() + 60  # ends a hung test only
        while not seen.exists() and waiting.poll() is None and time.time() < deadline:
            time.sleep(0.05)
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

    def test_wait_wakes_on_a_message_or_a_jobs_end_not_on_its_tick(self) -> None:
        self.started("fix", files="app.py")
        self.env["CAMPAIGN_WAIT_TICK"] = "40"  # far longer than either event takes to arrive
        self.assertEqual(self.run_cli(self.home, "wait", "--commits", "--timeout", "1").code, 3)  # starts from now
        for event in ("message", "job"):
            waiting = subprocess.Popen(
                [sys.executable, str(CAMPAIGN), "wait", "--commits", "--timeout", "60"],
                cwd=self.home,
                env=self.env,
                stdout=subprocess.PIPE,
                text=True,
            )
            time.sleep(2)
            start = time.time()
            if event == "message":
                self.ok(self.home, "message", "demo:", "pins moved", "--from", "peer")
            else:
                self.submit(self.home, "--quest", "fix", "--", "true")
            out, _ = waiting.communicate(timeout=60)
            self.assertLess(time.time() - start, 20, f"the {event} waited for the tick:\n{out}")
            self.assertIn("pins moved" if event == "message" else "done exit 0", out)

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

    def test_a_quest_file_without_the_criteria_heading_is_refused_where_it_would_land_unchecked(self) -> None:
        self.ok(self.home, "new", "fix", "--title", "fix", "--risk", "low", "--files", "app.py", "--check", "unit=true")
        brief = ".campaign/demo/quests/fix.md"
        wrong = "# fix\n\n## Criteria\n\n- A1 prints: the suite\n"
        self.commit(self.home, {brief: wrong})
        for args in (("set", "fix", "files=app.py"), ("start", "fix")):
            self.fails(self.home, *args, match="no `## Acceptance criteria` heading (it has `## Criteria`)")
        self.commit(self.home, {brief: "# fix\n\n## Acceptance criteria\n\n- A1 prints: the suite\n"})
        self.ok(self.home, "start", "fix")
        self.commit(self.home, {brief: wrong})  # the heading is lost after the start
        self.fails(self.home, "job", "submit", "--quest", "fix", "--for", "A1", "--", "true", match="## Acceptance")
        self.fails(self.home, "land", "fix", match="no `## Acceptance criteria` heading")
        self.ok(self.home, "set", "fix", "status=dropped", "reason=a dropped quest needs no criteria")

    def test_a_template_placeholder_is_no_criteria_but_none_is_an_answer(self) -> None:
        self.keep_criteria = True
        self.ok(self.home, "new", "fix", "--title", "fix", "--risk", "low", "--files", "app.py", "--check", "unit=true")
        self.ok(self.home, "set", "fix", "files=app.py,README.md")  # a lead sets a new quest up before writing it
        self.fails(self.home, "start", "fix", match="still holds the template's placeholder")
        brief = ".campaign/demo/quests/fix.md"
        self.commit(self.home, {brief: "# fix\n\n## Acceptance criteria\n\nNone: a refactor.\n"})
        self.ok(self.home, "start", "fix")

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
        self.ok(self.home, "set", "late", "status=review")
        self.assertIn("late is reviewed and waits to land after lib", self.ok(self.home, "check").out)

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
        self.fails(self.home, "set", "fix", "status=blocked", "until=owner", match="needs a reason")
        self.ok(self.home, "set", "fix", "status=blocked", "until=owner", "reason=waiting on the owner")
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
        self.env["CAMPAIGN_IDLE"] = "0"  # every journal counts as untouched
        self.started("one", files="lib/**")
        new = ("--title", "t", "--risk", "low", "--files")
        self.ok(self.home, "new", "clash", *new, "lib/util.py")  # needs met, but one holds its files
        self.ok(self.home, "new", "leaf", *new, "docs.md")
        self.ok(self.home, "new", "root", *new, "app.py")
        self.ok(self.home, "new", "tail", *new, "tail.py", "--needs", "root")  # waits on root
        self.ok(self.home, "new", "parked", *new, "parked.py")
        self.ok(self.home, "set", "parked", "status=blocked", "until=owner", "reason=owner")
        out = self.ok(self.home, "status").out
        idle = "  nothing moves without the lead: one (resume, review or land each)\n"
        heads = "  most waited on: root (1 waiting)\n"
        self.assertIn(f"  launchable now: root, leaf\n{heads}{idle}  held by file sets: one holds clash\n", out)
        self.submit(self.home, "--quest", "one", "--", "true")  # queued: the worker is down
        self.assertNotIn("without the lead: one", self.ok(self.home, "status").out)
        journal = self.wt("one") / ".campaign/demo/journal/one.md"
        journal.write_text(journal.read_text().replace("State:", "State: blocked on lead: launch task T2;", 1))
        self.assertIn(idle, self.ok(self.home, "status").out, "a journal waiting on the lead shows, jobs or not")
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

    def test_an_after_dependency_orders_the_landing_and_never_hides_idle_work(self) -> None:
        self.env["CAMPAIGN_IDLE"] = "0"
        self.ok(self.home, "new", "base", "--title", "t", "--risk", "low", "--files", "base.py")
        top = self.started("top", files="top.py", after="base")
        idle = "nothing moves without the lead: top"
        journal = top / ".campaign/demo/journal/top.md"
        journal.write_text(journal.read_text().replace("State:", "State: waiting on jobs J1 J2;", 1))
        self.assertIn(idle, self.ok(self.home, "status").out, "its jobs ended: the work is the lead's to move")
        journal.write_text(journal.read_text().replace("State:", "State: ready to land;", 1))
        self.assertNotIn(idle, self.ok(self.home, "status").out, "ready, it waits only on base landing")
        journal.write_text(journal.read_text().replace("State:", "State: working;", 1))
        self.env["CAMPAIGN_IDLE"] = "3600"
        self.assertNotIn(idle, self.ok(self.home, "status").out, "a journal touched this hour: an agent at work")
        self.env["CAMPAIGN_IDLE"] = "0"
        self.assertIn(idle, self.ok(self.home, "status").out, "a journal saying working, untouched: nobody is")

    def test_a_quest_never_holds_the_files_of_what_it_lands_after(self) -> None:
        new = ("--title", "t", "--risk", "low", "--check", "unit=true")
        self.ok(self.home, "new", "core", *new, "--files", "lib/**")
        self.ok(self.home, "new", "user", *new, "--files", "lib/util.py,app.py", "--after", "core")
        self.fails(self.home, "start", "user", match="lands after core, whose files it would hold")
        self.ok(self.home, "start", "user", "--force", "legacy start")
        self.assertIn("user holds files of core, which it lands after", self.run_cli(self.home, "check").out)
        self.ok(self.home, "set", "user", "files=app.py")
        self.assertNotIn("holds files of", self.run_cli(self.home, "check").out)
        self.ok(self.home, "start", "core")

    def test_a_landing_marks_its_hunt_row_done(self) -> None:
        hunt = self.home / ".campaign/demo/hunt.md"
        rows = "| Slow lib loads | job 1 | lib/** | . | open |\n| Slow lib saves | job 2 | lib/** | . | open |\n"
        hunt.write_text(hunt.read_text() + rows)
        self.git(self.home, "commit", "-qam", "hunt rows")
        wt = self.started("lib", files="lib/**", check="unit=true")
        self.fails(self.home, "set", "lib", "hunt=Slow lib", match="starts 2 open rows")
        self.ok(self.home, "set", "lib", "hunt=Slow lib loads")
        self.commit(wt, {"lib/util.py": "X = 2\n"})
        self.verified("lib")
        self.ok(self.home, "land", "lib")
        landed = self.quest("lib")["landed"][-1]["commit"]
        loads, saves = [r for r in hunt.read_text().splitlines() if r.startswith("| Slow lib")]
        self.assertEqual(loads, f"| Slow lib loads | job 1 | lib/** | . | done: {self.home.name}:{landed[:9]} (lib) |")
        self.assertTrue(saves.endswith("| open |"), "only its own row")
        self.assertEqual(self.git(self.home, "status", "--porcelain", "--", ".campaign/demo/hunt.md"), "", "committed")

    def test_a_landing_prints_and_mails_what_it_unlocks(self) -> None:
        wt = self.started("lib", files="lib/**", check="unit=true")
        new = ("--title", "t", "--risk", "low", "--check", "unit=true", "--files")
        self.ok(self.home, "new", "tail", *new, "tail.py", "--needs", "lib")
        self.ok(self.home, "new", "clash", *new, "lib/util.py")
        self.ok(self.home, "new", "parked", *new, "parked.py")
        self.ok(self.home, "new", "later", *new, "later.py")
        self.fails(self.home, "set", "tail", "status=blocked", "until=lib", "reason=r", match="block is derived")
        self.fails(self.home, "set", "parked", "status=blocked", "reason=r", match="names until=")
        self.ok(self.home, "set", "parked", "status=blocked", "until=lib", "reason=after lib")
        self.ok(self.home, "set", "later", "status=blocked", "until=owner", "reason=the owner decides")
        self.commit(wt, {"lib/util.py": "X = 2\n"})
        self.verified("lib")
        out = self.ok(self.home, "land", "lib").out
        unlocked = (
            "unlocks: tail; parked (until lib met: `campaign set parked status=planned`); frees lib/util.py for clash\n"
        )
        self.assertIn(unlocked, out)
        self.assertNotIn("later", out, "a park until owner waits for the owner")
        status = self.ok(self.home, "status").out
        self.assertEqual(status.count("from campaign: lib landed: unlocks tail;"), 1, status)
        self.assertIn("parked: parked until lib met", self.run_cli(self.home, "check").out)
        self.ok(self.home, "start", "tail")
        self.ok(self.home, "start", "clash")
        self.ok(self.home, "set", "parked", "status=planned")
        self.assertIn("lib landed: unlocks", self.ok(self.home, "brief").out, "parked has not started")
        self.ok(self.home, "start", "parked")
        self.assertNotIn("unlocks", self.ok(self.home, "brief").out)
        self.assertEqual(self.state()["messages"][-1]["closed"], "its quests started")

    def test_a_parked_quest_with_a_branch_keeps_its_file_set(self) -> None:
        self.started("holder", files="lib/**")
        new = ("--title", "t", "--risk", "low", "--files")
        self.ok(self.home, "new", "user", *new, "lib/util.py")
        out = self.ok(self.home, "set", "holder", "status=blocked", "until=owner", "reason=the owner decides").out
        self.assertNotIn("frees", out)
        self.assertNotIn("unlocks", self.ok(self.home, "brief").out)
        o = json.loads(self.ok(self.home, "status", "--json").out)["overview"]
        self.assertEqual((o["launchable"], o["held"]), ([], {"user": ["holder"]}))
        self.fails(self.home, "start", "user", match="may overlap holder")
        self.ok(self.home, "new", "idea", *new, "docs/**")
        self.ok(self.home, "new", "doc", *new, "docs/a.md")
        self.ok(self.home, "set", "idea", "status=blocked", "until=owner", "reason=later")
        self.ok(self.home, "start", "doc")  # a planned quest parked holds nothing

    def test_stored_blocks_become_derived_needs_or_parks_until_owner(self) -> None:
        new = ("--title", "t", "--risk", "low", "--files")
        self.ok(self.home, "new", "seg", *new, "seg.py")
        self.ok(self.home, "new", "words", *new, "words.py")
        self.ok(self.home, "new", "book", *new, "book.md")
        state = self.state()
        state["quests"]["words"].update(status="blocked", reason="needs seg (D43)")
        state["quests"]["book"].update(status="blocked", reason="owner: later, after seg")
        (self.home / ".campaign/demo/state.json").write_text(json.dumps(state))
        self.git(self.home, "commit", "-qam", "old blocks")
        self.assertIn("words planned", self.ok(self.home, "brief").out)
        self.ok(self.home, "set", "seg", "title=segmenter")
        words, book = self.quest("words"), self.quest("book")
        self.assertEqual((words["status"], words["needs"], "reason" in words), ("planned", ["seg"], False))
        self.assertEqual(
            (book["status"], book["until"], book["reason"]), ("blocked", ["owner"], "owner: later, after seg")
        )

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
