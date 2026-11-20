"""Where the time went (A2)."""

from __future__ import annotations

from pathlib import Path

from sources import Json
from timeline import World


def run(w: World, out: Path) -> Json:
    return {"jobs": len(w.jobs), "out": str(out)}
