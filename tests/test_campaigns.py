"""Several campaigns on one machine, and the health of the queue they share."""

from __future__ import annotations

import fcntl
import json
import time
from pathlib import Path
from typing import Any

from harness import CampaignCase


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

    def test_a_long_job_queued_past_its_age_runs_before_shorter_ones(self) -> None:
        """Shortest first never starves a long job: past CAMPAIGN_AGE it runs in submit order."""
        self.env.update(CAMPAIGN_LIGHT_JOBS="1", CAMPAIGN_AGE="2")
        first = self.submit(self.home, "--level", "light", "--force", "--", "sleep 4")
        self.wait_until(lambda: self.job(first)["state"] == "running")
        long = self.submit(self.home, "--level", "light", "--minutes", "100", "--force", "--", "true")
        short = [
            self.submit(self.home, "--level", "light", "--minutes", "1", "--force", "--", f"true {i}") for i in range(3)
        ]
        self.ok(self.home, "wait", first, long, *short, "--timeout", "60", timeout=90)
        self.assertLess(self.job(long)["started"], self.job(short[0])["started"], "the long job starved")

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

    def test_short_light_work_fills_an_overdue_drains_lull(self) -> None:
        """Past its wait, a drain still lets a light job start that ends before the running ones."""
        self.env.update(CAMPAIGN_LIGHT_JOBS="2", CAMPAIGN_DRAIN_WAIT="0", CAMPAIGN_DRAIN_SLACK="0")
        ours = self.submit(self.home, "--level", "light", "--force", "--minutes", "5", "--", "sleep 8")
        self.wait_until(lambda: self.job(ours)["state"] == "running")
        ab = self.submit(self.home, "--level", "exclusive", "--minutes", "1", "--", "true")
        short = self.submit(self.other, "--level", "light", "--minutes", "1", "--", "true")
        self.ok(self.home, "wait", ours, ab, "--timeout", "60", timeout=90)
        self.ok(self.other, "wait", short, "--timeout", "60", timeout=90)
        self.assertLess(self.job(short)["started"], self.job(ab)["started"], "the lull went unused")
        self.assertLess(float(self.job(short)["started"]), float(self.job(ours)["ended"]), "it waited out the lull")

    def test_light_work_running_far_past_a_drains_due_waits_for_the_drain(self) -> None:
        """Before a queued drain falls due, light work still starts, unless it would run past it by DRAIN_HOLD."""
        self.env.update(CAMPAIGN_LIGHT_JOBS="1", CAMPAIGN_DRAIN_WAIT="30", CAMPAIGN_DRAIN_HOLD="300")
        go = self.root / "go"  # ours holds the only light slot until the rest are queued, however slow submits are
        ours = self.submit(self.home, "--level", "light", "--force", "--", f"while [ ! -e {go} ]; do sleep 0.1; done")
        self.wait_until(lambda: self.job(ours)["state"] == "running")
        ab = self.submit(self.home, "--level", "exclusive", "--minutes", "1", "--", "true")
        long = self.submit(self.other, "--level", "light", "--minutes", "100", "--", "true long")
        short = self.submit(self.other, "--level", "light", "--minutes", "1", "--", "true short")
        go.touch()
        self.ok(self.home, "wait", ours, ab, "--timeout", "60", timeout=90)
        self.ok(self.other, "wait", long, short, "--timeout", "60", timeout=90)
        self.assertLess(self.job(short)["started"], self.job(ab)["started"], "short light work waited")
        self.assertLess(self.job(ab)["started"], self.job(long)["started"], "a long job held the drain past its turn")

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

    def test_a_drain_cancelled_while_queued_does_not_restart_the_wait(self) -> None:
        """Only a drain that ran restarts the wait: cancelling queued drains never starves the others."""
        # queued light work keeps the drain from a lull, so only its wait lets it claim
        self.env.update(CAMPAIGN_LIGHT_JOBS="1", CAMPAIGN_DRAIN_WAIT="6", CAMPAIGN_DRAIN_SLACK="600")
        ours = self.submit(self.home, "--level", "light", "--force", "--minutes", "5", "--", "sleep 30")
        self.wait_until(lambda: self.job(ours)["state"] == "running")
        self.submit(self.home, "--level", "light", "--force", "--", "true behind")
        ab = self.submit(self.home, "--level", "exclusive", "--minutes", "1", "--", "true")
        for n in range(6):
            time.sleep(3)
            # held queued behind the light job, so it is cancelled without ever running
            gone = self.submit(
                self.home, "--level", "exclusive", "--minutes", "1", "--force", "--after", ours, "--", f"true {n}"
            )
            self.ok(self.home, "job", "cancel", gone)
        self.ok(self.home, "wait", ab, "--timeout", "60", timeout=90)
        self.assertLess(
            float(self.job(ab)["claimed"]), float(self.job(ours)["started"]) + 25, "cancelled drains restarted its wait"
        )

    def test_a_drain_plans_its_lull_from_how_long_the_command_took(self) -> None:
        """A light job declaring far more minutes than its command took before does not hold off a lull."""
        self.env.update(CAMPAIGN_LIGHT_JOBS="1")
        first = self.submit(self.home, "--level", "light", "--minutes", "30", "--env", "S=1", "--", 'sleep "$S"')
        self.ok(self.home, "wait", first)
        ours = self.submit(self.home, "--level", "light", "--minutes", "30", "--env", "S=20", "--", 'sleep "$S"')
        self.wait_until(lambda: self.job(ours)["state"] == "running")
        ab = self.submit(self.home, "--level", "exclusive", "--minutes", "1", "--", "true")
        self.wait_until(lambda: self.job(ab).get("claimed"), timeout=30)
        self.assertLess(
            float(self.job(ab)["claimed"]), float(self.job(ours)["started"]) + 10, "it waited out --minutes"
        )
        self.ok(self.home, "wait", ours, ab, "--timeout", "60", timeout=90)

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

    def test_a_drop_moves_or_releases_every_campaigns_dependents_without_their_lead(self) -> None:
        """Dropping a quest another campaign's quest waits on must say into=SUCCESSOR, which the waiting
        quest then waits on, or into=none, which releases it; its lead hears either way. A dependency on
        no quest is refused, and blocks with an error."""
        for pid in ("hello", "lane"):
            self.ok(
                self.home, "new", pid, "--title", pid, "--risk", "low", "--files", f"{pid}.py", "--check", "unit=true"
            )
        new = ("new", "transport", "--title", "t", "--risk", "low", "--files", "o.py", "--check", "unit=true")
        self.fails(self.other, *new, "--after", "demo:nope", match="demo:nope names no quest")
        self.ok(self.other, *new, "--needs", "demo:hello")
        self.assertIn("most waited on: demo:hello (1 waiting)\n", self.ok(self.other, "status").out)
        self.fails(self.other, "set", "transport", "after=demo:gone", match="demo:gone names no quest")
        self.fails(self.home, "set", "hello", "into=lane", match="with status=dropped")
        self.fails(self.home, "set", "hello", "status=dropped", "reason=r", match="other:transport wait on hello")
        dropped = self.ok(self.home, "set", "hello", "status=dropped", "reason=folded into lane", "into=lane").out
        self.assertIn("told other's lead: transport wait on hello", dropped)
        mail = (self.other / ".git" / "campaign-mail" / "other" / "@" / "msg-1.json").read_text()
        self.assertIn("demo:hello was dropped (folded into lane): your transport wait on it; it folds into lane", mail)
        status = self.ok(self.other, "status").out
        self.assertIn("launchable now: none\n  most waited on: demo:lane (1 waiting)\n", status)
        self.fails(self.home, "set", "lane", "status=dropped", "reason=gone", match="other:transport wait on lane")
        self.ok(self.home, "set", "lane", "status=dropped", "reason=gone", "into=none")
        mail = (self.other / ".git" / "campaign-mail" / "other" / "@" / "msg-2.json").read_text()
        self.assertIn("it has no successor, so they no longer wait on it", mail)
        self.assertNotIn("transport", self.ok(self.home, "check").out)
        self.assertIn("launchable now: transport\n", self.ok(self.other, "status").out, "released, not blocked")
        warned = self.ok(self.other, "check").out
        self.assertIn("transport waits on demo:hello: demo:lane was dropped (gone) with no successor, so it no", warned)
        self.fails(self.other, "set", "transport", "after=demo:lane", match="demo:lane was dropped (gone) with no")
        self.ok(self.home, "new", "x", "--title", "x", "--risk", "low", "--files", "x.py", "--check", "unit=true")
        self.ok(self.home, "set", "x", "status=dropped", "reason=no one waits")  # into= only when someone waits

    def test_a_message_wakes_the_other_campaigns_lead_in_cmux(self) -> None:
        calls = self.root / "cmux-calls"
        fake = self.root / "bin" / "cmux"
        fake.write_text(f'#!/bin/sh\nprintf "%s|" "$@" >> {calls}; echo >> {calls}\n')
        fake.chmod(0o755)
        self.env["CMUX_SURFACE_ID"] = "surface-other"
        self.ok(self.other, "status")  # other's lead is seen in its surface
        self.env["CMUX_SURFACE_ID"] = "surface-demo"
        self.ok(self.home, "status")
        self.ok(self.other, "status")  # demo's lead looking in leaves other's lead where it is
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
        down = self.ok(self.home, "message", "other:", "cmux down", "--from", "x")
        self.assertIn("msg-3 for the lead", down.out)
        self.assertIn("msg-3 waits in other's mailbox unannounced (cmux failed", down.err)
        fake.write_text(f'#!/bin/sh\nprintf "%s|" "$@" >> {calls}; echo >> {calls}\n')
        calls.write_text("")
        self.env["CAMPAIGN_REANNOUNCE"] = "0"
        self.ok(self.home, "check")  # any later command retries it
        self.assertIn("agent|message|surface-other|--from|x|--|campaign other: msg-3 for the lead", calls.read_text())
        calls.write_text("")
        self.ok(self.home, "check")
        self.assertNotIn("agent|message", calls.read_text(), "a message that woke its lead is not sent again")

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

    def test_a_merged_tasks_journal_belongs_to_its_library_quest(self) -> None:
        self.lib_quest(self.home, "api", "--files", "lib.py,util.py")
        self.ok(self.home, "start", "api", "--task", "u", "--files", "util.py")
        t = self.quest("api")["tasks"]["u"]
        code, journal = (self.home / t["worktree"]).resolve(), (self.home / t["journal"]).resolve()
        self.commit(code, {"util.py": "Y = 1\n"})
        self.commit(journal, {".campaign/demo/journal/api--u.md": "done\n"})
        job = self.submit(code, "--quest", "api", "--task", "u", "--", "true")
        self.ok(self.home, "wait", job)
        self.ok(self.home, "task", "merge", "api", "u")
        self.assertNotIn("changes more than its journals", self.ok(self.home, "brief", "api").out)

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
        self.assertIn(
            "api and other:use: other:use consumes contract lib-api, which api changes: tell its lead",
            self.run_cli(self.home, "check").out,
        )
        self.assertIn(
            "use and demo:api: contract lib-api changing in demo:api: build against its brief",
            self.run_cli(self.other, "check").out,
        )

    def test_one_start_sends_one_message_listing_every_overlap_with_a_started_quest(self) -> None:
        for pid, files, consumes in (("cons", "app.py", "o-api,o-fmt"), ("plan", "README.md", "o-api")):
            self.ok(self.home, "new", pid, "--title", pid, "--risk", "low", "--files", files, "--check", "unit=true")
            self.ok(self.home, "set", pid, f"consumes={consumes}")
        self.ok(self.home, "start", "cons")  # `plan` stays planned
        self.ok(
            self.other,
            "new",
            "prov",
            "--title",
            "prov",
            "--risk",
            "low",
            "--files",
            "o.py",
            "--provides",
            "o-api,o-fmt",
            "--check",
            "unit=true",
        )
        started = self.ok(self.other, "start", "prov")
        self.assertEqual(started.out.count("told campaign demo's lead"), 1)
        told = self.ok(self.home, "status").out
        self.assertEqual(told.count("for the lead, from campaign other"), 1, told)
        for text in ("changes contract o-api, which your cons consumes", "changes contract o-fmt, which your cons"):
            self.assertIn(text, told)
        self.assertNotIn("your plan", told, "a planned consumer has not started")
        self.ok(self.home, "start", "plan")  # once started, it tells the provider's lead
        again = self.ok(self.other, "status").out
        self.assertEqual(again.count("for the lead, from campaign demo"), 1, again)
        self.assertIn("demo:plan started and builds against contract o-api, which your prov is changing", again)

    def test_a_start_notice_closes_itself_once_the_order_is_settled(self) -> None:
        self.lib_quest(self.home, "api", "--files", "lib.py")
        for pid in ("use", "more"):
            self.ok(
                self.other,
                "new",
                pid,
                "--title",
                pid,
                "--risk",
                "low",
                "--repo",
                "../lib",
                "--files",
                "lib.py",
                "--check",
                "unit=true",
            )
        self.ok(self.other, "start", "use")
        self.assertIn("msg-1 for the lead, from campaign other: other:use started", self.ok(self.home, "status").out)
        self.ok(self.home, "set", "api", "after=other:use")
        self.assertNotIn("other:use started", self.ok(self.home, "status").out, "ordered after it")
        self.assertEqual(self.state()["messages"][-1]["closed"], "ordered --after other:use")
        self.ok(self.other, "set", "use", "status=dropped", "reason=not now", "into=none")
        self.ok(self.other, "start", "more")
        self.assertIn("other:more started", self.ok(self.home, "brief").out)
        self.ok(self.other, "set", "more", "status=dropped", "reason=not now either")
        self.assertNotIn("other:more started", self.ok(self.home, "brief").out, "it dropped")

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
        self.assertNotIn("may overlap", started.err)  # ordered after it: landing enforces the order
        self.assertIn("told campaign demo's lead that use overlaps its work", started.out)
        told = self.ok(self.home, "status").out  # demo's lead hears it from the tool
        self.assertIn("msg-1 for the lead, from campaign other: other:use started", told)
        self.assertNotIn("may change files", told)
        self.assertIn(" started and builds against contract lib-api, which your api is changing", told)
        self.assertNotIn("msg-2", told)
        mine, theirs = self.run_cli(self.home, "check").out, self.run_cli(self.other, "check").out
        grouped = "api and other:use: other:use consumes contract lib-api, which api changes: tell its lead\n"
        self.assertIn(grouped, mine)
        self.assertEqual(mine.count("other:use"), grouped.count("other:use"), "one warning per peer quest")
        self.assertIn(
            "use and demo:api: contract lib-api changing in demo:api: build against its brief\n",
            theirs,
        )
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
        self.ok(self.other, "new", "x", "--title", "x", "--risk", "low", "--check", "unit=true")
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


