"""Quests, ownership, evidence, reviews, landing and state recovery."""

from __future__ import annotations

import fcntl
import json
import os
import unittest
from pathlib import Path
from typing import Any

from harness import CampaignCase


class Quests(CampaignCase):
    def test_task_file_scope_can_be_corrected_without_overlapping_a_peer(self) -> None:
        self.started("fix", files="app.py,lib/**,README.md")
        self.ok(self.home, "start", "fix", "--task", "one", "--files", "lib/**")
        self.ok(self.home, "start", "fix", "--task", "two", "--files", "app.py")
        self.ok(self.home, "set", "fix", "--task", "one", "files=lib/**,README.md")
        self.assertEqual(self.quest("fix")["tasks"]["one"]["files"], ["lib/**", "README.md"])
        self.fails(self.home, "set", "fix", "--task", "one", "files=app.py", match="overlap")
        self.assertEqual(self.quest("fix")["tasks"]["one"]["files"], ["lib/**", "README.md"])
        self.fails(self.home, "set", "fix", "--task", "missing", "files=lib/**", match="no task")
        self.fails(self.home, "set", "fix", "--task", "one", "status=review", match="files=")

    def test_start_records_branch_worktree_base_and_work_file(self) -> None:
        wt = self.started("fix", files="app.py")
        p = self.quest("fix")
        self.assertEqual(p["status"], "active")
        self.assertEqual(p["branch"], "quest/fix")
        self.assertTrue((wt / "docs/campaign/journal/fix.md").exists())
        self.assertEqual(self.git(self.home, "merge-base", "main", "quest/fix"), p["base"])
        self.assertTrue((self.home / "docs/campaign/quests/fix.md").exists())

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
        self.commit(wt, {"docs/campaign/quests/fix.md": "weakened criteria\n"})
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

    def test_campaign_metadata_commits_keep_the_evidence(self) -> None:
        wt = self.started("fix", files="app.py")
        self.commit(wt, {"app.py": "print(2)\n"})
        self.verified("fix")
        work = "docs/campaign/journal/fix.md"
        self.commit(wt, {work: (wt / work).read_text() + "\nEvidence: the unit check.\n"})
        self.assertIn("reused", self.ok(self.home, "verify", "fix").out)
        landed = self.ok(self.home, "land", "fix").out  # main moved only in campaign files: a merge
        self.assertIn(f"(merge {self.quest('fix')['landed'][0]['commit'][:12]})", landed)
        self.commit(self.home, {"docs/notes.md": "not campaign metadata\n"})
        b = self.started("other", files="README.md")
        self.commit(b, {"README.md": "x\n"})
        self.verified("other")
        self.commit(b, {"docs/other.md": "outside docs/campaign counts as code\n"})
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
        diff = self.git(self.home, "diff", tested, "main", "--", ".", ":(exclude)docs/campaign")
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
        self.ok(self.home, "accept", "feat", "correctness")
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

    def test_a_changed_brief_voids_acceptance(self) -> None:
        self.normal()
        self.review("feat", "correctness", "APPROVE")
        self.ok(self.home, "accept", "feat", "correctness")
        self.commit(self.home, {"docs/campaign/quests/feat.md": "new criteria\n"})
        self.fails(self.home, "land", "feat", match="brief changed")

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
        folder = self.home / "docs/campaign/reviews"
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
        self.assertNotIn("\n  jobs:", status)  # a landed quest's failures are history
        # the fix lands like any quest; rerunning the deferred checks on main clears the error
        self.ok(self.home, "set", "fast", "--later", "soak=true", "--later", "other-platform=echo other")
        self.ok(self.home, "verify", "fast", "--deferred")
        rerun = self.quest("fast")["landed"][0]["later"]
        self.assertNotEqual(rerun, record["later"])
        self.ok(self.home, "wait", *rerun, "--timeout", "60")
        self.ok(self.home, "check")

    def test_slow_ci_workflows_can_be_named_non_gating(self) -> None:
        self.ok(self.home, "config", "ci=github", "ci_later=macos")
        wt = self.started("fix", files="app.py", check="unit=true")
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
        return self.root / "lib-wt" / pid

    def landed_from(self, home: Path, pid: str) -> None:
        self.ok(home, "verify", pid)
        self.ok(home, "wait", "--quest", pid, "--timeout", "90", timeout=120)
        self.ok(home, "land", pid)

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
        mine, theirs = self.run_cli(self.home, "check").out, self.run_cli(self.other, "check").out
        self.assertIn("api: changes contract lib-api, which other:use consumes", mine)
        self.assertIn("api: files may overlap other:use", mine)
        self.assertIn("use: contract lib-api is changing in demo:api", theirs)
        machine = self.ok(self.other, "status", "--all").out
        self.assertIn("demo", machine)
        self.assertIn("in flight: use", machine)
        use = self.root / "lib-wt" / "use"
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
        (self.home / "docs/campaign/state.json").write_text(json.dumps(state))
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


class Sidebar(CampaignCase):
    def test_the_sidebar_follows_the_state_and_sends_only_changes(self) -> None:
        calls = self.root / "report-calls"
        fake = self.root / "bin" / "report"
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
        self.assertIn("set|0/1|demo M1: 0 of 1 quests landed|", log)
        self.assertIn("status|campaign-a|a: active|hammer|", log)
        calls.write_text("")
        self.ok(self.home, "status")
        self.assertEqual(calls.read_text(), "", "an unchanged view sends nothing")
        self.commit(self.wt("a"), {"app.py": "print(2)\n"})
        self.verified("a")
        self.ok(self.home, "land", "a")
        log = calls.read_text()
        self.assertIn("notify|Landed a|", log)
        self.assertIn("unstatus|campaign-a|", log)
        self.assertIn("set|0/1|demo M2: 0 of 1 quests landed · ready: b|", log)
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
        (self.home / "docs/campaign/state.json").write_text(json.dumps(state))
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
        (self.home / "docs/campaign/state.json").write_text(json.dumps(state))
        self.git(self.home, "commit", "-q", "-am", "intent on disk")
        r = self.ok(self.home, "new", "next", "--title", "next")
        self.assertIn("main never moved", r.err)
        self.assertEqual(self.quest("fix")["status"], "active")

    def test_check_finds_deleted_and_unrecorded_branches(self) -> None:
        wt = self.started("gone", files="app.py")
        self.git(self.home, "worktree", "remove", "--force", str(wt))
        self.git(self.home, "branch", "-D", "quest/gone")
        r = self.run_cli(self.home, "check")
        self.assertEqual(r.code, 1)
        self.assertIn("branch quest/gone is gone", r.out)
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
        self.assertNotIn("quest/fix", self.git(self.home, "branch", "--list"))
        self.assertNotIn("slots", self.git(self.home, "worktree", "list"))


if __name__ == "__main__":
    unittest.main()
