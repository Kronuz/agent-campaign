"""A lead's session title: read from Claude Code's transcript, one line to fix it, the cmux workspace named."""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import re
import stat
import unittest
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from unittest import mock

from harness import SKILL, CampaignCase, campaign_module

SESSION = "11111111-2222-3333-4444-555555555555"


class SessionTitle(CampaignCase):
    def setUp(self) -> None:
        super().setUp()
        self.tool = campaign_module()
        self.title: ModuleType = importlib.import_module(f"{self.tool.__name__}.title")
        self.logs = self.root / "claude"
        self.log = self.logs / "-project" / f"{SESSION}.jsonl"
        self.log.parent.mkdir(parents=True)
        self.calls = self.root / "cmux-calls"
        cmux = self.root / "bin" / "cmux"
        cmux.write_text(f'#!/bin/sh\necho "$@" >> "{self.calls}"\n')
        cmux.chmod(cmux.stat().st_mode | stat.S_IXUSR)

    @contextlib.contextmanager
    def session(self, cwd: Path | None = None, env: dict[str, str] | None = None) -> Iterator[None]:
        """The environment of a Claude Code session in cmux (a key set to "" is removed)."""
        now = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": SESSION, "CMUX_WORKSPACE_ID": "ws"} | (env or {})
        kept = {**self.env, **{k: v for k, v in now.items() if v}}
        for key in [k for k, v in now.items() if not v]:
            kept.pop(key, None)
        before = os.getcwd()
        try:
            os.chdir(cwd or self.home)
            with mock.patch.dict(os.environ, kept, clear=True):
                yield
        finally:
            os.chdir(before)

    def write(self, *records: tuple[str, str]) -> None:
        names = {"custom-title": "customTitle", "ai-title": "aiTitle"}
        lines = [json.dumps({"type": "user", "message": "hi"})]
        lines += [json.dumps({"type": t, names[t]: v, "sessionId": SESSION}) for t, v in records]
        lines.append(json.dumps({"type": "assistant", "message": "ok"}))
        self.log.write_text("\n".join(lines) + "\n")

    def check(self, **env: str) -> str | None:
        with self.session(env=env):
            return self.title.title_check(self.tool.find_campaign(self.home))

    def test_an_auto_title_is_told_the_name_to_type(self) -> None:
        self.write(("ai-title", "Async search engine to replace Xapian"))
        line = self.check()
        self.assertIsNotNone(line)
        assert line is not None
        self.assertIn("auto-titled 'Async search engine to replace Xapian'", line)
        self.assertIn("/rename demo · lead", line)

    def test_a_custom_title_naming_another_campaign_is_told_the_name_to_type(self) -> None:
        self.write(("custom-title", "detent · lead"))
        line = self.check()
        assert line is not None
        self.assertIn("named 'detent · lead'", line)
        self.assertIn("/rename demo · lead", line)

    def test_a_custom_title_naming_the_campaign_is_silent(self) -> None:
        self.write(("custom-title", "demo · lead"))
        self.assertIsNone(self.check())

    def test_the_newest_record_wins(self) -> None:
        self.write(("custom-title", "demo · lead"), ("ai-title", "Something else"))
        self.assertIn("auto-titled", self.check() or "")
        self.write(("ai-title", "Something else"), ("custom-title", "wrong"), ("custom-title", "demo · lead"))
        self.assertIsNone(self.check())

    def test_a_session_without_a_title_record_is_told_too(self) -> None:
        self.write()
        self.assertIn("unnamed", self.check() or "")

    def test_a_title_far_from_the_end_is_found(self) -> None:
        self.write(("custom-title", "demo · lead"))
        with self.log.open("a") as f:
            f.write((json.dumps({"type": "assistant", "message": "x" * 1000}) + "\n") * 600)  # beyond 256 KiB
        self.assertGreater(self.log.stat().st_size, 1 << 19)
        self.assertIsNone(self.check())

    def test_outside_claude_code_or_the_leads_directory_it_says_nothing(self) -> None:
        self.write(("ai-title", "Generated"))
        self.assertIsNone(self.check(CLAUDECODE=""))
        self.assertIsNone(self.check(CLAUDE_CODE_SESSION_ID="no-such-session"))
        worktree = self.root / "elsewhere"
        worktree.mkdir()
        with self.session(cwd=worktree):
            self.assertIsNone(self.title.title_check(self.tool.find_campaign(self.home)))

    def test_without_a_session_id_the_newest_transcript_of_the_directory_is_read(self) -> None:
        folder = self.logs / re.sub(r"[/.]", "-", str(self.home.resolve()))
        folder.mkdir(parents=True)
        (folder / "old.jsonl").write_text(json.dumps({"type": "custom-title", "customTitle": "demo · lead"}) + "\n")
        os.utime(folder / "old.jsonl", (1, 1))
        (folder / "new.jsonl").write_text(json.dumps({"type": "ai-title", "aiTitle": "Generated"}) + "\n")
        self.assertIn("auto-titled 'Generated'", self.check(CLAUDE_CODE_SESSION_ID="") or "")

    def test_the_cmux_workspace_is_named_for_the_lead(self) -> None:
        self.write(("custom-title", "demo · lead"))
        with self.session():
            line = self.title.keep_title(self.tool.find_campaign(self.home))
        self.assertIsNone(line)
        self.assertEqual(self.calls.read_text().strip(), "workspace-action --action rename --title demo · lead")

    def test_cmux_is_left_alone_outside_cmux_and_outside_the_leads_directory(self) -> None:
        self.write(("ai-title", "Generated"))
        with self.session(env={"CMUX_WORKSPACE_ID": ""}):
            self.assertIn("/rename", self.title.keep_title(self.tool.find_campaign(self.home)) or "")
        worktree = self.root / "elsewhere"
        worktree.mkdir()
        with self.session(cwd=worktree):
            self.title.keep_title(self.tool.find_campaign(self.home))
        self.assertFalse(self.calls.exists())

    def test_a_failing_cmux_is_reported_not_raised(self) -> None:
        (self.root / "bin" / "cmux").write_text("#!/bin/sh\necho boom >&2\nexit 3\n")
        with self.session():
            why = self.title.set_workspace_title(self.tool.find_campaign(self.home))
        self.assertEqual(why, "cmux failed: boom")

    def test_the_cmux_call_is_not_repeated_once_the_title_is_set(self) -> None:
        self.write(("custom-title", "demo · lead"))
        with self.session():
            for _ in range(3):
                self.title.keep_title(self.tool.find_campaign(self.home))
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)

    def test_a_failure_inside_the_title_never_fails_the_caller(self) -> None:
        with self.session(), mock.patch.object(self.title, "title_check", side_effect=RuntimeError("boom")):
            self.assertIsNone(self.title.keep_title(self.tool.find_campaign(self.home)))

    def test_brief_status_and_wait_print_the_line(self) -> None:
        self.write(("ai-title", "Generated"))
        self.env.update(CLAUDECODE="1", CLAUDE_CODE_SESSION_ID=SESSION, CAMPAIGN_CLAUDE_LOGS=str(self.logs))
        for args in (("brief",), ("status",), ("wait", "--timeout", "1")):
            with self.subTest(args=args):
                out = self.run_cli(self.home, *args)
                self.assertIn("type /rename demo · lead", out.out, out.err)
        self.env["CLAUDE_CODE_SESSION_ID"] = "no-such-session"
        self.assertNotIn("/rename", self.run_cli(self.home, "status").out)


