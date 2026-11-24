"""One answer to whether a gate check's job counts at a quest's head, for `verify`, `brief` and `land`."""

from __future__ import annotations

import json

from harness import CampaignCase


class GateReuse(CampaignCase):
    def ci_green(self) -> None:
        state = self.state()
        state["ci"] = "github"
        (self.home / ".campaign/demo/state.json").write_text(json.dumps(state))
        self.git(self.home, "commit", "-q", "-am", "require CI")
        gh = self.root / "bin" / "gh"
        gh.write_text('#!/bin/sh\necho \'[{"name": "build", "status": "completed", "conclusion": "success"}]\'\n')
        gh.chmod(0o755)

    def merged_quest(self, ci: bool) -> None:
        """A quest whose checks passed, then a merge from main touching none of its files."""
        if ci:
            self.ci_green()
        self.commit(self.home, {"other/o.txt": "0\n"})
        self.b = self.started("b", files="lib/**")
        self.commit(self.b, {"lib/util.py": "X = 2\n"})
        self.verified("b")
        self.commit(self.home, {"other/o.txt": "1\n"})
        self.git(self.b, "merge", "-q", "--no-edit", "main")

    def test_land_counts_what_verify_reuses_even_when_it_waives_ci(self) -> None:
        self.merged_quest(ci=True)
        out = self.ok(self.home, "verify", "b").out
        self.assertIn("(reused: passed at ", out)
        self.assertIn("only 1 merge from main since, none touching its files", out)
        self.assertIn("ready to land", self.ok(self.home, "brief", "b").out)
        self.ok(self.home, "land", "b", "--no-ci", "CI is slow")
        self.assertEqual(self.quest("b")["status"], "landed")

    def test_land_record_counts_what_verify_reuses(self) -> None:
        self.merged_quest(ci=True)
        head = self.git(self.b, "rev-parse", "HEAD")
        self.git(self.home, "merge", "-q", "--ff-only", "quest/demo/b")
        self.ok(self.home, "land", "b", "--record", head, "--reason", "landed by hand")
        self.assertEqual(self.quest("b")["landed"][-1]["unverified"], [])

    def test_without_ci_verify_runs_the_check_at_the_merge_and_land_counts_it(self) -> None:
        self.merged_quest(ci=False)
        self.ready_for_review(self.b, "b")  # else verify holds the passed 30-minute check until then
        head = self.git(self.b, "rev-parse", "HEAD")
        self.assertNotIn("reused", self.ok(self.home, "verify", "b").out)
        self.ok(self.home, "wait", "--quest", "b", "--timeout", "90", timeout=120)
        self.ok(self.home, "land", "b")
        (landing,) = self.quest("b")["landed"]
        self.assertEqual([e["commit"] for e in landing["evidence"]], [head])
