"""A gate check's job has one spec: the one `verify` queues is the one `brief` and `land` look up."""

from __future__ import annotations

import os
import re
from pathlib import Path
from unittest import mock

from harness import CampaignCase, campaign_module


class CheckSpec(CampaignCase):
    def setUp(self) -> None:
        super().setUp()
        self.env["HOME"] = str(self.root)  # a script under the user's home, in no checkout of the repository
        self.script = self.root / "libs" / "tools" / "split_check.py"
        self.script.parent.mkdir(parents=True)
        self.script.write_text("print('split')\n")

    def jobs(self, out: str) -> dict[str, str]:
        """Each check verify named, with its job id and " reused" when it said so."""
        return {m[1]: m[2] + (" reused" if m[3] else "") for m in re.finditer(r"^(\S+): (\S+)( \(reused)?", out, re.M)}

    def outside_quest(self) -> Path:
        wt = self.started("fix", files="app.py", check=f"split=python3 {self.script} --base main .")
        self.commit(wt, {"app.py": "print(2)\n"})
        return wt

    def test_a_check_naming_a_script_outside_the_repository_is_found_by_brief_and_land(self) -> None:
        self.outside_quest()
        (job,) = self.jobs(self.ok(self.home, "verify", "fix").out).values()
        self.ok(self.home, "wait", job, "--timeout", "60")
        self.assertEqual(self.jobs(self.ok(self.home, "verify", "fix").out), {"split": job + " reused"})
        brief = self.ok(self.home, "brief", "fix").out
        self.assertNotIn("no job", brief)
        self.assertIn("ready to land", brief)
        self.ok(self.home, "land", "fix")
        (landing,) = self.quest("fix")["landed"]
        self.assertEqual([e["job"] for e in landing["evidence"]], [job])

    def test_a_changed_outside_script_no_longer_counts_and_verify_runs_it_again(self) -> None:
        self.outside_quest()
        (first,) = self.jobs(self.ok(self.home, "verify", "fix").out).values()
        self.ok(self.home, "wait", first, "--timeout", "60")
        self.assertIn("ready to land", self.ok(self.home, "brief", "fix").out)
        self.script.write_text("print('split, changed')\n")
        self.assertIn("check split is not green: no job", self.ok(self.home, "brief", "fix").out)
        self.fails(self.home, "land", "fix", match="no job")
        (second,) = self.jobs(self.ok(self.home, "verify", "fix").out).values()
        self.assertNotEqual(second.split()[0], first)
        self.assertNotIn("reused", second)
        self.ok(self.home, "wait", second, "--timeout", "60")
        self.ok(self.home, "land", "fix")

    def test_verify_and_the_readers_find_the_same_job_for_every_check_shape(self) -> None:
        shapes = {
            "plain": ["--check", "plain=python3 app.py"],
            "inputs": ["--check", "inputs=python3 lib/util.py", "--inputs", "inputs=lib/**"],
            "outside": ["--check", f"outside=python3 {self.script}"],
            "later": ["--later", "later=python3 app.py --soak"],
        }
        self.ok(
            self.home,
            "new",
            "fix",
            "--title",
            "fix",
            "--risk",
            "low",
            "--files",
            "app.py,lib/**",
            *(arg for args in shapes.values() for arg in args),
        )
        self.ok(self.home, "start", "fix")
        wt = self.wt("fix")
        head = self.commit(wt, {"app.py": "print(2)\n", "lib/util.py": "X = 2\n"})
        queued = self.jobs(self.ok(self.home, "verify", "fix", "--all").out)
        self.assertEqual(sorted(queued), sorted(shapes))
        self.ok(self.home, "wait", *queued.values(), "--timeout", "90", timeout=120)
        tool = campaign_module()
        checks = {x["name"]: x for x in self.quest("fix")["checks"]}
        with mock.patch.dict(os.environ, {"HOME": self.env["HOME"], "CAMPAIGN_JOBS": self.env["CAMPAIGN_JOBS"]}):
            found = {
                name: tool.job_for_check(self.home, head, checks[name], files=["app.py", "lib/**"]) for name in shapes
            }
        self.assertEqual({name: j and j["id"] for name, j in found.items()}, queued)
