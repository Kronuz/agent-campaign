"""CI green at a quest's head carries across a clean merge from main; main's CI after such a landing."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest import mock

from harness import CampaignCase, campaign_module

LINES = "".join(f"L{i} = {i}\n" for i in range(12))  # far-apart lines merge without a conflict


class CiReuse(CampaignCase):
    def setUp(self) -> None:
        super().setUp()
        self.ok(self.home, "config", "ci=github")
        self.commit(self.home, {"lib/util.py": LINES, "other/o.txt": "0\n"})
        self.runs: dict[str, list[dict[str, Any]]] = {}
        gh = self.root / "bin" / "gh"
        gh.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            f"runs = json.load(open({str(self.root / 'gh.json')!r}))\n"
            "a = sys.argv[1:]\n"
            f"open({str(self.root / 'gh.log')!r}, 'a').write(' '.join(a) + '\\n')\n"
            "if a[:2] == ['run', 'list']:\n"
            "    print(json.dumps(runs.get(a[a.index('--commit') + 1], [])))\n"
            "else:\n"
            "    print(json.dumps({'jobs': []}))\n"
        )
        gh.chmod(0o755)
        self.ci({})

    def ci(self, runs: dict[str, str]) -> None:
        """CI's runs: commit -> the conclusion of its one "build" run ("" while it runs)."""
        for i, (commit, conclusion) in enumerate(runs.items(), len(self.runs) + 1):
            status = "completed" if conclusion else "in_progress"
            self.runs[commit] = [{"databaseId": i, "name": "build", "status": status, "conclusion": conclusion}]
        (self.root / "gh.json").write_text(json.dumps(self.runs))

    def quest_green_at_head(self, change: str) -> tuple[Path, str]:
        """Quest b changes lib/util.py's line CHANGE; its check and CI pass at its head."""
        b = self.started("b", files="lib/**")
        head = self.commit(b, {"lib/util.py": LINES.replace("L1 = 1", f"L1 = {change}")})
        self.verified("b")
        self.ci({head: "success"})
        return b, head

    def test_a_clean_merge_lands_on_the_heads_ci(self) -> None:
        b, head = self.quest_green_at_head("10")
        # main changes another file and, in another hunk, the file the quest changed
        self.commit(self.home, {"other/o.txt": "1\n", "lib/util.py": LINES.replace("L11 = 11", "L11 = 110")})
        merged = self.fails(
            self.home,
            "land",
            "b",
            "--merge-main",
            match=f"not pushed: it is clean, so the land gate reads CI at {head[:12]}",
        )
        self.assertIn("check unit is not green", merged.text)  # main touched its file: the local check reruns
        self.assertNotIn("\n  CI at", merged.text)  # the head's CI speaks for the merge
        merge = self.git(b, "rev-parse", "HEAD")
        self.assertNotIn(merge, self.runs)
        self.verified("b")
        self.ok(self.home, "land", "b")
        (landing,) = self.quest("b")["landed"]
        self.assertEqual(landing["tested"], merge)  # landed at the merge, which CI never ran at
        self.assertEqual(landing["ci"], {"commit": head, "runs": [1], "carried": True})

    def test_a_red_run_at_the_clean_merge_still_refuses(self) -> None:
        b, _ = self.quest_green_at_head("10")
        self.commit(self.home, {"other/o.txt": "1\n"})
        self.git(b, "merge", "-q", "--no-edit", "main")
        merge = self.git(b, "rev-parse", "HEAD")
        self.ci({merge: ""})  # running at the merge: the head's green decides
        self.assertNotIn("CI at", self.ok(self.home, "brief", "b").out)
        self.ci({merge: "failure"})
        self.fails(self.home, "land", "b", match=f"CI at {merge[:12]}: failed: build")

    def test_a_resolved_conflict_waits_for_ci_at_the_merge(self) -> None:
        b, _ = self.quest_green_at_head("10")
        self.commit(self.home, {"lib/util.py": LINES.replace("L1 = 1", "L1 = 100")})
        self.fails(self.home, "land", "b", "--merge-main", match="conflicts in lib/util.py")
        with self.assertRaises(AssertionError):  # git stops at the conflict
            self.git(b, "merge", "main")
        (b / "lib/util.py").write_text(LINES.replace("L1 = 1", "L1 = 1000"))
        self.git(b, "commit", "-qam", "Merge main, resolved")
        merge = self.git(b, "rev-parse", "HEAD")
        self.verified("b")
        self.fails(self.home, "land", "b", match=f"CI at {merge[:12]}: no CI runs")
        self.ci({merge: ""})
        self.fails(self.home, "land", "b", match=f"CI at {merge[:12]}: running: build")
        self.ci({merge: "success"})
        self.ok(self.home, "land", "b")
        self.assertEqual(self.quest("b")["landed"][0]["ci"], {"commit": merge, "runs": [3]})

    def test_an_edit_beside_a_clean_merge_waits_for_ci_at_the_merge(self) -> None:
        b, _ = self.quest_green_at_head("10")
        self.commit(self.home, {"other/o.txt": "1\n"})
        self.git(b, "merge", "-q", "--no-commit", "main")
        (b / "other/o.txt").write_text("2\n")
        self.git(b, "commit", "-qam", "Merge main, and an edit")
        merge = self.git(b, "rev-parse", "HEAD")
        self.verified("b")
        self.fails(self.home, "land", "b", match=f"CI at {merge[:12]}: no CI runs")

    def problems(self) -> tuple[str, str]:
        """What `check` and `status` say."""
        check = self.run_cli(self.home, "check")
        return check.text, self.ok(self.home, "status").text

    def test_red_ci_on_main_after_a_carried_landing_is_an_error(self) -> None:
        _, head = self.quest_green_at_head("10")
        self.commit(self.home, {"other/o.txt": "1\n"})
        self.ok(self.home, "land", "b", "--merge-main")
        landed = self.quest("b")["landed"][0]["commit"]
        self.assertEqual(self.quest("b")["landed"][0]["ci"]["commit"], head)
        error = (
            f"b: CI on main at {landed[:12]}, where it landed on CI carried from {head[:12]} across a merge "
            "from main (main's side landed no quest), failed: build (failure): fix main forward "
            "(`campaign new b-fix --title \"Fix main's CI after b\" --files 'lib/**'`)"
        )
        self.assertNotIn("b: CI on main", self.problems()[0])  # main's CI has not run yet
        self.ci({landed: "failure"})
        check, status = self.problems()
        self.assertIn(f"error: {error}", check)
        self.assertEqual(self.run_cli(self.home, "check").code, 1)
        self.assertIn(error, status)
        fix = self.commit(self.home, {"app.py": "print(2)\n"}, "fix main forward")
        self.ci({fix: "success"})
        self.assertNotIn("b: CI on main", self.problems()[0])  # main is green again

    def test_red_main_names_the_quests_on_mains_side_of_the_merge(self) -> None:
        _, head = self.quest_green_at_head("10")
        a = self.started("a", files="app.py", check="unit=true")
        self.commit(a, {"app.py": "print('a')\n"})
        self.verified("a")
        self.ci({self.git(a, "rev-parse", "HEAD"): "success"})
        self.ok(self.home, "land", "a")  # lands on main after b's green head
        self.ok(self.home, "land", "b", "--merge-main")
        landed = self.quest("b")["landed"][0]["commit"]
        self.assertEqual(self.quest("b")["landed"][0]["ci"]["commit"], head)
        self.ci({landed: "failure"})
        self.assertIn(
            f"carried from {head[:12]} across a merge from main (main's side brought a), failed", self.problems()[0]
        )

    def test_a_fixed_forward_landing_is_not_blamed_again(self) -> None:
        self.quest_green_at_head("10")
        self.commit(self.home, {"other/o.txt": "1\n"})
        self.ok(self.home, "land", "b", "--merge-main")
        self.ci({self.quest("b")["landed"][0]["commit"]: "failure"})
        fix = self.commit(self.home, {"app.py": "print(2)\n"}, "fix main forward")
        self.ci({fix: "success"})
        later = self.commit(self.home, {"app.py": "print(3)\n"}, "an unrelated later change")
        self.ci({later: ""})  # main's CI runs at its new head: the fix's green still clears b
        self.assertNotIn("b: CI on main", self.problems()[0])
        self.ci({later: "failure"})  # nor is a later red head b's
        self.assertNotIn("b: CI on main", self.problems()[0])

    def test_main_ci_reads_stay_bounded(self) -> None:
        self.quest_green_at_head("10")
        self.commit(self.home, {"other/o.txt": "1\n"})
        self.ok(self.home, "land", "b", "--merge-main")
        landed = self.quest("b")["landed"][0]["commit"]
        self.ci({landed: "failure"})
        for i in range(80):
            self.commit(self.home, {"app.py": f"print({i})\n"}, f"main {i}")
        self.ci({self.git(self.home, "rev-parse", "main"): ""})  # none of main's 80 commits finished

        def asked() -> tuple[int, str]:
            log = self.root / "gh.log"
            log.write_text("")
            text = self.run_cli(self.home, "check").text
            return sum(x.startswith("run list") for x in log.read_text().splitlines()), text

        calls, text = asked()
        self.assertLessEqual(calls, 20 + 1)  # its landing's CI, and at most MAIN_WALK of main's
        self.assertIn("main's CI not read past", text)
        self.ci({landed: "success"})  # its rerun passed: no walk at all
        self.assertEqual(asked()[0], 1)

    def test_land_reads_ci_past_the_hosts_cache(self) -> None:
        _, head = self.quest_green_at_head("10")
        self.env["CAMPAIGN_CI_SECONDS"] = "600"
        self.assertNotIn("CI at", self.ok(self.home, "brief", "b").out)  # the host keeps this green
        green = self.runs[head][0]
        self.runs[head] = [green, {**green, "databaseId": 2, "conclusion": "failure"}]  # a newer red rerun
        (self.root / "gh.json").write_text(json.dumps(self.runs))
        self.assertNotIn("CI at", self.ok(self.home, "brief", "b").out)  # brief keeps the cache
        log = self.root / "gh.log"
        log.write_text("")
        self.fails(self.home, "land", "b", match=f"CI at {head[:12]}: failed: build")
        asked = log.read_text().splitlines()
        self.assertEqual(sorted(asked), sorted(set(asked)))  # each question once per land
        log.write_text("")
        self.assertIn("failed: build", self.ok(self.home, "brief", "b").out)  # land's answer refreshed the host's
        self.assertEqual(log.read_text(), "")

    def test_a_failed_fresh_ask_keeps_the_hosts_good_answer(self) -> None:
        self.quest_green_at_head("10")
        self.env["CAMPAIGN_CI_SECONDS"] = "600"
        self.ok(self.home, "brief", "b")  # the host keeps this green
        (self.root / "gh.json").write_text("")  # GitHub fails every call now
        self.ok(self.home, "land", "b")

    def test_a_running_carried_head_is_a_live_waiter(self) -> None:
        b, head = self.quest_green_at_head("10")
        self.ci({head: ""})  # its CI runs again
        self.commit(self.home, {"other/o.txt": "1\n"})
        self.git(b, "merge", "-q", "--no-edit", "main")  # a clean merge, not pushed: no runs
        tool = campaign_module()
        with mock.patch.dict(os.environ, self.env, clear=True):
            c = tool.Campaign(self.home)
            self.assertTrue(tool.ci.ci_waiting(c, c.quests["b"]))

    def test_a_ci_criterion_counts_the_heads_ci_across_a_clean_merge(self) -> None:
        b, head = self.quest_green_at_head("10")
        brief = self.home / ".campaign/demo/quests/b.md"
        text = brief.read_text().replace("None in this test.", "- A1 [ci] CI is green.")
        self.commit(self.home, {".campaign/demo/quests/b.md": text}, "b: a [ci] criterion")
        self.commit(self.home, {"other/o.txt": "1\n"})
        self.git(b, "merge", "-q", "--no-edit", "main")
        merge = self.git(b, "rev-parse", "HEAD")
        out = self.ok(self.home, "brief", "b").out
        self.assertIn(f"| A1 | CI green at {head[:12]} (carried to {merge[:12]} across clean merges) |", out)
        self.ok(self.home, "land", "b")

    def test_a_recovered_landing_keeps_the_ci_it_relied_on(self) -> None:
        tool = campaign_module()
        head = self.git(self.home, "rev-parse", "HEAD")
        relied = {"commit": head, "runs": [5], "carried": True}
        intent = {"id": "b", "target": head, "final": True, "mode": "ff", "ci": relied}
        record = tool.landing_record(self.home, "main", intent)
        self.assertEqual(record["ci"], relied)

    def reviewed_high_risk_quest(self) -> Path:
        """Quest b, high risk, accepted at its head, whose CI passed there."""
        b = self.started("b", files="lib/**", risk="high", check="unit=true")
        head = self.commit(b, {"lib/util.py": LINES.replace("L1 = 1", "L1 = 10")})
        self.review("b", "correctness", "APPROVE")
        self.ok(self.home, "accept", "b", "correctness")
        self.ci({head: "success"})
        return b

    def test_a_clean_merge_keeps_the_review_accepted(self) -> None:
        b = self.reviewed_high_risk_quest()
        self.commit(self.home, {"other/o.txt": "1\n", "lib/util.py": LINES.replace("L11 = 11", "L11 = 110")})
        self.git(b, "merge", "-q", "--no-edit", "main")
        self.verified("b")
        self.assertNotIn("correctness", self.ok(self.home, "brief", "b").out)
        self.ok(self.home, "land", "b")

    def test_a_resolved_conflict_voids_the_review(self) -> None:
        b = self.reviewed_high_risk_quest()
        self.commit(self.home, {"other/o.txt": "1\n", "lib/util.py": LINES.replace("L1 = 1", "L1 = 100")})
        with self.assertRaises(AssertionError):  # git stops at the conflict
            self.git(b, "merge", "main")
        (b / "lib/util.py").write_text(LINES.replace("L1 = 1", "L1 = 10"))
        self.git(b, "commit", "-qam", "Merge main, resolved")
        merge = self.git(b, "rev-parse", "HEAD")
        self.verified("b")
        self.ci({merge: "success"})
        self.fails(self.home, "land", "b", match="correctness")

    def test_the_newest_run_at_a_commit_is_its_ci(self) -> None:
        _, head = self.quest_green_at_head("10")
        red = {"databaseId": 7, "name": "build", "status": "completed", "conclusion": "failure"}
        self.runs[head] = [red, {**red, "databaseId": 9, "conclusion": "success"}]  # a dispatch after the red run
        (self.root / "gh.json").write_text(json.dumps(self.runs))
        self.ok(self.home, "land", "b")
        self.assertEqual(self.quest("b")["landed"][0]["ci"], {"commit": head, "runs": [9]})


