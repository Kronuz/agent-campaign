"""Job identity and reuse: read sets, false reuse and what makes two runs the same."""

from __future__ import annotations

import json
import os
import shutil
import unittest
from pathlib import Path

from harness import CampaignCase, campaign_module


HAS_CMAKE = all(map(shutil.which, ("cmake", "ninja", "cc")))
CM = "cmake_minimum_required(VERSION 3.16)\nproject(p C)\nenable_testing()\n"
BUILD = "cmake -S . -B build -G Ninja >/dev/null && cmake --build build >/dev/null"
CXX = BUILD + " && ctest --test-dir build"
T_OK = "int main(void) { return 0; }\n"
FAKE_STRACE = """#!/bin/sh
while [ "$1" != "--" ]; do [ "$1" = "-o" ] && { shift; out=$1; }; shift; done
shift
"$@"
code=$?
case "$(cat "$STRACE_MODE")" in
  kill) echo "1 +++ killed by SIGKILL +++" > "$out" ;;
  ok) printf '%s\\n' '1 openat(AT_FDCWD, "app.py", O_RDONLY) = 3' '1 newfstatat(AT_FDCWD, "link.txt", 0x1, 0) = 0' \\
    '1 openat(AT_FDCWD, "/elsewhere/x", O_RDONLY) = 3' '1 +++ exited with 0 +++' > "$out" ;;
esac
exit $code
"""


