"""install.sh in a throwaway home: what it links, what it leaves alone, and how it uninstalls."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness import SKILL, scratch


class Install(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(dir=scratch())).resolve()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.env = {**os.environ, "HOME": str(self.home), "PATH": f"{self.home / '.local/bin'}:{os.environ['PATH']}"}
        for key in ("AGENTS_COPILOT", "AGENTS_CODEX", "AGENTS_SRC"):
            self.env.pop(key, None)

    def install(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SKILL / "install.sh"), *args], env=self.env, capture_output=True, text=True, check=False
        )

    def test_install_leaves_unrelated_files_and_uninstall_removes_only_its_own(self) -> None:
        bin_dir, claude, copilot = self.home / ".local/bin", self.home / ".claude/agents", self.home / ".copilot/agents"
        for d in (bin_dir, claude, copilot):
            d.mkdir(parents=True)
        (bin_dir / "report").symlink_to("/usr/bin/true")  # someone else's tool named report
        theirs = claude / sorted((SKILL / "agents").glob("*.md"))[0].name  # a user's agent of the same name
        theirs.write_text("---\nname: mine\n---\nmine\n")
        (copilot / "mine.agent.md").write_text("my own agent\n")
        older = self.home / "old-campaign"  # another checkout of the skill, as 1.x left it
        (older / "scripts").mkdir(parents=True)
        (older / "SKILL.md").write_text("---\nname: campaign\n---\n")
        (older / "scripts/quiet.py").write_text("")
        (bin_dir / "quiet").symlink_to(older / "scripts/quiet.py")
        before = {p: p.read_bytes() for p in (theirs, copilot / "mine.agent.md")}

        dry = self.install("--dry-run")
        self.assertIn("would: ln -sfn", dry.stdout)
        self.assertFalse((bin_dir / "campaign").exists())

        r = self.install()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)  # something was in the way
        self.assertIn(f"skipped {bin_dir / 'report'}: links to /usr/bin/true", r.stderr)
        self.assertIn(f"skipped {theirs}: exists and is not a link", r.stderr)
        self.assertEqual(os.readlink(bin_dir / "report"), "/usr/bin/true")
        self.assertEqual(
            os.readlink(bin_dir / "quiet"), str(SKILL / "scripts/quiet.py")
        )  # the old campaign's, replaced
        self.assertEqual(os.readlink(bin_dir / "campaign"), str(SKILL / "scripts/campaign.py"))
        self.assertEqual(os.readlink(self.home / ".agents/skills/campaign"), str(SKILL))
        generated = sorted(p.name for p in copilot.glob("*.agent.md") if p.name != "mine.agent.md")
        self.assertEqual(generated, sorted(f"{p.stem}.agent.md" for p in (SKILL / "agents").glob("*.md")))

        self.assertEqual(self.install("--check").returncode, 1)  # report and the agent file are still in the way

        self.assertEqual(self.install("--uninstall").returncode, 0)
        for gone in ("campaign", "quiet", "pace"):
            self.assertFalse((bin_dir / gone).is_symlink(), gone)
        self.assertFalse((self.home / ".agents/skills/campaign").is_symlink())
        self.assertEqual(sorted(p.name for p in copilot.iterdir()), ["mine.agent.md"])
        self.assertEqual(os.readlink(bin_dir / "report"), "/usr/bin/true")
        self.assertEqual({p: p.read_bytes() for p in before}, before)


if __name__ == "__main__":
    unittest.main()
