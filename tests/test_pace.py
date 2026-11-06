"""Codex quota events use timestamps and same-reset history across session files."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "codex_quota.py"
SPEC = importlib.util.spec_from_file_location("codex_quota", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
quota = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = quota
SPEC.loader.exec_module(quota)
NOW = 1_800_000_000


def event(at: float, used: float = 10, reset: float = NOW + 3600) -> dict[str, Any]:
    return {
        "timestamp": datetime.fromtimestamp(at, timezone.utc).isoformat(),
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": None,
            "rate_limits": {
                "limit_id": "codex",
                "primary": {"used_percent": used, "window_minutes": 300, "resets_at": reset},
                "secondary": {"used_percent": 64, "window_minutes": 10080, "resets_at": NOW + 300_000},
                "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
            },
        },
    }


class CodexQuota(unittest.TestCase):
    def test_real_shape_and_premium_null_windows(self) -> None:
        self.assertEqual(len(quota.event_samples(event(NOW))), 2)
        premium = event(NOW)
        premium["payload"]["rate_limits"].update(limit_id="premium", primary=None, secondary=None)
        self.assertEqual(quota.event_samples(premium), [])
        self.assertEqual(quota.event_samples({"type": "token_count"}), [])

    def test_freshest_across_files_and_incremental_truncated_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "cache.json"
            a = root / "rollout-a.jsonl"
            b = root / "nested" / "rollout-b.jsonl"
            b.parent.mkdir()
            a.write_text(json.dumps(event(NOW, 30)) + "\n")
            b.write_text("invalid\n" + json.dumps(event(NOW - 1800, 10)) + "\n" + '{"unfinished":')
            os.utime(a, (0, 0))
            result = quota.describe(quota.read_samples(root, cache), NOW, 900)
            self.assertIn("5h 30% used", result)
            self.assertIn("burn 40.00%/h (recent", result)
            self.assertIn("after reset", result)
            self.assertEqual(result, quota.describe(quota.read_samples(root, cache), NOW, 900))
            with b.open("a") as stream:
                stream.write("null}\n" + json.dumps(event(NOW + 60, 35)) + "\n")
            self.assertIn("5h 35% used", quota.describe(quota.read_samples(root, cache), NOW + 60, 900))

    def test_duplicates_zero_burn_and_new_reset(self) -> None:
        samples = quota.event_samples(event(NOW - 1800, 20)) * 2 + quota.event_samples(event(NOW, 20))
        result = quota.describe(quota.compact(samples), NOW, 900)
        self.assertIn("burn 0.00%/h (recent", result)
        self.assertIn("no exhaustion at this rate", result)
        samples += quota.event_samples(event(NOW + 60, 5, NOW + 18_000))
        result = quota.describe(quota.compact(samples), NOW + 60, 900)
        self.assertIn("5h 5% used", result)
        self.assertIn("window average", result)

    def test_fallback_stale_expired_and_exhaustion(self) -> None:
        result = quota.describe(quota.event_samples(event(NOW, 90)), NOW + 1200, 900)
        self.assertIn("stale (0h20m old)", result)
        self.assertIn("window average", result)
        self.assertIn("before reset", result)
        result = quota.describe(quota.event_samples(event(NOW, 90)), NOW + 4000, 900)
        self.assertIn("expired; burn/ETA unavailable", result)
        self.assertIn(
            "no elapsed window", quota.describe(quota.event_samples(event(NOW, reset=NOW + 18_000)), NOW, 900)
        )

    def test_missing_logs_and_codex_home_default_all(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = root / "custom" / "sessions"
            sessions.mkdir(parents=True)
            env = {**os.environ, "HOME": directory, "CODEX_HOME": str(root / "custom"), "QUOTA_NOW": str(NOW)}
            env.pop("CAMPAIGN_CODEX_LOGS", None)
            env.pop("QUOTA_HARNESS", None)
            pace = SCRIPT.with_name("pace.sh")
            missing = subprocess.check_output(["bash", str(pace)], env=env, text=True)
            self.assertIn("codex: no Codex quota events", missing)
            (sessions / "rollout-test.jsonl").write_text(json.dumps(event(NOW)) + "\n")
            present = subprocess.check_output(["bash", str(pace)], env=env, text=True)
            self.assertIn("codex: 5h 10% used", present)
            env["CAMPAIGN_CODEX_LOGS"] = str(root / "absent")
            self.assertIn(
                "no Codex quota events", subprocess.check_output(["bash", str(pace), "codex"], env=env, text=True)
            )
