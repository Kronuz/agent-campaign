"""What brief says about a journal's evidence rows agrees with what land accepts."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from harness import CampaignCase


class EvidenceHints(CampaignCase):
    def setUp(self) -> None:
        super().setUp()
        self.runs: dict[str, list[dict[str, Any]]] = {}
        gh = self.root / "bin" / "gh"
        gh.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            f"runs = json.load(open({str(self.root / 'gh.json')!r}))\n"
            "a = sys.argv[1:]\n"
            "if a[:2] == ['run', 'list']:\n"
            "    print(json.dumps(runs.get(a[a.index('--commit') + 1], [])))\n"
            "else:\n"
            "    run = [r for rs in runs.values() for r in rs if str(r['databaseId']) == a[2]]\n"
            "    print(json.dumps({'jobs': run}))\n"
        )
        gh.chmod(0o755)
        self.ci({})

    def ci(self, runs: dict[str, str]) -> None:
        """CI's runs: commit -> the conclusion of its one "build" run, whose one job is "build"."""
        for i, (commit, conclusion) in enumerate(runs.items(), len(self.runs) + 1):
            self.runs[commit] = [{"databaseId": i, "name": "build", "status": "completed", "conclusion": conclusion}]
        (self.root / "gh.json").write_text(json.dumps(self.runs))

    def criterion(self, pid: str, text: str) -> None:
        brief = self.home / f".campaign/demo/quests/{pid}.md"
        brief.write_text(brief.read_text().replace("None in this test.", text))
        self.git(self.home, "commit", "-qam", f"{pid}: criteria")

    def test_a_ci_row_carries_across_a_clean_merge_from_main(self) -> None:
        self.ok(self.home, "config", "ci=github")
        self.commit(self.home, {"README.md": "app\n\nmore\n"})
        b = self.started("b", files="lib/**")
        self.criterion("b", "- A1 CI builds it: the build job")
        head = self.commit(
            b, {"lib/util.py": "X = 2\n", ".campaign/demo/journal/b.md": "## Evidence\n\n| A1 | `ci:build` |\n"}
        )
        self.verified("b")
        self.ci({head: "success"})
        self.assertIn("A1  met", self.ok(self.home, "status", "b").out)
        self.commit(self.home, {"README.md": "app, changed on main\n\nmore\n"})
        self.git(b, "merge", "-q", "--no-edit", "main")  # clean, not pushed: CI never runs at the merge
        merge = self.git(b, "rev-parse", "HEAD")
        self.assertNotIn(merge, self.runs)
        self.assertIn("A1  met", self.ok(self.home, "status", "b").out)
        self.ok(self.home, "land", "b")

    def fails_quest(self) -> tuple[Path, str]:
        """Quest fix prints two; refs/campaign/demo/fix-a1 names the code without the fix (main)."""
        wt = self.started("fix", files="app.py")
        self.criterion("fix", "- A1 prints two: a probe\n- A2 prints two: a probe\n- A3 prints two: a regression test")
        self.git(self.home, "update-ref", "refs/campaign/demo/fix-a1", self.git(self.home, "rev-parse", "main"))
        self.commit(wt, {"app.py": "print(2)\n"})
        return wt, self.submit(wt, "--quest", "fix", "--for", "A3", "--", "python3 app.py | grep -q 2")

    def pre_fix(self, wt: Path, cmd: str) -> str:
        """A --fails-without job of CMD at the code without the fix."""
        tags = ("--quest", "fix", "--for", "A3", "--fails-without", "--commit", "refs/campaign/demo/fix-a1")
        return self.submit(wt, *tags, "--", cmd)

    def test_an_unfinished_fails_without_row_names_its_mutant_not_a_rerun_at_the_head(self) -> None:
        wt, probe = self.fails_quest()
        pre = self.pre_fix(wt, "sleep 60; python3 app.py | grep -q 2")
        self.ok(self.home, "job", "cancel", pre)
        self.wait_until(lambda: self.job(pre).get("state") not in ("queued", "running"))
        self.ok(self.home, "wait", probe)
        self.commit(wt, {".campaign/demo/journal/fix.md": f"## Evidence\n\n| A1 | {probe}, {pre} |\n"})
        out = self.ok(self.home, "brief", "fix").out  # the Evidence table holds each row's whole gap
        self.assertIn(f"`campaign job rerun {pre} --commit refs/campaign/demo/fix-a1`", out)
        self.assertNotIn(f"`campaign job rerun {pre}`", out)

    def test_a_cited_fails_without_job_is_read_by_its_own_rule(self) -> None:
        wt, probe = self.fails_quest()
        caught = self.pre_fix(wt, "python3 app.py | grep -q 2")  # fails at the mutant, as it must
        missed = self.pre_fix(wt, "python3 app.py")  # passes at the mutant: it tests nothing
        self.run_cli(self.home, "wait", probe, caught, missed, "--timeout", "60")
        rows = f"| A1 | {probe}, {caught} |\n| A2 | {probe}, {missed} |\n"
        self.commit(wt, {".campaign/demo/journal/fix.md": f"## Evidence\n\n{rows}"})
        out = self.ok(self.home, "brief", "fix").out
        self.assertIn(f"| A1 | {probe}, {caught} |", out)
        self.assertNotIn(f"| A2 | {probe}, {missed} |", out)
        self.assertIn(f"{missed} failed: the test passed without the fix", out)
        self.assertNotIn("ran other code", out)

    def test_a_bare_ci_citation_names_the_backtick_form(self) -> None:
        wt = self.started("fix", files="app.py")
        self.criterion("fix", "- A1 CI builds it: the build job")
        self.commit(wt, {"app.py": "print(2)\n", ".campaign/demo/journal/fix.md": "## Evidence\n\n| A1 | ci:build |\n"})
        out = self.ok(self.home, "brief", "fix").out
        self.assertIn("ci:build is read only in backticks: cite it as `ci:build`", out)
        self.fails(self.home, "land", "fix", match="criteria without evidence")
