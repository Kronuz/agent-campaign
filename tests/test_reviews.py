"""Reviews, their binding to a commit, and the risk policy."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest import mock

from harness import CampaignCase, campaign_module


class Reviews(CampaignCase):
    def normal(self) -> str:
        wt = self.started("feat", files="app.py", risk="normal", lenses="correctness")
        self.commit(wt, {"app.py": "print('feature')\n"})
        self.verified("feat")
        return self.git(wt, "rev-parse", "HEAD")

    def test_normal_risk_lands_on_green_checks_without_a_review(self) -> None:
        wt = self.started("plain", files="app.py", risk="normal")
        self.assertEqual(self.quest("plain")["lenses"], [])
        self.commit(wt, {"app.py": "print('plain')\n"})
        self.verified("plain")
        self.ok(self.home, "land", "plain")

    def test_a_named_lens_needs_an_accepted_review(self) -> None:
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

    def test_changes_with_only_nits_or_non_blocking_findings_is_an_approval(self) -> None:
        self.normal()
        self.review("feat", "correctness", "CHANGES", findings=(("C1", "yes", "nit"), ("C2", "no", "defect")))
        self.assertIn("approved; `campaign accept feat correctness`", self.ok(self.home, "status", "feat").out)
        self.ok(self.home, "accept", "feat", "correctness")  # no --confirm: nothing blocks
        self.assertEqual(self.quest("feat")["accepted"]["correctness"]["verdict"], "APPROVE")
        self.ok(self.home, "land", "feat")

    def test_a_review_is_never_waived_nor_counted_for_yield(self) -> None:
        self.normal()
        self.fails(self.home, "accept", "feat", "correctness", "--waive", "small", match="unrecognized arguments")
        self.fails(self.home, "status", "--reviews", match="unrecognized arguments")
        self.assertNotIn("reviews", json.loads(self.ok(self.home, "status", "--json").out))

    def test_changes_after_acceptance_void_it(self) -> None:
        self.normal()
        self.review("feat", "correctness", "APPROVE")
        self.ok(self.home, "accept", "feat", "correctness")
        self.commit(self.wt("feat"), {"app.py": "print('sneaky')\n"})
        self.verified("feat")
        self.fails(self.home, "land", "feat", match="changed since")

    def test_main_lines_merged_into_a_quest_file_keep_its_acceptance(self) -> None:
        wt = self.started("feat", files="lib.py,docs/**", risk="normal", lenses="correctness")
        self.commit(self.home, {"lib.py": "A = 1\n\n\n\n\nB = 2\n"})
        self.git(wt, "merge", "-q", "main")
        self.commit(wt, {"lib.py": "A = 10\n\n\n\n\nB = 2\n"})
        self.review("feat", "correctness", "APPROVE")
        self.ok(self.home, "accept", "feat", "correctness")
        self.commit(self.home, {"lib.py": "A = 1\n\n\n\n\nB = 20\n"})  # main's own line, same file
        self.git(wt, "merge", "-q", "--no-edit", "main")
        self.commit(wt, {"docs/note.md": "how it works\n"})  # documentation no test runs
        self.verified("feat")
        self.ok(self.home, "land", "feat")

    def test_main_moving_elsewhere_asks_for_a_rebase_not_a_new_review(self) -> None:
        self.normal()
        self.review("feat", "correctness", "APPROVE")
        self.ok(self.home, "accept", "feat", "correctness")
        self.commit(self.home, {"notes.md": "a handoff note on main\n"})
        status = self.ok(self.home, "status", "feat").out
        self.assertIn("merge main into it", status)
        self.assertNotIn("changed since", status)  # the reviewed code did not change
        self.started("other", files="lib.py", risk="normal", lenses="correctness")
        self.assertIn("not reviewed yet; launch a correctness reviewer", self.ok(self.home, "status", "other").out)
        self.review("other", "correctness", "CHANGES")
        status = self.ok(self.home, "status", "other").out
        self.assertIn("correctness review not accepted: verdict CHANGES; fix what blocks", status)

    def test_only_a_changed_criterion_voids_acceptance(self) -> None:
        self.normal()
        brief = ".campaign/demo/quests/feat.md"
        self.commit(self.home, {brief: "## Acceptance criteria\n\n- A1 prints: the suite\n  at the head\n"})
        self.review("feat", "correctness", "APPROVE")
        self.ok(self.home, "accept", "feat", "correctness")
        self.commit(
            self.home,
            {brief: "# Feat\n\nNew prose.\n\n## Acceptance criteria\n\n- A1 prints: the suite\n  at the head\n"},
        )
        self.assertNotIn("criterion changed", self.ok(self.home, "status", "feat").out)
        self.commit(self.home, {brief: "## Acceptance criteria\n\n- A1 prints: the suite\n  at the tip\n"})
        self.fails(self.home, "land", "feat", match="a criterion changed after correctness was accepted")

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


class ReviewBinding(CampaignCase):
    """An acceptance covers the code its review saw and the review's own content; after CHANGES, each
    blocking finding is confirmed by jobs that passed at the fixed code."""

    def feature(self) -> tuple[Path, str]:
        wt = self.started("feat", files="app.py,tests/**", risk="normal", lenses="correctness")
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
                mock.patch.object(campaign.quests, "read_review", read_then_rewrite),
            ):
                self.assertEqual(campaign.main(["accept", "feat", "correctness"]), 0)
        finally:
            os.chdir(cwd)
        self.fails(self.home, "land", "feat", match="correctness review changed after it was accepted")

    def test_a_verdict_agrees_with_its_findings(self) -> None:
        self.feature()
        self.review("feat", "correctness", "APPROVE", findings=(("C1", "yes", "defect"),))
        self.fails(self.home, "accept", "feat", "correctness", match="APPROVE but lists blocking findings C1")
        self.review("feat", "correctness", "CHANGES")
        self.fails(self.home, "accept", "feat", "correctness", match="CHANGES but lists no findings table")
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
        self.fails(self.home, "new", "x", "--title", "x", "--risk", "high", "--lenses", "", match="needs a code review")
        self.fails(
            self.home, "new", "y", "--title", "y", "--risk", "critical", "--lenses", "correctness", match="design"
        )
        self.started("z", files="app.py", risk="high", check="unit=true")
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

    def test_an_empty_later_clears_the_deferred_checks_on_the_record(self) -> None:
        self.started("fix", files="app.py", check="unit=true")
        self.ok(self.home, "set", "fix", "--later", "san=true")
        self.fails(self.home, "set", "fix", "--later", "", match="deferred check san removed")
        self.ok(self.home, "set", "fix", "--later", "", "--weaken", "CI runs the sanitizers")
        self.assertEqual([c["name"] for c in self.quest("fix")["checks"]], ["unit"])
        self.assertEqual(self.quest("fix")["history"][-1]["weakened"], ["deferred check san removed"])

    def test_strengthening_needs_no_reason(self) -> None:
        self.started("fix", files="app.py", risk="normal", check="unit=true")
        self.ok(self.home, "set", "fix", "risk=critical")
        self.assertEqual(sorted(self.quest("fix")["lenses"]), ["correctness", "design"])
        self.ok(self.home, "set", "fix", "--check", "unit=true", "--check", "lint=true")
        self.ok(self.home, "set", "fix", "lenses=correctness,design,security")
