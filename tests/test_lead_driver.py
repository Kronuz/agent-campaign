"""The lead's to-do list and what wakes the lead: `wait --commits` returns as soon as there is work
for the lead, and status, brief and every wait end with the list."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from harness import CAMPAIGN, CampaignCase


def ago(seconds: float) -> str:
    """An ISO timestamp SECONDS before now, as Claude Code writes them."""
    return datetime.fromtimestamp(time.time() - seconds, timezone.utc).isoformat().replace("+00:00", "Z")


class LeadDriver(CampaignCase):
    def setUp(self) -> None:
        super().setUp()
        self.env["CAMPAIGN_TODO_TICK"] = "0"  # every look makes the list again

    def waiting(self, *args: str) -> subprocess.Popen[str]:
        """`campaign wait --commits` in the background, as a lead runs it."""
        return subprocess.Popen(
            [sys.executable, str(CAMPAIGN), "wait", "--commits", *args],
            cwd=self.home,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def returns(self, waiter: subprocess.Popen[str], within: float = 20) -> str:
        """The waiter's output, once it returned (exit 0) within WITHIN seconds."""
        start = time.time()
        try:
            out, err = waiter.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            waiter.kill()
            out, err = waiter.communicate()
            self.fail(f"the wait never returned:\n{out}{err}")
        self.assertLess(time.time() - start, within, f"the wait returned late:\n{out}")
        self.assertEqual(waiter.returncode, 0, out + err)
        return out

    def quiet_now(self) -> None:
        """A wait that finds the list empty: what the lead has seen so far."""
        self.assertEqual(self.run_cli(self.home, "wait", "--commits", "--timeout", "1").code, 3)

    def journal(self, pid: str, state: str) -> None:
        path = self.wt(pid) / f".campaign/demo/journal/{pid}.md"
        path.write_text(re.sub(r"^State:.*$", f"State: {state}", path.read_text(), count=1, flags=re.M))

    def landed(self, pid: str) -> None:
        self.commit(self.wt(pid), {f"{pid}.py": "done\n"})
        self.verified(pid)
        self.ok(self.home, "land", pid)

    def todo(self, out: str) -> list[str]:
        """The list's items, in order, from a command's output: the lines after its heading."""
        lines = out.splitlines()
        heads = [i for i, x in enumerate(lines) if x.startswith("to do, critical path first")]
        return [x for x in lines[heads[-1] + 1 :] if x.startswith("- ")] if heads else []

    # A1: what makes work for the lead wakes it within a tick

    def test_a_wait_returns_when_a_quests_needs_land(self) -> None:
        self.started("base", files="base.py", check="unit=true")
        self.ok(self.home, "new", "top", "--title", "t", "--risk", "low", "--files", "top.py", "--needs", "base")
        self.commit(self.wt("base"), {"base.py": "done\n"})
        self.verified("base")
        self.run_cli(self.home, "wait", "--commits", "--timeout", "1")  # shown: the landing it is ready for
        waiter = self.waiting("--timeout", "60")
        time.sleep(1)
        self.ok(self.home, "land", "base")
        out = self.returns(waiter)
        self.assertIn("- start top: launchable: `campaign start top`", out)

    def test_a_wait_returns_when_a_file_set_frees(self) -> None:
        self.started("holder", files="lib/**", check="unit=true")
        self.ok(self.home, "new", "user", "--title", "t", "--risk", "low", "--files", "lib/util.py")
        self.quiet_now()
        waiter = self.waiting("--timeout", "60")
        time.sleep(1)
        self.ok(self.home, "set", "holder", "status=dropped", "reason=superseded", "into=none")
        out = self.returns(waiter)
        self.assertIn("- start user: launchable: `campaign start user`", out)

    def test_a_wait_returns_when_a_tasks_jobs_pass(self) -> None:
        self.started("fix", files="app.py,lib/**")
        self.ok(self.home, "start", "fix", "--task", "lib", "--files", "lib/**")
        self.commit(self.wt("fix--lib"), {"lib/util.py": "X = 2\n"})
        self.quiet_now()
        waiter = self.waiting("--timeout", "60")
        time.sleep(1)
        self.submit(self.wt("fix--lib"), "--quest", "fix", "--task", "lib", "--", "true")
        out = self.returns(waiter)
        self.assertIn("- merge fix's task lib: its jobs passed: `campaign task merge fix lib`", out)

    def test_a_wait_returns_when_a_quest_crosses_the_idle_bound(self) -> None:
        self.started("slow", files="app.py")
        self.env["CAMPAIGN_IDLE"] = "8"
        self.quiet_now()  # its journal was just written: a contributor at work
        waiter = self.waiting("--timeout", "60")
        out = self.returns(waiter, within=25)
        self.assertRegex(out, r"- resume slow: nothing moved it for \d+ min: working: `campaign status slow`")

    # A2: every return, status and brief end with the list, critical path first

    def test_status_brief_and_wait_end_with_the_list_in_critical_path_order(self) -> None:
        new = ("--title", "t", "--risk", "low", "--files")
        for k in range(10):
            self.ok(self.home, "new", f"leaf{k}", *new, f"leaf{k}.py")
        self.ok(self.home, "new", "root", *new, "root.py")
        for k in range(2):
            self.ok(self.home, "new", f"after{k}", *new, f"after{k}.py", "--needs", "root")
        self.started("ready", files="ready.py", check="unit=true")
        self.commit(self.wt("ready"), {"ready.py": "done\n"})
        self.verified("ready")
        self.started("asks", files="asks.py")
        self.journal("asks", "blocked on the lead: which wire format")
        self.started("reviewed", files="reviewed.py", risk="high")
        self.journal("reviewed", "ready for review")
        self.started("tasked", files="tasked/**")
        self.ok(self.home, "start", "tasked", "--task", "t1", "--files", "tasked/**")
        job = self.submit(self.wt("tasked--t1"), "--quest", "tasked", "--task", "t1", "--", "true")
        self.ok(self.home, "wait", job)
        self.started("stuck", files="stuck.py")
        self.journal("stuck", "waiting on jobs J1")  # they ended long ago: its contributor is gone
        self.assertEqual(len(self.state()["quests"]), 18)
        want = [
            "- start root: launchable: `campaign start root`",  # two quests wait on it
            *(f"- start leaf{k}: launchable: `campaign start leaf{k}`" for k in range(10)),
        ]
        out = self.ok(self.home, "status").out
        items = self.todo(out)
        self.assertEqual(items[0], want[0], out)
        self.assertEqual([x for x in items if x.startswith("- start")], want)
        kinds = {x.split()[1] for x in items}
        self.assertEqual(kinds, {"start", "land", "answer", "review", "merge", "resume"}, out)
        self.assertIn("- land ready: nothing keeps it from landing: `campaign land ready`", items)
        self.assertIn(
            "- answer asks: its journal waits on the lead: blocked on the lead: which wire format: "
            "`campaign status asks`",
            items,
        )
        self.assertIn(
            "- review reviewed: its journal says ready for review; then launch its correctness review: "
            "`campaign set reviewed status=review`",
            items,
        )
        self.assertIn("- merge tasked's task t1: its jobs passed: `campaign task merge tasked t1`", items)
        self.assertIn("- resume stuck: its jobs ended: waiting on jobs J1: `campaign status stuck`", items)
        self.assertTrue(all(re.search(r": `campaign [^`]+`$", x) for x in items), "each item ends with its command")
        self.assertEqual(out.rstrip().splitlines()[-1], items[-1], "status ends with the list")
        self.assertNotIn("wait again", out)
        shown = [*items[:10], f"- and {len(items) - 10} more: `campaign status`"]  # a brief or wait's ten
        brief = self.ok(self.home, "brief").out
        self.assertEqual(self.todo(brief), shown)
        self.assertEqual(brief.rstrip().splitlines()[-1], shown[-1], "brief ends with the list")
        self.assertEqual(
            json.loads(self.ok(self.home, "status", "--json").out)["todo"]["items"][0]["cmd"], "campaign start root"
        )
        waited = self.ok(self.home, "wait", "--commits", "--timeout", "30").out
        self.assertEqual(self.todo(waited), shown)
        self.assertEqual(waited.rstrip().splitlines()[-1], shown[-1], "the wait ends with the list")
        self.assertNotIn("wait again", waited)

    def test_wait_again_is_printed_only_when_the_list_is_empty(self) -> None:
        wt = self.started("fix", files="app.py")
        self.quiet_now()
        waiter = self.waiting("--timeout", "60")
        time.sleep(1)
        self.commit(wt, {"app.py": "print(2)\n"})
        out = self.returns(waiter)
        self.assertIn("fix: quest/demo/fix moved to", out)
        self.assertEqual(self.todo(out), [])
        self.assertTrue(out.rstrip().endswith("the lead has nothing to do: wait again after this"), out)
        self.ok(self.home, "new", "next", "--title", "t", "--risk", "low", "--files", "next.py")
        out = self.ok(self.home, "wait", "--commits", "--timeout", "30").out
        self.assertEqual(self.todo(out), ["- start next: launchable: `campaign start next`"])
        self.assertNotIn("wait again", out)

    # A3: a wait with work returns at once; an item left undone fires again after a bound, never sooner

    def test_a_wait_started_with_work_returns_at_once_and_repeats_only_after_the_bound(self) -> None:
        self.env["CAMPAIGN_TODO_REPEAT"] = "8"
        self.ok(self.home, "new", "next", "--title", "t", "--risk", "low", "--files", "next.py")
        start = time.time()
        out = self.ok(self.home, "wait", "--commits", "--timeout", "60").out
        shown = time.time()
        self.assertLess(shown - start, 10, "a wait with work returned late")
        self.assertIn("- start next: launchable: `campaign start next`", out)
        again = self.run_cli(self.home, "wait", "--commits", "--timeout", "2")
        self.assertEqual(again.code, 3, f"an item shown fired again before its bound:\n{again.text}")
        self.assertIn("- start next: launchable", again.out, "a timed-out wait still ends with the list")
        out = self.ok(self.home, "wait", "--commits", "--timeout", "60").out
        self.assertGreater(time.time() - shown, 7, "it fired again sooner than its bound")
        self.assertLess(time.time() - shown, 25, "it fired again long after its bound")
        self.assertIn("- start next: launchable", out)
        self.assertEqual(self.run_cli(self.home, "wait", "--commits", "--timeout", "2").code, 3)

    # A5: waiting on jobs, CI or a drain never empties the list

    def test_quests_waiting_on_ci_or_a_queued_job_never_hide_other_work(self) -> None:
        self.env["CAMPAIGN_IDLE"] = "0"  # no contributor at work anywhere
        state = self.state()
        state["ci"] = "github"
        (self.home / ".campaign/demo/state.json").write_text(json.dumps(state))
        self.git(self.home, "commit", "-q", "-am", "require CI")
        gh = self.root / "bin" / "gh"
        self.started("oncl", files="oncl.py", check="unit=true")
        pushed = self.commit(self.wt("oncl"), {"oncl.py": "done\n"})  # CI runs only at this commit
        queued = '[{"name": "build", "status": "queued", "conclusion": ""}]'
        gh.write_text(f'#!/bin/sh\ncase "$*" in *{pushed}*) echo \'{queued}\';; *) echo "[]";; esac\n')
        gh.chmod(0o755)
        self.journal("oncl", "waiting on CI")
        self.started("drain", files="drain.py")
        blocker = self.submit(self.home, "--", "sleep", "30")
        self.submit(self.wt("drain"), "--quest", "drain", "--level", "exclusive", "--after", blocker, "--", "true")
        self.journal("drain", "waiting on jobs: the exclusive timing")
        self.ok(self.home, "new", "third", "--title", "t", "--risk", "low", "--files", "third.py")
        self.started("fourth", files="fourth.py")
        start = time.time()
        out = self.ok(self.home, "wait", "--commits", "--timeout", "60").out
        self.assertLess(time.time() - start, 10, "the wait waited on the jobs and CI")
        items = self.todo(out)
        self.assertIn("- start third: launchable: `campaign start third`", items)
        self.assertTrue(any(x.startswith("- resume fourth: nothing moved it") for x in items), out)
        self.assertFalse([x for x in items if "oncl" in x or "drain" in x], "a quest with a live waiter is moving")
        self.assertIn("in flight: 0 worked on, 2 waiting on jobs, CI or a landing, 1 needing the lead", out)

    # A6: a landing waiting on live CI is not the lead's to move

    def test_a_landing_waiting_on_queued_ci_is_not_listed_as_needing_the_lead(self) -> None:
        self.env["CAMPAIGN_IDLE"] = "0"
        state = self.state()
        state["ci"] = "github"
        (self.home / ".campaign/demo/state.json").write_text(json.dumps(state))
        self.git(self.home, "commit", "-q", "-am", "require CI")
        gh = self.root / "bin" / "gh"
        gh.write_text('#!/bin/sh\necho \'[{"name": "build", "status": "queued", "conclusion": ""}]\'\n')
        gh.chmod(0o755)
        wt = self.started("install", files="app.py", check="unit=true")
        self.commit(wt, {"app.py": "print(2)\n"})
        self.verified("install")
        self.journal("install", "ready to land; waiting on CI")
        self.ok(self.home, "new", "other", "--title", "t", "--risk", "low", "--files", "other.py")
        out = self.ok(self.home, "status").out
        self.assertNotIn("nothing moves without the lead", out, "its waiter on CI moves it")
        items = self.todo(out)
        self.assertEqual(items, ["- start other: launchable: `campaign start other`"], out)
        gh.write_text('#!/bin/sh\necho \'[{"name": "build", "status": "completed", "conclusion": "success"}]\'\n')
        self.assertIn(
            "- land install: nothing keeps it from landing: `campaign land install`",
            self.todo(self.ok(self.home, "status").out),
        )

    # A4: a lead stuck on one tool call (a permission prompt) is the owner's to see

    def transcript(self, lines: list[dict[str, object]], started: Path | None = None) -> Path:
        """A Claude Code session started in STARTED (the home): its folder is named after that path,
        and its entries record it as their `cwd`."""
        where = started or self.home
        folder = self.root / "claude" / re.sub(r"[^A-Za-z0-9]", "-", str(where))
        folder.mkdir(parents=True, exist_ok=True)
        log = folder / "lead-session.jsonl"
        log.write_text("".join(json.dumps({"cwd": str(where), **x}) + "\n" for x in lines))
        return log

    def alarms(self) -> Path:
        """The file a fake cmux writes each notice to, with the alarm looking at every wait."""
        calls, fake = self.root / "cmux-calls", self.root / "bin" / "cmux"
        fake.write_text(f'#!/bin/sh\nprintf "%s|" "$@" >> {calls}; echo >> {calls}\n')
        fake.chmod(0o755)
        self.env.update(CMUX_WORKSPACE_ID="ws-demo", CAMPAIGN_ALARM_SECONDS="0")
        return calls

    @staticmethod
    def use(tid: str, name: str, seconds: float, sidechain: bool = False) -> dict[str, object]:
        content = [{"type": "tool_use", "id": tid, "name": name, "input": {"command": "make"}}]
        return {
            "type": "assistant",
            "timestamp": ago(seconds),
            "isSidechain": sidechain,
            "message": {"content": content},
        }

    def test_a_lead_stuck_on_a_tool_call_alarms_the_owner_once(self) -> None:
        calls = self.alarms()
        use = self.use

        def result(tid: str) -> dict[str, object]:
            content = [{"type": "tool_result", "tool_use_id": tid, "content": "ok"}]
            return {"type": "user", "timestamp": ago(0), "message": {"content": content}}

        self.started("fix", files="app.py")
        self.transcript(
            [
                use("t-answered", "Bash", 3000),
                result("t-answered"),
                use("t-sub", "Bash", 3000, True),
                use("t-fresh", "Read", 60),
            ]
        )
        self.run_cli(self.home, "wait", "--commits", "--timeout", "1")
        self.assertNotIn("stuck", calls.read_text() if calls.exists() else "", "answered, a subagent's or fresh")
        self.transcript([use("t-answered", "Bash", 3000), result("t-answered"), use("t-stuck", "Bash", 1000)])
        r = self.run_cli(self.home, "wait", "--commits", "--timeout", "1")
        self.assertIn("campaign: demo's lead is stuck on a Bash call, unanswered for 17 min", r.err)
        self.assertEqual(calls.read_text().count("notify|--title|campaign: demo's lead is stuck on a Bash call|"), 1)
        self.run_cli(self.home, "wait", "--commits", "--timeout", "1")
        self.assertEqual(calls.read_text().count("lead is stuck"), 1, "the owner was told twice of one call")

    def test_a_lead_started_through_a_symlink_is_watched(self) -> None:
        calls = self.alarms()
        self.started("fix", files="app.py")
        link = self.root / "link-home"
        link.symlink_to(self.home)
        self.transcript([self.use("t-stuck", "Bash", 1000)], started=link)
        r = self.run_cli(self.home, "wait", "--commits", "--timeout", "1")
        self.assertIn("campaign: demo's lead is stuck on a Bash call", r.err)
        self.assertEqual(calls.read_text().count("lead is stuck"), 1)

    def test_a_ten_minute_foreground_call_raises_no_alarm(self) -> None:
        calls = self.alarms()
        self.started("fix", files="app.py")
        self.transcript([self.use("t-build", "Bash", 660)])  # a foreground call at its limit, answered late
        r = self.run_cli(self.home, "wait", "--commits", "--timeout", "1")
        self.assertNotIn("stuck", r.err + (calls.read_text() if calls.exists() else ""))

    def test_no_transcript_raises_no_alarm(self) -> None:
        calls = self.alarms()
        self.started("fix", files="app.py")
        r = self.run_cli(self.home, "wait", "--commits", "--timeout", "1")
        self.assertEqual(r.code, 3, r.text)
        self.assertNotIn("stuck", r.err + (calls.read_text() if calls.exists() else ""))

    # A6: CI answers cost one GitHub call per quest head per interval, across processes

    def test_ci_is_asked_once_per_head_per_interval_across_processes(self) -> None:
        self.env.update(CAMPAIGN_IDLE="0", CAMPAIGN_CI_SECONDS="120")
        state = self.state()
        state["ci"] = "github"
        (self.home / ".campaign/demo/state.json").write_text(json.dumps(state))
        self.git(self.home, "commit", "-q", "-am", "require CI")
        asked, gh = self.root / "gh-calls", self.root / "bin" / "gh"
        queued = '[{"name": "build", "status": "queued", "conclusion": ""}]'
        gh.write_text(f"#!/bin/sh\necho \"$*\" >> {asked}\necho '{queued}'\n")
        gh.chmod(0o755)
        names = [f"q{i:02}" for i in range(20)]
        for k in names:
            self.commit(self.started(k, files=f"{k}.py"), {f"{k}.py": "done\n"})
            self.journal(k, "waiting on CI")
        status = subprocess.Popen(
            [sys.executable, str(CAMPAIGN), "status"], cwd=self.home, env=self.env, stdout=subprocess.PIPE, text=True
        )
        self.run_cli(self.home, "wait", "--commits", "--timeout", "3")
        status.communicate(timeout=120)
        self.ok(self.home, "status")
        self.ok(self.home, "status")
        self.ok(self.home, "brief")
        calls = asked.read_text().splitlines()
        self.assertEqual(len([x for x in calls if x.startswith("run list --commit")]), 20, calls)
        self.assertLessEqual(len(calls), 20, "a head's CI was asked twice in one interval")

    def github(self, answer: str = '[{"name": "build", "status": "queued", "conclusion": ""}]') -> Path:
        """CI on GitHub, through a gh stub that logs each call to gh-calls, answers gh-answer, sleeps
        while gh-sleep exists and exits with gh-exit's code; the stub's answers last 120 s."""
        self.env["CAMPAIGN_CI_SECONDS"] = "120"
        state = self.state()
        state["ci"] = "github"
        (self.home / ".campaign/demo/state.json").write_text(json.dumps(state))
        self.git(self.home, "commit", "-q", "-am", "require CI")
        r = self.root
        (r / "gh-answer").write_text(answer + "\n")
        (r / "gh-exit").write_text("0\n")
        gh = r / "bin" / "gh"
        gh.write_text(
            f'#!/bin/sh\necho "$*" >> {r}/gh-calls\nwhile [ -e {r}/gh-sleep ]; do sleep 0.2; done\n'
            f"cat {r}/gh-answer\nexit $(cat {r}/gh-exit)\n"
        )
        gh.chmod(0o755)
        return r / "gh-calls"

    def asked(self, log: Path, what: str = "run list --commit") -> list[str]:
        return [x for x in log.read_text().splitlines() if x.startswith(what)] if log.exists() else []

    def test_a_commit_no_remote_holds_is_never_asked_about(self) -> None:
        log = self.github()
        bare = self.root / "origin.git"
        self.git(self.root, "init", "-q", "--bare", str(bare))
        self.git(self.home, "remote", "add", "origin", str(bare))
        self.git(self.home, "push", "-q", "origin", "HEAD")
        wt = self.started("fix", files="app.py")
        head = self.commit(wt, {"app.py": "print(2)\n"})
        self.ok(self.home, "status")
        self.assertEqual(self.asked(log), [], "GitHub was asked about a commit it never saw")
        self.git(wt, "push", "-q", "origin", "HEAD")
        self.ok(self.home, "status")
        self.assertEqual([x.split()[3] for x in self.asked(log)], [head])

    def test_a_failed_answer_is_kept_and_a_broken_cache_file_is_a_miss(self) -> None:
        log = self.github()
        self.started("fix", files="app.py")
        (self.root / "gh-exit").write_text("1\n")  # an expired login
        self.ok(self.home, "status")
        self.ok(self.home, "status")
        calls = self.asked(log)
        self.assertTrue(calls)
        self.assertEqual(len(calls), len(set(calls)), "every process asked again after a failure")
        cache = sorted((self.root / "jobs" / ".gh").glob("*.json"))
        for text in ("null", "[1, 2]", '{"until": "x", "data": 1}'):
            for f in cache:
                f.write_text(text)
            self.ok(self.home, "status")

    def test_answers_untouched_for_a_day_are_pruned(self) -> None:
        self.github()
        self.started("fix", files="app.py")
        folder = self.root / "jobs" / ".gh"
        folder.mkdir(parents=True, exist_ok=True)
        old = folder / f"{'0' * 32}.json"
        old.write_text("{}")
        os.utime(old, (time.time() - 2 * 86400,) * 2)
        self.ok(self.home, "status")
        self.assertFalse(old.exists())

    def test_a_hung_gh_call_holds_no_other_process_long(self) -> None:
        self.github('[{"name": "build", "status": "completed", "conclusion": "success"}]')
        self.started("fix", files="app.py")
        (self.root / "gh-sleep").write_text("")
        first = subprocess.Popen(
            [sys.executable, str(CAMPAIGN), "status"], cwd=self.home, env=self.env, stdout=subprocess.PIPE, text=True
        )
        time.sleep(4)
        (self.root / "gh-sleep").unlink()  # GitHub answers at once for everyone else
        start = time.time()
        self.ok(self.home, "status")
        self.assertLess(time.time() - start, 20, "one hung gh call held another process")
        first.communicate(timeout=120)

    # A5: a quest that waits on the owner is the lead's to park, never to resume

    def test_a_quest_waiting_on_the_owner_is_parked_not_resumed(self) -> None:
        self.env["CAMPAIGN_IDLE"] = "0"
        self.started("fix", files="app.py")
        self.journal("fix", "blocked on the owner: A6's decision")
        park = "campaign set fix status=blocked until=owner reason=owner"
        want = f"- park fix: its journal waits on the owner: blocked on the owner: A6's decision: `{park}`"
        self.assertEqual(self.todo(self.ok(self.home, "status").out), [want])
        self.ok(self.home, *park.split()[1:])
        self.assertEqual(self.todo(self.ok(self.home, "status").out), [])

    def test_a_quest_waiting_on_owner_and_lead_stays_the_leads(self) -> None:
        self.env["CAMPAIGN_IDLE"] = "0"
        self.started("fix", files="app.py")
        self.journal("fix", "blocked on owner and lead: the wire format")
        want = "- answer fix: its journal waits on the lead: blocked on owner and lead: the wire format"
        self.assertEqual(self.todo(self.ok(self.home, "status").out), [f"{want}: `campaign status fix`"])

    # A2: a wait or brief prints ten items and counts the rest; status prints them all

    def test_the_list_shows_ten_items_and_counts_the_rest(self) -> None:
        names = [f"q{i:02}" for i in range(12)]
        for k in names:
            self.ok(self.home, "new", k, "--title", "t", "--risk", "low", "--files", f"{k}.py")
        out = self.ok(self.home, "wait", "--commits", "--timeout", "30").out
        items = self.todo(out)
        self.assertEqual(items[:10], [f"- start {k}: launchable: `campaign start {k}`" for k in names[:10]], out)
        self.assertEqual(items[10:], ["- and 2 more: `campaign status`"])
        self.assertEqual(len(self.todo(self.ok(self.home, "status").out)), 12)

    def test_the_item_that_woke_the_lead_is_printed(self) -> None:
        for i in range(11):
            self.ok(self.home, "new", f"q{i:02}", "--title", "t", "--risk", "low", "--files", f"q{i:02}.py")
        self.ok(self.home, "wait", "--commits", "--timeout", "30")  # ten shown, the eleventh due again
        self.ok(self.home, "wait", "--commits", "--timeout", "30")  # the eleventh shown
        self.ok(self.home, "new", "zz-new", "--title", "t", "--risk", "low", "--files", "zz.py")
        r = self.run_cli(self.home, "wait", "--commits", "--timeout", "30")
        self.assertEqual(r.code, 0)
        self.assertEqual(self.todo(r.out)[0], "- start zz-new: launchable: `campaign start zz-new`", r.out)
