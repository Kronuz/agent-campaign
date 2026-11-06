#!/usr/bin/env python3
"""pace: quota use per harness, as information; it never sets how many agents run.

Each harness is read from what it records on this machine, without a network request:
  codex    5-hour and weekly windows from the rate limits in its session logs
  copilot  the month's premium requests from the quota snapshots in its session events
  claude   5-hour and weekly windows from the snapshot a Claude Code status line writes
           (~/.scratch/quota/claude.json)

For each window it prints the use, the recent burn, and when that burn would exhaust it against
the reset.
"""

from __future__ import annotations

import argparse
import calendar
import contextlib
import hashlib
import json
import math
import os
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EPILOG = """\
environment:
  QUOTA_HARNESS          the default harness (default all)
  QUOTA_STALE            seconds after which a sample is marked old (default 900)
  QUOTA_NOW              fixes the clock (tests)
  CAMPAIGN_CODEX_LOGS    Codex's session logs (default ~/.codex/sessions)
  CAMPAIGN_COPILOT_LOGS  Copilot CLI's session state (default ~/.copilot/session-state)
  QUOTA_SNAPSHOT         the Claude Code snapshot (default ~/.scratch/quota/claude.json)
"""
HARNESSES = ("claude", "copilot", "codex")
ORDER = ("5h", "weekly", "month")


@dataclass(frozen=True)
class Sample:
    at: float
    window: str
    used: float
    reset: float
    minutes: float


def stamp(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def sample(at: float, window: str, used: Any, reset: Any, minutes: Any) -> Sample | None:
    values = (used, reset, minutes)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
        return None
    if 0 <= used <= 100 and reset > 0 and minutes > 0:
        return Sample(at, window, float(used), float(reset), float(minutes))
    return None


def event_samples(event: Any) -> list[Sample]:
    """Codex: a token_count event's primary (5-hour) and secondary (weekly) windows."""
    if not isinstance(event, dict) or event.get("type") != "event_msg":
        return []
    payload = event.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return []
    limits = payload.get("rate_limits")
    at = stamp(event.get("timestamp"))
    if not isinstance(limits, dict) or at is None:
        return []
    result: list[Sample] = []
    for name, label in (("primary", "5h"), ("secondary", "weekly")):
        window = limits.get(name)
        if isinstance(window, dict):
            s = sample(at, label, window.get("used_percent"), window.get("resets_at"), window.get("window_minutes"))
            if s:
                result.append(s)
    return result


def copilot_samples(event: Any) -> list[Sample]:
    """Copilot CLI: the premium-request quota a model call reports, a window that resets monthly."""
    if not isinstance(event, dict) or event.get("type") != "model.model_call_success":
        return []
    data = event.get("data")
    snapshots = data.get("quotaSnapshots") if isinstance(data, dict) else None
    premium = snapshots.get("premium_interactions") if isinstance(snapshots, dict) else None
    at, reset = stamp(event.get("timestamp")), stamp(premium.get("resetDate") if isinstance(premium, dict) else None)
    if not isinstance(premium, dict) or premium.get("isUnlimitedEntitlement") or at is None or reset is None:
        return []
    remaining = premium.get("remainingPercentage")
    if isinstance(remaining, bool) or not isinstance(remaining, (int, float)):
        return []
    end = datetime.fromtimestamp(reset, timezone.utc)
    year, month = (end.year, end.month - 1) if end.month > 1 else (end.year - 1, 12)
    start = end.replace(year=year, month=month, day=min(end.day, calendar.monthrange(year, month)[1]))
    s = sample(at, "month", 100 - remaining, reset, (reset - start.timestamp()) / 60)
    return [s] if s else []


def claude_samples(snapshot: Any, at: float) -> list[Sample]:
    """Claude Code: the rate limits its status line was given, as written to the snapshot."""
    limits = snapshot.get("rate_limits") if isinstance(snapshot, dict) else None
    if not isinstance(limits, dict):
        return []
    result: list[Sample] = []
    for name, label, minutes in (("five_hour", "5h", 300), ("seven_day", "weekly", 10080)):
        window = limits.get(name)
        if isinstance(window, dict):
            s = sample(at, label, window.get("used_percentage"), window.get("resets_at"), minutes)
            if s:
                result.append(s)
    return result


def lookback(s: Sample) -> float:
    """How far back a recent burn is measured: an hour, or a day for a monthly window."""
    return 86400.0 if s.minutes > 10080 else 3600.0


def compact(samples: list[Sample]) -> list[Sample]:
    """Per window, the samples of its latest reset within the lookback of the newest one."""
    result: list[Sample] = []
    for name in ORDER:
        windows = [s for s in samples if s.window == name]
        if windows:
            latest = max(windows, key=lambda s: (s.at, s.used))
            result.extend(
                sorted(
                    {s for s in windows if s.reset == latest.reset and s.at >= latest.at - lookback(latest)},
                    key=lambda s: (s.at, s.used),
                )
            )
    return result


def read_samples(
    files: list[Path], cache_path: Path, needle: bytes, parse: Callable[[Any], list[Sample]] = event_samples
) -> list[Sample]:
    """Samples from append-only JSON-lines logs, reading only what was appended since the last call."""
    cache: dict[str, Any] = {}
    try:
        decoded = json.loads(cache_path.read_text())
        if isinstance(decoded, dict):
            cache = decoded
    except (OSError, ValueError):
        pass
    updated: dict[str, Any] = {}
    result: list[Sample] = []
    for path in files:
        try:
            stat = path.stat()
            old = cache.get(str(path), {})
            samples: list[Sample] = []
            offset = 0
            if (
                isinstance(old, dict)
                and old.get("inode") == stat.st_ino
                and old.get("size", 0) <= stat.st_size
                and (old.get("size") != stat.st_size or old.get("mtime") == stat.st_mtime_ns)
            ):
                try:
                    samples = [Sample(**item) for item in old["samples"]]
                    offset = int(old["offset"])
                except (KeyError, TypeError, ValueError):
                    samples = []
                    offset = 0
            with path.open("rb") as stream:
                stream.seek(offset)
                while True:
                    line = stream.readline()
                    if not line or not line.endswith(b"\n"):
                        break
                    offset = stream.tell()
                    if needle not in line:
                        continue
                    try:
                        samples.extend(parse(json.loads(line)))
                    except ValueError:  # a UnicodeDecodeError is one too
                        continue
            samples = compact(samples)
            result.extend(samples)
            updated[str(path)] = {
                "inode": stat.st_ino,
                "size": stat.st_size,
                "mtime": stat.st_mtime_ns,
                "offset": offset,
                "samples": [asdict(s) for s in samples],
            }
        except OSError:
            continue
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(updated, separators=(",", ":")))
        temporary.replace(cache_path)
    except OSError:
        pass
    return compact(result)


