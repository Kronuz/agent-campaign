"""The job store: the worker cache, the job index, the CI cache and git mirrors."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable
from unittest import mock

from harness import CampaignCase, campaign_module


def recording(reads: list[str], real: Callable[[Path], Any]) -> Callable[[Path], Any]:
    """REAL, recording each path it reads in READS."""

    def read(path: Path) -> Any:
        reads.append(str(path))
        return real(path)

    return read


class WorkerCache(CampaignCase):
    def make(self, jid: str, state: str) -> Path:
        d = self.root / "jobs" / jid
        d.mkdir(parents=True)
        (d / "spec.json").write_text(json.dumps({"cmd": "true"}))
        (d / "state.json").write_text(json.dumps({"state": state}))
        return d

    def test_a_tick_reads_only_the_jobs_in_flight(self) -> None:
        module = campaign_module()
        done = self.make("a-done", "done")
        self.make("b-queued", "queued")
        seen: dict[str, tuple[int, dict[str, object]]] = {}
        reads: list[str] = []
        real = module.read_json
        with (
            mock.patch.dict(os.environ, {"CAMPAIGN_JOBS": str(self.root / "jobs")}),
            mock.patch.object(module.jobs, "read_json", recording(reads, real)),
        ):
            first = module.all_jobs(seen)
            for _ in range(3):
                self.assertEqual([j["id"] for j in module.all_jobs(seen)], ["a-done", "b-queued"])
            self.assertEqual(sum(r.startswith(str(done)) for r in reads), 2, "a terminal job was read again")
            self.assertEqual(sum("b-queued" in r for r in reads), 8, "a queued job must be read every tick")
            self.assertEqual(first[0]["state"], {"state": "done"})
            (self.root / "jobs" / "b-queued" / "state.json").write_text(json.dumps({"state": "done"}))
            self.assertEqual({j["id"]: j["state"]["state"] for j in module.all_jobs(seen)}["b-queued"], "done")
            # pruned, then submitted again under the same id; moved aside, not removed, so the new
            # directory never gets the old inode back (ext4 and tmpfs reuse a freed one at once)
            done.rename(self.root / "jobs" / ".pruned")
            self.make("a-done", "queued")
            self.assertEqual(module.all_jobs(seen)[0]["state"]["state"], "queued")
            shutil.rmtree(self.root / "jobs" / "b-queued")
            self.assertEqual([j["id"] for j in module.all_jobs(seen)], ["a-done"])
            self.assertEqual(set(seen), set(), "a pruned or reopened job stayed cached")

    def test_a_waiter_reads_finished_jobs_once(self) -> None:
        module = campaign_module()
        done = self.make("a-done", "done")
        busy = self.make("b-queued", "queued")
        ticks: list[int] = []
        reads: list[str] = []
        real = module.read_json

        def sleep(_: float) -> None:
            ticks.append(1)
            if len(ticks) == 4:
                (busy / "state.json").write_text(json.dumps({"state": "done"}))

        with (
            mock.patch.dict(os.environ, {"CAMPAIGN_JOBS": str(self.root / "jobs")}),
            mock.patch.object(module.jobs, "read_json", recording(reads, real)),
            mock.patch.object(module.wait, "ensure_worker", lambda: None),
            mock.patch.object(module.wait, "watch_leads", lambda: None),
            mock.patch.object(module.wait.time, "sleep", sleep),
        ):
            module.wait_for(["b-queued"], False, 0)
        self.assertEqual(len(ticks), 4)
        self.assertLessEqual(sum(r.startswith(str(done)) for r in reads), 2, "a waiter re-read a finished job")
        self.assertTrue(any("b-queued" in r for r in reads), "the read_json patch reached no read")


class JobIndex(CampaignCase):
    """Terminal jobs and key links are remembered across processes, never past a change."""

    def make(self, jid: str, state: str) -> Path:
        d = self.root / "jobs" / jid
        d.mkdir(parents=True)
        (d / "spec.json").write_text(json.dumps({"cmd": "true"}))
        (d / "state.json").write_text(json.dumps({"state": state}))
        return d

    def listed(self) -> dict[str, dict[str, object]]:
        """The jobs a fresh process lists: its own cache empty, the host's index as it is."""
        with mock.patch.dict(os.environ, {"CAMPAIGN_JOBS": str(self.root / "jobs")}):
            return {j["id"]: j for j in campaign_module().all_jobs()}

    def test_a_finished_job_changed_since_it_was_indexed_is_read_again(self) -> None:
        d = self.make("261117-000000-aaaaaa", "done")
        self.assertEqual(self.listed()[d.name]["state"], {"state": "done"})
        self.assertTrue((self.root / "jobs" / ".index" / "261117.json").exists(), "nothing was indexed")
        (d / "state.json").write_text(json.dumps({"state": "done", "slimmed": "now"}))  # in place
        self.assertEqual(self.listed()[d.name]["state"], {"state": "done", "slimmed": "now"})
        (d / "for.json").write_text(json.dumps({"tags": [{"quest": "q"}]}))  # tagged: a new file
        self.assertEqual(self.listed()[d.name]["tags"], [{"quest": "q"}])
        shutil.rmtree(d)
        self.make(d.name, "queued")  # pruned, then submitted again under the same id
        self.assertEqual(self.listed()[d.name]["state"], {"state": "queued"})

    def test_a_replaced_key_link_names_its_new_job(self) -> None:
        keys = self.root / "jobs" / "keys"
        keys.mkdir(parents=True)
        for jid in ("261117-000000-aaaaaa", "261117-000001-bbbbbb"):
            self.make(jid, "done")
        (keys / "k").symlink_to("261117-000000-aaaaaa")
        with mock.patch.dict(os.environ, {"CAMPAIGN_JOBS": str(self.root / "jobs")}):
            self.assertEqual(campaign_module().key_links("k"), ["261117-000000-aaaaaa"])
            time.sleep(0.01)
            (keys / ".k.tmp").symlink_to("261117-000001-bbbbbb")
            os.replace(keys / ".k.tmp", keys / "k")  # as submit files a newer job under the key
            self.assertEqual(campaign_module().key_links("k"), ["261117-000001-bbbbbb"])

    def test_a_frozen_ref_names_what_git_names_and_moves_after(self) -> None:
        first = self.git(self.home, "rev-parse", "HEAD")
        self.git(self.home, "tag", "-a", "v1", "-m", "v1")
        self.git(self.home, "tag", "lw")
        self.git(self.home, "update-ref", "refs/remotes/origin/main", first)
        self.git(self.home, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        self.git(self.home, "branch", "feat")
        later = self.commit(self.home, {"app.py": "print(2)\n"})
        m = campaign_module()
        names = ["main", "feat", "v1", "lw", "origin/main", "origin", "refs/heads/feat", "HEAD", first, "nope"]
        unfrozen = [(m.has_ref(self.home, x), m.has_ref(self.home, x) and m.rev(self.home, x)) for x in names]
        with m.frozen():
            frozen = [(m.has_ref(self.home, x), m.has_ref(self.home, x) and m.rev(self.home, x)) for x in names]
            self.git(self.home, "branch", "-f", "feat", later)
            self.assertEqual(m.rev(self.home, "feat"), first, "a frozen command reads the branches once")
        self.assertEqual(frozen, unfrozen)
        self.assertEqual(m.rev(self.home, "feat"), later)

    def test_a_shared_parse_follows_its_file(self) -> None:
        m = campaign_module()
        parsed: list[str] = []

        def parse(p: Path) -> str:
            parsed.append(p.read_text())
            return parsed[-1]

        plan = self.root / "PLAN.md"
        plan.write_text("one")
        self.assertEqual([m.shared(plan, parse), m.shared(plan, parse)], ["one", "one"])
        plan.write_text("three")  # another size
        self.assertEqual(m.shared(plan, parse), "three")
        self.assertEqual(parsed, ["one", "three"])
        plan.unlink()
        self.assertIsNone(m.shared(plan, parse))


class CiCache(CampaignCase):
    def test_only_a_passed_ci_answer_outlives_its_command(self) -> None:
        """A pending or failed CI answer is asked again by the next command (a failure can be re-run);
        a passed one is kept for the host."""
        answer = self.root / "ci-answer"
        calls = self.root / "gh-calls"
        fake = self.root / "bin" / "gh"
        fake.write_text(f'#!/bin/sh\necho "$2" >> {calls}\ncat {answer}\n')
        fake.chmod(0o755)
        self.ok(self.home, "config", "ci=github")
        wt = self.started("fix", files="app.py", check="unit=true")
        self.commit(wt, {"app.py": "print(2)\n"})
        self.verified("fix")
        for status, conclusion, gap in (
            ("in_progress", "", "running: build"),
            ("completed", "failure", "failed: build (failure)"),
            ("completed", "success", ""),
        ):
            run = {"name": "build", "databaseId": 7, "status": status, "conclusion": conclusion}
            answer.write_text(json.dumps([run]))
            out = self.ok(wt, "brief").out
            if gap:
                self.assertIn(gap, out)
            else:
                self.assertNotIn("CI at", out)
        calls.write_text("")
        self.assertNotIn("CI at", self.ok(wt, "brief").out)
        self.assertEqual(calls.read_text(), "", "a passed CI answer was asked again")


class GitMirrors(CampaignCase):
    """Jobs reach GitHub through one bare mirror per repository in the job store; "GitHub" here is a
    directory every https:// URL is rewritten to, outside the jobs' own rewrites."""

    def setUp(self) -> None:
        super().setUp()
        self.up = self.root / "up"
        (self.root / "gitconfig").write_text(f'[url "{self.up}/"]\n\tinsteadOf = https://\n')
        self.env["GIT_CONFIG_GLOBAL"] = str(self.root / "gitconfig")

    def upstream(self, name: str) -> Path:
        work = self.repo(f"{name}-work", {"f": "1\n"})
        bare = self.up / "github.com" / "demo" / f"{name}.git"
        bare.parent.mkdir(parents=True, exist_ok=True)
        self.git(self.root, "clone", "-q", "--bare", str(work), str(bare))
        return work

    def push(self, work: Path, text: str) -> str:
        head = self.commit(work, {"f": text})
        self.git(work, "push", "-q", str(self.up / "github.com" / "demo" / f"{work.name[:-5]}.git"), "HEAD:main")
        return head

    def clone(self, name: str, commit: str, git: str = "git", tag: str = "") -> str:
        """A job that clones like FetchContent (no checkout, then the pinned commit) and records how
        many of the clone's object files are hard links."""
        url = f"https://github.com/demo/{name}.git"
        linked = 'find d/.git/objects -type f -links +1 | wc -l > "$JOB_OUT/linked"'
        cmd = f"{git} clone -q --no-checkout {url} d && {git} -C d checkout -q {commit} -- && {linked} # {tag}"
        return self.submit(self.home, "--level", "light", "--force", "--", cmd)

    def test_jobs_clone_through_one_mirror_that_fetches_only_what_it_misses(self) -> None:
        work = self.upstream("dep")
        first = self.git(work, "rev-parse", "HEAD")
        jid = self.clone("dep", first)
        self.ok(self.home, "wait", jid, "--timeout", "60")
        mirror = self.root / "jobs" / "git-mirrors" / "github.com" / "demo" / "dep.git"
        self.assertTrue(mirror.is_dir(), "the clone made no mirror")
        second = self.push(work, "2\n")  # a new pin the mirror lacks: fetched once, then the clone retried
        self.ok(self.home, "wait", self.clone("dep", second), "--timeout", "60")
        linked = (self.root / "jobs" / jid / "a1" / "out" / "linked").read_text()
        self.assertGreater(int(linked), 0, "the clone copied the mirror's objects")
        self.upstream("many")  # concurrent jobs make one mirror, and none breaks it
        many = self.git(self.root / "many-work", "rev-parse", "HEAD")
        jobs = [self.clone("many", many, tag=str(n)) for n in range(4)]
        self.ok(self.home, "wait", *jobs, "--timeout", "90", timeout=120)
        self.git(mirror.with_name("many.git"), "fsck", "--no-progress")
        pin = "FetchContent_Declare(pinned GIT_REPOSITORY https://github.com/demo/pinned.git\n  GIT_TAG {})\n"
        pinned = self.git(self.upstream("pinned"), "rev-parse", "HEAD")
        self.commit(self.home, {"CMakeLists.txt": pin.format(pinned)})
        self.ok(self.home, "wait", self.submit(self.home, "--level", "light", "--", "true pins"), "--timeout", "60")
        self.git(mirror.with_name("pinned.git"), "cat-file", "-e", f"{pinned}^{{commit}}")
        shutil.rmtree(self.up)  # GitHub unreachable: what the mirror holds still clones, by any git
        cached = self.clone("dep", first, git='"$CAMPAIGN_GIT"', tag="cached")
        self.ok(self.home, "wait", cached, self.clone("dep", second, tag="shim"), "--timeout", "60")
        shim = self.root / "jobs" / "git-mirrors" / "bin"  # outside a job, a cached shim is plain git
        env = {k: v for k, v in self.env.items() if not k.startswith("CAMPAIGN_")}
        version = subprocess.run([shim / "git", "--version"], env={**env, "PATH": f"{shim}:{env['PATH']}"})
        self.assertEqual(version.returncode, 0)
