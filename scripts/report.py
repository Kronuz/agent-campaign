#!/usr/bin/env python3
"""report: report long-running work where the owner will see it.

Inside cmux ($CMUX_WORKSPACE_ID set) everything goes to the workspace's sidebar and notification
feed. Elsewhere `set` and `clear` draw an OSC 9;4 terminal progress bar when stderr is a terminal,
else print "progress: 60% phase 2 gates" on stderr; `notify` uses the desktop (osascript on macOS,
notify-send on Linux) or stderr; status pills are skipped. Every command with valid arguments
exits 0, so a failing display never fails the work it reports on. `campaign` and `quiet` import
this module rather than running it.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import subprocess
import sys


def cmux(args: list[str], workspace: str | None = None) -> bool:
    """Runs a cmux command in the workspace (default: $CMUX_WORKSPACE_ID); False outside cmux."""
    ws = os.environ.get("CMUX_WORKSPACE_ID", "") if workspace is None else workspace
    exe = shutil.which("cmux") if ws else None
    if not exe:
        return False
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        env = {**os.environ, "CMUX_WORKSPACE_ID": ws}
        subprocess.run([exe, *args], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    return True


def osc(state: str) -> bool:
    """A terminal progress bar (Ghostty, WezTerm, Windows Terminal, iTerm2); False without a terminal."""
    if not sys.stderr.isatty():
        return False
    sys.stderr.write(f"\033]9;4;{state}\033\\")
    sys.stderr.flush()
    return True


def fraction(text: str) -> float:
    """N/M, a percentage (60%) or a fraction (0.6), as a fraction."""
    try:
        if "/" in text:
            n, m = (float(x) for x in text.split("/", 1))
            return n / m if m > 0 else 0.0
        if text.endswith("%"):
            return float(text[:-1]) / 100
        return float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected N/M, PCT% or FRACTION, not '{text}'") from None


def progress(value: float, label: str = "") -> None:
    if cmux(["set-progress", f"{value:.4f}", *(["--label", label] if label else [])]):
        return
    pct = int(value * 100 + 0.5)
    if not osc(f"1;{pct}"):
        print(f"progress: {pct}%{' ' + label if label else ''}", file=sys.stderr)


def clear() -> None:
    if not cmux(["clear-progress"]):
        osc("0")


def status(key: str, value: str, icon: str = "", workspace: str | None = None) -> None:
    cmux(["set-status", key, value, *(["--icon", icon] if icon else [])], workspace)


def unstatus(key: str, workspace: str | None = None) -> None:
    cmux(["clear-status", key], workspace)


def notify(title: str, body: str = "") -> None:
    if cmux(["notify", "--title", title, *(["--body", body] if body else [])]):
        return
    script = "on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run"
    argv = (
        ["osascript", "-", title, body]
        if sys.platform == "darwin"
        else ["notify-send", title, body]
        if shutil.which("notify-send")
        else []
    )
    if argv:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                argv, input=script if argv[0] == "osascript" else None, text=True, capture_output=True, timeout=10
            )
            return
    print(f"notify: {title}{': ' + body if body else ''}", file=sys.stderr)


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="report", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("set", help='show a progress bar (e.g. 3/5 "phase 2 gates")')
    p.add_argument("value", type=fraction, metavar="N/M|PCT%|FRACTION")
    p.add_argument("label", nargs="?", default="")
    p = sub.add_parser("clear", help="remove the progress bar")
    p = sub.add_parser("status", help="set a sidebar pill (cmux only; e.g. icon hammer)")
    p.add_argument("key")
    p.add_argument("value")
    p.add_argument("icon", nargs="?", default="")
    p = sub.add_parser("unstatus", help="remove a sidebar pill")
    p.add_argument("key")
    p = sub.add_parser("notify", help="send a notification")
    p.add_argument("title")
    p.add_argument("body", nargs="?", default="")
    return ap


def main(argv: list[str] | None = None) -> int:
    a = parser().parse_args(argv)
    if a.command == "set":
        progress(a.value, a.label)
    elif a.command == "clear":
        clear()
    elif a.command == "status":
        status(a.key, a.value, a.icon)
    elif a.command == "unstatus":
        unstatus(a.key)
    else:
        notify(a.title, a.body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