class ReadSets(CampaignCase):
    """A job traced whole counts at any commit where every tracked file it read, and every path name,
    is unchanged; any other job keeps whole-tree identity."""

    def test_a_clean_trace_is_the_only_read_set(self) -> None:
        fake = self.root / "bin" / "fake-strace"
        fake.write_text(FAKE_STRACE)
        fake.chmod(0o755)
        mode = self.root / "strace-mode"
        self.env.update(CAMPAIGN_STRACE=str(fake), STRACE_MODE=str(mode))
        wt = self.started("rs", files="**")
        files = {"app.py": "print(1)\n", "real/v.txt": "ok\n", "other.txt": "unread\n", "tools/run.sh": "true\n"}
        self.commit(wt, files)
        os.symlink("real/v.txt", wt / "link.txt")
        self.git(wt, "add", "link.txt")
        self.commit(wt, {})

        def run(name: str) -> tuple[str, bool]:
            mode.write_text(name)
            out = self.ok(wt, "job", "submit", "--quest", "rs", "--sync", "--fresh", "--", f"sh tools/run.sh {name}")
            return out.out.split()[0], "(reused" in out.out

        jobs = {name: run(name)[0] for name in ("kill", "none", "ok")}
        reads = {n: json.loads((self.root / "jobs" / j / "reads.json").read_text()) for n, j in jobs.items()}
        self.assertEqual(reads["kill"]["why"], ["the trace ends before every process exits"])
        self.assertEqual(reads["none"]["why"], ["the trace is missing"])
        self.assertEqual(sorted(reads["ok"]["reads"]), ["**", "app.py", "link.txt", "real/v.txt"])
        self.assertTrue(reads["ok"]["complete"])
        self.verified("rs")
        self.commit(wt, {"other.txt": "unread, edited\n"})
        self.assertEqual(run("ok"), (jobs["ok"], True))
        self.assertEqual([run("kill")[1], run("none")[1]], [False, False])
        self.assertIn("check unit is not green", self.ok(self.home, "brief", "rs").out)  # the build-test is exact
        changes: list[dict[str, str]] = [{"real/v.txt": "no\n"}, {"new.txt": "a new path\n"}, {}]
        for change in changes:
            if not change:
                self.git(wt, "update-index", "--chmod=+x", "app.py")
            self.commit(wt, change)
            self.assertFalse(run("ok")[1], change or "a mode")

    def test_a_strace_log_resolves_paths_by_each_process_directory(self) -> None:
        log = self.root / "trace"
        log.write_text(
            '10 chdir("sub") = 0\n'
            "10 clone(child_stack=NULL, flags=SIGCHLD) = 11\n"
            '11 openat(AT_FDCWD, "a.h", O_RDONLY <unfinished ...>\n'
            '10 openat(4</r/other>, "b.c", O_RDONLY) = 5</r/other/b.c>\n'
            "11 <... openat resumed>) = 3</r/sub/a.h>\n"
            '11 newfstatat(AT_FDCWD, "/r/gone.h", 0x7ff, 0) = -1 ENOENT (No such file or directory)\n'
            '11 execve("/r/tool", ["tool"], 0x0 /* 3 vars */) = 0\n'
            "11 +++ exited with 0 +++\n"
            "10 +++ exited with 0 +++\n"
        )
        paths, problem = campaign_module().strace_paths(log, "/r")
        self.assertEqual((paths, problem), (["/r/sub", "/r/other/b.c", "/r/sub/a.h", "/r/gone.h", "/r/tool"], ""))

    def test_a_child_starts_in_its_parents_directory(self) -> None:
        log = self.root / "trace"
        log.write_text(
            '10 chdir("sub") = 0\n'
            "10 clone(child_stack=NULL, flags=SIGCHLD <unfinished ...>\n"
            '11 chdir("deeper") = 0\n'
            '11 openat(AT_FDCWD, "a.h", O_RDONLY) = 3</r/sub/deeper/a.h>\n'
            "10 <... clone resumed>) = 11\n"
            "11 +++ exited with 0 +++\n"
            '10 chdir("/r/x") = 0\n'
            "10 clone(child_stack=NULL, flags=SIGCHLD) = 11\n"  # a reused pid starts where its parent is
            '11 openat(AT_FDCWD, "b.h", O_RDONLY) = 3</r/x/b.h>\n'
            "11 +++ exited with 0 +++\n"
            "10 +++ exited with 0 +++\n"
        )
        paths, problem = campaign_module().strace_paths(log, "/r")
        self.assertEqual((paths[2], paths[-1], problem), ("/r/sub/deeper/a.h", "/r/x/b.h", ""))
        log.write_text('10 openat(AT_FDCWD, "a.h", O_RDONLY) = 3</r/a.h>\n11 openat(AT_FDCWD, "b.h", O_RDONLY) = 3\n')
        self.assertEqual(
            campaign_module().strace_paths(log, "/r")[1], "a process's directory is one the trace cannot tell"
        )

    def problem(self, log: str) -> str:
        (self.root / "trace").write_text(log + "10 +++ exited with 0 +++\n")
        return str(campaign_module().strace_paths(self.root / "trace", "/r")[1])

    def test_a_trace_through_git_is_not_whole(self) -> None:
        git = '10 openat(AT_FDCWD, "/home/repo/.git/objects/pack/pack-1.idx", O_RDONLY) = 3\n'
        self.assertEqual(self.problem(git), "a process read git's object store, which a read set does not key")

    def test_a_chdir_threads_share_is_not_whole(self) -> None:
        clone = "10 clone3({flags=CLONE_VM|CLONE_FS|CLONE_THREAD, exit_signal=0}, 88) = 13\n"
        opened = '13 openat(AT_FDCWD, "data.txt", O_RDONLY) = 3\n13 +++ exited with 0 +++\n'
        self.assertEqual(
            self.problem(clone + '10 chdir("sub") = 0\n' + opened), "a process's directory is one the trace cannot tell"
        )
        self.assertEqual(self.problem('10 chdir("sub") = 0\n' + clone + opened), "")  # set before the thread starts

    def test_a_pid_reused_after_an_early_exit_starts_in_its_parents_directory(self) -> None:
        log = "10 clone(flags=SIGCHLD <unfinished ...>\n11 +++ exited with 0 +++\n10 <... clone resumed>) = 11\n"
        log += '10 chdir("/r/sub") = 0\n10 clone(flags=SIGCHLD) = 11\n11 openat(AT_FDCWD, "a.h", O_RDONLY) = 3\n'
        (self.root / "trace").write_text(log + "11 +++ exited with 0 +++\n10 +++ exited with 0 +++\n")
        self.assertEqual(campaign_module().strace_paths(self.root / "trace", "/r"), (["/r/sub", "/r/sub/a.h"], ""))

    def test_a_trace_counts_only_when_every_process_exits(self) -> None:
        self.assertEqual(
            self.problem("10 clone(child_stack=NULL, flags=SIGCHLD) = 11\n"),
            "the trace ends before every process exits",
        )
        self.assertEqual(self.problem("10 clone(child_stack=NULL, flags=SIGCHLD) = 11\n11 +++ exited with 0 +++\n"), "")


