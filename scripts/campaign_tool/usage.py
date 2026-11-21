"""Usage: agents' token use and cost, read from what the harnesses and jobs already record."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from style import mark, paint

from .campaign import Campaign, find_campaign
from .core import ASSIGNMENTS, LENSES, ROLES, Json, git, plural, span
from .jobs import all_jobs

# Usage: read from what the harnesses and jobs already record; agents write nothing for it.
ROLE = re.compile(
    r"You (?:are (?:the |an? )?(?:(?P<lens>\S+) )?(?P<role>contributor|reviewer)\b|(?P<resume>resume) quest )"
)
QUEST = re.compile(r"\bquest (?P<id>[\w.-]+?)[:,.]?(?:\s|$)")
NAMED = re.compile(r"\bcampaign (?P<name>[\w.-]+)(?: \((?P<home>[^)]+)\))?")
ASSIGNMENT = re.compile(r"^Assignment: (\w+)", re.M)


def launch(prompt: str) -> Json | None:
    """The role, quest, assignment and campaign a launch prompt names (references/prompts.md)."""
    for line in prompt.splitlines():
        role, named = ROLE.search(line), NAMED.search(line)
        if role and named:
            quest, given = QUEST.search(line), ASSIGNMENT.search(prompt)
            kind = "contributor" if role["resume"] else role["role"]
            reviewer = kind == "reviewer"
            assignment = given[1] if given and given[1] in ASSIGNMENTS else "implementation"
            return {
                "role": kind,
                "resume": bool(role["resume"]),
                "lens": role["lens"] or "" if reviewer else "",
                "quest": quest["id"] if quest else "-",
                "assignment": "" if reviewer else assignment,
                "name": named["name"],
                "home": named["home"] or "",
            }
    return None


# (usage field, Copilot's name): inputTokens includes the cached reads, as Codex's input_tokens does
COPILOT_TOKENS = (
    ("cached_input_tokens", "cacheReadTokens"),
    ("cache_write_tokens", "cacheWriteTokens"),
    ("output_tokens", "outputTokens"),
    ("reasoning_tokens", "reasoningTokens"),
)
BREAKDOWN = ("cached_input_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens", "requests")


def copilot_runs(since: float) -> tuple[list[Json], list[Json]]:
    """Agent runs and lead sessions from Copilot CLI's session events."""
    root = Path(os.environ.get("CAMPAIGN_COPILOT_LOGS") or Path.home() / ".copilot" / "session-state")
    runs: list[Json] = []
    sessions: list[Json] = []
    for log in sorted(root.glob("*/events.jsonl")):
        if log.stat().st_mtime < since:
            continue
        prompts: dict[str, str] = {}
        agents: dict[str, Json] = {}
        lead: Json = {
            "harness": "copilot",
            "session": log.parent.name,
            "cost": 0.0,
            "tokens": 0,
            "cost_available": True,
        }
        with log.open(errors="replace") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                t, d = e.get("type", ""), e.get("data") or {}
                if t == "tool.execution_start" and d.get("toolName") == "task":
                    prompts[d.get("toolCallId", "")] = str((d.get("arguments") or {}).get("prompt", ""))
                    continue
                if t == "session.usage_record":
                    u = d.get("usage") or {}
                    who = agents.setdefault(e["agentId"], {}) if e.get("agentId") else lead
                    who["cost"] = who.get("cost", 0.0) + float(u.get("cost") or 0)
                    who["tokens"] = (
                        who.get("tokens", 0) + int(u.get("inputTokens") or 0) + int(u.get("outputTokens") or 0)
                    )
                    for field, key in COPILOT_TOKENS:
                        who[field] = who.get(field, 0) + int(u.get(key) or 0)
                    who["requests"] = who.get("requests", 0) + 1
                    continue
                if not t.startswith("subagent.") or not e.get("agentId"):
                    continue
                a = agents.setdefault(e["agentId"], {})
                a.setdefault("began", e.get("timestamp", ""))
                a["call"] = d.get("toolCallId") or a.get("call", "")
                a["type"] = d.get("agentName") or a.get("type", "")
                a["model"] = d.get("model") or a.get("model", "")
                a["effort"] = d.get("reasoningEffort") or a.get("effort", "")
                if t == "subagent.completed":
                    a["seconds"] = a.get("seconds", 0.0) + float(d.get("durationMs") or 0) / 1000
                    a["tool_calls"] = a.get("tool_calls", 0) + int(d.get("totalToolCalls") or 0)
        mine = 0
        for aid, a in agents.items():
            what = launch(prompts.get(a.get("call", ""), ""))
            if what is None:
                continue
            mine += 1
            runs.append(
                {
                    "harness": "copilot",
                    "session": log.parent.name,
                    "agent": aid,
                    **what,
                    **{k: a.get(k, d) for k, d in (("type", ""), ("model", ""), ("effort", ""), ("began", ""))},
                    **{k: a.get(k, 0) for k in ("tokens", "cost", "seconds", "tool_calls", *BREAKDOWN)},
                    "cost_available": True,
                }
            )
        if mine:
            sessions.append(lead)
    return runs, sessions


