"""A task's probe that must fail is submitted with --fails-without and never holds its merge."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from harness import CampaignCase

PROBE = "grep -q 'X = 0' lib/util.py"  # fails at the task's head by design: it holds X = 2


class TaskProbes(CampaignCase):
    def task(self) -> Path:
        self.started("fix", files="app.py,lib/**")
        self.ok(self.home, "start", "fix", "--task", "lib", "--files", "lib/**")
        task = self.wt("fix--lib")
        self.commit(task, {"lib/util.py": "X = 2\n"})
        test = self.submit(task, "--quest", "fix", "--task", "lib", "--", "grep -q 'X = 2' lib/util.py")
        self.ok(self.home, "wait", test, "--timeout", "60")
        return task

    def test_a_task_probe_that_must_fail_passes_by_failing_and_never_holds_the_merge(self) -> None:
        task = self.task()
        probe = self.submit(task, "--quest", "fix", "--task", "lib", "--fails-without", "--", PROBE)
        self.ok(self.home, "wait", probe, "--timeout", "60")
        self.assertEqual(self.job(probe)["state"], "done", "it exited nonzero, as it must")
        self.ok(self.home, "task", "merge", "fix", "lib")

    def test_a_quest_level_fails_without_still_names_its_criteria(self) -> None:
        wt = self.started("fix", files="app.py")
        self.fails(wt, "job", "submit", "--quest", "fix", "--fails-without", "--", "false", match="needs --for")

    def test_a_task_job_failing_at_its_head_names_both_ways_out(self) -> None:
        task = self.task()
        probe = self.submit(task, "--quest", "fix", "--task", "lib", "--", PROBE)
        self.run_cli(self.home, "wait", probe, "--timeout", "60")
        hint = f"`campaign job rerun {probe}`, or, if it must fail, submit it again with --fails-without"
        self.fails(self.home, "task", "merge", "fix", "lib", match=hint)
        self.assertIn(hint, self.ok(self.home, "brief", "fix").out)
        again = self.submit(task, "--quest", "fix", "--task", "lib", "--fails-without", "--", PROBE)
        self.ok(self.home, "wait", again, "--timeout", "60")
        self.ok(self.home, "task", "merge", "fix", "lib")

    def test_a_probe_withdraws_an_earlier_run_whose_id_sorts_after_it(self) -> None:
        task = self.task()
        probe = self.submit(task, "--quest", "fix", "--task", "lib", "--fails-without", "--", PROBE)
        self.ok(self.home, "wait", probe, "--timeout", "60")
        plain = self.submit(task, "--quest", "fix", "--task", "lib", "--", PROBE)
        self.run_cli(self.home, "wait", plain, "--timeout", "60")
        specs = {jid: self.root / "jobs" / jid / "spec.json" for jid in (probe, plain)}
        times = {jid: json.loads(path.read_text())["submitted"] for jid, path in specs.items()}
        spec = json.loads(specs[plain].read_text())  # submitted first, as when both share one second
        spec["submitted"] = times[probe] - 1
        specs[plain].write_text(json.dumps(spec))
        shutil.rmtree(self.root / "jobs" / ".index")  # it keeps a finished job's spec
        self.ok(self.home, "task", "merge", "fix", "lib")
