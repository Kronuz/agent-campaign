"""One job's run: its checkout, its commands under quiet, its result, and runners that died."""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from .core import (
    BEAT,
    HEAVY,
    HOST,
    KILL_GRACE,
    LIGHT_RUN,
    SKILL,
    STALE,
    UNSTARTED_ATTEMPTS,
    CampaignError,
    Json,
    alive,
    boot_id,
    die,
    git,
    git_ok,
    jobs_dir,
    same_boot,
    stamp,
    write_json,
)
from .jobs import FAILS_WITHOUT, NO_RUN, all_jobs, attempt_dir, campaign_env, finish, load_job, slot_of
from .mirrors import mirror_env, mirror_pins
from .procs import (
    _slot_locks,
    drop_quest_slot,
    kill_group,
    mtime,
    runs,
    slot_lock,
    sweep,
    tracees,
    worktree_add,
    worktree_remove,
)
from .reads import STRACE_ARGS, record_reads, save_reads, tracer
from .repo import _listed, rev

LIGHT_BUILD_JOBS = int(os.environ.get("CAMPAIGN_LIGHT_BUILD_JOBS") or 4)  # per light job; several run at once


def prepare(spec: Json, d: Path) -> Path:
    """The job's sources at its exact commits; the agent's own worktree is never used."""
    if not spec.get("commit"):
        return Path(spec["cwd"])
    repo = Path(spec["repo"])
    if spec.get("slot"):
        slot = jobs_dir() / "slots" / spec["slot"]
        slot_lock(slot, d / "heartbeat")
        if not (slot / ".git").exists():
            shutil.rmtree(slot, ignore_errors=True)
            worktree_add(repo, slot, spec["commit"], d / "heartbeat")
        else:
            git(slot, "checkout", "-q", "--detach", "--force", spec["commit"])
        if rev(slot, "HEAD") != spec["commit"] or git(slot, "status", "--porcelain", "--untracked-files=no"):
            die(f"slot {slot} is not clean at {spec['commit'][:12]}")
        root = slot
    else:
        root = d / "src"
        worktree_remove(str(repo), root)
        worktree_add(repo, root, spec["commit"], d / "heartbeat")
    for name, ref in spec["refs"].items():
        path = ref_path(spec, d, name)
        reset = ["checkout", "-q", "--detach", "--force", ref["commit"]]
        if slot_of(spec) and (path / ".git").exists() and git_ok(path, *reset):
            continue  # a slot's ref keeps its path and its untracked files, reset to the ref
        worktree_remove(ref["repo"], path)
        git_ok(Path(ref["repo"]), "worktree", "prune")  # a slot removed whole leaves its refs registered
        worktree_add(Path(ref["repo"]), path, ref["commit"], d / "heartbeat")
    return root / spec["cwd"]


def ref_path(spec: Json, d: Path, name: str) -> Path:
    """Where a job's --ref NAME is checked out: in its slot, at one path for every job there (a build
    directory kept in the slot configured from it stays valid), else under the job's own directory."""
    slot = slot_of(spec)
    return (jobs_dir() / "slots" / slot if slot else d) / f"ref-{name}"


def release(spec: Json, d: Path) -> None:
    """Drops this runner's slot lock, if it holds one, and the job's own checkouts unless it keeps them
    (a slot's checkouts stay for the next job there)."""
    fh = _slot_locks.pop(spec.get("slot") or "", None)
    if fh is not None:
        fh.close()
    if spec.get("commit") and not spec.get("keep"):
        worktree_remove(spec["repo"], d / "src")
        for name, ref in spec.get("refs", {}).items():
            if not slot_of(spec):
                worktree_remove(ref["repo"], ref_path(spec, d, name))


def quiet_argv() -> list[str]:
    found = shutil.which("quiet")
    return [found] if found else [sys.executable, str(SKILL / "scripts" / "quiet.py")]