def codex_runs(since: float) -> tuple[list[Json], list[Json]]:
    """Read thread-local cumulative usage; cached/reasoning tokens are subsets, not additions."""
    root = Path(
        os.environ.get("CAMPAIGN_CODEX_LOGS")
        or Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "sessions"
    )
    threads: dict[str, Json] = {}
    launches: dict[tuple[str, str], str] = {}

    def timestamp(value: str) -> float:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            return 0.0

    for log in sorted(root.rglob("rollout-*.jsonl")):
        if log.stat().st_mtime < since:
            continue
        meta: Json = {}
        what: Json | None = None
        model, effort, began, ended = "", "", "", ""
        records: dict[str, Json] = {}
        cumulative: Json = {}
        turns: dict[str, Json] = {}
        starts: dict[str, float] = {}
        durations: dict[tuple[str, str], float] = {}
        calls = 0
        with log.open(errors="replace") as f:
            for line in f:
                if not any(
                    key in line
                    for key in (
                        '"session_meta"',
                        '"turn_context"',
                        '"token_usage_record"',
                        '"token_count"',
                        '"task_started"',
                        '"task_complete"',
                        '"function_call"',
                        '"agent_message"',
                        '"role":"user"',
                        '"role": "user"',
                    )
                ):
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(e, dict) or not isinstance(e.get("payload"), dict):
                    continue
                kind, d, at = e.get("type"), e["payload"], e.get("timestamp", "")
                if kind == "session_meta":
                    meta = d
                    began = d.get("timestamp") or at
                elif kind == "turn_context":
                    model = d.get("model") or model
                    effort = d.get("effort") or d.get("reasoning_effort") or effort
                    turns[d.get("turn_id", "")] = {"model": model, "effort": effort}
                elif kind == "token_usage_record":
                    # A response may be recorded again after a retry; replace, never sum it twice.
                    ident = d.get("response_id")
                    if ident:
                        records[ident] = {
                            "usage": d.get("usage") or {},
                            "model": model,
                            "effort": effort,
                            "turn": d.get("turn_id", ""),
                            "at": at,
                        }
                    ended = at
                elif kind == "event_msg":
                    if d.get("type") == "token_count" and isinstance(d.get("info"), dict):
                        cumulative = d["info"].get("total_token_usage") or cumulative
                        ended = at
                    elif d.get("type") == "task_started":
                        starts[d.get("turn_id", "")] = timestamp(at)
                    elif d.get("type") == "task_complete":
                        turn = d.get("turn_id", "")
                        config = turns.get(turn, {"model": model, "effort": effort})
                        key = (config["model"], config["effort"])
                        durations[key] = durations.get(key, 0.0) + max(
                            0.0, timestamp(at) - starts.pop(turn, timestamp(at))
                        )
                        ended = at
                elif kind == "response_item":
                    if d.get("type") == "function_call":
                        calls += 1
                        if d.get("name", "").split(".")[-1] == "spawn_agent":
                            try:
                                args = json.loads(d.get("arguments") or "{}")
                            except (ValueError, TypeError):
                                continue
                            if isinstance(args, dict):
                                launches[(meta.get("id", ""), args.get("task_name", ""))] = args.get(
                                    "message", args.get("prompt", "")
                                )
                    elif what is None and (d.get("type") == "agent_message" or d.get("role") == "user"):
                        prompt = "".join(c.get("text", "") for c in d.get("content", []) if isinstance(c, dict))
                        what = launch(prompt)
        ident = meta.get("id")
        if not ident:
            continue
        groups: dict[tuple[str, str], Json] = {}
        for record in records.values():
            config = turns.get(record["turn"], record)
            key = (config["model"], config["effort"])
            g = groups.setdefault(
                key, {"tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}
            )
            u = record["usage"]
            g["tokens"] += int(
                u.get("total_tokens", int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0)) or 0
            )
            for field in ("cached_input_tokens", "output_tokens"):
                g[field] += int(u.get(field) or 0)
            g["reasoning_tokens"] += int(u.get("reasoning_output_tokens") or 0)
            g["requests"] = g.get("requests", 0) + 1
        if not records:
            groups[(model, effort)] = {
                "tokens": int(
                    cumulative.get(
                        "total_tokens",
                        int(cumulative.get("input_tokens") or 0) + int(cumulative.get("output_tokens") or 0),
                    )
                    or 0
                ),
                "cached_input_tokens": int(cumulative.get("cached_input_tokens") or 0),
                "output_tokens": int(cumulative.get("output_tokens") or 0),
                "reasoning_tokens": int(cumulative.get("reasoning_output_tokens") or 0),
            }
        for turn, start in starts.items():
            config = turns.get(turn, {"model": model, "effort": effort})
            key = (config["model"], config["effort"])
            durations[key] = durations.get(key, 0.0) + max(0.0, timestamp(ended) - start)
        source = meta.get("source") or {}
        spawn: Json = source.get("subagent", {}).get("thread_spawn", {}) if isinstance(source, dict) else {}
        threads[ident] = {
            "meta": meta,
            "parent": meta.get("parent_thread_id") or spawn.get("parent_thread_id", ""),
            "path": meta.get("agent_path") or spawn.get("agent_path", ""),
            "what": what,
            "groups": groups,
            "durations": durations,
            "began": began,
            "ended": ended,
            "calls": calls,
        }
    runs: list[Json] = []
    sessions: list[Json] = []
    for ident, t in threads.items():
        meta, parent = t["meta"], t["parent"]
        sid = meta.get("session_id") or parent or ident
        attribution: Json = {}
        if parent:
            path = t["path"]
            # This installation encrypts agent messages, but plaintext harnesses retain attribution.
            prompt = launches.get((parent, path), launches.get((parent, path.rsplit("/", 1)[-1]), ""))
            what = launch(prompt) or t["what"]
            if what is None:
                what = {
                    "name": "",
                    "home": meta.get("cwd", ""),
                    "quest": "-",
                    "role": "unattributed",
                    "assignment": "",
                    "lens": "",
                }
            attribution = what
        for (model, effort), totals in t["groups"].items():
            row = {
                "harness": "codex",
                "session": sid,
                "agent": ident,
                "path": t["path"],
                "model": model,
                "effort": effort,
                "began": t["began"],
                "ended": t["ended"],
                "cost": 0.0,
                "cost_available": False,
                "tool_calls": t["calls"],
                **totals,
                "seconds": t["durations"].get((model, effort), max(0.0, timestamp(t["ended"]) - timestamp(t["began"]))),
            }
            if parent:
                runs.append({**row, **attribution})
            else:
                sessions.append({**row, "session": ident, "home": meta.get("cwd", "")})
    return runs, sessions


