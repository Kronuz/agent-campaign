"""A command's `git cat-file` readers end quietly at exit: one whose git exited prints nothing, and
one whose git is stuck is killed instead of hanging the exit."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
EXIT_LIMIT = 30  # seconds: far above READER_EXIT, so only a hang reaches it

# Runs in a child process: reads HEAD through a reader, does MODE to its git, then exits.
CHILD = textwrap.dedent(
    """
    import os, shutil, signal, sys
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    from campaign_tool import repo
    root, mode = Path(sys.argv[2]), sys.argv[3]
    if mode == "removed":  # a worktree removed before its reader starts: git exits at once
        shutil.rmtree(root / "wt")
        assert repo.read_object(root / "wt", "HEAD") is None
        assert repo.read_object(root / "wt", "HEAD") is None
        sys.exit(0)
    assert repo.read_object(root / "r", "HEAD") is not None
    proc = repo._cat[str(root / "r")][1]
    print(proc.pid, flush=True)
    if mode == "killed":
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait()
        assert repo.read_object(root / "r", "HEAD") is None
    elif mode == "stuck":  # stopped: closing its stdin cannot end it
        os.kill(proc.pid, signal.SIGSTOP)
    """
)


class CloseReaders(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.root)], check=False)
        subprocess.run([*GIT, "init", "-q", str(self.root / "r")], check=True)
        subprocess.run([*GIT, "-C", str(self.root / "r"), "commit", "-q", "--allow-empty", "-m", "x"], check=True)
        subprocess.run([*GIT, "-C", str(self.root / "r"), "worktree", "add", "-q", str(self.root / "wt")], check=True)

    def exit_of(self, mode: str) -> tuple[int, str, int]:
        """The child's exit status, stderr and reader pid (0 when it printed none)."""
        with subprocess.Popen(
            [sys.executable, "-c", CHILD, str(SCRIPTS), str(self.root), mode],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as child:
            assert child.stdout is not None
            line = child.stdout.readline().strip()
            pid = int(line) if line else 0
            try:
                _, err = child.communicate(timeout=EXIT_LIMIT)
            except subprocess.TimeoutExpired:
                child.kill()
                _, err = child.communicate()
                err += f"\nexit hung over {EXIT_LIMIT} s"
                if pid:  # the hung child leaves its stopped git behind
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(pid, signal.SIGKILL)
        return child.returncode, err, pid

    def test_killed_git_exits_quietly(self) -> None:
        self.assertEqual(self.exit_of("killed")[:2], (0, ""))

    def test_removed_worktree_exits_quietly(self) -> None:
        self.assertEqual(self.exit_of("removed")[:2], (0, ""))

    def test_stuck_git_is_killed_not_waited_on(self) -> None:
        status, err, pid = self.exit_of("stuck")
        self.assertEqual((status, err), (0, ""))
        with self.assertRaises(ProcessLookupError):  # killed and reaped by the exiting command
            os.kill(pid, 0)


if __name__ == "__main__":
    unittest.main()