class FalseReuse(CampaignCase):
    """Each case passes, commits a change that breaks the same command, and checks the job reruns there:
    under strace, or with whole-tree identity where there is none."""

    checkout: Path

    def assert_reruns(self, files: dict[str, str], cmd: str, broken: dict[str, str], *extra: str) -> None:
        self.checkout = self.started("rs", files="**")
        self.commit(self.checkout, files)
        self.ok(self.checkout, "job", "submit", "--quest", "rs", "--sync", *extra, "--", cmd)
        self.commit(self.checkout, broken)
        r = self.run_cli(self.checkout, "job", "submit", "--quest", "rs", "--sync", *extra, "--", cmd)
        self.assertNotIn("(reused", r.out)
        self.assertNotEqual(r.code, 0, "the change breaks the command")

    def test_a_file_only_looked_up(self) -> None:
        files = {"conf/a": "x\n", "app.py": "import os, sys\nsys.exit(os.path.exists('conf/off'))\n"}
        self.assert_reruns(files, "python3 app.py", {"conf/off": "1\n"}, "--fresh")

    def test_a_python_child_without_site(self) -> None:
        child = "import subprocess, sys\nsys.exit(subprocess.run([sys.executable, '-I', 'tools/c.py']).returncode)\n"
        files = {"tools/c.py": "raise SystemExit(0)\n", "app.py": child}
        self.assert_reruns(files, "python3 app.py", {"tools/c.py": "raise SystemExit(1)\n"}, "--fresh")

    def test_a_quoted_command_substitution(self) -> None:
        files = {"app.py": "print(1)\n", "tools/v.sh": "cat data/v.txt\n", "data/v.txt": "ok\n"}
        self.assert_reruns(files, 'python3 app.py && test "$(sh tools/v.sh)" = ok', {"data/v.txt": "no\n"}, "--fresh")

    def test_a_python_with_its_own_pythonpath(self) -> None:
        b = "import sys\nsys.exit(open('data/v.txt').read() != 'ok\\n')\n"
        files = {"a.py": "print(1)\n", "b.py": b, "data/v.txt": "ok\n", "lib/x.py": "\n"}
        self.assert_reruns(files, "python3 a.py && PYTHONPATH=lib python3 b.py", {"data/v.txt": "no\n"}, "--fresh")

    def test_a_file_c_code_opens(self) -> None:
        app = "import sqlite3, sys\nc = sqlite3.connect('data/f.db')\nsys.exit(c.execute('select 1 from t').fetchone() is None)\n"
        make = "import sqlite3\nsqlite3.connect('data/f.db').executescript('create table t (v); insert into t values (1);')\n"
        files = {"app.py": app, "tools/make.py": make, "data/.keep": ""}
        self.assert_reruns(files, "python3 tools/make.py && python3 app.py", {"tools/make.py": "\n"}, "--fresh")

    def test_a_plain_make(self) -> None:
        files = {"tools/check.mk": "all:\n\tgrep -q ok data/v.txt\n", "data/v.txt": "ok\n"}
        self.assert_reruns(files, "make -s -f tools/check.mk", {"data/v.txt": "no\n"}, "--fresh")

    @unittest.skipUnless(HAS_CMAKE, "needs cmake, ninja and a C compiler")
    def test_a_post_build_command_under_ninja(self) -> None:
        post = "add_custom_command(TARGET t POST_BUILD COMMAND sh ${CMAKE_SOURCE_DIR}/scripts/post.sh)\n"
        files = {"CMakeLists.txt": CM + "add_executable(t tests/t.c)\n" + post, "tests/t.c": T_OK}
        files["scripts/post.sh"] = "exit 0\n"
        self.assert_reruns(files, BUILD, {"scripts/post.sh": "exit 1\n"}, "--fresh")

    @unittest.skipUnless(HAS_CMAKE, "needs cmake, ninja and a C compiler")
    def test_a_compiled_test_reads_data(self) -> None:
        cm = CM + "add_executable(t tests/t.c)\nadd_test(NAME t COMMAND t WORKING_DIRECTORY ${CMAKE_SOURCE_DIR})\n"
        t = '#include <stdio.h>\nint main(void) { FILE *f = fopen("data/v.txt", "r"); return !f || fgetc(f) != \'o\'; }\n'
        files = {"CMakeLists.txt": cm, "tests/t.c": t, "data/v.txt": "ok\n"}
        self.assert_reruns(files, CXX, {"data/v.txt": "no\n"}, "--slot", "p-data")

    @unittest.skipUnless(HAS_CMAKE, "needs cmake, ninja and a C compiler")
    def test_a_new_source_a_glob_finds(self) -> None:
        cm = CM + "file(GLOB SRC src/*.c)\nadd_executable(t tests/t.c ${SRC})\n"
        files = {"CMakeLists.txt": cm, "src/lib.c": "int v(void) { return 1; }\n", "tests/t.c": T_OK}
        self.assert_reruns(files, BUILD, {"src/bad.c": "#error broken\n"}, "--slot", "p-glob")


