#!/usr/bin/env python3
"""Read Codex quota windows from local session events, without a network request."""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Sample:
    at: float
    window: str
    used: float
    reset: float
    minutes: float


def event_samples(event: Any) -> list[Sample]:
    if not isinstance(event, dict) or event.get("type") != "event_msg":
        return []
    payload = event.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return []
    limits = payload.get("rate_limits")
    if not isinstance(limits, dict):
        return []
    try:
        at = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        return []
    result: list[Sample] = []
    for name, label in (("primary", "5h"), ("secondary", "weekly")):
        window = limits.get(name)
        if not isinstance(window, dict):
            continue
        used, reset, minutes = (window.get(key) for key in ("used_percent", "resets_at", "window_minutes"))
        if (
            not isinstance(used, (int, float))
            or not isinstance(reset, (int, float))
            or not isinstance(minutes, (int, float))
        ):
            continue
        if any(isinstance(value, bool) or not math.isfinite(value) for value in (used, reset, minutes)):
            continue
        if 0 <= used <= 100 and reset > 0 and minutes > 0:
            result.append(Sample(at, label, float(used), float(reset), float(minutes)))
    return result


def compact(samples: list[Sample]) -> list[Sample]:
    result: list[Sample] = []
    for name in ("5h", "weekly"):
        windows = [sample for sample in samples if sample.window == name]
        if windows:
            latest = max(windows, key=lambda sample: (sample.at, sample.used))
            result.extend(
                sorted(
                    {sample for sample in windows if sample.reset == latest.reset and sample.at >= latest.at - 3600},
                    key=lambda sample: (sample.at, sample.used),
                )
            )
    return result


def read_samples(root: Path, cache_path: Path) -> list[Sample]:
    cache: dict[str, Any] = {}
    try:
        decoded = json.loads(cache_path.read_text())
        if isinstance(decoded, dict):
            cache = decoded
    except (OSError, ValueError):
        pass
    updated: dict[str, Any] = {}
    result: list[Sample] = []
    for path in root.rglob("rollout-*.jsonl"):
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
                    if b'"token_count"' not in line:
                        continue
                    try:
                        samples.extend(event_samples(json.loads(line)))
                    except (ValueError, UnicodeDecodeError):
                        continue
            samples = compact(samples)
            result.extend(samples)
            updated[str(path)] = {
                "inode": stat.st_ino,
                "size": stat.st_size,
                "mtime": stat.st_mtime_ns,
                "offset": offset,
                "samples": [asdict(sample) for sample in samples],
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
    if not samples:
        return "no Codex quota events in session logs"
    parts: list[str] = []
    for name in ("5h", "weekly"):
        window = [sample for sample in samples if sample.window == name]
        if not window:
            continue
        latest = max(window, key=lambda sample: (sample.at, sample.used))
        age = max(0, now - latest.at)
        reset = datetime.fromtimestamp(latest.reset, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        text = f"{name} {latest.used:g}% used; reset {reset}"
        if age > stale:
            text += f"; stale ({duration(age)} old)"
        if latest.reset <= now:
            parts.append(text + "; expired; burn/ETA unavailable")
            continue
        recent = [sample for sample in window if sample.reset == latest.reset and 0 <= latest.at - sample.at <= 3600]
        first = min(recent, key=lambda sample: (sample.at, sample.used))
        elapsed = latest.at - first.at
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


def main() -> None:
    root = Path(
        os.environ.get(
            "CAMPAIGN_CODEX_LOGS", str(Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "sessions")
        )
    ).expanduser()
    identity = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
    cache = Path.home() / ".scratch" / "quota" / f"codex-events-{identity}.json"
    samples = read_samples(root, cache)
    print(
        describe(
            samples, float(os.environ.get("QUOTA_NOW", str(time.time()))), float(os.environ.get("QUOTA_STALE", "900"))
        )
    )


if __name__ == "__main__":
    main()
