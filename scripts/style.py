"""Color and emoji for a terminal, plain words for pipes and logs: agents read the tools through
pipes, so what they see is unchanged. NO_COLOR turns both off, FORCE_COLOR on. Emojis come from a
small set that terminals draw two cells wide, so columns stay aligned."""

from __future__ import annotations

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
