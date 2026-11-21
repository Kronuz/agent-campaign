"""A job never fails on a build tree that another CMake generator or source configured: the runner
removes such a tree from the job's slot before the command runs, and touches nothing else."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import CampaignCase, campaign_module

HAS_CMAKE = bool(shutil.which("cmake") and shutil.which("ninja"))
PROJECT = "cmake_minimum_required(VERSION 3.10)\nproject(p NONE)\n"
MAKEFILES, NINJA = "Unix Makefiles", "Ninja"


def cache(tree: Path, generator: str, home: Path | None = None) -> None:
    """A CMakeCache.txt in TREE as CMake writes it: configured by GENERATOR from HOME (default: TREE)."""
    tree.mkdir(parents=True, exist_ok=True)
    (tree / "CMakeCache.txt").write_text(
        f"CMAKE_CACHEFILE_DIR:INTERNAL={tree.resolve()}\n"
        f"CMAKE_GENERATOR:INTERNAL={generator}\n"
        f"CMAKE_HOME_DIRECTORY:INTERNAL={(home or tree).resolve()}\n"
    )


@unittest.skipUnless(HAS_CMAKE, "needs cmake and ninja")
class Configure(CampaignCase):
    """Real CMake: a job's configure in a slot another generator or source configured."""

    def setUp(self) -> None:
        super().setUp()
        self.checkout = self.started("gc", files="**")
        self.commit(
            self.checkout,
            {"CMakeLists.txt": PROJECT, "sub/CMakeLists.txt": PROJECT, "keep/.keep": ""},
        )

    def run_job(self, cmd: str, generator: str) -> tuple[int, str]:
        r = self.run_cli(
            self.checkout,
            *["job", "submit", "--quest", "gc", "--sync", "--slot", "gc", "--env", f"CMAKE_GENERATOR={generator}"],
            "--",
            cmd,
        )
        log = self.root / "jobs" / r.out.split()[0] / "a1" / "log"
        return r.code, log.read_text() + r.text

    def test_a_makefiles_tree_does_not_fail_a_ninja_job(self) -> None:
        code, text = self.run_job("cmake -S . -B build", MAKEFILES)
        self.assertEqual(code, 0, text)
        code, text = self.run_job("cmake -S . -B build && test -f build/build.ninja", NINJA)
        self.assertEqual(code, 0, text)
        self.assertIn(f"campaign: removed build: configured by {MAKEFILES}, this job uses {NINJA}", text)

    def test_a_tree_from_another_source_does_not_fail_the_job(self) -> None:
        self.assertEqual(self.run_job("cmake -S sub -B build", NINJA)[0], 0)
        code, text = self.run_job("cmake -S . -B build", NINJA)
        self.assertEqual(code, 0, text)
        self.assertRegex(text, r"campaign: removed build: configured from \S+/sub, not this job's source")

    def test_a_sub_build_another_generator_configured_does_not_fail_the_job(self) -> None:
        sub = "mkdir -p .deps/x-subbuild && cp CMakeLists.txt .deps/x-subbuild/ && cmake -S .deps/x-subbuild -B .deps/x-subbuild"
        self.assertEqual(self.run_job(sub, MAKEFILES)[0], 0)
        code, text = self.run_job(sub, NINJA)
        self.assertEqual(code, 0, text)
        self.assertIn(f"campaign: removed .deps/x-subbuild: configured by {MAKEFILES}, this job uses {NINJA}", text)

    def test_a_tree_that_matches_is_kept(self) -> None:
        self.assertEqual(self.run_job("cmake -S . -B build && touch build/marker", NINJA)[0], 0)
        code, text = self.run_job("cmake -S . -B build && test -f build/marker && echo again", NINJA)
        self.assertEqual(code, 0, text)
        self.assertNotIn("campaign: removed", text)


