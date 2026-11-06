"""The instructions agents read stay within their word budget."""

from __future__ import annotations

import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
AGENTS = SKILL / "agents"

# A change to the skill replaces text, or raises this with evidence in its commit message
# (`campaign usage`, a live agent test, a lore incident). Wording fixes fit in the slack.
BUDGET = 10_900


class Budget(unittest.TestCase):
    def test_instructions_stay_within_their_word_budget(self) -> None:
        files = [SKILL / "SKILL.md", *sorted((SKILL / "references").glob("*.md")), *sorted(AGENTS.glob("*.md"))]
        words = sum(len(f.read_text().split()) for f in files)
        self.assertLessEqual(words, BUDGET, f"instructions grew to {words} words (budget {BUDGET}): cut, or justify")
