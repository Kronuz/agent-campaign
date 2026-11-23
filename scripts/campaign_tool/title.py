"""Session titles: a lead's session name leads with its campaign, and the cmux workspace says so too.

Boundary: reading a harness's own record of the session's name and the one line that tells the owner
how to fix it; nothing else of a session. Opened alone when a harness changes where or how it records
a session's name (references/harness.md, "Harness-specific mechanisms").
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from .core import Json, jobs_dir
from .mail import recorded_lead

if TYPE_CHECKING:  # annotations only: they import this module, so importing them here is a cycle
    from .campaign import Campaign

TITLE_TYPES = ("custom-title", "ai-title")  # the transcript's records: the owner's name, a generated one
TAIL_BYTES = (1 << 18, 1 << 21, 1 << 24)  # read this much of a transcript's end before reading it whole


def session_name(c: Campaign, role: str = "lead") -> str:
    """The title a campaign session carries: its campaign, then its role."""
    return f"{c.name} · {role}"


def transcript() -> Path | None:
    """This Claude Code session's transcript, or None outside Claude Code. The harness names the session
    ($CLAUDE_CODE_SESSION_ID); without it, the newest transcript of the current directory's project."""
    if not os.environ.get("CLAUDECODE"):
        return None
    root = Path(os.environ.get("CAMPAIGN_CLAUDE_LOGS") or Path.home() / ".claude" / "projects")
    ident = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if ident:
        return next(iter(root.glob(f"*/{ident}.jsonl")), None)
    folder = root / re.sub(r"[/.]", "-", str(Path.cwd()))
    found = sorted(folder.glob("*.jsonl"), key=lambda p: p.stat().st_mtime) if folder.is_dir() else []
    return found[-1] if found else None


def newest_title(path: Path) -> tuple[str, str]:
    """The newest title record of the transcript at PATH as (type, title), or ("", "") when it has none."""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            for window in (*(w for w in TAIL_BYTES if w < size), size):
                f.seek(size - window)
                found = title_in(f.read().decode("utf-8", "replace").splitlines())
                if found:
                    return found
    except OSError:
        pass
    return "", ""


def title_in(lines: list[str]) -> tuple[str, str] | None:
    """The newest title record among LINES (the first of a tail may be cut), or None."""
    for line in reversed(lines):
        if not any(f'"{t}"' in line for t in TITLE_TYPES):
            continue
        try:
            record: Json = json.loads(line)
        except ValueError:
            continue
        kind = record.get("type")
        if kind in TITLE_TYPES:
            return str(kind), str(record.get("customTitle" if kind == "custom-title" else "aiTitle", ""))
    return None


def in_lead_directory(c: Campaign) -> bool:
    """Whether the current directory is in the home checkout: a quest's worktree is not the lead's
    session, and its agent's commands say nothing of the lead's title."""
    try:
        Path.cwd().resolve().relative_to(c.home.resolve())
    except ValueError:
        return False
    return True


def title_check(c: Campaign) -> str | None:
    """The one line to print when the lead's Claude Code session is not named for the campaign (what
    to type), else None: right, not a lead's directory, or not Claude Code."""
    if not in_lead_directory(c):
        return None
    path = transcript()
    if path is None:
        return None
    kind, title = newest_title(path)
    want = session_name(c)
    if kind == "custom-title" and title.startswith(f"{c.name} · "):
        return None
    now = {"custom-title": f"named {title!r}", "ai-title": f"auto-titled {title!r}"}.get(kind, "unnamed")
    return f"session title: this session is {now}, which the harness never replaces itself; type /rename {want}"


def set_workspace_title(c: Campaign) -> str:
    """Names the cmux workspace for the campaign's lead (it is the sender name of cmux messages); ""
    when set, when cmux is not here or when this session is not the recorded lead (`note_lead`), else
    why it was not."""
    ident = os.environ.get("CMUX_WORKSPACE_ID")  # named explicitly: cmux's "current" workspace is not ours
    surface = os.environ.get("CMUX_SURFACE_ID", "")
    if not ident or not surface or surface != recorded_lead(c.name) or not shutil.which("cmux"):
        return ""
    stamp = jobs_dir() / "titles" / re.sub(r"[^\w.-]", "_", ident)  # the title last set: no call to repeat it
    try:
        if stamp.read_text() == session_name(c):
            return ""
    except OSError:
        pass
    try:
        done = subprocess.run(
            ["cmux", "workspace-action", "--workspace", ident, "--action", "rename", "--title", session_name(c)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return f"cmux failed: {e}"
    if done.returncode != 0:
        return f"cmux failed: {(done.stderr or done.stdout).strip()[:120]}"
    with contextlib.suppress(OSError):
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(session_name(c))
    return ""


def keep_title(c: Campaign) -> str | None:
    """What a lead's command calls: names the cmux workspace and returns title_check's line to print.
    Never raises: a title is not worth a failed command."""
    try:
        if in_lead_directory(c):
            set_workspace_title(c)
        return title_check(c)
    except Exception:
        return None
