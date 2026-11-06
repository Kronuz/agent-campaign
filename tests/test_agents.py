"""Generated agents preserve the requested reasoning effort, and only generated files are replaced."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "sync-agents.py"


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
            subprocess.run([sys.executable, str(SCRIPT)], env=env, check=True, capture_output=True)
            self.assertIn('model_reasoning_effort = "max"', (root / "codex/sage.toml").read_text())
            subprocess.run([sys.executable, str(SCRIPT), "--check"], env=env, check=True, capture_output=True)

    def test_files_generated_by_the_shell_version_are_replaced_and_others_kept(self) -> None:
        base = os.environ.get("CAMPAIGN_TEST_TMP")
        with tempfile.TemporaryDirectory(dir=base) as temporary:
            root = Path(temporary)
            (root / "source").mkdir()
            (root / "copilot").mkdir()
            (root / "source" / "sage.md").write_text("---\nname: sage\nmodel: opus\neffort: high\n---\nBody.\n")
            old = root / "copilot" / "sage.agent.md"
            old.write_text(
                "<!-- Generated from agents/sage.md by agent-campaign sync-agents.sh; edit the source. -->\n"
            )
            mine = root / "copilot" / "mine.agent.md"
            mine.write_text("hand-written\n")
            gone = root / "copilot" / "gone.agent.md"
            gone.write_text("<!-- by campaign/scripts/sync-agents.sh -->\n")
            env = dict(
                os.environ, AGENTS_SRC=str(root / "source"), AGENTS_CODEX="", AGENTS_COPILOT=str(root / "copilot")
            )
            subprocess.run([sys.executable, str(SCRIPT)], env=env, check=True, capture_output=True)
            self.assertIn("Body.", old.read_text())
            self.assertEqual(mine.read_text(), "hand-written\n")
            self.assertFalse(gone.exists())