class Identity(CampaignCase):
    def test_submit_warns_about_inputs_outside_the_jobs_checkout(self) -> None:
        wt = self.started("fix", files="app.py")
        first = self.submit(wt, "--quest", "fix", "--", "true")
        other = self.root / "jobs" / first / "a1/out/oracle"
        r = self.ok(
            wt, "job", "submit", "--quest", "fix", "--", f"{other} {self.home}/.scratch/expected.bin x=/usr/bin/env"
        )
        warnings = "\n".join(x for x in r.err.splitlines() if x.startswith("warning:"))
        self.assertIn(f"warning: {other}, {self.home}/.scratch/expected.bin are outside this job's own checkout", r.err)
        self.assertNotIn("/usr/bin/env", warnings)  # a system path is not the job's code
        r = self.ok(wt, "job", "submit", "--quest", "fix", "--", 'python3 app.py --out "$JOB_OUT/x"')
        self.assertEqual([x for x in r.err.splitlines() if x.startswith(("warning:", "note:"))], [])

    def test_an_evidence_jobs_script_outside_the_checkout_is_part_of_its_identity(self) -> None:
        wt = self.started("fix", files="app.py")
        probe = self.home / ".scratch" / "probe.sh"
        probe.parent.mkdir()
        probe.write_text("echo one\n")
        first = self.submit(wt, "--quest", "fix", "--sync", "--", f"sh {probe}")
        r = self.ok(wt, "job", "submit", "--quest", "fix", "--", f"sh {probe}")
        self.assertEqual(r.out.split()[0], first)
        self.assertIn(f"warning: {probe} is outside this job's own checkout", r.err)
        probe.write_text("echo two\n")
        r = self.ok(wt, "job", "submit", "--quest", "fix", "--sync", "--", f"sh {probe}")
        second = r.out.split()[0]
        self.assertNotEqual(second, first, "a changed script reused the old script's result")
        self.assertIn(f"note: the bytes of {probe} are part of this evidence job's identity", r.err)
        self.assertIn("two", (self.root / "jobs" / second / "a1" / "log").read_text())
        probe.write_text("echo three\n")
        out = self.ok(self.home, "job", "rerun", second).out
        self.assertNotIn("reused", out, "a rerun kept the script digest it was submitted with")

    def test_explicit_head_uses_the_submitting_task_worktree(self) -> None:
        self.started("fix", files="app.py,lib/**")
        self.ok(self.home, "start", "fix", "--task", "index", "--files", "lib/**")
        wt = self.wt("fix--index")
        head = self.commit(wt, {"lib/util.py": "print('task')\n"})
        jid = self.submit(wt / "lib", "--quest", "fix", "--commit", "HEAD", "--", 'python3 util.py > "$JOB_OUT/seen"')
        self.ok(wt, "wait", jid)
        spec = json.loads((self.root / "jobs" / jid / "spec.json").read_text())
        self.assertEqual(spec["commit"], head)
        self.assertEqual(spec["cwd"], "lib")
        self.assertEqual((self.root / "jobs" / jid / "a1/out/seen").read_text(), "task\n")

    def test_a_job_runs_the_submitted_commit_while_the_branch_moves_on(self) -> None:
        wt = self.started("fix", files="app.py")
        first = self.commit(wt, {"app.py": "print('first')\n"})
        jid = self.submit(wt, "--what", "slow read", "--", 'sleep 1; python3 app.py > "$JOB_OUT/seen"')
        self.commit(wt, {"app.py": "print('second')\n"})  # the contributor keeps working
        (wt / "app.py").write_text("print('uncommitted')\n")
        self.ok(wt, "wait", jid, "--timeout", "60")
        seen = (self.root / "jobs" / jid / "a1" / "out" / "seen").read_text().strip()
        self.assertEqual(seen, "first")
        self.assertEqual(json.loads((self.root / "jobs" / jid / "spec.json").read_text())["commit"], first)

    def test_a_job_runs_with_its_campaigns_env_and_its_own_env_wins(self) -> None:
        self.env["CMAKE_GENERATOR"] = "Unix Makefiles"  # what the worker happened to inherit
        show = 'echo "$CMAKE_GENERATOR ${DEMO:-unset}" > "$JOB_OUT/env"'
        queued = self.submit(self.home, "--after", self.submit(self.home, "--", "sleep 1"), "--", show)
        self.ok(self.home, "config", "env=CMAKE_GENERATOR=Ninja,DEMO=1")
        mine = self.submit(self.home, "--env", "DEMO=2", "--", show)
        self.ok(self.home, "wait", queued, mine, "--timeout", "60")
        seen = {j: (self.root / "jobs" / j / "a1" / "out" / "env").read_text().split() for j in (queued, mine)}
        self.assertEqual(seen[queued], ["Ninja", "1"], "a job queued before the setting runs with it")
        self.assertEqual(seen[mine], ["Ninja", "2"], "a job's own --env wins")
        self.assertEqual(self.ok(self.home, "config", "env").out.strip(), "CMAKE_GENERATOR=Ninja,DEMO=1")

    def test_a_light_job_builds_with_a_light_holds_parallelism(self) -> None:
        self.env["CMUX_SURFACE_ID"] = "surface-lead"
        show = 'echo "${CMAKE_BUILD_PARALLEL_LEVEL:-unset} ${CMUX_SURFACE_ID:-none}" > "$JOB_OUT/env"'
        light = self.submit(self.home, "--level", "light", "--", show)
        mine = self.submit(self.home, "--level", "light", "--env", "CMAKE_BUILD_PARALLEL_LEVEL=2", "--", show)
        strict = self.submit(self.home, "--level", "strict", "--", show)
        self.ok(self.home, "wait", light, mine, strict, "--timeout", "60")
        seen = {j: (self.root / "jobs" / j / "a1" / "out" / "env").read_text().split() for j in (light, mine, strict)}
        self.assertEqual(seen[light], ["4", "none"], "a job is never the lead's surface")
        self.assertEqual(seen[mine], ["2", "none"], "a job's own --env wins")
        self.assertEqual(seen[strict], ["unset", "none"], "a strict job has the machine's cores to itself")
        self.ok(
            self.home,
            "new",
            "big",
            "--title",
            "big",
            "--risk",
            "low",
            "--files",
            "app.py",
            "--check",
            "unit=cmake --build b -j8 && ctest -j3",
        )
        self.assertIn("big: light check unit asks for -j8", self.ok(self.home, "check").out)

    def test_a_ctest_run_stops_at_its_first_failure_where_the_job_can_say_so(self) -> None:
        """Jobs get ctest's output on failure and a light hold's test parallelism; ctest has no variable
        for stopping at the first failure, so submit and check warn of a ctest run without the flag."""
        show = 'echo "${CTEST_PARALLEL_LEVEL:-unset} ${CTEST_OUTPUT_ON_FAILURE:-unset}" > "$JOB_OUT/env"'
        light = self.submit(self.home, "--level", "light", "--", show)
        strict = self.submit(self.home, "--level", "strict", "--", show)
        self.ok(self.home, "wait", light, strict, "--timeout", "60")
        seen = {j: (self.root / "jobs" / j / "a1" / "out" / "env").read_text().split() for j in (light, strict)}
        self.assertEqual(seen, {light: ["4", "1"], strict: ["unset", "1"]})
        warned = self.ok(self.home, "job", "submit", "--level", "none", "--", "ctest --test-dir b -j4").err
        self.assertIn("warning: this job runs ctest without --stop-on-failure", warned)
        quiet = self.ok(self.home, "job", "submit", "--level", "none", "--", "ctest --test-dir b --stop-on-failure").err
        self.assertNotIn("--stop-on-failure", quiet.replace("ctest --test-dir b --stop-on-failure", ""))
        self.ok(self.home, "new", "t", "--title", "t", "--risk", "low", "--check", "unit=make && ctest --test-dir b")
        self.assertIn("t: check unit runs ctest without --stop-on-failure", self.ok(self.home, "check").out)

    def test_a_rerun_chain_keeps_its_order(self) -> None:
        first = self.submit(self.home, "--", "true first")
        second = self.submit(self.home, "--after", first, "--", "true second")  # not the first's twin
        self.ok(self.home, "wait", second)
        (self.home / "more.py").write_text("print(2)\n")
        self.git(self.home, "add", "more.py")
        self.git(self.home, "commit", "-qm", "more")
        lines = self.ok(self.home, "job", "rerun", second, first).out.splitlines()
        new = {line.split(": ")[1].split()[0]: line.split()[0].rstrip(":") for line in lines if ": " in line}
        new_first, new_second = new[first], new[second]
        self.assertNotEqual(new_first, first)
        spec = json.loads((self.root / "jobs" / new_second / "spec.json").read_text())
        self.assertEqual(spec["after"], [new_first])

    def test_a_timed_out_job_reruns_with_more_minutes(self) -> None:
        timed = self.submit(self.home, "--minutes", "1", "--", "exit 124")  # 124: the job's timeout
        self.run_cli(self.home, "wait", timed)
        self.assertEqual(self.job(timed)["state"], "timeout")
        new = self.ok(self.home, "job", "rerun", timed, "--minutes", "3").out.split()[0].rstrip(":")
        self.assertNotEqual(new, timed)
        self.assertEqual(json.loads((self.root / "jobs" / new / "spec.json").read_text())["minutes"], 3)
        ab = self.submit(self.home, "--level", "exclusive", "--minutes", "1", "--", "exit 124")
        self.fails(self.home, "job", "rerun", ab, "--minutes", "11", match="lasts at most 10 minutes")

    def test_a_rerun_keeps_the_criteria_its_job_proved(self) -> None:
        wt = self.started("fix", files="app.py")
        brief = self.home / ".campaign/demo/quests/fix.md"
        brief.write_text(brief.read_text().replace("None in this test.", "- A1 prints two: a probe"))
        self.git(self.home, "commit", "-qam", "criteria")
        jid = self.submit(wt, "--quest", "fix", "--for", "A1", "--", "cat app.py")  # reads what changes next
        self.ok(self.home, "wait", jid)
        self.commit(wt, {"app.py": "print(2)\n"})
        new = self.ok(self.home, "job", "rerun", jid).out.split()[0].rstrip(":")
        self.assertNotEqual(new, jid)
        tags = json.loads((self.root / "jobs" / new / "for.json").read_text())["tags"]
        self.assertEqual([t["for"] for t in tags], [["A1"]])

    def test_a_rerun_keeps_a_fails_without_job_at_the_code_it_names(self) -> None:
        """A --fails-without job must fail without the fix; at the quest's head it would run the fix."""
        wt = self.started("fix", files="app.py")
        brief = self.home / ".campaign/demo/quests/fix.md"
        brief.write_text(brief.read_text().replace("None in this test.", "- A1 prints two: a probe"))
        self.git(self.home, "commit", "-qam", "criteria")
        before = self.git(wt, "rev-parse", "HEAD")
        self.commit(wt, {"app.py": "print(2)\n"})
        test = "python3 app.py | grep -q 2"
        jid = self.submit(wt, "--quest", "fix", "--for", "A1", "--fails-without", "--commit", before, "--", test)
        self.run_cli(self.home, "wait", jid)  # it fails at the code without the fix, as it must
        self.fails(self.home, "job", "rerun", jid, match=f"{jid} must fail at the code without the fix")
        self.assertIn(f"{jid} at {before[:12]}", self.ok(self.home, "job", "rerun", jid, "--commit", before).out)

    def test_a_docs_folder_counts_as_named_only_as_a_whole_argument(self) -> None:
        doc_path = campaign_module().doc_path
        self.assertTrue(doc_path("docs/design/results.md", "docs/design/bench/run.sh row 1000"))
        self.assertTrue(doc_path("docs/perf/notes.md", "docs/perf/run.sh measure"))
        self.assertFalse(doc_path("docs/design/results.md", "pytest docs/design"))
        self.assertFalse(doc_path("docs/design/results.md", "check docs/"))
        self.assertFalse(doc_path("docs/design/results.md", "cat docs/design/results.md"))

    def test_a_rerun_never_moves_a_commit_the_quests_branch_never_held(self) -> None:
        """A mutant (a commit only a scratch ref holds) is refused without --commit and kept with it; a
        commit the branch held before it was rewritten still moves to the head."""
        wt = self.started("fix", files="app.py")
        self.git(wt, "checkout", "-q", "--detach")
        mutant = self.commit(wt, {"app.py": "print('mutant')\n"}, "mutant")
        self.git(wt, "update-ref", "refs/scratch/mutant", mutant)
        self.git(wt, "checkout", "-q", "quest/demo/fix")
        jid = self.submit(wt, "--quest", "fix", "--commit", mutant, "--", 'python3 app.py > "$JOB_OUT/seen"')
        self.ok(self.home, "wait", jid)
        self.commit(wt, {"app.py": "print(2)\n"})
        r = self.fails(self.home, "job", "rerun", jid, match=f"{jid} ran at {mutant[:12]}, which was never on ")
        self.assertIn(f"Pass --commit {mutant[:12]} to keep it", r.text)
        self.assertIn(f"{jid} at {mutant[:12]}", self.ok(self.home, "job", "rerun", jid, "--commit", mutant).out)
        probe = self.submit(wt, "--quest", "fix", "--", "python3 app.py")
        self.ok(self.home, "wait", probe)
        (wt / "app.py").write_text("print(3)\n")
        self.git(wt, "commit", "-q", "--amend", "-am", "rewritten")
        out = self.ok(self.home, "job", "rerun", probe).out
        self.assertIn(f"{probe} at {self.git(wt, 'rev-parse', 'HEAD')[:12]}", out)

    def test_identical_jobs_are_reused_and_force_reruns(self) -> None:
        jid = self.submit(self.home, "--", "true")
        self.ok(self.home, "wait", jid)
        again = self.ok(self.home, "job", "submit", "--", "true").out
        self.assertEqual(again.split()[0], jid)
        self.assertIn("reused", again)
        forced = self.submit(self.home, "--force", "--", "true")
        self.assertNotEqual(forced, jid)

    def test_a_different_toolchain_is_a_different_identity(self) -> None:
        jid = self.submit(self.home, "--", "true")
        self.ok(self.home, "wait", jid)
        self.env["CAMPAIGN_FINGERPRINT"] = "echo compiler-2"
        self.assertNotEqual(self.submit(self.home, "--", "true"), jid)

    def test_refs_check_out_other_commits_and_repositories(self) -> None:
        lib = self.repo("lib", {"version": "1\n"})
        old = self.git(lib, "rev-parse", "HEAD")
        self.commit(lib, {"version": "2\n"})
        base = self.git(self.home, "rev-parse", "HEAD")
        self.commit(self.home, {"README.md": "changed\n"})
        jid = self.submit(
            self.home,
            "--ref",
            f"base={base}",
            "--ref",
            f"lib={lib}@{old}",
            "--",
            'cat "$JOB_REF_BASE/README.md" "$JOB_REF_LIB/version" > "$JOB_OUT/seen"',
        )
        self.ok(self.home, "wait", jid)
        self.assertEqual((self.root / "jobs" / jid / "a1" / "out" / "seen").read_text(), "app\n1\n")

    def test_sync_runs_without_a_worker_and_records_the_same_result(self) -> None:
        r = self.ok(self.home, "job", "submit", "--sync", "--", "echo hi")
        jid = r.out.split()[0]
        self.assertEqual(self.job(jid)["state"], "done")
        self.assertIn("hi", (self.root / "jobs" / jid / "a1" / "log").read_text())
        failing = self.run_cli(self.home, "job", "submit", "--sync", "--", "exit 4")
        self.assertEqual(failing.code, 1)

    def test_a_job_runs_with_its_submitters_path(self) -> None:
        """The worker may have been started by another session; the job runs with the PATH its toolchain
        fingerprint was taken with, the submitter's."""
        first = self.submit(self.home, "--", "sleep 4")  # starts a worker with this PATH
        tools = self.root / "tools"
        tools.mkdir()
        (tools / "greet").write_text("#!/bin/sh\necho hi\n")
        (tools / "greet").chmod(0o755)
        self.env["PATH"] = f"{tools}:{self.env['PATH']}"
        second = self.submit(self.home, "--", "greet")
        self.ok(self.home, "wait", first, second, "--timeout", "60")
        self.assertEqual(self.job(second)["state"], "done")
