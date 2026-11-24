"""One writer per file: `set files=` keeps a quest that holds files off another holder's."""

from __future__ import annotations

import json

from harness import CampaignCase


class FileHolds(CampaignCase):
    def test_set_files_refuses_widening_into_a_holder_and_narrowing_never_refuses(self) -> None:
        self.started("split", files="src/auth/**")
        self.started("relay", files="src/relay.py")
        self.started("tidy", files="tools/**")
        self.ok(self.home, "set", "tidy", "status=review")
        self.started("parked", files="docs/**")
        self.ok(self.home, "set", "parked", "status=blocked", "until=owner", "reason=the owner decides")
        self.ok(self.home, "new", "idea", "--title", "t", "--risk", "low", "--files", "lib/**")

        # widening into an active, review or parked-with-branch holder names it and the shared globs
        for wider, named in (
            ("src/auth/service.cc", "split (src/auth/**)"),
            ("tools/lint.py", "tidy (tools/**)"),
            ("docs/a.md", "parked (docs/**)"),
        ):
            self.fails(self.home, "set", "relay", f"files=src/relay.py,{wider}", match=named)
            self.assertEqual(self.quest("relay")["files"], ["src/relay.py"], "a refused set changes nothing")
        self.fails(self.home, "set", "relay", "files=src/relay.py,src/**", match="needs=")
        self.assertEqual(self.quest("relay")["files"], ["src/relay.py"])

        # a planned quest holds nothing, and a planned quest's own set is checked at its start
        self.ok(self.home, "set", "relay", "files=src/relay.py,lib/x.py")
        self.ok(self.home, "set", "idea", "files=lib/**,src/auth/**")
        self.assertIn("split", self.fails(self.home, "start", "idea", match="may overlap").text)

        # narrowing never refuses, even while it still shares a file with a holder it started over
        new = ("--title", "t", "--risk", "low", "--files", "src/auth/service.cc,src/wide.py")
        self.ok(self.home, "new", "wide", *new)
        self.ok(self.home, "start", "wide", "--force", "split rebases onto it")
        self.ok(self.home, "set", "wide", "files=src/auth/service.cc")
        self.assertEqual(self.quest("wide")["files"], ["src/auth/service.cc"])
        self.ok(self.home, "set", "relay", "files=src/relay.py")
        self.assertEqual(self.quest("relay")["files"], ["src/relay.py"])

    def test_a_holder_that_waits_for_a_quest_never_blocks_its_start(self) -> None:
        new = ("--title", "t", "--risk", "low", "--files")
        self.ok(self.home, "new", "pin", *new, "cmake/**")
        self.ok(self.home, "new", "mid", *new, "mid.py", "--needs", "pin")
        for holder, files, needs in (("direct", "cmake/deps.json", "demo:pin"), ("chain", "cmake/a.cmake", "mid")):
            self.ok(self.home, "new", holder, *new, files, "--needs", needs)
            self.ok(self.home, "start", holder, "--force", "its journal first")
            self.ok(self.home, "set", holder, "status=blocked", "until=owner", "reason=waits")
        self.started("parked", files="cmake/b.cmake")
        self.ok(self.home, "set", "parked", "status=blocked", "until=pin", "reason=after pin")
        self.started("other", files="cmake/c.cmake")
        self.ok(self.home, "set", "other", "status=blocked", "until=owner", "reason=the owner decides")

        # a parked holder that does not wait for pin still holds its files
        o = json.loads(self.ok(self.home, "status", "--json").out)["overview"]
        self.assertNotIn("pin", o["launchable"])
        self.assertEqual(o["held"]["pin"], ["other"])
        refused = self.fails(self.home, "start", "pin", match="may overlap other ").text
        for waiting in ("direct", "chain", "parked"):
            self.assertNotIn(waiting, refused)

        # holders that need pin (as demo:pin, or through mid) or are parked until it never block it
        self.ok(self.home, "set", "other", "status=dropped", "reason=folded elsewhere", "into=none")
        o = json.loads(self.ok(self.home, "status", "--json").out)["overview"]
        self.assertIn("pin", o["launchable"])
        self.assertNotIn("pin", o["held"])
        self.ok(self.home, "start", "pin")
