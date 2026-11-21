"""`land --merge-main` merges main into the quest's branch only when a plain land would refuse for it."""

from __future__ import annotations

from pathlib import Path

from harness import CampaignCase


class MergeMainWhenNeeded(CampaignCase):
    def origin(self) -> Path:
        remote = self.root / "remote.git"
        self.git(self.root, "init", "-q", "--bare", "-b", "main", str(remote))
        self.git(self.home, "remote", "add", "origin", str(remote))
        self.git(self.home, "push", "-q", "origin", "main")
        return remote

    def pushed(self, remote: Path, branch: str) -> str:
        listed = self.git(self.root, "ls-remote", str(remote), f"refs/heads/{branch}")
        return listed.split()[0] if listed else ""

    def ready(self, pid: str, files: str, content: dict[str, str]) -> Path:
        wt = self.started(pid, files=files, check="unit=true")
        self.commit(wt, content)
        self.verified(pid)
        return wt

    def test_main_gaining_only_campaign_records_leaves_the_branch_alone(self) -> None:
        remote = self.origin()
        for pid, extra in (("a", []), ("b", ["--push"])):
            wt = self.ready(pid, f"{pid}.py", {f"{pid}.py": "1\n"})
            head = self.git(wt, "rev-parse", "HEAD")
            self.commit(self.home, {f".campaign/demo/notes/{pid}.md": "a record\n"}, "campaign: a record")
            out = self.ok(self.home, "land", pid, "--merge-main", *extra).out
            self.assertNotIn("merged main", out)
            self.assertEqual(self.pushed(remote, f"quest/demo/{pid}"), "")  # nothing pushed for it
            (landing,) = self.quest(pid)["landed"]
            self.assertEqual(landing["tested"], head)  # landed at the branch head: its CI run stays

    def test_main_changing_code_is_merged_and_pushed(self) -> None:
        remote = self.origin()
        self.ready("a", "a.py", {"a.py": "1\n"})
        b = self.ready("b", "b.py", {"b.py": "1\n"})
        self.ok(self.home, "land", "a")  # main gains a.py: a plain land of b refuses for it
        self.fails(self.home, "land", "b", match="merge main into it (`campaign land b --merge-main`)")
        before = self.git(b, "rev-parse", "HEAD")
        refused = self.fails(self.home, "land", "b", "--merge-main", match="merged main into quest/demo/b at ")
        self.assertIn(", pushed", refused.text)  # main's code voids the check: verify again, then it lands
        head = self.git(b, "rev-parse", "HEAD")
        self.assertNotEqual(head, before)
        self.assertEqual(self.pushed(remote, "quest/demo/b"), head)
        self.verified("b")
        self.ok(self.home, "land", "b", "--merge-main")
        self.assertEqual(self.quest("b")["landed"][0]["tested"], head)