def attend(
    argv: list[str],
    cwd: Path,
    env: dict[str, str],
    d: Path,
    *,
    began: Path | None = None,
    ended: Path | None = None,
    minutes: int = 0,
    limit: float,
) -> tuple[int, bool, bool]:
    """Runs one command of a job in its own process group, appending to its log, beating its heart,
    and killing it on cancel, past LIMIT, or MINUTES after BEGAN appears: (exit, cancelled, timed out).
    Once ENDED names the traced command's shell, the helpers it left are stopped: they keep its tracer,
    and so the job, running."""
    beat = d / "heartbeat"
    job = load_job(d)
    with open(attempt_dir(job) / "log", "ab") as out:
        child = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            preexec_fn=os.setpgrp,
        )
    st = job["state"]
    st["pgid"] = child.pid
    write_json(d / "state.json", st)
    cancelled = timed_out = False
    stopping = 0.0
    while True:
        try:
            return child.wait(timeout=BEAT if ended is None else min(BEAT, 1.0)), cancelled, timed_out
        except subprocess.TimeoutExpired:
            pass
        beat.touch()
        shell = ended.read_text().strip() if ended is not None and ended.exists() else ""
        if shell:
            stopping = stopping or time.time()
            sig = signal.SIGTERM if time.time() - stopping < KILL_GRACE else signal.SIGKILL
            for pid in tracees(os.getsid(0)):
                if str(pid) != shell:  # it exits on its own, with the command's code
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.kill(pid, sig)
        if (d / "cancel").exists() and not cancelled:
            cancelled = True
            kill_group(child, beat)
        elif not timed_out and (
            (began is not None and began.exists() and time.time() > began.stat().st_mtime + 60 * minutes)
            or time.time() > limit
        ):
            timed_out = True
            kill_group(child, beat)


def outcome(spec: Json, code: int, cancelled: bool = False, timed_out: bool = False) -> tuple[str, str]:
    """A command's result as the job's (state, note). A --fails-without job passes when its command
    exits nonzero; a timeout, a cancel and a command that did not run never count as failing."""
    if cancelled:
        return "cancelled", ""
    if timed_out or code == 124:
        return "timeout", ""
    if spec.get("fails_without", 0) < FAILS_WITHOUT:
        return ("done" if code == 0 else "failed"), ""
    if code in NO_RUN:
        return "failed", f"exit {code}: the command did not run, so it shows nothing about the fix"
    if code == 0:
        return "failed", "the test passed without the fix: it must fail there"
    return "done", f"the test failed without the fix, as it must (exit {code})"


def admission(d: Path, limit: float) -> str:
    """Waits for the worker to admit a built job to its hold (""), or for its cancel or LIMIT first
    ("cancelled", "timeout"). The `admitted` directory decides a race between them: who makes it wins."""
    gate, beat = d / "admitted", d / "heartbeat"
    while load_job(d)["state"].get("phase") != "run":
        made = mtime(gate)
        if made is not None and time.time() - made > 10:
            return ""  # the worker died between its two writes: it admitted the job
        why = "cancelled" if (d / "cancel").exists() else "timeout" if time.time() > limit else ""
        if why and made is None:
            with contextlib.suppress(FileExistsError):
                gate.mkdir()
                return why
        if time.time() - (mtime(beat) or 0) > BEAT:
            beat.touch()
        time.sleep(0.5)
    return ""


def admit(job: Json) -> bool:
    """Admits a built job (its runner waits in `admission`) to its hold, with the state plan gave it."""
    try:
        (job["dir"] / "admitted").mkdir()
    except FileExistsError:
        return False
    write_json(job["dir"] / "state.json", {**job["state"], "admitted": time.time()})
    return True


