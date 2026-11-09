"""The tools' command lines: every command explains itself, and `help` shows the same as --help."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import harness  # first: it puts the tools' directory on sys.path
import quiet
import report

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
PLAIN = {k: v for k, v in os.environ.items() if k != "FORCE_COLOR"} | {"NO_COLOR": "1"}


def walk(
    parser: argparse.ArgumentParser, path: tuple[str, ...] = ()
) -> list[tuple[tuple[str, ...], argparse.ArgumentParser]]:
    """Every listed command under parser, with its words."""
    out: list[tuple[tuple[str, ...], argparse.ArgumentParser]] = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            listed = {a.dest for a in action._choices_actions}
            for name, sub in action.choices.items():
                if name in listed:
                    out += [((*path, name), sub), *walk(sub, (*path, name))]
    return out


class CommandLine(unittest.TestCase):
    def tools(self) -> list[tuple[str, argparse.ArgumentParser]]:
        return [
            ("campaign.py", harness.campaign_module().parser()),
            ("quiet.py", quiet.parser()),
            ("report.py", report.parser()),
        ]

    def test_every_command_has_a_summary_and_a_description(self) -> None:
        for script, ap in self.tools():
            for words, p in walk(ap):
                self.assertTrue((p.description or "").strip(), f"{script} {' '.join(words)} has no description")

    def test_help_shows_what_help_flag_shows(self) -> None:
        for script, ap in self.tools():
            for words, _ in walk(ap):
                if words[-1] == "help":
                    continue
                run = [sys.executable, str(SCRIPTS / script)]
                flag = subprocess.run([*run, *words, "--help"], capture_output=True, text=True, env=PLAIN)
                command = subprocess.run([*run, "help", *words], capture_output=True, text=True, env=PLAIN)
                self.assertEqual(flag.returncode, 0, flag.stderr)
                self.assertEqual(command.stdout, flag.stdout, f"{script} help {' '.join(words)}")
            r = subprocess.run(
                [sys.executable, str(SCRIPTS / script), "help", "nope"], capture_output=True, text=True, env=PLAIN
            )
            self.assertEqual(r.returncode, 2)
            self.assertIn("no command 'nope'", r.stderr)
            self.assertIn(
                "commands:",
                subprocess.run([sys.executable, str(SCRIPTS / script), "help"], capture_output=True, text=True).stdout,
            )


class Pills(unittest.TestCase):
    def test_a_hand_set_pill_expires_at_the_next_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            calls = Path(tmp) / "calls"
            fake = Path(tmp) / "cmux"
            fake.write_text(f'#!/bin/sh\nprintf "%s|" "$@" >> {calls}; echo >> {calls}\n')
            fake.chmod(0o755)
            env = {**os.environ, "CMUX_WORKSPACE_ID": "ws", "REPORT_STATE": tmp, "PATH": f"{tmp}:{os.environ['PATH']}"}

            def run(*args: str) -> None:
                subprocess.run([sys.executable, str(SCRIPTS / "report.py"), *args], env=env, check=True)

            run("status", "short", "gone soon", "--ttl", "0")
            run("status", "long", "stays")
            run("set", "1/2", "anything")  # any report command expires what is due
            log = calls.read_text()
            self.assertIn("clear-status|short|", log)
            self.assertNotIn("clear-status|long|", log)
            run("unstatus", "long")
            self.assertEqual(json.loads((Path(tmp) / "ws.json").read_text()), {})


if __name__ == "__main__":
    unittest.main()
