"""One writer per file: `set files=` keeps a quest that holds files off another holder's."""

from __future__ import annotations

from harness import CampaignCase


class FileHolds(CampaignCase):
    def test_set_files_refuses_widening_into_a_holder_unless_forced_and_narrowing_never_refuses(self) -> None:
        self.started("split", files="src/auth/**")
        self.started("relay", files="src/relay.py")
        self.started("parked", files="docs/**")
        self.ok(self.home, "set", "parked", "status=blocked", "until=owner", "reason=the owner decides")
        self.ok(self.home, "new", "idea", "--title", "t", "--risk", "low", "--files", "lib/**")

        # widening an active quest into an active or parked-with-branch holder names it and its globs
        self.fails(self.home, "set", "relay", "files=src/relay.py,src/auth/service.cc", match="split (src/auth/**)")
        self.fails(self.home, "set", "relay", "files=src/relay.py,docs/a.md", match="parked (docs/**)")
        self.assertEqual(self.quest("relay")["files"], ["src/relay.py"])

        # a planned quest holds nothing, and a planned quest's own set is checked at its start
        self.ok(self.home, "set", "relay", "files=src/relay.py,lib/x.py")
        self.ok(self.home, "set", "idea", "files=lib/**,src/auth/**")
        self.assertIn("split", self.fails(self.home, "start", "idea", match="may overlap").text)

        # --force REASON passes and records the reason; with nothing to force it is refused
        self.fails(self.home, "set", "relay", "files=src/relay.py", "--force", "no clash", match="--force")
        why = "split rebases onto relay's service.cc change"
        self.ok(self.home, "set", "relay", "files=src/relay.py,src/auth/service.cc", "--force", why)
        self.assertEqual(self.quest("relay")["files"], ["src/relay.py", "src/auth/service.cc"])
        self.assertIn(f"(forced over split: {why})", self.git(self.home, "log", "-1", "--format=%s"))

        # narrowing never refuses, even while it still shares a file with a forced-over holder
        self.ok(self.home, "set", "relay", "files=src/auth/service.cc")
        self.ok(self.home, "set", "relay", "files=src/relay.py")
        self.assertEqual(self.quest("relay")["files"], ["src/relay.py"])
