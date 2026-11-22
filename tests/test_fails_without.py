"""A --fails-without job is evidence only when the code under test built and a test ran and failed."""

from __future__ import annotations

import shutil
from pathlib import Path

from harness import CampaignCase, campaign_module


class FailsWithout(CampaignCase):
    def quest_with_a_test(self) -> Path:
        """Quest `fix` with criterion A1, its fix committed, and its plain test passing at the head."""
        wt = self.started("fix", files="app.py")
        brief = self.home / ".campaign/demo/quests/fix.md"
        brief.write_text(brief.read_text().replace("None in this test.", "- A1 prints two: a regression test"))
        self.git(self.home, "commit", "-qam", "criteria")
        self.commit(wt, {"app.py": "print(2)\n"})
        self.submit(wt, "--quest", "fix", "--for", "A1", "--", "python3 app.py | grep -q 2")
        self.ok(self.home, "verify", "fix")
        return wt

    def mutants(self, wt: Path, *commands: list[str]) -> list[str]:
        args = ("--quest", "fix", "--for", "A1", "--fails-without", "--commit", "main")
        jids = [self.submit(wt, *args, *cmd) for cmd in commands]
        self.run_cli(self.home, "wait", "--quest", "fix", "--timeout", "60")
        return jids

    def refused(self, wt: Path, jids: list[str], *whys: str) -> None:
        """Each of JIDS failed, and brief, status and land's gate all read A1 as without evidence."""
        brief = self.ok(wt, "brief").out
        self.assertEqual(len(jids), len(whys))
        for jid, why in zip(jids, whys):
            self.assertEqual(self.job(jid)["state"], "failed", jid)
            self.assertIn(why, self.job(jid)["note"], jid)
            self.assertIn(f"{jid} failed: {why}", brief)
        self.assertNotIn("A1  met", self.ok(self.home, "status", "fix").out)
        self.fails(self.home, "land", "fix", match="criteria without evidence")

    def test_a_mutant_that_did_not_build_is_not_evidence(self) -> None:
        wt = self.quest_with_a_test()
        (broken,) = self.mutants(wt, ["--build", "exit 3", "--", "exit 1"])
        self.assertEqual(self.job(broken)["build"]["exit"], 3)
        self.refused(wt, [broken], "the mutant did not build")
        campaign = campaign_module()
        record = {"state": "done", "exit": 1, "build": {"exit": 2}}  # one place reads the build's fact
        self.assertFalse(campaign.fails_without_met({"id": "j", "spec": {"fails_without": 2}, "state": record}))

    def test_a_command_that_did_not_run_is_not_evidence(self) -> None:
        wt = self.quest_with_a_test()
        jids = self.mutants(wt, ["--", "exit 125"], ["--", "no-such-command-here"], ["--", "/etc/hosts"])
        self.refused(wt, jids, "exited 125 (git bisect's skip", "exit 127", "exit 126")
        campaign = campaign_module()  # a record finished as "done" before 125 was read as not run
        old = {"id": "j", "spec": {"fails_without": campaign.FAILS_WITHOUT}, "state": {"state": "done", "exit": 125}}
        self.assertFalse(campaign.fails_without_met(old))
        self.assertIn("command's own refusal", campaign.fails_without_why(old))

    def test_a_test_killed_from_outside_is_not_evidence_and_a_crash_is(self) -> None:
        wt = self.quest_with_a_test()
        jids = self.mutants(wt, ["--", "kill -KILL $$"], ["--", "kill -TERM $$"])
        self.refused(wt, jids, "exit 137: it was killed by SIGKILL", "exit 143: it was killed by SIGTERM")
        (crash,) = self.mutants(wt, ["--", "kill -SEGV $$"])  # a mutant that crashes its test is a kill
        self.assertEqual(self.job(crash)["state"], "done", self.job(crash))
        self.assertIn("A1  met", self.ok(self.home, "status", "fix").out)

    def test_a_test_that_timed_out_or_was_interrupted_is_not_evidence(self) -> None:
        wt = self.quest_with_a_test()
        timeout = "1/1 Test #1: t ......***Timeout   1.09 sec"  # ctest: past the test's TIMEOUT
        # SIGINT at its default action, then sent to itself: a shell may ignore it (Linux bash -c does)
        sigint = "python3 -c 'import os, signal; signal.signal(2, signal.SIG_DFL); os.kill(os.getpid(), 2)'"
        commands = [["--", f"echo '{timeout}'; exit 8"], ["--", sigint]]
        whys = ["no test result (1/1 Test #1: t .", "exit 130: it was killed by SIGINT"]
        if shutil.which("ctest"):  # the real one, a test past its TIMEOUT
            slow = "add_test(t /bin/sleep 30)\\nset_tests_properties(t PROPERTIES TIMEOUT 1)\\n"
            commands.append(["--", f"mkdir -p s && cd s && printf '{slow}' > CTestTestfile.cmake && ctest"])
            whys.append("no test result (1/1 Test #1: t .")
        self.refused(wt, self.mutants(wt, *commands), *whys)

    def test_a_ctest_run_that_ran_no_test_is_not_evidence(self) -> None:
        wt = self.quest_with_a_test()
        not_run = "1/1 Test #1: t ......***Not Run   0.00 sec"  # ctest: the test's executable was never built
        unittest = "printf 'import unittest\\nclass T(unittest.TestCase):\\n def test_a(self): pass\\n' > t_test.py"
        commands = [
            ["--", "echo 'No tests were found!!!'; exit 8"],  # ctest --no-tests=error's output
            ["--", f"echo '{not_run}'; exit 8"],
            ["--", f"{unittest} && python3 -m unittest -k nothing t_test"],  # NO TESTS RAN, exit 5
            ["--", "echo 'import nosuch' > bad_test.py && python3 -m unittest bad_test"],  # failed to import
        ]
        if shutil.which("ctest"):  # the real one: its line, whatever its version exits, then forced nonzero
            commands.append(["--", "mkdir -p e && cd e && ctest -R '^nothing$'; exit 8"])
            missing = "mkdir -p m && cd m && echo 'add_test(t /nonexistent/t_bin)' > CTestTestfile.cmake"
            commands.append(["--", f"{missing} && ctest -R '^t$'; exit 8"])
        own = [["--", "mkdir -p o && cd o && ctest -R '^nothing$' --no-tests=error"]] if shutil.which("ctest") else []
        jids = self.mutants(wt, *commands, *own)  # OWN: a ctest's own exit, 0 on some versions
        self.refused(wt, jids, *["no test result ("] * len(commands), *[""] * len(own))
        for jid, line in zip(jids, ("No tests were found", "***Not Run", "NO TESTS RAN", "Failed to import")):
            self.assertIn(line, self.job(jid)["note"], jid)
        for jid in jids[len(commands) :]:  # not evidence by either rule: it passed, or it ran no test
            note = self.job(jid)["note"]
            self.assertTrue(note.startswith(("the test passed without the fix", "no test result (No tests")), note)
        (real,) = self.mutants(wt, ["--", "python3 app.py | grep -q 2"])  # main prints 1: the test fails
        self.assertEqual(self.job(real)["state"], "done")
        self.assertIn("A1  met", self.ok(self.home, "status", "fix").out)
        self.ok(self.home, "land", "fix")
