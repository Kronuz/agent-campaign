"""Waiting: sleeping until a quest branch moves, a job ends or a message arrives."""

from __future__ import annotations

import argparse
import contextlib
import os
import select as os_select
import sys
import time
from pathlib import Path
from typing import Any

from .campaign import Campaign, find_campaign
from .core import FLIGHT, TERMINAL, Json, git, jobs_dir, read_json, write_json
from .jobs import all_jobs, attempt_dir, job_summary, load_job, resolve_job
from .journals import journal_state
from .leads import watch_leads
from .mail import fetch_peers, inbox, mailbox, peers, push_outbox
from .repo import common_dir, frozen, has_ref, rev
from .runner import ensure_worker, reap
from .state import branches


def branch_heads(c: Campaign) -> dict[tuple[str, str], str]:
    """(name, branch) -> head, for every branch of the quests in flight."""
    heads: dict[tuple[str, str], str] = {}
    for pid, p in c.quests.items():
        if p["status"] in FLIGHT:
            repo = c.repo(p)
            for name, branch, _, _ in branches(c, pid):
                if has_ref(repo, branch):
                    heads[(name, branch)] = rev(repo, branch)
    return heads


def watch(paths: list[Path], timeout: float) -> None:
    """Sleeps up to TIMEOUT seconds, waking as soon as an entry of one of the directories PATHS is
    created, replaced or removed: kqueue on macOS and the BSDs, inotify on Linux, else a plain sleep."""
    dirs = list(dict.fromkeys(p for p in paths if p.is_dir()))
    fds: list[int] = []
    kq: Any = None
    try:
        if dirs and sys.platform != "linux" and sys.platform != "win32" and hasattr(os_select, "kqueue"):
            kq = os_select.kqueue()
            changes = []
            for d in dirs:
                with contextlib.suppress(OSError):
                    fd = os.open(d, getattr(os, "O_EVTONLY", os.O_RDONLY))
                    fds.append(fd)
                    note = os_select.KQ_NOTE_WRITE | os_select.KQ_NOTE_DELETE | os_select.KQ_NOTE_RENAME
                    changes.append(
                        os_select.kevent(
                            fd, os_select.KQ_FILTER_VNODE, os_select.KQ_EV_ADD | os_select.KQ_EV_CLEAR, note
                        )
                    )
            kq.control(changes, 1, timeout)
            return
        if dirs and sys.platform == "linux":
            import ctypes  # noqa: PLC0415  # Linux only

            libc = ctypes.CDLL(None, use_errno=True)
            fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
            if fd >= 0:
                fds.append(fd)
                # IN_CLOSE_WRITE | IN_MOVED_FROM | IN_MOVED_TO | IN_CREATE | IN_DELETE
                if sum(
                    libc.inotify_add_watch(fd, os.fsencode(d), 0x8 | 0x40 | 0x80 | 0x100 | 0x200) >= 0 for d in dirs
                ):
                    os_select.select([fd], (), (), timeout)
                    return
    except OSError:
        pass
    finally:
        if kq is not None:
            kq.close()
        for fd in fds:
            with contextlib.suppress(OSError):
                os.close(fd)
    time.sleep(timeout)


