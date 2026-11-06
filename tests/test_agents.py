"""Generated Codex agents preserve the requested reasoning effort."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class Agents(unittest.TestCase):
    def test_max_effort_survives_generation_and_drift_check(self) -> None:
        base = os.environ.get("CAMPAIGN_TEST_TMP")
        with tempfile.TemporaryDirectory(dir=base) as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "sage.md").write_text(
                "---\nname: sage\ndescription: Frozen design\nmodel: opus\neffort: max\n---\nReview the design.\n"
            )
            env = dict(
                os.environ,
                AGENTS_SRC=str(source),
                AGENTS_CODEX=str(root / "codex"),
                AGENTS_COPILOT=str(root / "copilot"),
            )
            script = Path(__file__).resolve().parent.parent / "scripts/sync-agents.sh"
            subprocess.run(["bash", str(script)], env=env, check=True, capture_output=True)
            self.assertIn('model_reasoning_effort = "max"', (root / "codex/sage.toml").read_text())
            subprocess.run(["bash", str(script), "--check"], env=env, check=True, capture_output=True)
