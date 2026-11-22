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
        for jid, why in zip(jids, whys, strict=True):
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
        self.refused(wt, jids, "exit 125: the command did not run", "exit 127", "exit 126")
        campaign = campaign_module()  # a record finished as "done" before 125 was read as not run
        old = {"id": "j", "spec": {"fails_without": campaign.FAILS_WITHOUT}, "state": {"state": "done", "exit": 125}}
        self.assertFalse(campaign.fails_without_met(old))
        self.assertIn("checkout failed", campaign.fails_without_why(old))

    def test_a_ctest_run_that_ran_no_test_is_not_evidence(self) -> None:
        wt = self.quest_with_a_test()
        commands = [["--", "echo 'No tests were found!!!'; exit 8"]]  # ctest --no-tests=error's output
        if shutil.which("ctest"):  # the real one, its -R matching nothing
            commands.append(["--", "mkdir -p e && cd e && ctest -R '^nothing$' --no-tests=error"])
        jids = self.mutants(wt, *commands)
        self.assertTrue(all(self.job(j)["exit"] != 0 for j in jids), "ran no test, yet exited 0")
        self.refused(wt, jids, *["ctest ran no test"] * len(jids))
        (real,) = self.mutants(wt, ["--", "python3 app.py | grep -q 2"])  # main prints 1: the test fails
        self.assertEqual(self.job(real)["state"], "done")
        self.assertIn("A1  met", self.ok(self.home, "status", "fix").out)
        self.ok(self.home, "land", "fix")
