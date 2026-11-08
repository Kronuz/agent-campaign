#!/usr/bin/env python3
"""context-size: how much of the skill each agent loads, so its context cost is a number and not a
guess. It reports the files by group (the core SKILL.md, references, agent types, templates) and
what each launch reads: the skill and agent descriptions every session lists, the lead's SKILL.md,
and each role's agent file, prompt and the references its prompt sends it to.

Sizes are reports, never a gate. --check prints one line of the main loads and warns when a load
grew more than 10% (and 150 words) past the recorded baseline; a deliberate change records the new
sizes with --update and says why in its commit message. tests/run.py runs --check after the suite.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from style import mark, paint

SKILL = Path(__file__).resolve().parent.parent
BASELINE = SKILL / "tests" / "context-baseline.json"
GROUPS = {
    "core": ["SKILL.md"],
    "references": ["references/**/*.md"],
    "agents": ["agents/*.md"],
    "templates": ["templates/*.md"],
}
# What a launch reads: a file, a file's front-matter description ("path:description"), or a
# prompt from references/prompts.md ("prompts.md#Section"). References read on demand (review,
# measurement, host, harness, lessons) are not counted.
LOADS = {
    "every session (skill and agent lists)": ["SKILL.md:description", "agents/*.md:description"],
    "lead": ["SKILL.md"],
    "lead, launching": ["references/prompts.md"],
    "contributor": ["agents/campaign-contributor.md", "prompts.md#Contributor", "templates/journal.md"],
    "contributor, on a design": [
        "agents/campaign-contributor.md",
        "prompts.md#Contributor",
        "templates/journal.md",
        "references/assignments/design.md",
    ],
    "contributor, resumed": ["agents/campaign-contributor.md", "prompts.md#Resume", "prompts.md#Contributor"],
    "contributor, exploring": [
        "agents/campaign-contributor.md",
        "prompts.md#Exploration",
        "references/assignments/exploration.md",
        "references/measurement.md",
    ],
    "reviewer": [
        "agents/campaign-reviewer.md",
        "prompts.md#Reviewer",
        "references/review.md",
        "templates/review.md",
    ],
}
GROWTH, FLOOR = 0.10, 150  # warn above 10% and 150 words


def front(text: str) -> tuple[str, str]:
    """A file's front matter and its body."""
    if text.startswith("---\n"):
        end = text.index("\n---\n", 4)
        return text[4:end], text[end + 5 :]
    return "", text


def piece(item: str) -> list[tuple[str, str]]:
    """The (label, text) pieces an item names; raises if one is missing, so the report can't drift."""
    if "#" in item:
        name, section = item.split("#", 1)
        text = (SKILL / "references" / name).read_text()
        found = re.search(rf"^## {re.escape(section)}\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
        if not found:
            raise LookupError(f"no section {section!r} in references/{name}")
        return [(item, found.group(1))]
    path, _, part = item.partition(":")
    files = sorted(SKILL.glob(path))
    if not files:
        raise LookupError(f"no file {path}")
    out = []
    for f in files:
        meta, body = front(f.read_text())
        if part == "description":
            found = re.search(r"^description: (.*)$", meta, re.MULTILINE)
            if not found:
                raise LookupError(f"no description in {f.relative_to(SKILL)}")
            out.append((f"{f.relative_to(SKILL)}:description", found.group(1)))
        else:
            out.append((str(f.relative_to(SKILL)), meta + body if f.name == "SKILL.md" else body))
    return out


def words(text: str) -> int:
    return len(text.split())


def tokens(text: str) -> int:
    """An estimate: about four characters per token for English prose and markdown."""
    return round(len(text) / 4)


def measure() -> dict[str, dict[str, int]]:
    sizes: dict[str, dict[str, int]] = {}
    for name, items in {**{f"group: {g}": v for g, v in GROUPS.items()}, **LOADS}.items():
        texts = [t for item in items for _, t in piece(item)]
        sizes[name] = {"words": sum(map(words, texts)), "tokens": sum(map(tokens, texts))}
    return sizes


def growth(now: dict[str, dict[str, int]], then: dict[str, dict[str, int]]) -> list[str]:
    warnings = []
    for name, size in now.items():
        old = then.get(name, {}).get("words")
        if old and size["words"] - old > max(FLOOR, GROWTH * old):
            warnings.append(f"{name} grew {size['words'] - old:+,} words ({(size['words'] - old) / old:+.0%})")
    return warnings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="context-size", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--check", action="store_true", help="one line of the main loads, and growth warnings")
    ap.add_argument("--update", action="store_true", help="record the current sizes as the baseline")
    ap.add_argument("--json", action="store_true", help="print the sizes as JSON")
    a = ap.parse_args(argv)
    now = measure()
    if a.update:
        BASELINE.write_text(json.dumps(now, indent=2) + "\n")
    then = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    if a.json:
        print(json.dumps(now, indent=2))
        return 0
    if a.check:
        main_loads = ("lead", "contributor", "reviewer", "every session (skill and agent lists)")
        print("context (words): " + " · ".join(f"{n.split(' (')[0]} {now[n]['words']:,}" for n in main_loads))
    else:
        print(paint("Context per load (words, ~tokens at 4 characters each; baseline in parentheses)", "bold"))
        for name, size in now.items():
            old = then.get(name, {}).get("words")
            was = f" ({old:,})" if old is not None and old != size["words"] else ""
            print(f"  {name:40} {size['words']:7,} {size['tokens']:7,}{was}")
    for warning in growth(now, then):
        print(f"{mark('⚠️')}{warning}: intended? record it with context-size --update", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
