"""What the campaign shows: brief and status, the cmux sidebar and the map."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from harness import CampaignCase


class Style(CampaignCase):
    def test_brief_status_shows_what_to_act_on_and_a_worktree_sees_its_quest(self) -> None:
        wt = self.started("fix", files="app.py")
        self.ok(self.home, "new", "other", "--title", "other", "--risk", "low", "--files", "README.md")
        self.commit(wt, {"app.py": "print(2)\n"})
        failed = self.submit(self.home, "--quest", "fix", "--", "exit 1")
        self.run_cli(self.home, "wait", failed, "--timeout", "60")
        self.ok(self.home, "message", "fix", "check the overflow", "--from", "explorer")
        brief = self.ok(wt, "brief").out  # in the quest's worktree: that quest only
        self.assertTrue(brief.startswith("fix active low: quest/demo/fix at "), brief)
        self.assertIn("  files: app.py\n", brief)
        self.assertIn("  - check unit is not green at", brief)
        self.assertIn("  - unanswered messages: msg-1:", brief)
        self.assertIn("  - msg-1 from explorer: check the overflow\n", brief)
        self.assertIn(f"  - {failed} failed, in no journal yet: exit 1 at ", brief)
        self.assertNotIn("other", brief)
        self.assertNotIn("criteria", brief)
        self.assertNotIn("\nother  planned", self.ok(wt, "status").out)  # the full view lists only this quest too
        home = self.ok(self.home, "brief").out
        self.assertIn("campaign demo: 1 in flight (1 active), 1 planned, 0 landed; jobs 0 queued, 0 running\n", home)
        self.assertIn("other planned low\n  ready to start: `campaign start other`\n", home)
        self.assertNotIn("files:", home)
        data = json.loads(self.ok(wt, "brief", "--json").out)  # the same, for parsing
        self.assertNotIn("counts", data)
        [quest] = data["quests"]
        self.assertEqual((quest["id"], quest["files"], quest["next"]), ("fix", ["app.py"], ""))
        self.assertTrue(any(g.startswith("check unit is not green") for g in quest["to_land"]), quest)
        self.assertEqual([(n["id"], n["by"]) for n in quest["messages"]], [("msg-1", "explorer")])
        self.assertEqual([f["job"] for f in quest["failed"]], [failed])
        out = self.ok(self.home, "map").out
        self.assertIn("- 🔥 **fix**", out)
        self.assertIn(f"Failing: {failed} failed", out)
        journal = wt / ".campaign/demo/journal/fix.md"
        journal.write_text(journal.read_text() + f"\n{failed} failed on purpose: the overflow check.\n")
        self.assertIn("- 🔨 **fix**", self.ok(self.home, "map").out, "a failure its journal names still burns")
        data = json.loads(self.ok(self.home, "brief", "--json").out)
        self.assertEqual(data["counts"]["planned"], 1)
        other = next(q for q in data["quests"] if q["id"] == "other")
        self.assertEqual(other["next"], "campaign start other")
        self.assertNotIn("files", other)

    def test_color_and_marks_only_on_a_terminal(self) -> None:
        wt = self.started("fix", files="app.py")
        self.commit(wt, {"app.py": "print(2)\n"})
        self.verified("fix")
        plain = self.ok(self.home, "status", "fix").out + self.ok(self.home, "job", "list").out
        self.assertNotIn("\033[", plain)  # agents read through pipes: words only
        self.assertFalse([c for c in plain if ord(c) >= 0x1F300], plain)
        self.env["FORCE_COLOR"] = "1"
        styled = self.ok(self.home, "status", "fix").out
        self.assertIn("\033[", styled)
        self.assertIn("🔨 \033[1mfix\033[0m  \033[36mactive\033[0m", styled)
        self.assertIn("  🌿 branch quest/demo/fix", styled)
        self.assertIn("  ✅ \033[32mready to land", styled)
        self.assertIn("✅ ", self.ok(self.home, "job", "list").out)
        self.env["NO_COLOR"] = "1"  # wins over FORCE_COLOR
        out = self.ok(self.home, "status", "fix").out
        self.assertNotIn("\033[", out)
        self.assertNotIn("🌿", out)


def logged(calls: Path) -> str:
    """The calls a fake cmux logged, but `tree`: note_lead and the worker's to-do mail ask it, at any
    time, whether the lead lives."""
    return "".join(x for x in calls.read_text().splitlines(keepends=True) if "|tree|" not in x)


class Sidebar(CampaignCase):
    def test_the_sidebar_follows_the_state_and_sends_only_changes(self) -> None:
        calls = self.root / "cmux-calls"
        fake = self.root / "bin" / "cmux"
        fake.write_text(f'#!/bin/sh\nprintf "%s|" "$@" >> {calls}; echo >> {calls}\n')
        fake.chmod(0o755)
        self.env["CMUX_WORKSPACE_ID"] = "ws-test"
        self.env["CMUX_SURFACE_ID"] = "surface-test"  # the lead: only a lead's session names its workspace
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
        plan = self.home / ".campaign/demo/PLAN.md"  # PLAN.md orders M2 first, unlike the quests
        plan.write_text(plan.read_text().replace("| <M1> | <ids> | <command> |", "| M2 | b | |\n| M1 | a | |"))
        self.git(self.home, "commit", "-q", "-am", "milestones")
        self.ok(self.home, "new", "b", "--title", "b", "--risk", "low", "--files", "README.md", "--milestone", "M2")
        self.assertIn("set-progress|0.0000|--label|demo M2: 0 of 1 quests landed · launchable: b|", logged(calls))
        self.ok(self.home, "start", "a")
        log = logged(calls)
        self.assertIn("set-progress|0.0000|--label|demo M1: 0 of 1 quests landed|", log)  # the active one
        self.assertIn("set-status|campaign-demo-a|a: active|--icon|hammer|", log)
        calls.write_text("")
        self.ok(self.home, "status")
        self.assertEqual(
            logged(calls),
            "workspace-action|--workspace|ws-test|--action|rename|--title|demo · lead|\n",
            "named once",
        )
        calls.write_text("")
        self.ok(self.home, "status")
        self.assertEqual(logged(calls), "", "an unchanged view sends nothing")
        failed = self.submit(self.home, "--quest", "a", "--", "exit 1")
        self.run_cli(self.home, "wait", failed, "--timeout", "60")
        self.ok(self.home, "status")
        self.assertIn("set-status|campaign-demo-a|a: active · 🔥 1 failed job|", logged(calls))
        calls.write_text("")
        self.ok(self.home, "set", "a", "status=blocked", "until=owner", "reason=owner")
        self.assertIn("clear-status|campaign-demo-a|", logged(calls), "a blocked quest keeps no pill")
        self.ok(self.home, "set", "a", "status=active")
        self.commit(self.wt("a"), {"app.py": "print(2)\n"})
        self.verified("a")
        self.ok(self.home, "land", "a")
        log = logged(calls)
        self.assertIn("notify|--title|Landed a|--body|", log)
        self.assertIn("clear-status|campaign-demo-a|", log)
        self.assertIn("set-progress|0.0000|--label|demo M2: 0 of 1 quests landed · launchable: b|", log)
        calls.write_text("")
        self.ok(self.root, "--campaign", "demo", "new", "c", "--title", "c", "--risk", "low", "--files", "c.py")
        self.assertEqual(logged(calls), "", "a command run outside the home (a message) draws nothing")
        self.env.pop("CMUX_WORKSPACE_ID")
        calls.write_text("")
        self.ok(self.home, "set", "b", "status=blocked", "until=owner", "reason=owner")
        self.assertEqual(logged(calls), "", "outside cmux nothing is drawn")

    def workspace_cmux(self) -> Path:
        """A fake cmux logging each call as WORKSPACE>ARGS, with the lead's session in ws-lead."""
        calls = self.root / "cmux-calls"
        fake = self.root / "bin" / "cmux"
        fake.write_text(
            f'#!/bin/sh\nprintf "%s>" "$CMUX_WORKSPACE_ID" >> {calls}; printf "%s|" "$@" >> {calls}; echo >> {calls}\n'
        )
        fake.chmod(0o755)
        self.env["CMUX_WORKSPACE_ID"] = "ws-lead"
        self.env["CMUX_SURFACE_ID"] = "surface-lead"
        self.ok(self.home, "new", "a", "--title", "a", "--risk", "low", "--files", "app.py", "--check", "unit=true")
        self.ok(self.home, "start", "a")
        self.assertIn("ws-lead>set-status|campaign-demo-a|a: active|--icon|hammer|", logged(calls))
        return calls

    def test_only_the_recorded_lead_draws_the_sidebar(self) -> None:
        calls = self.workspace_cmux()
        self.env["CMUX_WORKSPACE_ID"] = "ws-other"  # another campaign's lead, looking in from the home
        self.env["CMUX_SURFACE_ID"] = "surface-other"
        calls.write_text("")
        self.ok(self.home, "status")
        self.ok(self.home, "new", "b", "--title", "b", "--risk", "low", "--files", "README.md")
        self.ok(self.home, "start", "b")
        self.assertEqual(logged(calls), "", "a session that is not the lead sends nothing")
        cache = json.loads((self.home / ".git" / "campaign-sidebar.json").read_text())
        self.assertEqual([v.partition(" ")[0] for v in cache], ["ws-lead"], "and caches no view")

    def test_the_leads_draw_clears_the_campaign_from_other_workspaces(self) -> None:
        calls = self.workspace_cmux()
        path = self.home / ".git" / "campaign-sidebar.json"
        views = json.loads(path.read_text())
        [(lead, drawn)] = views.items()
        ident = lead.partition(" ")[2]
        views[f"ws-stray {ident}"] = {**drawn, "status:campaign-demo-gone": "gone: active"}  # drawn before the rule
        views["ws-stray /elsewhere/.campaign/other"] = {
            "status:campaign-other": "other: 0 landed"
        }  # another campaign's
        path.write_text(json.dumps(views))
        calls.write_text("")
        self.ok(self.home, "status")
        stray = sorted(line for line in logged(calls).splitlines() if line.startswith("ws-stray>"))
        self.assertEqual(
            stray,
            [
                "ws-stray>clear-progress|",
                "ws-stray>clear-status|campaign-demo-a|",
                "ws-stray>clear-status|campaign-demo-gone|",
                "ws-stray>clear-status|campaign-demo|",
            ],
        )
        mine = [line for line in logged(calls).splitlines() if line.startswith("ws-lead>") and "rename" not in line]
        self.assertEqual(mine, [], "the lead's own view is unchanged")
        after = json.loads(path.read_text())
        self.assertEqual(
            sorted(after), sorted([lead, "ws-stray /elsewhere/.campaign/other"]), "the stray view is dropped"
        )
        calls.write_text("")
        self.ok(self.home, "status")
        self.assertNotIn("ws-stray>", logged(calls), "and cleared once")

    def test_a_campaign_sharing_the_cleared_workspace_redraws_its_bar(self) -> None:
        calls = self.workspace_cmux()
        other = self.repo("other", {"README.md": "other\n"})
        self.env["CMUX_WORKSPACE_ID"] = "ws-shared"  # another campaign's lead, in its own home
        self.env["CMUX_SURFACE_ID"] = "surface-other"
        self.ok(other, "init", "--name", "other")
        self.ok(other, "new", "o", "--title", "o", "--risk", "low", "--files", "README.md", "--check", "unit=true")
        self.assertIn("ws-shared>set-progress|", logged(calls))
        path = self.home / ".git" / "campaign-sidebar.json"
        views = json.loads(path.read_text())
        [(lead, drawn)] = views.items()
        views[f"ws-shared {lead.partition(' ')[2]}"] = drawn  # demo drawn there before the rule
        path.write_text(json.dumps(views))
        self.env["CMUX_WORKSPACE_ID"] = "ws-lead"
        self.env["CMUX_SURFACE_ID"] = "surface-lead"
        self.ok(self.home, "status")
        self.assertIn("ws-shared>clear-progress|", logged(calls), "demo's stray bar is cleared, and cmux has one")
        self.env["CMUX_WORKSPACE_ID"] = "ws-shared"
        self.env["CMUX_SURFACE_ID"] = "surface-other"
        calls.write_text("")
        self.ok(other, "status")
        sent = [line for line in logged(calls).splitlines() if "progress" in line]
        self.assertEqual(sent, ["ws-shared>set-progress|0.0000|--label|other: 0 of 1 quests landed · launchable: o|"])