def cmd_run(a: argparse.Namespace) -> int:
    """One attempt of one job, supervised by this process, the leader of its own session."""
    d = Path(a.jobdir)
    job = load_job(d)
    spec, st = job["spec"], job["state"]
    me = os.getpid()
    # a staged job's build is light work: its hold, and so its start, comes once the worker admits it
    staged = st.get("phase") == "build"
    began = {"build": {"started": time.time()}} if staged else {"started": time.time()}
    st.update(state="running", runner=me, sid=os.getsid(0), boot=boot_id(), host=HOST, **began)
    write_json(d / "state.json", st)
    job["state"] = st
    beat = d / "heartbeat"
    beat.touch()
    ad = attempt_dir(job)
    shutil.rmtree(ad, ignore_errors=True)
    (ad / "out").mkdir(parents=True)
    if (d / "cancel").exists():
        finish(job, -1, "cancelled", fence=me)
        return 0
    try:
        cwd = prepare(spec, d)
    except CampaignError as e:
        release(spec, d)
        (ad / "exit").write_text("125")
        finish(job, 125, "failed", f"could not prepare sources: {e}", fence=me)
        return 0
    env = dict(os.environ)
    env.pop("CMUX_WORKSPACE_ID", None)  # the worker may have started in another workspace
    env.pop("CMUX_SURFACE_ID", None)  # nor is a job the lead whose surface `note_lead` records
    if spec.get("level") not in HEAVY:  # a light hold's build and test parallelism, where a command names none
        env.setdefault("CMAKE_BUILD_PARALLEL_LEVEL", str(LIGHT_BUILD_JOBS))
        env.setdefault("CTEST_PARALLEL_LEVEL", str(LIGHT_BUILD_JOBS))
    env.setdefault("CTEST_OUTPUT_ON_FAILURE", "1")
    if spec.get("cmux"):
        env["CMUX_WORKSPACE_ID"] = spec["cmux"]  # quiet's pill and notice go to the submitter's
    if spec.get("path"):
        env["PATH"] = spec["path"]  # the toolchain its fingerprint was taken with, whoever started the worker
    env.update(campaign_env(spec))  # over whatever the worker inherited; the job's own --env wins
    env.update(spec["env"])
    env.update(
        JOB_ID=d.name,
        JOB_DIR=str(ad),
        JOB_OUT=str(ad / "out"),
        JOB_SRC=str(cwd),
        JOB_COMMIT=spec.get("commit") or "",
        QUIET_KILL="1",
        QUIET_WHO=f"campaign job {d.name}",
        QUIET_BESIDE=str(ad / "beside"),
    )
    if spec.get("fails_without", 0) >= FAILS_WITHOUT:
        env["QUIET_EXPECT"] = "fail"  # its failing is its passing: no failure notice
    for name in spec["refs"]:
        env[f"JOB_REF_{name.upper()}"] = str(ref_path(spec, d, name))
    env.update(mirror_env(env))
    if spec.get("commit") and env.get("CAMPAIGN_GIT"):
        try:
            mirror_pins(env["CAMPAIGN_GIT"], jobs_dir() / "slots" / spec["slot"] if spec.get("slot") else d / "src")
        except (OSError, CampaignError) as e:  # the job still runs: its own git reaches GitHub
            print(f"campaign: filling git mirrors failed: {e}", file=sys.stderr)
    if spec.get("build"):  # built under light in this checkout, then run at the job's level
        what = f"build for {spec.get('what') or spec['cmd'][:60]}"
        built = [*quiet_argv(), "light", f"job-{d.name}-build", str(LIGHT_RUN), what, "--", "bash", "-c", spec["build"]]
        light_env = {"CMAKE_BUILD_PARALLEL_LEVEL": str(LIGHT_BUILD_JOBS), **env}
        wait_max = float(os.environ.get("CAMPAIGN_WAIT_MAX", str(24 * 3600)))
        since = float(st.get("build", {}).get("started") or time.time())
        code, cancelled, timed_out = attend(built, cwd, light_env, d, limit=time.time() + 60 * LIGHT_RUN + wait_max)
        sweep(os.getsid(0), beat)
        st["build"] = {"started": since, "ended": time.time(), "exit": code}
        if code == 0 and not cancelled and not timed_out and staged:
            st["phase"] = "built"
            write_json(d / "state.json", st)
            why = admission(d, time.time() + wait_max) or ("cancelled" if (d / "cancel").exists() else "")
            st = job["state"] = {**load_job(d)["state"], **({} if why else {"phase": "run", "started": time.time()})}
            write_json(d / "state.json", st)
            cancelled, timed_out = why == "cancelled", why == "timeout"
        if code != 0 or cancelled or timed_out:
            (ad / "exit").write_text(str(code))
            release(spec, d)
            state = "cancelled" if cancelled else "timeout" if timed_out or code == 124 else "failed"
            finish(job, code, state, f"its build failed (exit {code})" if state == "failed" else "", fence=me)
            return 0
    argv = ["bash", "-c", 'date +%s > "$JOB_DIR/began"\n' + spec["cmd"]]
    traced = tracer() if spec.get("commit") and spec["level"] in ("none", "light") and not spec.get("build") else ""
    if traced:  # what it reads decides where its result counts (record_reads)
        # strace waits for every helper the command leaves: the shell says when the command ended
        script = f'date +%s > "$JOB_DIR/began"\n(\n{spec["cmd"]}\n)\ns=$?\necho $$ > "$JOB_DIR/ended"\nexit $s'
        argv = [traced, *STRACE_ARGS, "-o", str(ad / "strace"), "--", "bash", "-c", script]
    if spec["level"] != "none":
        argv = [
            *quiet_argv(),
            spec["level"],
            f"job-{d.name}",
            str(spec["minutes"]),
            spec.get("what") or spec["cmd"][:60],
            "--",
            *argv,
        ]
    limit = time.time() + 60 * spec["minutes"] + float(os.environ.get("CAMPAIGN_WAIT_MAX", str(24 * 3600)))
    began = ad / "began" if spec["level"] == "none" else None
    ended = ad / "ended" if traced else None
    code, cancelled, timed_out = attend(
        argv, cwd, env, d, began=began, ended=ended, minutes=spec["minutes"], limit=limit
    )
    sweep(os.getsid(0), beat)
    (ad / "exit").write_text(str(code))
    state, note = outcome(spec, code, cancelled, timed_out)
    reads: Json | None = None
    if traced and state == "done":
        root = jobs_dir() / "slots" / spec["slot"] if spec.get("slot") else d / "src"
        try:
            reads = record_reads(spec, root, cwd, ad / "strace")
        except Exception as e:  # recorded: the job keeps whole-tree identity
            reads = {"complete": False, "why": [f"recording its reads failed: {e}"], "reads": {}}
    (ad / "strace").unlink(missing_ok=True)
    release(spec, d)
    if finish(job, code, state, note, fence=me) and reads is not None:
        save_reads(job, reads)
    with contextlib.suppress(CampaignError, OSError):
        drop_quest_slot(spec)
    return 0


