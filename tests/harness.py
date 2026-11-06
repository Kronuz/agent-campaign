"""Shared fixtures for the campaign tests: throwaway repositories, a private job queue and quiet."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

SKILL = Path(__file__).resolve().parent.parent
CAMPAIGN = SKILL / "scripts" / "campaign.py"
QUIET = SKILL / "scripts" / "quiet.py"
sys.path.insert(0, str(SKILL / "scripts"))  # the tools import report, so loading one in a test needs it


def alone(test: Callable[..., None]) -> Callable[..., None]:
    """Marks a test that loads the whole machine or times it: run.py runs it with nothing beside it."""
    test.__dict__["alone"] = True
    return test


def session_members(sids: set[int]) -> list[int]:
    """Live processes in any of these sessions (portable: macOS ps has no session column)."""
    found: list[int] = []
    for token in subprocess.run(["ps", "-A", "-o", "pid="], capture_output=True, text=True).stdout.split():
        with contextlib.suppress(ProcessLookupError, PermissionError):
            if int(token) != os.getpid() and os.getsid(int(token)) in sids:
                found.append(int(token))
    return found


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Result:
    def __init__(self, p: subprocess.CompletedProcess[str]) -> None:
        self.code = p.returncode
        self.out = p.stdout
        self.err = p.stderr

    @property
    def text(self) -> str:
        return self.out + self.err


class CampaignCase(unittest.TestCase):
    """Each test gets its own directory, repository, job queue and quiet state."""

    root: Path
    home: Path
    env: dict[str, str]

    def setUp(self) -> None:
        base = os.environ.get("CAMPAIGN_TEST_TMP")
        if base:
            Path(base).mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=base)).resolve()
        self.env = dict(os.environ)
        for key in (
            "CAMPAIGN_HOME",
            "CAMPAIGN_NO_COMMIT",
            "CAMPAIGN_MAIN",
            "CAMPAIGN_FINGERPRINT",
            "CMUX_WORKSPACE_ID",
        ):
            self.env.pop(key, None)
        self.env.update(
            CAMPAIGN_JOBS=str(self.root / "jobs"),
            QUIET_DIR=str(self.root / "quiet"),
            CAMPAIGN_WORKER_TICK="0.2",
            CAMPAIGN_WAIT_TICK="0.2",
            CAMPAIGN_HEARTBEAT_SECONDS="0.5",
            CAMPAIGN_STALE_SECONDS="10",
            QUIET_POLL="0.2",
            QUIET_LOAD_WAIT="0",
            QUIET_MAX_LOAD="1000",
            QUIET_DISK_MIN_GB="0",
            QUIET_LIGHT_SLOTS="4",
            CAMPAIGN_LIGHT_JOBS="4",
            GIT_AUTHOR_NAME="Test",
            GIT_AUTHOR_EMAIL="test@example.com",
            GIT_COMMITTER_NAME="Test",
            GIT_COMMITTER_EMAIL="test@example.com",
        )
        self.env["CAMPAIGN_CODEX_LOGS"] = str(self.root / "codex")
        self.env["CAMPAIGN_COPILOT_LOGS"] = str(self.root / "copilot")
        self.env["PATH"] = f"{self.root / 'bin'}:{self.env['PATH']}"
        (self.root / "bin").mkdir()
        (self.root / "bin" / "quiet").symlink_to(QUIET)  # this checkout's quiet, not the installed one
        self.home = self.repo("app", {"app.py": "print(1)\n", "lib/util.py": "X = 1\n", "README.md": "app\n"})
        self.ok(self.home, "init", "--name", "demo")

    def tearDown(self) -> None:
        sids = set()
        for d in (self.root / "jobs").glob("*/state.json"):
            state = json.loads(d.read_text())
            if state.get("sid"):
                sids.add(int(state["sid"]))
        for pid in session_members(sids):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
        subprocess.run(["pkill", "-f", str(self.root)], capture_output=True)
        with contextlib.suppress(FileNotFoundError, ValueError, ProcessLookupError):
            os.kill(int((self.root / "jobs" / ".worker" / "pid").read_text()), signal.SIGKILL)
        if os.environ.get("CAMPAIGN_TEST_KEEP"):
            return
        for _ in range(50):  # a dying worker or runner may still be writing
            try:
                shutil.rmtree(self.root)
                return
            except FileNotFoundError:
                return
            except OSError:
                time.sleep(0.2)
        shutil.rmtree(self.root)

    # -- repositories

    def repo(self, name: str, files: dict[str, str]) -> Path:
        path = self.root / name
        path.mkdir()
        self.git(path, "init", "-q", "-b", "main")
        for rel, text in files.items():
            (path / rel).parent.mkdir(parents=True, exist_ok=True)
            (path / rel).write_text(text)
        self.git(path, "add", ".")
        self.git(path, "commit", "-q", "-m", "initial")
        return path

    def git(self, repo: Path, *args: str) -> str:
        p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=self.env)
        if p.returncode != 0:
            raise AssertionError(f"git {' '.join(args)}: {p.stderr}")
        return p.stdout.strip()

    def commit(self, repo: Path, files: dict[str, str], message: str = "change") -> str:
        for rel, text in files.items():
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text(text)
        self.git(repo, "add", *files)
        self.git(repo, "commit", "-q", "-m", message)
        return self.git(repo, "rev-parse", "HEAD")

    def wt(self, name: str) -> Path:
        return self.root / "app-wt" / "demo" / name

    # -- the tool

    def run_cli(self, cwd: Path, *args: str, timeout: float = 120) -> Result:
        p = subprocess.run(
            [sys.executable, str(CAMPAIGN), *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            env=self.env,
            timeout=timeout,
        )
        return Result(p)

    def ok(self, cwd: Path, *args: str, timeout: float = 120) -> Result:
        r = self.run_cli(cwd, *args, timeout=timeout)
        self.assertEqual(r.code, 0, f"campaign {' '.join(args)} failed:\n{r.text}")
        return r

    def fails(self, cwd: Path, *args: str, match: str = "") -> Result:
        r = self.run_cli(cwd, *args)
        self.assertNotEqual(r.code, 0, f"campaign {' '.join(args)} should fail:\n{r.text}")
        if match:
            self.assertIn(match, r.text)
        return r

    def state(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads((self.home / ".campaign/demo/state.json").read_text())
        return data

    def quest(self, pid: str) -> dict[str, Any]:
        result: dict[str, Any] = self.state()["quests"][pid]
        return result

    def job(self, jid: str) -> dict[str, Any]:
        jid = jid.split(maxsplit=1)[0]
        result: dict[str, Any] = json.loads((self.root / "jobs" / jid / "state.json").read_text())
        return result

    def submit(self, cwd: Path, *args: str) -> str:
        return self.ok(cwd, "job", "submit", *args).out.split()[0]

    def verified(self, pid: str) -> None:
        """Queues the quest's checks at its head and waits for them to pass."""
        self.ok(self.home, "verify", pid)
        self.ok(self.home, "wait", "--quest", pid, "--timeout", "90", timeout=120)

    def review(self, pid: str, lens: str, verdict: str, commit: str = "") -> None:
        folder = self.home / ".campaign/demo/reviews"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{pid}-{lens}.md").write_text(f"# {pid} review: {lens}\n\nCommit: {commit}\nVerdict: {verdict}\n")
        self.git(self.home, "add", ".campaign/demo/reviews")
        self.git(self.home, "commit", "-q", "-m", f"review {pid} {lens}")

    def wait_until(self, predicate: Any, timeout: float = 30) -> None:
        end = time.time() + timeout
        while time.time() < end:
            if predicate():
                return
            time.sleep(0.2)
        self.fail("condition not reached in time")

    def started(
        self, pid: str, files: str = "", risk: str = "low", check: str = "unit=python3 app.py", **extra: str
    ) -> Path:
        args = ["new", pid, "--title", pid, "--risk", risk, "--check", check]
        if files:
            args += ["--files", files]
        for key, value in extra.items():
            args += [f"--{key}", value]
        self.ok(self.home, *args)
        self.ok(self.home, "start", pid)
        return self.wt(pid)