def duration(seconds: float) -> str:
    minutes = max(0, int(seconds // 60))
    return f"{minutes // 60}h{minutes % 60:02d}m"


def describe(samples: list[Sample], now: float, stale: float) -> str:
    parts: list[str] = []
    for name in ORDER:
        window = [s for s in samples if s.window == name]
        if not window:
            continue
        latest = max(window, key=lambda s: (s.at, s.used))
        age = max(0, now - latest.at)
        reset = datetime.fromtimestamp(latest.reset, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        text = f"{name} {latest.used:g}% used; reset {reset}"
        if age > stale:
            text += f"; stale ({duration(age)} old)"
        if latest.reset <= now:
            parts.append(text + "; expired; burn/ETA unavailable")
            continue
        recent = [s for s in window if s.reset == latest.reset and 0 <= latest.at - s.at <= lookback(latest)]
        first = min(recent, key=lambda s: (s.at, s.used))
        elapsed = latest.at - first.at
        rate: float | None
        if elapsed > 0 and latest.used >= first.used:
            rate = (latest.used - first.used) * 3600 / elapsed
            basis = f"recent {duration(elapsed)}"
        else:
            elapsed = latest.at - (latest.reset - latest.minutes * 60)
            rate = latest.used * 3600 / elapsed if elapsed > 0 else None
            basis = "window average"
        text += f" (in {duration(latest.reset - now)})"
        if rate is None:
            text += "; burn/ETA unavailable (no elapsed window)"
        elif latest.used >= 100:
            text += f"; burn {rate:.2f}%/h ({basis}); quota exhausted"
        elif rate == 0:
            text += f"; burn 0.00%/h ({basis}); no exhaustion at this rate"
        else:
            eta = (100 - latest.used) * 3600 / rate
            relation = "before" if latest.at + eta < latest.reset else "after"
            text += f"; burn {rate:.2f}%/h ({basis}); exhaustion in {duration(eta)} from sample ({relation} reset)"
        parts.append(text)
    return " | ".join(parts)


def cache_for(harness: str, root: Path) -> Path:
    identity = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
    return Path.home() / ".scratch" / "quota" / f"{harness}-events-{identity}.json"


def report(harness: str) -> tuple[bool, str]:
    """(whether this harness is installed here, what pace says about it)."""
    home = Path.home()
    if harness == "codex":
        codex_home = Path(os.environ.get("CODEX_HOME", str(home / ".codex"))).expanduser()
        root = Path(os.environ.get("CAMPAIGN_CODEX_LOGS") or codex_home / "sessions").expanduser()
        files = sorted(root.rglob("rollout-*.jsonl")) if root.is_dir() else []
        samples = read_samples(files, cache_for(harness, root), b'"token_count"') if files else []
        missing = "no Codex quota events in session logs"
    elif harness == "copilot":
        root = Path(os.environ.get("CAMPAIGN_COPILOT_LOGS") or home / ".copilot" / "session-state").expanduser()
        files = sorted(root.glob("*/events.jsonl")) if root.is_dir() else []
        samples = read_samples(files, cache_for(harness, root), b'"quotaSnapshots"', copilot_samples) if files else []
        missing = "no Copilot quota snapshots in session events (no model call yet, or no premium quota)"
    else:
        root = Path(os.environ.get("QUOTA_SNAPSHOT") or home / ".scratch" / "quota" / "claude.json").expanduser()
        samples: list[Sample] = []
        with contextlib.suppress(OSError, ValueError):
            samples = claude_samples(json.loads(root.read_text()), root.stat().st_mtime)
        missing = f"no snapshot at {root} (a Claude Code status line writes it)"
        root = home / ".claude" if not root.exists() else root
    now = float(os.environ.get("QUOTA_NOW", str(time.time())))
    stale = float(os.environ.get("QUOTA_STALE", "900"))
    return root.exists(), describe(samples, now, stale) if samples else missing


def parser() -> argparse.ArgumentParser:
    raw = argparse.RawDescriptionHelpFormatter
    ap = argparse.ArgumentParser(prog="pace", description=__doc__, epilog=EPILOG, formatter_class=raw)
    ap.add_argument(
        "harness",
        nargs="?",
        choices=(*HARNESSES, "all"),
        default=os.environ.get("QUOTA_HARNESS") or "all",
        help="whose quota (default $QUOTA_HARNESS, else all installed)",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    harness = parser().parse_args(argv).harness
    if harness != "all":
        print(report(harness)[1])
        return 0
    lines = [f"{h}: {text}" for h in HARNESSES for installed, text in [report(h)] if installed]
    print("\n".join(lines) if lines else "no harness records on this machine")
    return 0


if __name__ == "__main__":
    sys.exit(main())