class QueueHealth(CampaignCase):
    def fake(self, jid: str, level: str, state: str, submitted: float, spec: Any = None, **times: float) -> str:
        d = self.root / "jobs" / jid
        d.mkdir(parents=True)
        base = {"cmd": "true", "level": level, "minutes": 10, "submitted": submitted, "repo": "", "commit": ""}
        (d / "spec.json").write_text(json.dumps({**base, "after": [], "campaign": "", "quest": "", **(spec or {})}))
        (d / "state.json").write_text(json.dumps({"state": state, **times}))
        return jid

    def test_status_shows_host_use_and_waste_and_check_warns_of_a_stall(self) -> None:
        now, m = time.time(), 60
        self.fake("261110-000001-aaaaaa", "light", "done", now - 51 * m, started=now - 50 * m, ended=now - 26 * m)
        self.fake("261110-000002-bbbbbb", "exclusive", "done", now - 30 * m, started=now - 20 * m, ended=now - 10 * m)
        self.fake("261110-000003-cccccc", "light", "cancelled", now - 15 * m, ended=now - 14 * m)
        self.fake("261110-000004-dddddd", "light", "skipped", now - 6 * m, ended=now - 5 * m)
        self.fake("261110-000005-eeeeee", "light", "failed", now - 9 * m, started=now - 8 * m, ended=now - 7 * m)
        self.assertNotIn("waited over", self.ok(self.home, "check").out)
        stuck = self.fake("261110-000006-ffffff", "light", "queued", now - 120 * m)
        out = self.ok(self.home, "status").out
        self.assertIn(
            "host, last hour: light slots 10% used (25 of 240 slot-minutes), drains 10 min, "
            "runnable work beside idle slots 50 min\n",
            out,
        )
        self.assertIn("jobs ended, last hour: 5, 1 failed, 2 produced nothing (1 cancelled, 1 skipped)\n", out)
        self.assertIn("light slots 90% idle and no --priority -1 work queued", out)
        filler = self.root / "jobs" / self.fake("261110-000007-gggggg", "light", "queued", now - m)
        spec = json.loads((filler / "spec.json").read_text())
        (filler / "spec.json").write_text(json.dumps({**spec, "priority": -1}))
        self.assertNotIn("no --priority -1 work queued", self.ok(self.home, "status").out)
        warned = self.ok(self.home, "check").out
        self.assertRegex(
            warned, rf"1 queued job waited over 30 minutes beside idle slots \(longest {stuck}, 1[01]\d min\)"
        )
        self.assertIn("the worker is down", warned)

    def test_check_notes_a_time_cap_far_above_what_the_runs_took(self) -> None:
        """A kind of run whose --minutes is over twice three times its passed runs' median gets a note."""
        now, m = time.time(), 60
        ident = str(self.home / ".campaign" / "demo")
        for i, took in enumerate((4, 5, 6)):
            what = {"what": f"demo check suite at {i:012x}", "campaign": ident, "minutes": 60}
            self.fake(
                f"261110-00000{i}-aaaaaa",
                "light",
                "done",
                now - 90 * m,
                what,
                started=now - 80 * m,
                ended=now - (80 - took) * m,
            )
        out = self.ok(self.home, "check").out
        self.assertIn(
            "demo check suite: --minutes 60, but its last 3 passed runs took 5.0 min at the median: --minutes 15", out
        )
        tight = {"what": "demo check suite at 0000000000ff", "campaign": ident, "minutes": 30}
        self.fake("261110-000009-aaaaaa", "light", "queued", now - m, tight)
        self.assertNotIn("demo check suite: --minutes", self.ok(self.home, "check").out)
