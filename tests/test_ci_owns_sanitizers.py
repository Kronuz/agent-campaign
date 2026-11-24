"""A local gate check that builds a sanitizer CI builds is flagged by check and refused by new and set."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from harness import SKILL, CampaignCase, campaign_module

ASAN_CI = """\
name: CI
on: [push]
jobs:
  asan:
    name: Clang ASan
    runs-on: ubuntu-24.04
    steps:
      - name: Build
        run: cmake -B build -DCMAKE_CXX_FLAGS=-fsanitize=address && cmake --build build
"""
PLAIN_CI = """\
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-24.04
    steps:
      - run: python3 app.py
"""
LOCAL = "mem=cc -fsanitize=address -o t t.c && ./t"  # no asan word in it: placed() keeps it a gate
SAID = "gate check mem builds asan locally, which CI builds (ci.yml job asan): `--check 'mem=ci:Clang ASan'`"


class CiOwnsSanitizers(CampaignCase):
    def workflow(self, text: str) -> None:
        self.commit(self.home, {".github/workflows/ci.yml": text})

    def test_a_local_asan_gate_ci_builds_is_warned_by_check_and_refused_by_set_and_new(self) -> None:
        self.workflow(ASAN_CI)
        self.started("fix", files="app.py")
        refused = self.fails(self.home, "set", "fix", "--check", "unit=python3 app.py", "--check", LOCAL)
        self.assertIn(f"fix: {SAID}; --weaken REASON keeps it local", refused.err)
        self.assertEqual([x["name"] for x in self.quest("fix")["checks"]], ["unit"])
        self.fails(self.home, "new", "two", "--title", "two", "--check", LOCAL, match=SAID)
        self.assertNotIn("two", self.state()["quests"])
        self.ok(self.home, "set", "fix", "--check", "unit=python3 app.py", "--check", LOCAL, "--weaken", "kept local")
        self.assertEqual(self.quest("fix")["history"][-1]["reason"], "kept local")
        self.assertIn(f"fix: {SAID}", self.ok(self.home, "check").text)
        self.ok(self.home, "set", "fix", "--check", "unit=python3 app.py", "--check", LOCAL, "--check", "lint=true")

    def test_a_sanitizer_ci_does_not_build_is_accepted_silently_and_a_ci_check_is_never_flagged(self) -> None:
        self.workflow(PLAIN_CI)
        self.started("fix", files="app.py", check=LOCAL)
        self.ok(self.home, "set", "fix", "--check", "unit=python3 app.py", "--check", "thr=cc -fsanitize=thread t.c")
        self.assertNotIn("locally", self.ok(self.home, "check").text)
        self.workflow(ASAN_CI)
        self.ok(self.home, "new", "two", "--title", "two", "--check", "mem=ci:Clang ASan -fsanitize=address")
        self.ok(self.home, "set", "two", "--check", "san=ci:Clang ASan")
        self.assertNotIn("two: gate check", self.ok(self.home, "check").text)


DETENT_CI = """\
jobs:
  ci:
    uses: Kronuz/cpp-libraries/.github/workflows/cmake.yml@main
    with:
      # Clang ASan/UBSan with LeakSanitizer, and Clang ThreadSanitizer.
      asan_cxx_flags: -O1
      tsan: true
      msan: false
  docs:
    name: Docs ${{ matrix.os }}
    strategy:
      matrix:
        sanitizer: [memory]
    steps:
      - name: Notes  # a step's name is not the job's
        run: echo
"""


class Workflows(unittest.TestCase):
    """The workflow reader on the shapes CI files take: a reusable workflow's inputs, comments,
    switched-off keys, a matrix of sanitizers, and a name GitHub computes."""

    def setUp(self) -> None:
        self.tool = campaign_module()
        self.repo = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.repo, True)
        (self.repo / ".github" / "workflows").mkdir(parents=True)

    def test_a_reusable_workflow_s_inputs_name_its_sanitizers_and_comments_do_not(self) -> None:
        (self.repo / ".github" / "workflows" / "ci.yml").write_text(DETENT_CI)
        reusable = "ci.yml job ci, a reusable workflow: NAME is its job's"
        self.assertEqual(
            self.tool.ci_sanitizers(self.repo),
            {"asan": (reusable, "NAME"), "tsan": (reusable, "NAME"), "msan": ("ci.yml job docs", "docs")},
        )
        tsan = {"name": "t3-tsan", "cmd": 'cmake -DCMAKE_CXX_FLAGS="-fsanitize=thread -O1" -B b && ctest'}
        ubsan = {"name": "ub", "cmd": "cmake -B .scratch/build-ubsan && ctest"}
        self.assertEqual(
            self.tool.ci_owned([tsan, ubsan, {**tsan, "name": "late", "later": True}], self.repo),
            [f"gate check t3-tsan builds tsan locally, which CI builds ({reusable}): `--check 't3-tsan=ci:NAME'`"],
        )

    def test_a_command_s_sanitizers_are_its_flags_and_words(self) -> None:
        cases = {
            "-fsanitize=address,undefined -fno-sanitize-recover=undefined": {"asan", "ubsan"},
            "-fsanitize-recover=address -fsanitize=fuzzer": set(),
            "-DCMAKE_BUILD_TYPE=ASan .scratch/build-tsan MSAN_OPTIONS=1": {"asan", "tsan", "msan"},
            "Clang ThreadSanitizer, a pleasant basant": {"tsan"},
        }
        for text, found in cases.items():
            self.assertEqual(self.tool.sanitizers(text), found, text)


class Skill(unittest.TestCase):
    def test_the_gate_paragraph_gives_the_suite_and_sanitizers_to_ci_in_its_four_lines(self) -> None:
        text = (SKILL / "SKILL.md").read_text()
        start = text.index("A **task**'s gate is its own jobs")
        paragraph = text[start : text.index("\n\n", start)]
        said = " ".join(paragraph.split())
        self.assertIn("A **quest**'s gate, once, when the feature is done: its targeted tests and CI", said)
        self.assertIn("The full suite, sanitizers and the platform matrix are CI's", said)
        self.assertIn("`check` flags a local sanitizer gate check CI already builds", said)
        self.assertEqual(len(paragraph.splitlines()), 4, paragraph)


if __name__ == "__main__":
    unittest.main()