class Documented(unittest.TestCase):
    def read(self, rel: str) -> str:
        return (SKILL / rel).read_text()

    def test_the_skill_gives_the_launch_that_names_the_session(self) -> None:
        text = " ".join(self.read("SKILL.md").split())
        self.assertIn("claude -n '<campaign> · lead'", text)
        self.assertIn("cmux new-workspace --name '<campaign> · lead'", text)

    def test_each_harness_has_its_name_mechanism_or_what_to_look_for(self) -> None:
        rows = [
            r for r in self.read("references/harness.md").splitlines() if r.startswith("| The owner sees the campaign")
        ]
        self.assertEqual(len(rows), 1)
        _, claude, copilot, codex = (cell.strip() for cell in rows[0].strip("|").split(" | ", 3))
        self.assertIn("claude -n", claude)
        self.assertIn("/rename", claude)
        self.assertIn("copilot --name", copilot)
        self.assertIn("/rename [NAME]", copilot)
        self.assertIn("/rename <name>", codex)
        self.assertIn("session_index.jsonl", codex)  # where an unbuilt check would look
        self.assertNotIn("| unknown |", rows[0])

    def test_the_agent_definitions_say_it_in_one_line_each(self) -> None:
        for name, role in (("contributor", "contributor <quest>"), ("reviewer", "reviewer <quest> <lens>")):
            lines = " ".join(self.read(f"agents/campaign-{name}.md").split("\n- "))
            self.assertEqual(lines.count(f"is titled `<campaign> · {role}`"), 1, name)


if __name__ == "__main__":
    unittest.main()
