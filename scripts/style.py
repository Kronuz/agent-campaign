"""How the tools present themselves: color and emoji on a terminal, plain words in pipes and logs
(agents read the tools through pipes, so what they see is unchanged), and a `help` command.
NO_COLOR turns color and emoji off, FORCE_COLOR on. Emojis come from a small set that terminals draw
two cells wide, so columns stay aligned."""

from __future__ import annotations

import argparse
import os
import sys

STYLES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "magenta": "35", "cyan": "36"}


def styled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return sys.stdout.isatty() and os.environ.get("TERM") != "dumb"


def paint(text: str, *styles: str) -> str:
    if not styles or not styled():
        return text
    return f"\033[{';'.join(STYLES[x] for x in styles)}m{text}\033[0m"


def mark(emoji: str) -> str:
    """The emoji and a space on a terminal; nothing elsewhere."""
    return f"{emoji} " if styled() else ""


def commands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    """A parser's subcommands by name."""
    for action in parser._actions:  # argparse keeps subcommands only here
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def add_help(sub: argparse._SubParsersAction[argparse.ArgumentParser], tool: str) -> None:
    """`TOOL help [COMMAND...]`: the same as `TOOL [COMMAND...] --help`."""
    p = sub.add_parser(
        "help",
        help="this help, or a command's",
        description=f"Prints {tool}'s help, or a command's: `{tool} help COMMAND` is `{tool} COMMAND --help`.",
    )
    p.add_argument("topic", nargs="*", metavar="COMMAND")


def show_help(parser: argparse.ArgumentParser, topic: list[str]) -> int:
    for word in topic:
        found = commands(parser)
        if word not in found:
            parser.error(f"no command {word!r}; `{parser.prog} --help` lists them")
        parser = found[word]
    parser.print_help()
    return 0