def claude_runs(since: float) -> tuple[list[Json], list[Json]]:
    """Agent runs and lead sessions from Claude Code's transcripts: `<project>/<session>.jsonl` is the
    lead, `<project>/<session>/subagents/agent-<id>.jsonl` (with its `.meta.json`) each agent. A
    response's content blocks are logged as separate lines with one message id: each id counts once."""
    root = Path(os.environ.get("CAMPAIGN_CLAUDE_LOGS") or Path.home() / ".claude" / "projects")

    def identof(value: str) -> float:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError):
            return 0.0

    def read(log: Path) -> Json:
        """Totals of the responses logged at or after `since` (a lead session may predate it)."""
        seen: dict[str, Json] = {}
        t: Json = {"prompt": "", "cwd": "", "model": "", "effort": "", "began": "", "ended": "", "calls": 0}
        with log.open(errors="replace") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                kind, stamp = e.get("type"), e.get("timestamp") or ""
                if stamp:
                    t["began"] = t["began"] or stamp
                    t["ended"] = stamp
                t["cwd"] = t["cwd"] or e.get("cwd") or ""
                if kind == "api-request":
                    effort = ((e.get("params") or {}).get("output_config") or {}).get("effort")
                    t["effort"] = effort or t["effort"]
                    continue
                m = e.get("message") if isinstance(e.get("message"), dict) else {}
                if kind == "user" and not t["prompt"]:
                    body = m.get("content")
                    t["prompt"] = body if isinstance(body, str) else ""
                if kind != "assistant":
                    continue
                t["model"] = m.get("model") or t["model"]
                content: list[Json] = m["content"] if isinstance(m.get("content"), list) else []
                t["calls"] += sum(1 for b in content if isinstance(b, dict) and b.get("type") == "tool_use")
                if m.get("usage") and identof(stamp) >= since:
                    seen[m.get("id") or f"line{len(seen)}"] = m["usage"]
        totals = dict.fromkeys(("tokens", *BREAKDOWN), 0)
        for u in seen.values():
            fresh, made = int(u.get("input_tokens") or 0), int(u.get("cache_creation_input_tokens") or 0)
            cached, out = int(u.get("cache_read_input_tokens") or 0), int(u.get("output_tokens") or 0)
            totals["tokens"] += fresh + made + cached + out
            totals["cached_input_tokens"] += cached
            totals["cache_write_tokens"] += made
            totals["output_tokens"] += out
            totals["reasoning_tokens"] += int((u.get("output_tokens_details") or {}).get("thinking_tokens") or 0)
        totals["requests"] = len(seen)
        return {**t, **totals}

    def seconds(t: Json) -> float:
        began = max(identof(t["began"]), since)
        return max(0.0, identof(t["ended"]) - began) if t["ended"] else 0.0

    runs: list[Json] = []
    sessions: list[Json] = []
    for lead_log in sorted(root.glob("*/*.jsonl")):
        agents = sorted((lead_log.parent / lead_log.stem / "subagents").glob("agent-*.jsonl"))
        fresh = [a for a in agents if a.stat().st_mtime >= since]
        if lead_log.stat().st_mtime < since and not fresh:
            continue
        mine = 0
        for log in fresh:
            t = read(log)
            what = launch(t["prompt"])
            if what is None:
                continue
            mine += 1
            meta: Json
            try:
                meta = json.loads(log.with_suffix(".meta.json").read_text())
            except (OSError, ValueError):
                meta = {}
            runs.append(
                {
                    "harness": "claude",
                    "session": lead_log.stem,
                    "agent": log.stem.removeprefix("agent-"),
                    **what,
                    "type": meta.get("agentType", ""),
                    "model": t["model"] or meta.get("pickedEffortModel") or meta.get("model", ""),
                    "effort": t["effort"],
                    "began": t["began"],
                    "ended": t["ended"],
                    "seconds": seconds(t),
                    "tool_calls": t["calls"],
                    "cost": 0.0,
                    "cost_available": False,
                    **{k: t[k] for k in ("tokens", *BREAKDOWN)},
                }
            )
        if mine or lead_log.stat().st_mtime >= since:
            t = read(lead_log)
            sessions.append(
                {
                    "harness": "claude",
                    "session": lead_log.stem,
                    "home": t["cwd"],
                    "model": t["model"],
                    "effort": t["effort"],
                    "began": t["began"],
                    "ended": t["ended"],
                    "seconds": seconds(t),
                    "cost": 0.0,
                    "cost_available": False,
                    **{k: t[k] for k in ("tokens", *BREAKDOWN)},
                }
            )
    return runs, sessions


