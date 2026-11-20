#!/usr/bin/env python3
"""Replay campaign v2's records: `inventory` (A1), `account` (A2), `simulate` (A3).

Read-only over the job store, state.json and transcripts; writes only under --out.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import account
import calls
import sources as src
import timeline


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="replay", description=__doc__)
    p.add_argument("--out", type=Path, default=src.OUT, help="output and cache directory")
    p.add_argument("--refresh", action="store_true", help="re-read usage and transcripts instead of the cache")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("inventory", help="load every source, write timeline.jsonl and inventory.json")
    sub.add_parser("account", help="where the time went: account.json")
    sub.add_parser("calls", help="every campaign and quiet call in the transcripts, with latency: calls.json")
    a = p.parse_args(argv)
    if a.cmd == "inventory":
        inv = timeline.write(a.out, timeline.load(a.out, a.refresh))
        print(json.dumps(inv, indent=1, default=str))
    elif a.cmd == "account":
        res = account.run(timeline.load(a.out, a.refresh, repos=False), a.out)
        print(json.dumps(res, indent=1, default=str))
    elif a.cmd == "calls":
        sessions = {}
        for name, home in src.campaign_homes().items():
            leads = sorted(src.usage(home, name, a.out).get("lead", []), key=lambda r: -float(r.get("tokens") or 0))
            if leads:
                sessions[name] = leads[0]["session"]
        print(json.dumps(calls.run(sessions, a.out, a.refresh), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