class Map(CampaignCase):
    def test_the_map_shows_intent_result_documents_and_progress(self) -> None:
        plan = self.home / ".campaign/demo/PLAN.md"
        text = plan.read_text().replace(
            "- <what success means, measurably>", "- Totals print right,\n  in every locale"
        )
        text = text.replace("| <M1> | <ids> | <command> |", "| M1 | fix, open | python3 app.py |")
        plan.write_text(text)
        self.git(self.home, "commit", "-q", "-am", "plan")
        wt = self.started("fix", files="app.py,docs/**", milestone="M1")
        brief = self.home / ".campaign/demo/quests/fix.md"
        brief.write_text(brief.read_text().replace("## Outcome\n", "## Outcome\n\nPrints two instead of one.\n", 1))
        self.git(self.home, "commit", "-q", "-am", "fix outcome")
        journal = ".campaign/demo/journal/fix.md"
        result = (
            (wt / journal)
            .read_text()
            .replace(
                "## Result\n", "## Result\n\nPrints 2; 1.0 ms per call (job x). How: docs/mechanisms/print.md.\n", 1
            )
        )
        self.commit(wt, {"app.py": "print(2)\n", "docs/mechanisms/print.md": "# Print\n", journal: result})
        self.verified("fix")
        self.ok(self.home, "map", "--write")
        self.ok(self.home, "new", "open", "--title", "Open work", "--files", "README.md", "--milestone", "M1")
        self.ok(self.home, "land", "fix")
        written = (self.home / ".campaign/demo/MAP.md").read_text()
        self.assertIn("- ✅ **fix**", written, "the landing did not refresh MAP.md")
        out = self.ok(self.home, "map").out
        self.assertIn("- 🎯 Totals print right, in every locale\n", out)
        self.assertIn("## 🔨 M1: 1 of 2 landed", out)
        self.assertIn("- ✅ **fix**: fix", out)
        self.assertIn("⚠️ landed, criteria left open", out)  # the legend
        self.assertIn("Outcome: Prints two instead of one.", out)
        self.assertIn("Result: Prints 2; 1.0 ms per call", out)
        self.assertIn("Docs: docs/mechanisms/print.md", out)
        self.assertIn("- 📋 **open**: Open work", out)
        self.assertNotIn("**open**", self.ok(self.home, "map", "fix").out)
        data = json.loads(self.ok(self.home, "map", "--json").out)
        self.assertEqual([q["id"] for q in data["milestones"][0]["quests"]], ["fix", "open"])
        self.env["FORCE_COLOR"] = "1"
        colored = self.ok(self.home, "map").out
        self.assertIn("\033[", colored)
        self.assertEqual(re.sub(r"\033\[[0-9;]*m", "", colored), out, "color changed the map's text")
        self.ok(self.home, "map", "--write")
        self.assertNotIn("\033", (self.home / ".campaign/demo/MAP.md").read_text(), "MAP.md holds color codes")

    def test_work_done_before_the_campaign_is_recorded_on_the_map(self) -> None:
        self.env.update(GIT_AUTHOR_DATE="2026-10-01T12:00:00", GIT_COMMITTER_DATE="2026-10-01T12:00:00")
        old = self.commit(self.home, {"docs/status/pins.md": "1. [x] K1 pins at head\n"}, message="Pins at head")
        for key in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"):
            self.env.pop(key)
        self.ok(self.home, "new", "pins", "--title", "Pins at head", "--risk", "normal")
        (self.home / ".campaign/demo/quests/pins.md").write_text(
            "# pins: Pins at head\n\n## Outcome\n\nEvery library at its head.\n\n## Acceptance criteria\n\n"
            "- A1 [artifact] Pins at head: `docs/status/pins.md`\n"
            "- A2 [artifact] Pins checked in CI: `docs/status/pins.md`\n"
            "- A3 [artifact] Pins listed: `docs/status/pins.md`\n"
        )
        journal = self.home / ".campaign/demo/journal/pins.md"
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_text(
            "# pins: journal\n\nState: landed\n\n## Evidence\n\n| Criterion | Evidence |\n|---|---|\n"
            '| A1 | `docs/status/pins.md`: "[x] K1 pins  at head" |\n| A2 | open: left unticked in `docs/status/pins.md` |\n'
            '| A3 | `docs/status/pins.md`: "[x] K9 pins listed" |\n\n'
            "## Result\n\nAll 12 pins at head; CI green.\n"
        )
        self.git(self.home, "add", ".campaign")
        self.git(self.home, "commit", "-q", "-m", "history: pins")
        self.fails(self.home, "land", "pins", "--earlier", match="--record")
        reason = "docs/status/pins.md; reviewed in docs/reviews/pins.md, APPROVE; CI run 1"
        self.ok(self.home, "land", "pins", "--record", old, "--earlier", "--reason", reason)
        record = self.quest("pins")["landed"][0]
        self.assertTrue(record["at"].startswith("2026-10-01"), record)
        self.assertEqual(record["unverified"], [], "work verified before the campaign is listed as unverified")
        self.assertIn("(before the campaign)", self.ok(self.home, "status", "pins").out)
        out = self.ok(self.home, "map").out
        self.assertIn("- ⚠️ **pins**: Pins at head (normal risk, landed before the campaign 2026-10-01 at app:", out)
        self.assertIn("; left open: A2, A3)", out)  # A3 quotes what the ledger never says
        self.assertIn("Result: All 12 pins at head; CI green.", out)
        self.assertNotIn("none yet", out)  # reviewed by the process of its time
        plan = self.home / ".campaign/demo/PLAN.md"
        plan.write_text(plan.read_text().replace("| <M1> | <ids> | <command> |", "| M9 | | grep -E 'a\\|b' x |"))
        self.git(self.home, "commit", "-q", "-am", "an empty milestone")
        out = self.ok(self.home, "map").out
        self.assertIn("## ⬜ M9: no quests yet", out)
        self.assertIn("Validation on main: grep -E 'a|b' x\n", out, "an escaped pipe split the cell")
        (pins,) = [q for q in json.loads(self.ok(self.home, "map", "--json").out)["other"] if q["id"] == "pins"]
        self.assertEqual(
            [x["met"] for x in pins["criteria"]], [True, False, False], "an unticked criterion reads as met"
        )


if __name__ == "__main__":
    unittest.main()
