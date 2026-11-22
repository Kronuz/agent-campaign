"""A quest whose own CI run is queued or running at its head is waiting on CI, never a to-do item,
whatever repository it lives in (detent msg-24)."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

from harness import CAMPAIGN, CampaignCase

QUEUED_SUITE = '{"check_suites": [{"app": {"slug": "github-actions"}, "status": "queued", "conclusion": null}]}'


class CiWaiter(CampaignCase):
    def setUp(self) -> None:
        super().setUp()
        self.env.update(CAMPAIGN_IDLE="0", CAMPAIGN_TODO_TICK="0")  # no contributor at work anywhere
        self.lib = self.repo("lib", {"lib.py": "X = 1\n"})
        self.ok(self.home, "config", "ci=github")
        r = self.root
        (r / "gh-runs").write_text("[]\n")  # GitHub's run list omits a queued run (msg-24)
        (r / "gh-suites").write_text(QUEUED_SUITE + "\n")
        gh = r / "bin" / "gh"
        gh.write_text(
            f'#!/bin/sh\necho "$*" >> {r}/gh-calls\n'
            f'case "$1" in run) cat {r}/gh-runs;; api) cat {r}/gh-suites;; *) echo "{{}}";; esac\n'
        )
        gh.chmod(0o755)

    def lib_quest(self, pid: str) -> Path:
        """A quest in ../lib, not the home's repository, whose jobs passed and ended."""
        self.ok(self.home, "new", pid, "--title", pid, "--risk", "low", "--repo", "../lib", "--check", "unit=true")
        self.ok(self.home, "start", pid)
        wt = self.root / "lib-wt" / "demo" / pid
        self.commit(wt, {"lib.py": "X = 2\n"})
        self.verified(pid)
        path = self.wt(pid) / f".campaign/demo/journal/{pid}.md"  # its journal's worktree, in the home
        state = "State: waiting on CI run 44989408833"
        path.write_text(re.sub(r"^State:.*$", state, path.read_text(), count=1, flags=re.M))
        return wt

    def runs(self, status: str, conclusion: str) -> None:
        run = {"databaseId": 7, "name": "CI", "status": status, "conclusion": conclusion}
        (self.root / "gh-runs").write_text(json.dumps([run]) + "\n")

    def todo(self, out: str) -> list[str]:
        lines = out.splitlines()
        heads = [i for i, x in enumerate(lines) if x.startswith("to do, critical path first")]
        return [x for x in lines[heads[-1] + 1 :] if x.startswith("- ")] if heads else []

    # A1: msg-24, a queued run GitHub does not list yet

    def test_a_queued_run_github_does_not_list_is_a_waiter(self) -> None:
        self.lib_quest("flakes")
        out = self.ok(self.home, "status").out
        self.assertEqual([x for x in self.todo(out) if "flakes" in x], [], out)
        self.assertIn("running: a queued run GitHub does not list yet", out)

    # A2: running, then completed green (land) and red (resume)

    def test_running_then_green_lands_and_red_resumes(self) -> None:
        self.lib_quest("flakes")
        self.runs("in_progress", "")
        self.assertEqual([x for x in self.todo(self.ok(self.home, "status").out) if "flakes" in x], [])
        self.runs("completed", "success")
        self.assertIn(
            "- land flakes: nothing keeps it from landing: `campaign land flakes`",
            self.todo(self.ok(self.home, "status").out),
        )
        self.runs("completed", "failure")
        items = [x for x in self.todo(self.ok(self.home, "status").out) if "flakes" in x]
        self.assertEqual(len(items), 1, items)
        self.assertTrue(items[0].startswith("- resume flakes: its jobs ended"), items)

    # A3: an item wakes no sooner than CAMPAIGN_TODO_REPEAT; other wakes print it as it stands

    def test_an_item_wakes_once_per_bound_and_other_wakes_only_print_it(self) -> None:
        self.env["CAMPAIGN_TODO_REPEAT"] = "20"
        self.lib_quest("flakes")
        self.runs("completed", "failure")  # a resume item
        other = self.started("other", files="app.py")
        start = time.time()
        first = self.ok(self.home, "wait", "--commits", "--timeout", "30").out
        self.assertTrue(any(x.startswith("- resume flakes") for x in self.todo(first)), first)
        self.assertEqual(
            self.run_cli(self.home, "wait", "--commits", "--timeout", "2").code, 3, "it woke again at once"
        )
        waiter = subprocess.Popen(
            [sys.executable, str(CAMPAIGN), "wait", "--commits", "--timeout", "30"],
            cwd=self.home,
            env=self.env,
            stdout=subprocess.PIPE,
            text=True,
        )
        time.sleep(1)
        self.commit(other, {"app.py": "print(2)\n"})  # another event wakes the lead
        out, _ = waiter.communicate(timeout=60)
        self.assertIn("other: quest/demo/other moved to", out)
        self.assertTrue(any(x.startswith("- resume flakes") for x in self.todo(out)), "every return prints the list")
        self.assertLess(time.time() - start, 20, "the test ran past the bound")
        again = self.ok(self.home, "wait", "--commits", "--timeout", "30").out  # due again only past the bound
        self.assertGreater(time.time() - start, 20, "it fired again before its bound")
        self.assertTrue(any(x.startswith("- resume flakes") for x in self.todo(again)), again)

    # A4: a task's State names jobs tagged with its quest only; they are queued, so it is waiting

    def test_jobs_a_state_names_are_its_waiters_whatever_their_tag(self) -> None:
        self.started("heal", files="heal/**")
        self.ok(self.home, "start", "heal", "--task", "witness", "--files", "heal/**")
        blocker = self.submit(self.home, "--", "sleep", "30")
        jid = self.submit(self.wt("heal"), "--quest", "heal", "--after", blocker, "--", "true")  # no --task
        path = self.wt("heal--witness") / ".campaign/demo/journal/heal--witness.md"
        path.write_text(re.sub(r"^State:.*$", f"State: waiting on jobs {jid}", path.read_text(), count=1, flags=re.M))
        items = [x for x in self.todo(self.ok(self.home, "status").out) if "witness" in x]
        self.assertEqual(items, [], "a queued job its State names is a live waiter")
        self.ok(self.home, "job", "cancel", jid)
        items = [x for x in self.todo(self.ok(self.home, "status").out) if "witness" in x]
        self.assertTrue(items and items[0].startswith("- resume heal's task witness: its jobs ended"), items)
