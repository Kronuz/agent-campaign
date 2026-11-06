"""Scenarios A-I from the Campaign 2.0 brief, driven end to end with scripted agent actions.

Each scenario asserts the orchestration footprint (reviews required, handoffs, waits) as well as
the outcome. Scenario G (shared-host contention) is test_jobs.Contention.
"""

from __future__ import annotations

import json
import os
import re
import signal
import unittest

from harness import SKILL, CampaignCase


class Scenarios(CampaignCase):
    def test_a_small_bug_fix_needs_one_adventurer_and_no_paladin(self) -> None:
        wt = self.started("fix-off-by-one", files="app.py", risk="low")
        self.commit(wt, {"app.py": "print(1 + 1)\n"}, "fix the off-by-one")
        self.verified("fix-off-by-one")
        self.ok(self.home, "land", "fix-off-by-one")
        p = self.quest("fix-off-by-one")
        self.assertEqual(p["lenses"], [])
        self.assertEqual(p["accepted"], {})
        self.assertFalse((self.home / ".campaign/demo/reviews").exists())
        self.assertEqual(p["status"], "landed")

    def test_b_a_medium_feature_gets_one_integrated_review(self) -> None:
        self.ok(
            self.home,
            "new",
            "search",
            "--title",
            "search",
            "--risk",
            "normal",
            "--files",
            "app.py,lib/**",
            "--check",
            "unit=python3 app.py",
        )
        self.ok(self.home, "start", "search")
        self.ok(self.home, "start", "search", "--task", "index", "--files", "lib/**")
        self.fails(self.home, "start", "search", "--task", "again", "--files", "lib/util.py", match="overlap")
        self.ok(self.home, "start", "search", "--task", "query", "--files", "app.py")
        self.commit(self.wt("search--index"), {"lib/util.py": "INDEX = {}\n"}, "index")
        self.commit(self.wt("search--query"), {"app.py": "import lib.util\nprint('query')\n"}, "query")
        party = self.wt("search")
        self.git(party, "merge", "-q", "--no-edit", "quest/demo/search--index")  # the game master integrates the tasks
        self.git(party, "merge", "-q", "--no-edit", "quest/demo/search--query")
        self.verified("search")
        self.review("search", "correctness", "APPROVE")
        self.ok(self.home, "accept", "search", "correctness")
        self.ok(self.home, "land", "search")
        reviews = list((self.home / ".campaign/demo/reviews").glob("*.md"))
        self.assertEqual([r.name for r in reviews], ["search-correctness.md"])
        self.assertEqual(sorted(self.quest("search")["tasks"]), ["index", "query"])

    def test_c_performance_work_measures_exact_commits_without_a_waiting_agent(self) -> None:
        wt = self.started("faster", files="app.py", risk="normal", check="unit=python3 app.py")
        base = self.git(self.home, "rev-parse", "main")
        candidate = self.commit(wt, {"app.py": "print(sum(range(10)))\n"}, "candidate")
        bench = 'python3 "$JOB_REF_BASE/app.py" > "$JOB_OUT/a"; python3 app.py > "$JOB_OUT/b"'
        screen = self.submit(
            wt, "--quest", "faster", "--level", "strict", "--minutes", "5", "--ref", f"base={base}", "--", bench
        )
        confirm = self.submit(
            wt,
            "--quest",
            "faster",
            "--level",
            "exclusive",
            "--minutes",
            "5",
            "--ref",
            f"base={base}",
            "--after",
            screen,
            "--",
            bench,
        )
        # the adventurer ends its session here; it writes the job ids in its journal
        self.commit(wt, {"app.py": "print('next experiment')\n"}, "next idea")
        self.ok(
            self.home, "wait", "--quest", "faster", "--timeout", "120", timeout=150
        )  # the game master's single blocking wait
        spec = json.loads((self.root / "jobs" / confirm / "spec.json").read_text())
        self.assertEqual(spec["commit"], candidate)
        self.assertEqual(spec["refs"]["base"]["commit"], base)
        out = self.root / "jobs" / confirm / "a1" / "out"
        self.assertEqual(((out / "a").read_text(), (out / "b").read_text()), ("1\n", "45\n"))
        self.assertGreaterEqual(self.job(confirm)["started"], self.job(screen)["ended"])

    def test_d_a_security_surface_needs_its_own_lens_and_no_waiver(self) -> None:
        wt = self.started("decoder", files="lib/**", risk="high", check="unit=true", lenses="security")
        self.commit(wt, {"lib/util.py": "def decode(b):\n    return b[:16]\n"}, "bound the decoder")
        fuzz = self.submit(
            wt,
            "--quest",
            "decoder",
            "--",
            "python3 -c 'import lib.util as u; [u.decode(bytes(n)) for n in range(4096)]'",
        )
        self.verified("decoder")
        self.ok(self.home, "wait", fuzz)
        self.fails(self.home, "accept", "decoder", "security", "--waive", "looks fine", match="cannot be waived")
        self.fails(self.home, "land", "decoder", match="security review not accepted")
        self.review("decoder", "security", "CHANGES")
        self.ok(self.home, "accept", "decoder", "security", "--jobs", fuzz)
        self.ok(self.home, "land", "decoder")

    def test_e_durability_gets_an_early_design_review_and_one_code_review(self) -> None:
        wt = self.started(
            "wal", files="lib/**,docs/design/**", risk="high", check="unit=true", lenses="design,durability"
        )
        self.commit(wt, {"docs/design/wal.md": "fsync the log before the index\n"}, "design")
        self.review("wal", "design", "APPROVE")
        self.ok(self.home, "accept", "wal", "design")  # before the implementation exists
        self.commit(wt, {"lib/util.py": "def commit(log, index):\n    log.fsync()\n    index.write()\n"}, "implement")
        self.verified("wal")
        self.fails(self.home, "land", "wal", match="durability review not accepted")
        self.review("wal", "durability", "CHANGES")
        self.commit(
            wt,
            {"lib/util.py": "def commit(log, index):\n    log.fsync()\n    index.write()\n    index.fsync()\n"},
            "fix D1",
        )
        probe = self.submit(wt, "--quest", "wal", "--", "grep -q 'index.fsync' lib/util.py")
        self.ok(self.home, "wait", probe)
        self.verified("wal")
        self.ok(self.home, "accept", "wal", "durability", "--jobs", probe)
        self.ok(self.home, "land", "wal")
        reviews = sorted(r.name for r in (self.home / ".campaign/demo/reviews").glob("*.md"))
        self.assertEqual(reviews, ["wal-design.md", "wal-durability.md"])  # no recheck, no second round

    def test_f_an_interrupted_campaign_is_reconstructed_from_files(self) -> None:
        wt = self.started("half", files="app.py", risk="normal", check="unit=python3 app.py")
        self.commit(wt, {"app.py": "print('part one')\n"}, "part one")
        work = wt / ".campaign/demo/journal/half.md"
        work.write_text(work.read_text().replace("State: working", "State: waiting on jobs (see Evidence)"))
        self.git(wt, "commit", "-q", "-am", "work file: waiting")
        (wt / "app.py").write_text("print('part two, unsaved')\n")
        jid = self.submit(wt, "--quest", "half", "--retries", "1", "--", "sleep 4")
        self.wait_until(lambda: self.job(jid)["state"] == "running" and "runner" in self.job(jid))
        # everything dies: the runner, the game master and the adventurer sessions
        os.kill(self.job(jid)["runner"], signal.SIGKILL)
        data = json.loads(self.ok(self.home, "status", "--json").out)
        view = next(v for v in data["quests"] if v["id"] == "half")
        self.assertEqual(view["status"], "active")
        self.assertEqual(view["journal"]["half"]["state"], "waiting on jobs (see Evidence)")
        self.assertEqual(view["journal"]["half"]["dirty"], 1)
        again = self.ok(wt, "job", "submit", "--quest", "half", "--retries", "1", "--", "sleep 4").out
        self.assertIn("reused", again)  # resuming does not duplicate the machine work
        self.ok(self.home, "wait", jid, "--timeout", "90", timeout=120)
        self.assertEqual(self.job(jid)["attempt"], 2)
        self.assertEqual((wt / "app.py").read_text(), "print('part two, unsaved')\n")

    def test_a_scouting_party_hunts_disjoint_areas_in_parallel(self) -> None:
        """A scouting party: one quest per target, disjoint areas, timing serialized by jobs."""
        self.ok(
            self.home,
            "new",
            "hunt-app",
            "--title",
            "hunt app",
            "--risk",
            "low",
            "--files",
            "app.py",
            "--check",
            "unit=python3 app.py",
        )
        self.ok(
            self.home,
            "new",
            "hunt-lib",
            "--title",
            "hunt lib",
            "--risk",
            "low",
            "--files",
            "lib/**",
            "--check",
            "unit=true",
        )
        self.ok(self.home, "new", "hunt-lib-again", "--title", "overlap", "--risk", "low", "--files", "lib/util.py")
        self.ok(self.home, "start", "hunt-app")
        self.ok(self.home, "start", "hunt-lib")
        self.fails(self.home, "start", "hunt-lib-again", match="overlap hunt-lib")
        timing = 'python3 -c "import time; print(time.time())" > "$JOB_OUT/t0"; sleep 1; python3 -c "import time; print(time.time())" > "$JOB_OUT/t1"'
        args = ["--level", "strict", "--minutes", "5"]
        a = self.submit(self.wt("hunt-app"), "--quest", "hunt-app", *args, "--env", "TARGET=app", "--", timing)
        b = self.submit(self.wt("hunt-lib"), "--quest", "hunt-lib", *args, "--env", "TARGET=lib", "--", timing)
        self.ok(self.home, "wait", a, b, "--timeout", "90", timeout=120)
        spans = [
            tuple(float((self.root / "jobs" / j / "a1" / "out" / f).read_text()) for f in ("t0", "t1")) for j in (a, b)
        ]
        self.assertTrue(spans[0][1] <= spans[1][0] or spans[1][1] <= spans[0][0], f"measurements overlapped: {spans}")
        self.commit(self.wt("hunt-app"), {"app.py": "print(1)  # found and fixed\n"})
        self.verified("hunt-app")
        self.ok(self.home, "land", "hunt-app")
        self.ok(self.home, "set", "hunt-lib", "status=dropped", "reason=nothing found: boundaries 0, 1, 2**31 checked")
        self.ok(self.home, "clean", "hunt-lib")
        statuses = {k: v["status"] for k, v in self.state()["quests"].items() if k.startswith("hunt-")}
        self.assertEqual(statuses, {"hunt-app": "landed", "hunt-lib": "dropped", "hunt-lib-again": "planned"})

    def test_h_interacting_quests_are_ordered_and_integrated(self) -> None:
        lib = self.repo("lib", {"store.py": "def get(k):\n    return k\n"})
        self.ok(
            self.home,
            "new",
            "store-api",
            "--title",
            "store api",
            "--repo",
            "../lib",
            "--risk",
            "low",
            "--provides",
            "api:store",
            "--files",
            "store.py",
            "--check",
            "unit=python3 store.py",
        )
        self.ok(
            self.home,
            "new",
            "use-store",
            "--title",
            "use store",
            "--risk",
            "low",
            "--consumes",
            "api:store",
            "--after",
            "store-api",
            "--files",
            "app.py",
            "--check",
            "unit=true",
        )
        self.ok(self.home, "start", "store-api")
        self.ok(self.home, "start", "use-store")
        self.assertIn(
            "contract api:store is changing in store-api; consumers use-store", self.ok(self.home, "check").out
        )
        libwt = self.root / "lib-wt" / "demo" / "store-api"
        self.commit(libwt, {"store.py": "def get(k, default=None):\n    return k\n"}, "additive default")
        self.commit(self.wt("use-store"), {"app.py": "print('uses get(k, default)')\n"}, "use it")
        tuple_check = self.submit(
            self.wt("use-store"),
            "--quest",
            "use-store",
            "--ref",
            f"lib={lib}@quest/demo/store-api",
            "--",
            "python3 -c \"import sys; sys.path.insert(0, '$JOB_REF_LIB'); import store; store.get(1, default=2)\"",
        )
        self.ok(self.home, "wait", tuple_check)
        self.verified("use-store")
        self.fails(self.home, "land", "use-store", match="land store-api first")
        self.verified("store-api")
        self.ok(self.home, "land", "store-api")
        self.ok(self.home, "land", "use-store")
        self.assertIn("default=None", (lib / "store.py").read_text())

    def test_i_campaign_has_no_way_to_rent_machines(self) -> None:
        pattern = re.compile(r"run-instances|start-instances|aws ec2|lease run|--instance-type", re.I)
        offenders = [
            str(f.relative_to(SKILL))
            for f in SKILL.rglob("*")
            # the tests and the design record (docs/) name what 1.x had; the skill and tool must not
            if f.is_file()
            and not {"tests", "docs", ".git"} & set(f.relative_to(SKILL).parts)
            and pattern.search(f.read_text(errors="ignore"))
        ]
        self.assertEqual(offenders, [])
        self.assertFalse((SKILL / "scripts" / "lease.sh").exists())
        commands = self.ok(self.home, "--help").out
        self.assertNotRegex(commands, r"\b(lease|rent|cloud|provision)\b")
        self.assertNotIn("lease", (SKILL / "install.sh").read_text())


if __name__ == "__main__":
    unittest.main()
