"""One writer per file: `set files=` keeps a quest that holds files off another holder's."""

from __future__ import annotations

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
