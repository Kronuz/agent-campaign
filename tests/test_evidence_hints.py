"""What brief says about a journal's evidence rows agrees with what land accepts."""

from __future__ import annotations

import json
import sys
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

    def test_a_fails_without_row_names_its_mutant_not_a_rerun_at_the_head(self) -> None:
        wt = self.started("fix", files="app.py")
        self.criterion("fix", "- A1 prints two: a probe\n- A2 prints two: a regression test")
        mutant = self.git(self.home, "rev-parse", "main")  # the code without the fix
        self.git(self.home, "update-ref", "refs/campaign/demo/fix-a1", mutant)
        self.commit(wt, {"app.py": "print(2)\n"})
        test = "python3 app.py | grep -q 2"
        probe = self.submit(wt, "--quest", "fix", "--for", "A2", "--", test)
        tags = ("--quest", "fix", "--for", "A2", "--fails-without", "--commit", "refs/campaign/demo/fix-a1")
        pre = self.submit(wt, *tags, "--", test)
        self.ok(self.home, "wait", probe, pre)
        self.commit(wt, {".campaign/demo/journal/fix.md": f"## Evidence\n\n| A1 | {probe}, {pre} |\n"})
        self.verified("fix")
        refused = self.fails(self.home, "land", "fix", match="evidence jobs ran other code")
        self.assertIn(f"`campaign job rerun {pre} --commit refs/campaign/demo/fix-a1`", refused.text)
        self.assertNotIn(f"`campaign job rerun {pre}`", refused.text)

    def test_a_bare_ci_citation_names_the_backtick_form(self) -> None:
        wt = self.started("fix", files="app.py")
        self.criterion("fix", "- A1 CI builds it: the build job")
        self.commit(wt, {"app.py": "print(2)\n", ".campaign/demo/journal/fix.md": "## Evidence\n\n| A1 | ci:build |\n"})
        out = self.ok(self.home, "brief", "fix").out
        self.assertIn("ci:build is read only in backticks: cite it as `ci:build`", out)
        self.fails(self.home, "land", "fix", match="criteria without evidence")
