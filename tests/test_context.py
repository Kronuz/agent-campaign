"""The context report counts what each launch reads, and warns about growth without failing."""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest import mock

from harness import SKILL, script_module


class Context(unittest.TestCase):
    def setUp(self) -> None:
        self.cs = script_module(SKILL / "scripts" / "context-size.py")

    def test_every_load_resolves_and_the_baseline_covers_it(self) -> None:
        sizes = self.cs.measure()  # raises if a file, description or prompt section a load names is gone
        self.assertTrue(all(size["words"] > 0 for size in sizes.values()), sizes)
        self.assertEqual(set(json.loads(Path(self.cs.BASELINE).read_text())), set(sizes))

    def test_growth_warns_past_ten_percent_and_never_fails(self) -> None:
        then = {"lead": {"words": 3000}, "reviewer": {"words": 1000}}
        now = {"lead": {"words": 3400}, "reviewer": {"words": 1149}}
        self.assertEqual(self.cs.growth(now, then), ["lead grew +400 words (+13%)"])
        doubled = {name: {"words": size["words"] * 2, "tokens": 0} for name, size in self.cs.measure().items()}
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(self.cs, "measure", return_value=doubled),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            self.assertEqual(self.cs.main(["--check"]), 0)  # a report, never a gate
        self.assertTrue(out.getvalue().startswith("context (words): lead "), out.getvalue())
        self.assertIn("lead grew +", err.getvalue())