USAGE_READERS = {
    "claude": claude_runs,
    "copilot": copilot_runs,
    "codex": codex_runs,
}  # harness -> reader(since) -> (agent runs, lead sessions)


def task_attribution(c: Campaign, task: str) -> Json | None:
    """Role, quest and assignment or lens from a launch's task name, for harnesses that keep only the
    name in plaintext (Codex, whose names allow only lowercase letters, digits and underscores):
    `<role>_<quest>[_<assignment or lens>]`, `_` read as `-`. A name counts toward a quest when it
    holds one of the campaign's quest or task ids as whole words (the longest wins); a lens marks a
    reviewer, and a quest or task without one its contributor. Parts of words are never guessed at."""
    words = task.lower().replace("_", "-").split("-")  # "--" leaves an empty word, as in task ids
    role = words[0] if words[0] in ROLES else ""
    lens = next((w for w in words if w in LENSES), "")
    given = next((w for w in words[1:] if w in ASSIGNMENTS), "")

    def holds(ident: str) -> bool:
        part = ident.split("-")
        return any(words[i : i + len(part)] == part for i in range(len(words) - len(part) + 1))

    found: dict[str, int] = {}
    for pid, q in c.quests.items():
        for ident in [pid, *(f"{pid}--{t}" for t in q.get("tasks", {})), *q.get("tasks", {})]:
            if holds(ident):
                found[pid] = max(found.get(pid, 0), len(ident.split("-")))
    best = max(found.values(), default=0)
    quests = [pid for pid, n in found.items() if n == best]
    quest = quests[0] if len(quests) == 1 else "-"
    role = role or ("reviewer" if lens else "contributor" if quest != "-" else "")
    if not role:
        return None
    reviewer = role == "reviewer"
    assignment = "" if reviewer else given or "implementation"
    return {"role": role, "quest": quest, "assignment": assignment, "lens": lens if reviewer else ""}


