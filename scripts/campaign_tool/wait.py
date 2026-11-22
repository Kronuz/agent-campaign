"""Waiting: sleeping until a quest branch moves, a job ends, a message arrives or the lead has work."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import os
import select as os_select
import sys
import time
from pathlib import Path
from typing import Any

from .alarm import watch_calls
from .campaign import Campaign, find_campaign
from .ci import forget_ci
from .core import FLIGHT, TERMINAL, Json, git, jobs_dir, plural, read_json, write_json
from .jobs import all_jobs, attempt_dir, job_summary, load_job, resolve_job
from .journals import journal_state
from .leads import watch_leads
from .mail import fetch_peers, inbox, mailbox, peers, push_outbox
from .repo import common_dir, frozen, has_ref, rev
from .runner import ensure_worker, reap
from .state import branches
from .todo import SHOWN, repeat_seconds, todo, todo_lines


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
    since the lead last saw them (recorded in the home's .git), so nothing that happens between two
    waits is missed, or as soon as the lead's to-do list has an item it has not been shown, or one
    shown CAMPAIGN_TODO_REPEAT seconds ago and still undone; then prints what is in flight and the
    list."""
    seen_file = Path(git(c.home, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "campaign-seen.json"
    records: Json = (read_json(seen_file) if seen_file.exists() else None) or {}
    recorded: Json | None = records.get(c.ident)
    shown: dict[str, float] = {str(k): float(v) for k, v in dict((recorded or {}).get("todo") or {}).items()}

    def remember(heads: dict[str, str], ended: dict[str, Json]) -> None:
        records[c.ident] = {"heads": heads, "jobs": sorted(ended), "messages": sorted(messages()), "todo": shown}
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
    repeat = repeat_seconds()
    loaded = [state_stamp(c)]

    mine: list[Json] = []

    def current() -> tuple[dict[str, str], dict[str, Json]]:
        with frozen():  # each look reads the branches once
            heads = {f"{name} {branch}": head for (name, branch), head in branch_heads(c).items()}
        mine[:] = [j for j in all_jobs() if j["spec"].get("campaign") == c.ident]
        ended = {j["id"]: j for j in mine if j["state"].get("state") in TERMINAL}
        return heads, ended

    made: list[Json] = [{"at": 0.0, "of": None, "list": {"items": []}}]
    every = max(tick, float(os.environ.get("CAMPAIGN_TODO_TICK", "30")))

    def listing(heads: dict[str, str], fresh: bool = False) -> Json:
        """The to-do list, made again when the state, a job or a branch changed, else every EVERY
        seconds (time alone moves a journal past the idle bound, or CI to its end); FRESH, made
        from the state read again (what a return prints: a command may be writing it)."""
        nonlocal c
        last = made[0]
        if fresh:  # as the writer leaves it: a command writes mail before the state
            with open(Path(common_dir(c.home)) / "campaign.lock", "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_SH)
                loaded[0] = state_stamp(c)
                c = Campaign(c.home, c.name)
        elif state_stamp(c) != loaded[0]:  # the lead landed, started or set something
            loaded[0] = state_stamp(c)
            c = Campaign(c.home, c.name)
        of = (loaded[0], sorted(heads.items()), sorted((j["id"], j["state"].get("state")) for j in mine))
        if not fresh and of == last["of"] and time.time() - last["at"] < every:
            return dict(last["list"])
        forget_ci()  # CI that was running may have finished: asked again once its answer on disk expires
        with frozen():
            made[0] = {"at": time.time(), "of": of, "list": todo(c, all_jobs())}
        return dict(made[0]["list"])

    def due(items: list[Json]) -> list[Json]:
        now = time.time()
        return [x for x in items if now - shown.get(x["key"], -repeat) >= repeat]

    def places() -> list[Path]:  # where a message, a job's end, a branch's move or a state change writes
        mail = mailbox(c, "")
        mail.mkdir(parents=True, exist_ok=True)
        found = [mail, mail.parent, *(x for x in mail.parent.iterdir() if x.is_dir()), jobs_dir(), c.path.parent]
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
    listed: Json = {"items": []}
    code = 0
    while True:
        reap()
        exchange()
        watch_leads()
        watch_calls()
        heads, ended = current()
        waiting = messages()
        moved = sorted(k for k, h in heads.items() if seen_heads.get(k) != h)
        finished = sorted(set(ended) - seen_jobs)
        arrived = sorted(set(waiting) - seen_messages)
        listed = listing(heads)
        if moved or finished or arrived or due(listed["items"]):
            break
        running = mine
        idle = not any(j["state"].get("state") not in TERMINAL for j in running)
        flying = any(p["status"] in FLIGHT for p in c.quests.values())  # by the state, not by branches mid-landing
        if not flying and not peers(c) and idle and not listed["items"]:
            print("nothing to wait for: no quest in flight and no job of this campaign")
            return 0
        if timeout and time.time() - start >= timeout:
            code = 3
            break
        if any(j["state"].get("state") == "queued" for j in running):
            ensure_worker()
        watch(places(), tick)
    listed = listing(heads, fresh=True)
    now = time.time()
    keys = {x["key"] for x in listed["items"]}
    for key in [k for k in shown if k not in keys]:  # done, or gone: it fires anew if it comes back
        del shown[key]
    fired = due(listed["items"])  # printed first: what woke the lead is never past the SHOWN cut
    listed["items"] = fired + [x for x in listed["items"] if x not in fired]
    for x in listed["items"][:SHOWN]:  # only what is printed counts as shown
        if code == 0 or x["key"] not in shown:
            shown[x["key"]] = now
    print(woken_by(moved, finished, [waiting[k]["id"] for k in arrived], fired, timeout if code == 3 else 0))
    if code == 3:
        print(f"nothing new after {timeout:g}s")
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
        print(f"still in flight: {', '.join(flight) or 'no quest'}; {plural(pending, 'job')} pending")
    for line in todo_lines(listed):
        print(line)
    return code


def woken_by(moved: list[str], finished: list[str], messages: list[str], fired: list[Json], timeout: float) -> str:
    """The first line of a wait's return: what woke it, and the to-do items that are new or due
    again ("no new to-do" when the list printed below holds only reminders)."""
    causes = [f"branch move {k.split(' ', 1)[0]}" for k in moved]
    causes += [f"job end {j}" for j in finished] + [f"message {m}" for m in messages]
    causes += [] if causes or not timeout else [f"timeout after {timeout:g}s"]
    todo = ", ".join(f"{x['kind']} {x['quest']}".strip() for x in fired)
    if todo:
        return f"woken by: {'; '.join([*causes, f'to-do: {todo}'])}"
    return f"woken by: {'; '.join(causes)} (no new to-do)"


def state_stamp(c: Campaign) -> tuple[int, int]:
    """When and how big state.json was written: whether a wait must read it again."""
    try:
        st = c.path.stat()
    except OSError:
        return (0, 0)
    return (st.st_mtime_ns, st.st_size)


def wait_for(ids: list[str], any_one: bool, timeout: float) -> int:
    start = time.time()
    tick = float(os.environ.get("CAMPAIGN_WAIT_TICK", "5"))
    while True:
        reap()
        ensure_worker()
        watch_leads()
        watch_calls()
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
