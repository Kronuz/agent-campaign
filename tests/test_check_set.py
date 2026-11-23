"""`set --check` and `--later` edit checks by name: the named one changes, every other one stays."""

from __future__ import annotations

from typing import Any

from harness import CampaignCase


class CheckSetByName(CampaignCase):
    def checks(self, pid: str) -> dict[str, dict[str, Any]]:
        return {c["name"]: c for c in self.quest(pid)["checks"]}

    def test_converting_sanitizers_to_ci_keeps_the_other_gates(self) -> None:  # A1, detent msg-30
        self.ok(
            self.home,
            *("new", "q", "--title", "q", "--risk", "high", "--files", "app.py"),
            *("--check", "native=python3 app.py", "--check", "targeted=python3 -c pass"),
            *("--check", "asan=make asan", "--check", "tsan=make tsan"),
            *("--inputs", "targeted=tests/**"),
        )
        before = self.checks("q")
        self.assertEqual(list(before), ["native", "targeted", "asan", "tsan"])
        self.assertTrue(before["asan"].get("later"))  # a sanitizer is a milestone check at any risk
        self.assertFalse(before["tsan"].get("later"))  # TSan gates at high risk
        out = self.ok(
            self.home,
            *("set", "q", "--check", "asan=ci:AddressSanitizer", "--check", "tsan=ci:ThreadSanitizer"),
            *("--weaken", "CI runs the sanitizers"),
        ).out
        self.assertNotIn("dropped", out)
        after = self.checks("q")
        self.assertEqual(list(after), ["native", "targeted", "asan", "tsan"])
        self.assertEqual(after["native"], before["native"])
        self.assertEqual(after["targeted"], before["targeted"])
        self.assertEqual(after["targeted"]["inputs"], ["tests/**"])
        self.assertEqual((after["asan"]["cmd"], after["asan"].get("later")), ("ci:AddressSanitizer", None))
        self.assertEqual((after["tsan"]["cmd"], after["tsan"].get("later")), ("ci:ThreadSanitizer", None))
        self.assertEqual(
            self.quest("q")["history"][-1]["weakened"], ["deferred check asan changed", "gate check tsan changed"]
        )

    def test_a_deferred_check_edited_by_name_keeps_the_other_deferred_ones(self) -> None:  # A2
        self.ok(
            self.home,
            *("new", "r", "--title", "r", "--files", "app.py", "--check", "unit=true"),
            *("--later", "asan=make asan", "--later", "soak=make soak"),
        )
        self.ok(self.home, "set", "r", "--later", "t3-asan=make t3-asan")  # adding weakens nothing
        after = self.checks("r")
        self.assertEqual(list(after), ["unit", "asan", "soak", "t3-asan"])
        self.assertEqual([n for n, c in after.items() if c.get("later")], ["asan", "soak", "t3-asan"])
        self.ok(self.home, "set", "r", "--later", "soak::5=make soak")  # a shorter budget weakens nothing
        after = self.checks("r")
        self.assertEqual(list(after), ["unit", "asan", "soak", "t3-asan"])
        self.assertEqual((after["soak"]["minutes"], after["asan"]["cmd"]), (5, "make asan"))
        self.fails(self.home, "set", "r", "--later", "asan=make asan2", match="deferred check asan changed")
        self.ok(self.home, "set", "r", "--later", "asan=make asan2", "--weaken", "the target was renamed")
        self.assertEqual(
            [(n, c["cmd"]) for n, c in self.checks("r").items()],
            [("unit", "true"), ("asan", "make asan2"), ("soak", "make soak"), ("t3-asan", "make t3-asan")],
        )

    def test_an_empty_command_removes_only_that_check_on_the_record(self) -> None:  # A3
        self.ok(
            self.home,
            *("new", "s", "--title", "s", "--files", "app.py", "--check", "unit=true", "--check", "lint=true"),
            *("--later", "soak=make soak", "--later", "fuzz=make fuzz"),
        )
        self.fails(self.home, "set", "s", "--check", "lint=", match="gate check lint removed")
        self.assertEqual(list(self.checks("s")), ["unit", "lint", "soak", "fuzz"])
        out = self.ok(self.home, "set", "s", "--check", "lint=", "--weaken", "ruff runs in the suite").out
        self.assertIn("dropped: lint\n", out)
        self.assertEqual(list(self.checks("s")), ["unit", "soak", "fuzz"])
        self.assertEqual(self.quest("s")["history"][-1]["weakened"], ["gate check lint removed"])
        self.fails(self.home, "set", "s", "--later", "soak=", match="deferred check soak removed")
        out = self.ok(self.home, "set", "s", "--later", "soak=", "--weaken", "CI soaks it").out
        self.assertIn("dropped: soak\n", out)
        self.assertEqual(list(self.checks("s")), ["unit", "fuzz"])
        self.fails(self.home, "set", "s", "--check", "nope=", "--weaken", "w", match="no check nope to remove")
        self.fails(self.home, "set", "s", "--check", "unit=a", "--later", "unit=b", match="unit: named twice")
        self.assertEqual(list(self.checks("s")), ["unit", "fuzz"])