def reap() -> None:
    """Finds jobs whose runner died (crash, kill, reboot): records, requeues or interrupts them."""
    for job in all_jobs():
        d, st, spec = job["dir"], job["state"], job["spec"]
        if st.get("state") != "running":
            continue
        beat = d / "heartbeat"
        last = mtime(beat) or 0.0
        if time.time() - last < STALE:
            continue
        lock = d / "reaping"
        try:
            lock.mkdir()
        except FileExistsError:
            held = mtime(lock)
            if held is None or time.time() - held < 60:
                continue  # another reaper holds it, or just finished
        _listed.clear()  # the job may change
        try:
            if load_job(d)["state"] != st:
                continue
            booted_here = same_boot(st.get("boot"))
            sid = int(st.get("sid") or 0)
            # the runner leads its session; a live leader that is not this job's runner reused its id
            if sid and booted_here and (not alive(sid) or runs(sid, f"_run {d}")):
                sweep(sid)
            exit_file = attempt_dir(job) / "exit"
            if exit_file.exists() and booted_here:
                code = int(exit_file.read_text().strip() or "1")
                state, note = outcome(spec, code)
                finish(job, code, state, "; ".join(filter(None, [note, "recorded after its runner died"])))
                continue
            release(spec, d)
            attempt = int(st.get("attempt", 1))
            unstarted = not (attempt_dir(job) / "began").exists()  # its command never ran: safe to repeat
            if attempt <= int(spec.get("retries", 0)) or (unstarted and attempt < UNSTARTED_ATTEMPTS):
                shutil.rmtree(d / "claim", ignore_errors=True)
                shutil.rmtree(d / "admitted", ignore_errors=True)
                why = "lost its runner before its command started" if unstarted else "lost its runner"
                write_json(
                    d / "state.json",
                    {
                        "state": "queued",
                        "attempt": attempt + 1,
                        "note": f"requeued at {stamp()}: attempt {attempt} {why}",
                    },
                )
            else:
                finish(
                    job,
                    -1,
                    "interrupted",
                    f"attempt {attempt} lost its runner"
                    + (" before its command started" if unstarted else "" if booted_here else " (the host rebooted)")
                    + "; not retried",
                )
        finally:
            shutil.rmtree(lock, ignore_errors=True)