def usage(c: Campaign) -> Json:
    created = git(c.home, "log", "--follow", "--diff-filter=A", "--format=%ct", "--", f"{c.meta}/state.json").split()
    since = float(created[-1]) if created else 0.0
    runs: list[Json] = []
    sessions: list[Json] = []
    for reader in USAGE_READERS.values():
        r, s = reader(since)
        runs += r
        sessions += s
    home = c.home.resolve()
    runs = [
        r
        for r in runs
        if r["name"] == c.state["campaign"] or (r["home"] and Path(r["home"]).expanduser().resolve() == home)
    ]
    for r in runs:
        r["task"] = str(r.get("path", "")).rsplit("/", 1)[-1]
        if r["role"] == "unattributed" and r["task"]:
            r.update(task_attribution(c, r["task"]) or {})
    related = {(r["harness"], r["session"]) for r in runs}
    sessions = [
        s
        for s in sessions
        if (s["harness"], s["session"]) in related or (s.get("home") and Path(s["home"]).expanduser().resolve() == home)
    ]
    jobs: list[Json] = []
    for j in all_jobs():
        if j["spec"].get("campaign") != c.ident:
            continue
        st, sp = j["state"], j["spec"]
        jobs.append(
            {
                "id": j["id"],
                "quest": sp.get("quest") or "-",
                "level": sp.get("level", ""),
                "state": st.get("state", ""),
                "waited": max(0.0, st["began"] - sp["submitted"]) if st.get("began") and sp.get("submitted") else 0.0,
                "ran": max(0.0, st["ended"] - st["began"]) if st.get("ended") and st.get("began") else 0.0,
            }
        )
    return {"campaign": c.state["campaign"], "since": since, "runs": runs, "lead": sessions, "jobs": jobs}


def amount(n: float) -> str:
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e4 else f"{n:,.0f}"


