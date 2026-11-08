#!/usr/bin/env python3
"""sync-agents: generate Copilot CLI and Codex agent files from the Claude Code agent types in
agents/, so every harness runs the same roles from one source.

  Copilot  <role>.agent.md: the same body, a Copilot model ID, and the effort as a first line
           (a Copilot agent file cannot pin effort; pass it per launch)
  Codex    <role>.toml: the body as developer_instructions and the effort as
           model_reasoning_effort; no model, so Codex uses the session's

Only files this tool generated are ever replaced or removed.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path

from style import mark, paint

EPILOG = """\
environment:
  AGENTS_SRC            the source agent types (default: this repository's agents/)
  AGENTS_COPILOT        Copilot's agents directory (default ~/.copilot/agents when Copilot is
                        installed; empty skips it)
  AGENTS_CODEX          Codex's agents directory (default ~/.codex/agents when Codex is
                        installed; empty skips it)
  COPILOT_MODEL_<FAMILY>  overrides a model mapping (e.g. COPILOT_MODEL_OPUS=claude-opus-5.5)
"""
MARK = "by agent-campaign sync-agents.py"
# the generated line itself, whole, never a mention of the tool elsewhere in a file
OURS = re.compile(
    r"^(?:<!-- |# )Generated from agents/[\w.-]+\.md by (?:agent-campaign sync-agents\.(?:py|sh)"
    r"|campaign/scripts/sync-agents\.sh); edit the source\.(?: -->)?$|^<!-- by campaign/scripts/sync-agents\.sh -->$",
    re.MULTILINE,
)
MODELS = {"opus": "claude-opus-5.5", "sonnet": "claude-sonnet-5.5", "haiku": "claude-haiku-5.5"}


def field(name: str, text: str) -> str:
    """The value of the first `name: value` line."""
    match = re.search(rf"^{name}: (.*)$", text, re.MULTILINE)
    return match.group(1) if match else ""


def body(text: str) -> str:
    """Everything after the front matter's closing ---, one newline per line."""
    lines: list[str] = []
    fences = 0
    rows = text.split("\n")
    for line in rows[:-1] if rows[-1] == "" else rows:
        if line == "---" and fences < 2:
            fences += 1
        elif fences >= 2:
            lines.append(line + "\n")
    return "".join(lines)


def model_for(family: str) -> str:
    if family not in MODELS:
        return family
    return os.environ.get(f"COPILOT_MODEL_{family.upper()}") or MODELS[family]


def render_copilot(source: Path) -> str:
    text = source.read_text()
    return (
        f"---\nname: {field('name', text)}\ndescription: {field('description', text)}\n"
        f"model: {model_for(field('model', text))}\n---\n\n"
        f"<!-- Generated from agents/{source.name} {MARK}; edit the source. -->\n"
        f"Launch effort: `{field('effort', text)}` (pass it as `reasoning_effort`; a Copilot agent file cannot pin it).\n"
        + body(text)
    )


def render_codex(source: Path) -> str:
    text = source.read_text()
    return (
        f"# Generated from agents/{source.name} {MARK}; edit the source.\n"
        f'name = "{field("name", text)}"\ndescription = """{field("description", text)}"""\n'
        f'model_reasoning_effort = "{field("effort", text)}"\n'
        f'developer_instructions = """\n{body(text).rstrip(chr(10))}\n"""\n'
    )


def ours(path: Path) -> bool:
    try:
        return OURS.search(path.read_text()) is not None
    except (OSError, UnicodeDecodeError):
        return False


def target(name: str, harness: str) -> Path | None:
    """AGENTS_<NAME> when set (empty skips), else the harness's agents directory when it is installed."""
    value = os.environ.get(name)
    if value is not None:
        return Path(value) if value else None
    home = Path.home() / harness
    return home / "agents" if home.is_dir() else None


def main(argv: list[str] | None = None) -> int:
    raw = argparse.RawDescriptionHelpFormatter
    ap = argparse.ArgumentParser(prog="sync-agents", description=__doc__, epilog=EPILOG, formatter_class=raw)
    ap.add_argument("--check", action="store_true", help="exit 1 when a generated file is missing, stale or orphaned")
    ap.add_argument("--remove", action="store_true", help="remove every file this tool generated (uninstall)")
    args = ap.parse_args(argv)
    check = args.check
    src = Path(os.environ.get("AGENTS_SRC") or Path(__file__).resolve().parent.parent / "agents")
    sources = sorted(src.glob("*.md"))
    renderers: list[tuple[Callable[[Path], str], Path | None, str]] = [
        (render_copilot, target("AGENTS_COPILOT", ".copilot"), ".agent.md"),
        (render_codex, target("AGENTS_CODEX", ".codex"), ".toml"),
    ]
    stale, outputs = False, 0
    if args.remove:
        for _, directory, ext in renderers:
            for out in sorted(directory.glob("*" + ext)) if directory and directory.is_dir() else []:
                if ours(out):
                    out.unlink()
                    print(f"removed {out}")
        return 0
    for render, directory, ext in renderers:
        if directory is None:
            continue
        directory.mkdir(parents=True, exist_ok=True)
        for source in sources:
            out, text = directory / (source.stem + ext), render(source)
            outputs += 1
            if check:
                if not out.is_file() or out.read_text() != text:
                    print(f"{mark('❗')}{paint('stale', 'yellow')}: {out}")
                    stale = True
            elif out.exists() and not ours(out):
                print(f"skipped {out}: not generated by this tool", file=sys.stderr)
            else:
                tmp = out.with_name(out.name + ".tmp")
                tmp.write_text(text)
                tmp.replace(out)
        for out in sorted(directory.glob("*" + ext)):
            if not ours(out) or (src / (out.name[: -len(ext)] + ".md")).is_file():
                continue
            if check:
                print(f"{mark('❗')}{paint('orphan', 'yellow')}: {out}")
                stale = True
            else:
                out.unlink()
    if check:
        return 1 if stale else 0
    print(f"{mark('✅')}generated {outputs} agent files from {sum(1 for _ in src.rglob('*.md'))} sources")
    return 0


if __name__ == "__main__":
    sys.exit(main())