class LandingCompletes(CampaignCase):
    def test_a_failed_cleanup_after_main_moved_warns_and_the_landing_is_recorded(self) -> None:
        lib = self.repo("lib", {"lib.py": "X = 1\n"})
        self.ok(self.home, "new", "pin", "--title", "pin", "--risk", "low", "--repo", "../lib", "--check", "unit=true")
        self.ok(self.home, "start", "pin")
        self.commit(self.root / "lib-wt/demo/pin", {"lib.py": "X = 2\n"})
        self.verified("pin")
        # the journal branch's upstream is not merged: `git branch -d` refuses it once it is on main
        side = self.git(self.home, "commit-tree", "-p", "HEAD", "-m", "elsewhere", "HEAD^{tree}")
        self.git(self.home, "remote", "add", "origin", str(self.root / "nowhere.git"))
        self.git(self.home, "update-ref", "refs/remotes/origin/quest/demo/pin", side)
        self.git(self.home, "config", "branch.quest/demo/pin.remote", "origin")
        self.git(self.home, "config", "branch.quest/demo/pin.merge", "refs/heads/quest/demo/pin")
        self.ok(self.home, "land", "pin")
        self.assertEqual(self.quest("pin")["status"], "landed")
        self.assertEqual(self.git(lib, "show", "main:lib.py"), "X = 2")
        self.assertEqual(self.git(self.home, "branch", "--list", "quest/demo/pin"), "")

    def test_clean_merges_the_journal_a_failed_landing_step_left(self) -> None:
        self.repo("lib", {"lib.py": "X = 1\n"})
        self.ok(self.home, "new", "pin", "--title", "pin", "--risk", "low", "--repo", "../lib", "--check", "unit=true")
        self.ok(self.home, "start", "pin")
        self.commit(self.root / "lib-wt/demo/pin", {"lib.py": "X = 2\n"})
        place = self.root / "app-wt/demo/pin"
        journal = ".campaign/demo/journal/pin.md"
        self.commit(place, {journal: (place / journal).read_text() + "\nA NOTE\n"})
        self.verified("pin")
        hook = (
            Path(self.git(self.home, "rev-parse", "--path-format=absolute", "--git-path", "hooks")) / "pre-merge-commit"
        )
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\nexit 1\n")  # the journal merge fails after main moved
        hook.chmod(0o755)
        landed = self.ok(self.home, "land", "pin")
        self.assertIn("merging its journal failed", landed.text)
        self.assertIn("`campaign clean pin` retries it", landed.text)
        self.assertEqual(self.quest("pin")["status"], "landed")
        self.assertEqual(self.git(self.home, "ls-tree", "main", journal), "")  # not merged
        self.fails(self.home, "clean", "pin")  # the hook still refuses: clean leaves no merge half done
        merging = Path(self.git(self.home, "rev-parse", "--path-format=absolute", "--git-path", "MERGE_HEAD"))
        self.assertFalse(merging.exists())
        hook.unlink()
        self.ok(self.home, "clean", "pin")
        self.assertIn("A NOTE", self.git(self.home, "show", f"main:{journal}"))
        self.assertEqual(self.git(self.home, "branch", "--list", "quest/demo/pin"), "")