def cost_of(rows: list[Json]) -> str:
    """The rows' cost in their harness's unit; "n/a" for what a harness does not report, never 0."""
    known = [r for r in rows if r.get("cost_available", True)]
    text = amount(sum(r["cost"] for r in known)) if known else ""
    return text + ("+n/a" if text and len(known) < len(rows) else "" if text else "n/a")


def cached_share(rows: list[Json]) -> str:
    tokens = sum(r["tokens"] for r in rows)
    return f"{round(100 * sum(r.get('cached_input_tokens', 0) for r in rows) / tokens)}%" if tokens else "-"


def cmd_usage(a: argparse.Namespace) -> int:
    u = usage(find_campaign(Path.cwd()))
    if a.json:
        print(json.dumps(u, indent=2))
        return 0
    runs, jobs = u["runs"], u["jobs"]

    def table(title: str, key: Callable[[Json], str]) -> None:
        groups: dict[str, list[Json]] = {}
        for r in runs:
            groups.setdefault(key(r), []).append(r)
        width = max(len(title), *map(len, groups))
        print("\n" + paint(f"{title:<{width}} runs  tokens cached  output    cost  agent time", "bold"))
        for k, rs in sorted(groups.items(), key=lambda x: (-sum(r["tokens"] for r in x[1]), x[0])):
            tokens, out = sum(r["tokens"] for r in rs), sum(r.get("output_tokens", 0) for r in rs)
            print(
                f"{k:<{width}} {len(rs):>4} {amount(tokens):>7} {cached_share(rs):>6} {amount(out):>7} "
                f"{cost_of(rs):>7}  {span(sum(r['seconds'] for r in rs)):>10}"
            )

    when = time.strftime("%Y-%m-%d", time.localtime(u["since"]))
    print(
        f"{mark('📊')}{paint('campaign ' + u['campaign'], 'bold')} since {when}: {len(runs)} agent runs, "
        f"{amount(sum(r['tokens'] for r in runs))} tokens ({cached_share(runs)} cached input), cost {cost_of(runs)}; "
        f"{len(jobs)} jobs ran {span(sum(j['ran'] for j in jobs))} and waited {span(sum(j['waited'] for j in jobs))}"
    )
    if runs:
        table("by quest", lambda r: "(unattributed)" if r["quest"] == "-" else r["quest"])
        table(
            "by role, assignment, model and effort",
            lambda r: " ".join(
                x
                for x in (
                    r["role"],
                    "resume" if r.get("resume") else "",
                    r["lens"],
                    r["assignment"],
                    r["task"] if r["role"] == "unattributed" else "",
                    r["model"],
                    r["effort"],
                )
                if x
            ),
        )
        resumed = [r for r in runs if r.get("resume")]
        if resumed:
            print(
                f"\nrecovery: {plural(len(resumed), 'fresh resume')}, {amount(sum(r['tokens'] for r in resumed))} tokens"
            )
    if jobs:
        quests = sorted({j["quest"] for j in jobs})
        labels = {q: "(no quest)" if q == "-" else q for q in quests}
        width = max(len("jobs by quest"), *map(len, labels.values()))
        print("\n" + paint(f"{'jobs by quest':<{width}}  jobs     ran  waited", "bold"))
        for q in quests:
            js = [j for j in jobs if j["quest"] == q]
            print(
                f"{labels[q]:<{width}}  {len(js):>4} {span(sum(j['ran'] for j in js)):>7} {span(sum(j['waited'] for j in js)):>7}"
            )
    for s in u["lead"]:
        print(
            f"\nlead session {s['harness']} {s['session'][:8]}: {amount(s['tokens'])} tokens "
            f"({cached_share([s])} cached input), cost {cost_of([s])} (the whole session, which may include other work)"
        )
    print(
        "\n"
        + paint(
            f"read: {', '.join(USAGE_READERS)}. Tokens are cumulative per request (input, cached reads included, plus "
            "output): a measure of work, not of billing. Cost is in each harness's own unit; Claude Code and "
            "Codex record none (n/a, never 0).",
            "dim",
        )
    )
    return 0
