"""The owner's alarm: a lead whose session holds one tool call unanswered, most often a permission
prompt nobody sees, read from the harness's transcript (Claude Code's; other harnesses' unread)."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from .core import Json, jobs_dir, read_json, write_json
from .mail import notify
from .state import campaigns

TAIL = 4 << 20  # bytes of a transcript read from its end: a stuck call is among its last lines
_looked = [0.0]


def stuck_after() -> float:
    """Seconds a lead's tool call may go unanswered before the owner is told: past any foreground
    command's limit (Claude Code's is 10 minutes)."""
    return float(os.environ.get("CAMPAIGN_STUCK_CALL", "600"))


def lead_transcript(home: Path) -> Path | None:
    """The newest Claude Code session transcript started in HOME: the lead's, or None."""
    root = Path(os.environ.get("CAMPAIGN_CLAUDE_LOGS") or Path.home() / ".claude" / "projects")
    folder = root / re.sub(r"[^A-Za-z0-9]", "-", str(home))
    logs: list[tuple[float, Path]] = []
    for log in folder.glob("*.jsonl") if folder.is_dir() else []:
        with contextlib.suppress(OSError):
            logs.append((log.stat().st_mtime, log))
    return max(logs)[1] if logs else None


def unanswered(log: Path, now: float) -> list[Json]:
    """The lead's tool calls in LOG with no result yet, made over stuck_after() seconds before NOW:
    a `tool_use` of the main thread (not a subagent's) without its `tool_result`."""
    try:
        with log.open("rb") as f:
            f.seek(max(0, log.stat().st_size - TAIL))
            lines = f.read().decode(errors="replace").splitlines()
    except OSError:
        return []
    calls: dict[str, Json] = {}
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict) or e.get("isSidechain"):
            continue
        m = e.get("message")
        content = m.get("content") if isinstance(m, dict) else None
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            if e.get("type") == "assistant" and b.get("type") == "tool_use" and b.get("id"):
                calls[str(b["id"])] = {"id": str(b["id"]), "tool": str(b.get("name", "")), "at": e.get("timestamp")}
            elif e.get("type") == "user" and b.get("type") == "tool_result":
                calls.pop(str(b.get("tool_use_id")), None)
    out: list[Json] = []
    for call in calls.values():
        with contextlib.suppress(ValueError, AttributeError, TypeError):
            at = datetime.fromisoformat(str(call["at"]).replace("Z", "+00:00")).timestamp()
            if now - at > stuck_after():
                out.append({**call, "since": at})
    return out


def stuck_calls(now: float) -> list[Json]:
    """Every campaign lead's unanswered calls on this machine, each with its campaign."""
    found: list[Json] = []
    homes: dict[str, str] = {}
    for name, o in campaigns().items():
        homes.setdefault(str(o["home"]), name)
    for home, name in homes.items():
        log = lead_transcript(Path(home))
        found += [{**x, "campaign": name, "log": str(log)} for x in unanswered(log, now)] if log else []
    return found


def watch_calls() -> None:
    """At most once a minute of a wait or the worker: tells the owner once of each lead tool call
    unanswered past stuck_after() (a permission prompt, most often), across processes."""
    if time.time() - _looked[0] < float(os.environ.get("CAMPAIGN_ALARM_SECONDS", "60")):
        return
    _looked[0] = now = time.time()
    stuck = stuck_calls(now)
    if not stuck:
        return
    told_file = jobs_dir() / ".alarms.json"
    with open(jobs_dir() / ".alarms.lock", "w") as lock:  # one process tells of each call
        fcntl.flock(lock, fcntl.LOCK_EX)
        told: Json = (read_json(told_file) or {}) if told_file.exists() else {}
        told = {k: v for k, v in told.items() if now - float(v) < 7 * 86400}
        for call in stuck:
            if call["id"] in told:
                continue
            told[call["id"]] = now
            minutes = round((now - call["since"]) / 60)
            title = f"campaign: {call['campaign']}'s lead is stuck on a {call['tool'] or 'tool'} call"
            body = f"unanswered for {minutes} min (a permission prompt?): {call['log']}"
            notify(title, body)
            print(f"{title}, {body}", file=sys.stderr)
        write_json(told_file, told)
