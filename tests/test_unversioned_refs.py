"""An unversioned job's --ref: checked out from its own repository at its commit, as a versioned job's is."""

from __future__ import annotations

import json
import unittest

from harness import CampaignCase

SHOW = 'echo "${JOB_REF_CONSENSUS-unset}" > "$JOB_OUT/path"; cat "$JOB_REF_CONSENSUS/README.md" > "$JOB_OUT/seen"'


class UnversionedRefs(CampaignCase):
    def test_an_unversioned_job_sees_its_ref_at_its_commit(self) -> None:
        """msg-29: --unversioned --ref NAME=PATH@SHA sets $JOB_REF_NAME to PATH checked out at SHA,
        from outside any repository, and the checkout goes when the job ends."""
        other = self.repo("consensus", {"README.md": "old\n"})
        old = self.git(other, "rev-parse", "HEAD")
        self.commit(other, {"README.md": "new\n"})
        scratch = self.root / "scratch"
        scratch.mkdir()
        jid = self.submit(scratch, "--unversioned", "--sync", "--ref", f"consensus={other}@{old}", "--", SHOW)
        self.assertEqual(self.job(jid)["state"], "done", self.job(jid))
        d = self.root / "jobs" / jid
        out = d / "a1" / "out"
        self.assertEqual((out / "path").read_text().strip(), str(d / "ref-consensus"))
        self.assertEqual((out / "seen").read_text(), "old\n")
        self.assertFalse((d / "ref-consensus").exists(), "the ref's checkout outlived its job")
        self.assertNotIn(str(d / "ref-consensus"), self.git(other, "worktree", "list"))

    def test_an_unversioned_job_in_a_repository_resolves_a_bare_ref_there(self) -> None:
        old = self.git(self.home, "rev-parse", "HEAD")
        self.commit(self.home, {"README.md": "second\n"})
        jid = self.submit(self.home, "--unversioned", "--sync", "--ref", f"consensus={old}", "--", SHOW)
        self.assertEqual(self.job(jid)["state"], "done", self.job(jid))
        self.assertEqual((self.root / "jobs" / jid / "a1" / "out" / "seen").read_text(), "app\n")

    def test_an_unversioned_job_without_a_ref_is_unchanged(self) -> None:
        """No --ref: no ref in its spec, none in its environment or job directory, and it runs where
        it was submitted."""
        scratch = self.root / "scratch"
        scratch.mkdir()
        show = 'pwd > "$JOB_OUT/cwd"; env | grep "^JOB_REF_" > "$JOB_OUT/refs"; echo "$JOB_COMMIT" > "$JOB_OUT/commit"'
        jid = self.submit(scratch, "--unversioned", "--sync", "--", show)
        d = self.root / "jobs" / jid
        self.assertEqual(self.job(jid)["state"], "done", self.job(jid))
        spec = json.loads((d / "spec.json").read_text())
        self.assertEqual((spec["repo"], spec["commit"], spec["refs"]), ("", "", {}))
        out = d / "a1" / "out"
        self.assertEqual((out / "cwd").read_text().strip(), str(scratch))
        self.assertEqual((out / "refs").read_text(), "")
        self.assertEqual((out / "commit").read_text(), "\n")
        self.assertEqual(sorted(x.name for x in d.glob("ref-*")), [])


if __name__ == "__main__":
    unittest.main()