class Scan(unittest.TestCase):
    """The scan and its removals on fake caches: no CMake needed, each refusal visible."""

    def setUp(self) -> None:
        self.module = campaign_module()
        self.dir = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.dir, True)
        patch = mock.patch.dict(os.environ, {"CAMPAIGN_JOBS": str(self.dir / "jobs")})
        patch.start()
        self.addCleanup(patch.stop)
        self.slot = self.dir / "jobs" / "slots" / "s"
        self.slot.mkdir(parents=True)
        self.said: list[str] = []

    def clear(self, generator: str, *sources: Path, slot: Path | None = None) -> None:
        self.module.clear_stale_trees(slot or self.slot, generator, sources or [self.slot], self.said.append)

    def test_a_tree_another_generator_configured_is_removed_with_one_line_saying_why(self) -> None:
        cache(self.slot / "build", MAKEFILES, self.slot)
        self.clear(NINJA)
        self.assertFalse((self.slot / "build").exists())
        self.assertEqual(self.said, [f"campaign: removed build: configured by {MAKEFILES}, this job uses {NINJA}"])

    def test_a_tree_whose_source_is_gone_or_in_another_slot_is_removed(self) -> None:
        other = self.dir / "jobs" / "slots" / "other"
        other.mkdir()
        cache(self.slot / "gone", NINJA, self.slot / "removed-source")
        cache(self.slot / "elsewhere", NINJA, other)
        self.clear(NINJA)
        self.assertFalse((self.slot / "gone").exists() or (self.slot / "elsewhere").exists())
        self.assertEqual(len(self.said), 2)

    def test_a_tree_moved_to_another_path_is_removed(self) -> None:
        cache(self.slot / "build", NINJA, self.slot)
        shutil.copytree(self.slot / "build", self.slot / "copy")
        self.clear(NINJA)
        self.assertTrue((self.slot / "build").exists())
        self.assertFalse((self.slot / "copy").exists())

    def test_a_tree_that_matches_is_kept(self) -> None:
        (self.slot / "ref-x").mkdir()
        cache(self.slot / "build", NINJA, self.slot)  # the slot's source
        cache(self.slot / "ref-x" / "build", NINJA, self.slot / "ref-x")  # a ref's
        cache(self.slot / "tests" / "build-t", NINJA, self.slot / "tests")  # a directory around it
        cache(self.slot / ".deps" / "y-subbuild", NINJA)  # a sub-build: its own source
        self.clear(NINJA, self.slot, self.slot / "ref-x")
        self.assertEqual(self.said, [])
        self.assertEqual(len(list(self.slot.rglob("CMakeCache.txt"))), 4)

    def test_a_job_that_names_no_generator_keeps_every_tree_of_its_source(self) -> None:
        cache(self.slot / "build", MAKEFILES, self.slot)
        self.clear("")
        self.assertTrue((self.slot / "build").exists())
        self.assertEqual(self.said, [])

    def test_a_slot_outside_the_runners_slots_is_never_touched(self) -> None:
        mine = self.dir / "my-checkout"
        cache(mine / "build", MAKEFILES, mine)
        self.clear(NINJA, mine, slot=mine)
        self.assertTrue((mine / "build").exists())
        self.assertEqual(self.said, [])

    def test_a_tree_that_is_a_symlink_out_of_the_slot_is_never_followed(self) -> None:
        outside = self.dir / "outside"
        cache(outside, MAKEFILES, outside)
        (self.slot / "build").symlink_to(outside)
        self.clear(NINJA)
        self.assertTrue((outside / "CMakeCache.txt").exists())
        self.assertTrue((self.slot / "build").is_symlink())
        self.assertEqual(self.said, [])

    def test_the_slot_itself_is_never_removed(self) -> None:
        cache(self.slot, MAKEFILES, self.slot)  # an in-source configure
        self.assertFalse(self.module.remove_tree(self.slot, self.slot))
        self.clear(NINJA)
        self.assertTrue((self.slot / "CMakeCache.txt").exists())

    def test_a_path_outside_the_slot_is_never_removed(self) -> None:
        other = self.dir / "jobs" / "slots" / "other" / "build"
        cache(other, MAKEFILES, other)
        self.assertFalse(self.module.remove_tree(self.slot, other))
        self.assertFalse(self.module.remove_tree(self.slot, self.slot / ".." / "other" / "build"))
        self.assertTrue(other.exists())

    def test_sources_and_the_git_directory_are_not_scanned(self) -> None:
        cache(self.slot / ".deps" / "z-src", MAKEFILES, self.slot)
        cache(self.slot / ".git" / "build", MAKEFILES, self.slot)
        cache(self.slot / "a" / "b" / "c" / "too-deep", MAKEFILES, self.slot)
        self.clear(NINJA)
        self.assertEqual(self.said, [])
        self.assertEqual(len(list(self.slot.rglob("CMakeCache.txt"))), 3)


if __name__ == "__main__":
    unittest.main()