def wait_events(c: Campaign, timeout: float) -> int:
    """Returns at the first commit on a quest branch in flight or job of this campaign that ended
    since the lead last saw them (recorded in the home's .git), so nothing that happens
    between two waits is missed; then lists what is still in flight, to wait for next."""
    seen_file = Path(git(c.home, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "campaign-seen.json"
    records: Json = (read_json(seen_file) if seen_file.exists() else None) or {}
    recorded: Json | None = records.get(c.ident)

    def remember(heads: dict[str, str], ended: dict[str, Json]) -> None:
        records[c.ident] = {"heads": heads, "jobs": sorted(ended), "messages": sorted(messages())}
        write_json(seen_file, records)

    def messages() -> dict[str, Json]:
        open_quests = [pid for pid, q in c.quests.items() if q["status"] not in ("landed", "dropped")]
        return {f"{pid or c.name + ':'} {m['id']}": m for pid in ["", *open_quests] for m in inbox(c, pid)}

    exchanged = [0.0]

    def exchange() -> None:  # the peers' outboxes, and this one's, about once a minute
        if peers(c) and time.time() - exchanged[0] >= float(os.environ.get("CAMPAIGN_PEER_SECONDS", "60")):
            fetch_peers(c)
            push_outbox(c)
            exchanged[0] = time.time()

    tick = float(os.environ.get("CAMPAIGN_WAIT_TICK", "5"))
    start = time.time()

    mine: list[Json] = []

    def current() -> tuple[dict[str, str], dict[str, Json]]:
        with frozen():  # each look reads the branches once
            heads = {f"{name} {branch}": head for (name, branch), head in branch_heads(c).items()}
        mine[:] = [j for j in all_jobs() if j["spec"].get("campaign") == c.ident]
        ended = {j["id"]: j for j in mine if j["state"].get("state") in TERMINAL}
        return heads, ended

    def places() -> list[Path]:  # where a message, a job's end or a branch's move writes
        mail = mailbox(c, "")
        mail.mkdir(parents=True, exist_ok=True)
        found = [mail, mail.parent, *(x for x in mail.parent.iterdir() if x.is_dir()), jobs_dir()]
        found += [j["dir"] for j in mine if j["state"].get("state") not in TERMINAL]
        for repo in {c.repo(p) for p in c.quests.values() if p["status"] in FLIGHT}:
            found.append(Path(common_dir(repo)) / "refs" / "heads" / "quest" / c.name)
        return found

    heads, ended = current()
    if recorded is None:  # the first wait starts from now
        remember(heads, ended)
        seen_heads, seen_jobs, seen_messages = dict(heads), set(ended), set(messages())
    else:
        seen_heads = {str(k): str(v) for k, v in dict(recorded.get("heads") or {}).items()}
        seen_jobs = {str(x) for x in recorded.get("jobs") or []}
        seen_messages = {str(x) for x in recorded.get("messages") or []}
    while True:
        reap()
        exchange()
        watch_leads()
        heads, ended = current()
        waiting = messages()
        moved = sorted(k for k, h in heads.items() if seen_heads.get(k) != h)
        finished = sorted(set(ended) - seen_jobs)
        arrived = sorted(set(waiting) - seen_messages)
        if moved or finished or arrived:
            break
        running = mine
        if not heads and not peers(c) and not any(j["state"].get("state") not in TERMINAL for j in running):
            print("nothing to wait for: no quest in flight and no job of this campaign")
            return 0
        if timeout and time.time() - start >= timeout:
            print(f"nothing new after {timeout:g}s")
            return 3
        if any(j["state"].get("state") == "queued" for j in running):
            ensure_worker()
        watch(places(), tick)
    for key in moved:
        name, branch = key.split(" ", 1)
        print(f"{name}: {branch} moved to {heads[key][:12]}; journal: {journal_state(c, name, branch)}")
    for key in arrived:
        n = waiting[key]
        target = key.split()[0]
        whom = f" for the lead, from {n['by']}" if target.endswith(":") else f" from {n['by']}"
        print(f"{target.rstrip(':')}: {n['id']}{whom}: {n['text']}")
    for jid in finished:
        s = job_summary(ended[jid])
        print(f"{jid}  {s['state']}" + ("" if s["exit"] is None else f" exit {s['exit']}") + f"  {s['what']}")
    remember(heads, ended)
    with frozen():
        flight = [f"{name} ({journal_state(c, name, branch)})" for (name, branch) in branch_heads(c)]
    pending = sum(
        1 for j in all_jobs() if j["spec"].get("campaign") == c.ident and j["state"].get("state") not in TERMINAL
    )
    if flight or pending:
        print(f"still in flight: {', '.join(flight) or 'no quest'}; {pending} job(s) pending; wait again after this")
    return 0


def wait_for(ids: list[str], any_one: bool, timeout: float) -> int:
    start = time.time()
    tick = float(os.environ.get("CAMPAIGN_WAIT_TICK", "5"))
    while True:
        reap()
        ensure_worker()
        watch_leads()
        jobs = {x: load_job(jobs_dir() / x) for x in ids}
        pending = [x for x, j in jobs.items() if j["state"].get("state") not in TERMINAL]
        if not pending or (any_one and len(pending) < len(ids)):
            break
        if timeout and time.time() - start >= timeout:
            print(f"still pending after {timeout:g}s: {', '.join(pending)}")
            return 3
        time.sleep(tick)
    for x, j in jobs.items():
        s = job_summary(j)
        took = f" in {s['seconds']}s" if s["seconds"] is not None else ""
        code = "" if s["exit"] is None else f" exit {s['exit']}"
        print(f"{x}  {s['state']}{code}{took}  {s['what']}  log {attempt_dir(j) / 'log'}")
    finished = [j for j in jobs.values() if j["state"].get("state") in TERMINAL]
    return 0 if all(j["state"].get("state") == "done" for j in finished) else 1


def cmd_wait(a: argparse.Namespace) -> int:
    reap()
    if a.commits:
        return wait_events(find_campaign(Path.cwd()), a.timeout)
    if a.jobs:
        ids = [resolve_job(x)["id"] for x in a.jobs]
    else:
        mine = [j for j in all_jobs() if not a.quest or j["spec"].get("quest") == a.quest]
        ids = [j["id"] for j in mine if j["state"].get("state") not in TERMINAL]
        if not ids and a.quest:
            ids = [j["id"] for j in mine]  # all finished: report them
    if not ids:
        print("nothing to wait for")
        return 0
    return wait_for(ids, a.any, a.timeout)
