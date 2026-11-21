"""Usage: what the sessions and jobs of a campaign cost."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness import CampaignCase


def at(seconds: float) -> str:
    """An ISO timestamp `seconds` after now, as Claude Code writes them."""
    return datetime.fromtimestamp(time.time() + 600 + seconds, timezone.utc).isoformat().replace("+00:00", "Z")


class Usage(CampaignCase):
    def codex_log(self, fixture: str, name: str) -> Path:
        source = Path(__file__).parent / "fixtures" / f"codex-{fixture}.jsonl"
        log = self.root / "codex/2026/11/05" / f"rollout-{name}.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(source.read_text().replace("HOME", str(self.home)) + "not json\n")
        return log

    def test_codex_real_shapes_attribute_tokens_without_counting_snapshots_twice(self) -> None:
        self.codex_log("lead", "lead")
        self.codex_log("agent", "agent")
        u = json.loads(self.ok(self.home, "usage", "--json").out)
        (run,) = u["runs"]
        self.assertEqual((run["role"], run["assignment"], run["quest"]), ("contributor", "performance", "fix"))
        self.assertEqual(
            (run["model"], run["effort"], run["tokens"], run["seconds"]), ("gpt-6-astra", "high", 1200, 120)
        )
        self.assertEqual((run["cached_input_tokens"], run["output_tokens"], run["cost"]), (800, 200, 0))
        (lead,) = u["lead"]
        self.assertEqual(
            (lead["tokens"], lead["seconds"], lead["model"], lead["effort"]), (550, 180, "gpt-6.1-sol", "medium")
        )
        out = self.ok(self.home, "usage").out
        self.assertIn("Claude Code and Codex record none", out)
        self.assertIn("cost n/a;", out)  # unavailable, never reported as zero
        self.assertNotIn("cost 0", out)

    def test_codex_encrypted_launch_is_unattributed_and_unrelated_leads_are_excluded(self) -> None:
        self.codex_log("agent", "opaque")
        other = self.codex_log("lead", "other")
        events = []
        for line in other.read_text().splitlines():
            if not line.startswith("{"):
                continue
            event = json.loads(line)
            payload = event["payload"]
            if payload.get("type") == "function_call":
                arguments = json.loads(payload["arguments"])
                arguments["message"] = "SCRUBBED_ENCRYPTED_PROMPT"
                payload["arguments"] = json.dumps(arguments)
            events.append(event)
        other.write_text(
            "".join(json.dumps(e) + "\n" for e in events).replace(str(self.home), str(self.root / "elsewhere"))
        )
        u = json.loads(self.ok(self.home, "usage", "--json").out)
        (run,) = u["runs"]
        self.assertEqual((run["role"], run["quest"], run["tokens"]), ("unattributed", "-", 1200))
        # This parent is related by thread id even though its cwd differs.
        self.assertEqual(len(u["lead"]), 1)
        other.write_text(other.read_text().replace('"lead"', '"unrelated"'))
        self.assertEqual(json.loads(self.ok(self.home, "usage", "--json").out)["lead"], [])

    def test_codex_runs_are_attributed_by_task_name_when_the_prompt_is_encrypted(self) -> None:
        self.ok(self.home, "new", "fix", "--title", "fix", "--risk", "low")
        names = (
            "contributor_fix_performance",
            "reviewer_fix_security",
            "fix_security",
            "night_fix_v2",
            "night_security",
        )
        names += ("fixer", "builder")
        for task in names:
            log = self.codex_log("agent", task)  # no parent log: the launch prompt is unreadable
            log.write_text(log.read_text().replace("/root/builder", f"/root/{task}").replace('"builder"', f'"{task}"'))
        u = json.loads(self.ok(self.home, "usage", "--json").out)
        got = {r["task"]: (r["role"], r["quest"], r["assignment"], r["lens"]) for r in u["runs"]}
        self.assertEqual(
            got,
            {
                "contributor_fix_performance": ("contributor", "fix", "performance", ""),
                "reviewer_fix_security": ("reviewer", "fix", "", "security"),
                "fix_security": ("reviewer", "fix", "", "security"),  # a lens marks a reviewer
                "night_fix_v2": ("contributor", "fix", "implementation", ""),  # the quest's id as a whole word
                "night_security": ("reviewer", "-", "", "security"),  # a role, but no quest named
                "fixer": ("unattributed", "-", "", ""),  # part of a word is not guessed at
                "builder": ("unattributed", "-", "", ""),
            },
        )
        out = self.ok(self.home, "usage").out
        self.assertIn("(unattributed)", out)
        self.assertFalse([x for x in out.splitlines() if x.startswith("- ")], out)  # one label for no quest
        self.assertIn("unattributed builder gpt-6-astra high", out)

    def test_codex_legacy_cumulative_snapshots_and_old_logs(self) -> None:
        log = self.codex_log("agent", "fallback")
        lines = [line for line in log.read_text().splitlines() if '"token_usage_record"' not in line]
        log.write_text("\n".join(lines) + "\n{truncated")
        (run,) = json.loads(self.ok(self.home, "usage", "--json").out)["runs"]
        self.assertEqual(run["tokens"], 1200)
        os.utime(log, (0, 0))
        self.assertEqual(json.loads(self.ok(self.home, "usage", "--json").out)["runs"], [])

    def test_codex_model_effort_changes_keep_separate_usage_and_active_time(self) -> None:
        log = self.codex_log("agent", "models")
        events = [json.loads(line) for line in log.read_text().splitlines() if line.startswith("{")]
        events += [
            {
                "type": "event_msg",
                "timestamp": "2026-11-05T12:00:00Z",
                "payload": {"type": "task_started", "turn_id": "next"},
            },
            {"type": "turn_context", "payload": {"turn_id": "next", "model": "gpt-6.1-sol", "effort": "medium"}},
            {
                "type": "token_usage_record",
                "timestamp": "2026-11-05T12:01:00Z",
                "payload": {
                    "turn_id": "next",
                    "response_id": "resp-next",
                    "usage": {"input_tokens": 20, "output_tokens": 5},
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-11-05T12:01:00Z",
                "payload": {"type": "task_complete", "turn_id": "next"},
            },
        ]
        log.write_text("".join(json.dumps(e) + "\n" for e in events))
        runs = json.loads(self.ok(self.home, "usage", "--json").out)["runs"]
        self.assertEqual(
            sorted((r["model"], r["tokens"], r["seconds"]) for r in runs),
            [("gpt-6-astra", 1200, 120), ("gpt-6.1-sol", 25, 60)],
        )

    def copilot_session(self, name: str, launches: list[tuple[str, str, float]], lead_cost: float) -> Path:
        """A Copilot session log in its real event shapes: (prompt, agent id, cost) per launch."""
        events: list[Any] = []
        for n, (prompt, aid, cost) in enumerate(launches):
            call = f"call-{aid}"
            events += [
                {
                    "type": "tool.execution_start",
                    "data": {"toolName": "task", "toolCallId": call, "arguments": {"prompt": prompt}},
                },
                {
                    "type": "subagent.started",
                    "agentId": aid,
                    "timestamp": f"2026-11-05T10:0{n}:00Z",
                    "data": {"toolCallId": call, "agentName": "general-purpose", "model": "claude-opus-5.5"},
                },
                {
                    "type": "subagent.configured",
                    "agentId": aid,
                    "data": {"model": "claude-opus-5.5", "reasoningEffort": "high"},
                },
                {
                    "type": "session.usage_record",
                    "agentId": aid,
                    "data": {
                        "usage": {
                            "cost": cost,
                            "inputTokens": 1000,
                            "outputTokens": 200,
                            "cacheReadTokens": 900,
                            "reasoningTokens": 50,
                        }
                    },
                },
                {
                    "type": "subagent.completed",
                    "agentId": aid,
                    "data": {"toolCallId": call, "durationMs": 120000, "totalToolCalls": 7},
                },
            ]
        events.append(
            {
                "type": "session.usage_record",
                "data": {"usage": {"cost": lead_cost, "inputTokens": 500, "outputTokens": 50}},
            }
        )
        log = self.root / "copilot" / name / "events.jsonl"
        log.parent.mkdir(parents=True)
        log.write_text("".join(json.dumps(e) + "\n" for e in events) + "not json\n")
        return log

    def test_usage_attributes_agent_runs_and_jobs_to_quests_roles_and_classes(self) -> None:
        self.env["CAMPAIGN_COPILOT_LOGS"] = str(self.root / "copilot")
        self.started("fix", files="app.py")
        jid = self.submit(self.home, "--quest", "fix", "--level", "none", "--", "sleep 1")
        self.ok(self.home, "wait", jid, "--timeout", "60")
        home = str(self.home)
        self.copilot_session(
            "s1",
            [
                (
                    f"---\nname: campaign-contributor\n---\nYou are the contributor for quest fix of campaign demo ({home}).\nAssignment: performance; for any",
                    "a1",
                    10,
                ),
                (f"You resume quest fix of campaign demo ({home}) after the previous contributor stopped.", "a2", 4),
                (f"You are the correctness reviewer for quest fix of campaign demo ({home}). You have not", "p1", 6),
                ("You are researching orchestration systems for a campaign skill.", "x1", 99),
                ("You are the contributor for quest fix of campaign other (/elsewhere).", "o1", 50),
            ],
            lead_cost=5,
        )
        self.copilot_session("unrelated", [("Explore the repository.", "u1", 70)], lead_cost=77)
        old = self.copilot_session(
            "before-init", [(f"You are the contributor for quest fix of campaign demo ({home}).", "z1", 33)], 0
        )
        os.utime(old, (0, 0))
        u = json.loads(self.ok(self.home, "usage", "--json").out)
        runs = sorted((r["role"], r["lens"], r["assignment"], r["quest"], r["cost"]) for r in u["runs"])
        self.assertEqual(
            runs,
            [
                ("contributor", "", "implementation", "fix", 4),
                ("contributor", "", "performance", "fix", 10),
                ("reviewer", "correctness", "", "fix", 6),
            ],
        )
        first = next(r for r in u["runs"] if r["agent"] == "a1")
        self.assertEqual(
            (first["effort"], first["tokens"], first["seconds"], first["tool_calls"]), ("high", 1200, 120, 7)
        )
        self.assertEqual([(s["session"], s["cost"]) for s in u["lead"]], [("s1", 5)])
        (job,) = u["jobs"]
        self.assertEqual((job["id"], job["quest"], job["state"]), (jid, "fix", "done"))
        self.assertGreaterEqual(job["ran"], 1)
        self.assertEqual(
            (first["cached_input_tokens"], first["output_tokens"], first["reasoning_tokens"]), (900, 200, 50)
        )
        self.assertEqual([r["agent"] for r in u["runs"] if r["resume"]], ["a2"])
        text = self.ok(self.home, "usage").out
        self.assertIn("recovery: 1 fresh resume, 1,200 tokens", text)
        self.assertIn("contributor performance claude-opus-5.5 high", text)
        self.assertIn("reviewer correctness claude-opus-5.5 high", text)

    def claude_agent(self, session: Path, aid: str, prompt: str, usages: list[tuple[str, int, int, int]]) -> Path:
        """A Claude Code subagent transcript in its real shapes: (message id, cache read, cache write,
        output) per logged block; blocks of one response repeat its message id."""
        lines: list[Any] = [
            {
                "type": "user",
                "agentId": aid,
                "timestamp": at(0),
                "message": {"role": "user", "content": prompt},
            },
            {
                "type": "api-request",
                "timestamp": at(1),
                "params": {"output_config": {"effort": "high"}},
            },
        ]
        for n, (mid, read, write, out) in enumerate(usages):
            usage = {
                "input_tokens": 2,
                "cache_read_input_tokens": read,
                "cache_creation_input_tokens": write,
                "output_tokens": out,
                "output_tokens_details": {"thinking_tokens": 1},
            }
            block = {"type": "tool_use", "name": "Bash"} if n % 2 else {"type": "text", "text": "ok"}
            lines.append(
                {
                    "type": "assistant",
                    "timestamp": at(60 * (n + 1)),
                    "message": {"id": mid, "model": "claude-opus-5-5", "usage": usage, "content": [block]},
                }
            )
        log = session / "subagents" / f"agent-{aid}.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("".join(json.dumps(x) + "\n" for x in lines) + "not json\n")
        log.with_suffix(".meta.json").write_text(json.dumps({"agentType": "campaign-contributor", "model": "opus"}))
        return log

    def test_usage_reads_claude_code_agents_once_per_response(self) -> None:
        self.started("fix", files="app.py")
        home = str(self.home)
        project = self.root / "claude" / "-app"
        project.mkdir(parents=True)
        lead = project / "s1.jsonl"
        lead.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "cwd": home,
                    "timestamp": at(0),
                    "message": {
                        "id": "m0",
                        "model": "claude-opus-5-5",
                        "usage": {"input_tokens": 7, "output_tokens": 3},
                    },
                }
            )
            + "\n"
        )
        prompt = f"You are the contributor for quest fix of campaign demo ({home}).\nAssignment: performance;"
        # m1 is logged twice (two content blocks of one response): counted once.
        self.claude_agent(project / "s1", "a1", prompt, [("m1", 100, 10, 5), ("m1", 100, 10, 5), ("m2", 200, 0, 7)])
        self.claude_agent(
            project / "s1",
            "o1",
            "You are the contributor for quest fix of campaign other (/elsewhere).",
            [("m3", 999, 0, 1)],
        )
        old = self.claude_agent(project / "s1", "z1", prompt, [("m4", 50, 0, 1)])
        os.utime(old, (0, 0))
        u = json.loads(self.ok(self.home, "usage", "--json").out)
        (run,) = u["runs"]
        self.assertEqual(
            (run["harness"], run["agent"], run["role"], run["assignment"], run["quest"], run["model"], run["effort"]),
            ("claude", "a1", "contributor", "performance", "fix", "claude-opus-5-5", "high"),
        )
        self.assertEqual(
            (
                run["tokens"],
                run["cached_input_tokens"],
                run["cache_write_tokens"],
                run["output_tokens"],
                run["requests"],
            ),
            (2 + 100 + 10 + 5 + 2 + 200 + 7, 300, 10, 12, 2),
        )
        self.assertEqual((round(run["seconds"]), run["tool_calls"], run["cost_available"]), (180, 1, False))
        self.assertEqual([(s["harness"], s["session"], s["tokens"]) for s in u["lead"]], [("claude", "s1", 10)])
        self.assertIn("read: claude, copilot, codex", self.ok(self.home, "usage").out)
