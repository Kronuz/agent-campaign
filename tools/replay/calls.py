"""Every `campaign` and `quiet` call the lead and its agents made (Claude Code transcripts), with latency.

A call's latency is from its tool_use line to its tool_result line, both stamped by the harness; a
call sent to the background is left out of latency (its result line only says it started).
"""

from __future__ import annotations

import json
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path

import sources as src
from sources import Json

CALL = re.compile(
    r"(?:^|[\s;&|(`/])(?P<tool>campaign|quiet)\s+(?:-C\s+\S+\s+|--campaign\s+\S+\s+)*(?P<verb>job\s+[a-z]+|[a-z]+)"
)
TOOL_VERBS = {
    "campaign": {
        "init",
        "config",
        "new",
        "get",
        "set",
        "start",
        "status",
        "brief",
        "usage",
        "map",
        "check",
        "sweep",
        "verify",
        "accept",
        "land",
        "pin",
        "message",
        "clean",
        "wait",
        "job",
        "help",
        "review",
        "rerun",
    },
    "quiet": {"check", "light", "strict", "exclusive", "list", "status", "wait"},
}


def transcripts(session: str) -> list[tuple[str, Path]]:
    """The lead's transcript and its agents' (`<session>/subagents/agent-*.jsonl`)."""
    lead = src.lead_transcript(session)
    if lead is None:
        return []
    out = [("lead", lead)]
    out += [("agent", p) for p in sorted((lead.parent / session / "subagents").glob("agent-*.jsonl"))]
    return out


def scan(role: str, log: Path, campaign: str) -> list[Json]:
    asked: dict[str, tuple[float, list[tuple[str, str]], bool]] = {}
    calls: list[Json] = []
    with log.open(errors="replace") as f:
        for line in f:
            if "campaign" not in line and "quiet" not in line and "tool_result" not in line:
                continue
            if '"tool_use"' not in line and '"tool_result"' not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if role == "lead" and e.get("isSidechain"):
                continue
            t = src.stamp(e.get("timestamp", ""))
            content = (e.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and block.get("name") == "Bash":
                    inp = block.get("input") or {}
                    cmd = str(inp.get("command", ""))
                    found = [
                        (m["tool"], re.sub(r"\s+", " ", m["verb"]))
                        for m in CALL.finditer(cmd)
                        if m["verb"].split()[0] in TOOL_VERBS[m["tool"]]
                    ]
                    if found:
                        asked[str(block.get("id"))] = (t, found, bool(inp.get("run_in_background")))
                elif block.get("type") == "tool_result" and block.get("tool_use_id") in asked:
                    t0, found, bg = asked.pop(str(block["tool_use_id"]))
                    body = block.get("content")
                    text = body if isinstance(body, str) else json.dumps(body)[:200]
                    bg = bg or "running in background" in text[:200]
                    for tool, verb in found:
                        calls.append(
                            {
                                "t": t0,
                                "campaign": campaign,
                                "role": role,
                                "tool": tool,
                                "verb": verb,
                                "seconds": None if bg or len(found) > 1 else round(t - t0, 3),
                            }
                        )
    return calls


def run(sessions: dict[str, str], out: Path, refresh: bool = False) -> Json:
    """query calls: per tool and verb, calls in the window, per day, and single-call latency."""
    cache = out / "calls.json"
    if refresh or not cache.exists():
        rows: list[Json] = []
        for campaign, session in sessions.items():
            for role, log in transcripts(session):
                rows += scan(role, log, campaign)
        out.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(rows))
    rows = src.read_json(cache) or []
    rows = [r for r in rows if src.SINCE <= r["t"] < src.UNTIL]
    days = (src.UNTIL - src.SINCE) / 86400
    by: dict[str, list[Json]] = defaultdict(list)
    for r in rows:
        by[f"{r['tool']} {r['verb']}"].append(r)
    table: Json = {}
    for k, rs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        lat = sorted(r["seconds"] for r in rs if r["seconds"] is not None)
        roles = Counter(r["role"] for r in rs)
        table[k] = {
            "calls": len(rs),
            "per_day": round(len(rs) / days, 1),
            "lead": roles["lead"],
            "agents": roles["agent"],
            "timed": len(lat),
            "median_s": round(statistics.median(lat), 2) if lat else None,
            "p90_s": round(lat[int(0.9 * (len(lat) - 1))], 2) if lat else None,
            "total_h": round(sum(lat) / 3600, 2),
        }
    return {"calls": len(rows), "per_day": round(len(rows) / days), "by_verb": table}
