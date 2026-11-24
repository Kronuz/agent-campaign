"""Messages: mailboxes for quests and leads, waking a lead in cmux, and outboxes between machines."""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from collections.abc import Collection
from pathlib import Path
from typing import TYPE_CHECKING, Any

import report

from .core import CampaignError, Json, atomic_write, die, git, git_ok, jobs_dir, read_json, stamp, write_json
from .repo import common_dir
from .state import foreign

if TYPE_CHECKING:  # annotations only: they import this module, so importing them here is a cycle
    from .campaign import Campaign


def leads() -> Path:
    """Where each campaign's lead was last seen in cmux: one JSON file per campaign name."""
    return jobs_dir().parent / "leads"


def recorded_lead(name: str) -> str:
    """The cmux surface recorded as campaign NAME's lead, or ""."""
    try:
        return str(json.loads((leads() / name).read_text()).get("surface", ""))
    except (OSError, ValueError, AttributeError):
        return ""


def campaigns_led_by(surface: str, but: str = "") -> list[str]:
    """The campaigns (but BUT) whose recorded lead is SURFACE."""
    folder = leads()
    names = sorted(x.name for x in folder.iterdir()) if folder.is_dir() else []
    return [n for n in names if n != but and recorded_lead(n) == surface]


def live_surfaces() -> set[str] | None:
    """The surfaces cmux lists in every window, or None when cmux is absent or does not answer."""
    spaces = surface_spaces()
    return None if spaces is None else set(spaces)


def surface_spaces() -> dict[str, str] | None:
    """Each surface cmux lists in every window -> its workspace, or None when cmux is absent or
    does not answer."""
    exe = shutil.which("cmux")
    if not exe:
        return None
    try:
        args = [exe, "--json", "--id-format", "uuids", "tree", "--all"]
        done = subprocess.run(args, capture_output=True, text=True, timeout=10)
        tree: Any = json.loads(done.stdout) if done.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    if not isinstance(tree, dict) or not isinstance(tree.get("windows"), list):
        return None
    found: dict[str, str] = {}
    for window in tree["windows"]:
        for space in window.get("workspaces", []) if isinstance(window, dict) else []:
            for pane in space.get("panes", []) if isinstance(space, dict) else []:
                if isinstance(pane, dict):
                    for s in pane.get("surfaces", []):
                        if isinstance(s, dict):
                            found[str(s.get("id"))] = str(space.get("id", ""))
    return found


def lead_gone(surface: str) -> bool:
    """Whether cmux answers and no longer lists SURFACE; a cmux that does not answer keeps the lead."""
    live = live_surfaces()
    return live is not None and surface not in live


def wake_lead(c: Campaign, whom: str, nid: str, text: str, by: str, *, shown: bool = False) -> str:
    """Wakes the campaign's lead in cmux with the message (an idle session reads it at once; a busy
    one at its next step); "" once woken, else why it was not. SHOWN: the running command printed
    the message or was given it, so it needs no cmux message when its session is the lead."""
    surface = recorded_lead(c.name)
    if not shutil.which("cmux"):
        return "no cmux here"
    if not surface:
        return f"no lead of {c.name} recorded in cmux"
    if shown and surface == os.environ.get("CMUX_SURFACE_ID"):
        return ""
    body = f"campaign {c.name}: {nid} for {whom}, from {by}: {text} (close or answer it by that id)"
    try:
        done = subprocess.run(
            ["cmux", "agent", "message", surface, "--from", by or "campaign", "--", body],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"cmux failed: {e}"
    return "" if done.returncode == 0 else f"cmux failed: {(done.stderr or done.stdout).strip()[:120]}"


def notify(title: str, body: str) -> None:
    """A notice in the owner's cmux feed; silent outside cmux."""
    if os.environ.get("CMUX_WORKSPACE_ID"):
        report.notify(title, body)


INBOX_STALE = 15 * 60  # seconds a message to a lead waits undelivered before the owner is told
REANNOUNCE = float(os.environ.get("CAMPAIGN_REANNOUNCE", "300"))  # seconds between tries to wake a lead


def announce(c: Campaign, path: Path, message: Json, whom: str, *, shown: bool = False) -> str:
    """Wakes the lead with the message at PATH and records the try in it; "" once woken, else why not.
    SHOWN as in `wake_lead`."""
    why = wake_lead(c, whom, str(message["id"]), str(message["text"]), str(message["by"]), shown=shown)
    if why == "no cmux here":
        return why  # nothing to retry outside cmux
    record = {k: v for k, v in message.items() if k not in ("announced", "unannounced")}
    record["unannounced" if why else "announced"] = {"surface": recorded_lead(c.name), "at": stamp(), "why": why}
    with contextlib.suppress(OSError):
        if path.exists():
            atomic_write(path, json.dumps(record, indent=2) + "\n")
    return why


def settled(c: Campaign, m: Json) -> str:
    """Why a message the tool left has closed itself, or "": the quests it announced started; or the
    other campaign's quest whose start it reported landed or dropped, or ours named all order after it."""
    s = m.get("settles") or {}
    if s.get("todo"):
        current = str((todo_mail(c).get(c.ident) or {}).get("token", ""))
        if current != s["todo"]:
            return "a newer to-do replaced it" if current else "its to-do is done"
    if s.get("start") and all(c.quests.get(k, {}).get("status") not in ("planned", "blocked") for k in s["start"]):
        return "its quests started"
    if s.get("quest"):
        q = foreign(s["quest"])
        if q is not None and q.get("status") in ("landed", "dropped"):
            return f"{s['quest']} {q['status']}"
        mine = [c.quests[k] for k in s.get("ours", []) if k in c.quests]
        if mine and all(s["quest"] in x["after"] or x["status"] in ("landed", "dropped") for x in mine):
            return f"ordered --after {s['quest']}"
    return ""


def todo_mail_file(c: Campaign) -> Path:
    """What the worker mailed of each campaign's to-do, and when, in the home's .git (`mail_todo`)."""
    return Path(common_dir(c.home)) / "campaign-todo-mail.json"


def todo_mail(c: Campaign) -> Json:
    """The to-do mail record of the home: campaign ident -> its last look, message and items."""
    with contextlib.suppress(OSError, CampaignError):
        return read_json(todo_mail_file(c)) or {}
    return {}


def mailbox(c: Campaign, pid: str) -> Path:
    """Where messages for a quest ("" for the campaign's lead) wait, in the home's .git: any
    agent on the machine can leave one there without being the campaign's one writer."""
    common = common_dir(c.home)
    if not common:
        die(f"{c.home} is not a git checkout")
    return Path(common) / "campaign-mail" / c.state["campaign"] / (pid or "@")


def leave_message(
    c: Campaign,
    pid: str,
    text: str,
    by: str,
    origin: str = "",
    *,
    wake: bool = True,
    shown: bool = False,
    **extra: Any,
) -> str:
    """Leaves a message for a quest ("" for the lead) under a fresh id, and notifies. SHOWN as in
    `wake_lead`; EXTRA holds what closes a message the tool leaves by itself (`settled`)."""
    folder = mailbox(c, pid)
    folder.mkdir(parents=True, exist_ok=True)
    record = c.quests[pid] if pid else c.state
    taken = [m["id"] for m in [*inbox(c, pid, settled_too=True), *record.get("messages", [])]]
    n = 1 + max([int(str(x).partition("-")[2] or 0) for x in taken] or [0])
    tmp = folder / f".message.{os.getpid()}.tmp"
    while True:  # a fresh id even when two agents write at once
        message = {"id": f"msg-{n}", "at": stamp(), "by": by, "text": text, **({"origin": origin} if origin else {})}
        message.update(extra)
        tmp.write_text(json.dumps(message, indent=2) + "\n")
        try:
            os.link(tmp, folder / f"msg-{n}.json")
            break
        except FileExistsError:
            n += 1
        finally:
            tmp.unlink(missing_ok=True)
    notify(f"Message for {pid or c.name}", f"{by}: {text}"[:200])
    if wake:
        announce(c, folder / f"msg-{n}.json", message, pid or "the lead", shown=shown)
    return f"msg-{n}"


def inbox(c: Campaign, pid: str, settled_too: bool = False) -> list[Json]:
    """The open messages for a quest ("" for the lead), oldest first; a lead's message the tool left
    is closed once `settled`."""
    found: list[Json] = []
    folder = mailbox(c, pid)
    for f in folder.glob("*-*.json") if folder.is_dir() else []:
        with contextlib.suppress(CampaignError):
            message = read_json(f)
            if message and (settled_too or pid or not settled(c, message)):
                found.append(message)
    return sorted(found, key=lambda n: int(str(n["id"]).partition("-")[2] or 0))


def settle_mail(c: Campaign) -> list[Path]:
    """Records the lead's messages that closed themselves (`settled`) in the state; returns their files,
    to remove once the state is written."""
    gone = c.closing
    c.closing = []
    for m in inbox(c, "", settled_too=True):
        if (f := mailbox(c, "") / f"{m['id']}.json") not in gone and (why := settled(c, m)):
            c.state.setdefault("messages", []).append({**m, "closed": why})
            gone.append(f)
    return gone


def answered(message: Json, journals: Collection[str]) -> bool:
    """Whether a journal names the message by its id."""
    return any(re.search(rf"\b{re.escape(str(message['id']))}\b", j) for j in journals)


def outbox_ref(name: str) -> str:
    """A campaign's outbox: a ref of its own in its home, published under the same name."""
    return f"refs/campaign/outbox/{name}"


def peers(c: Campaign) -> dict[str, str]:
    """The campaigns on other machines this one exchanges messages with: name -> where its outbox is."""
    return dict(x.split("=", 1) for x in c.state.get("peers", []) if "=" in x)


def send_message(c: Campaign, peer: str, pid: str, text: str, by: str, *, uid: str = "") -> str:
    """Adds a message for a campaign on another machine to this campaign's outbox (a ref of its own,
    never main) and pushes it; one that can't be pushed now goes with the next push. A message under
    a UID the outbox already holds is not added again."""
    uid = uid or f"{c.name}-{secrets.token_hex(4)}"
    message = {"uid": uid, "from": c.name, "quest": pid, "at": stamp(), "by": by, "text": text}
    blob = subprocess.run(
        ["git", "-C", str(c.home), "hash-object", "-w", "--stdin"],
        input=json.dumps(message, indent=2) + "\n",
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    index = Path(git(c.home, "rev-parse", "--path-format=absolute", "--git-dir")) / f"outbox.{os.getpid()}.index"
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}

    def plumb(*args: str) -> str:
        cmd = ["git", "-C", str(c.home), *args]
        return subprocess.run(cmd, env=env, capture_output=True, text=True, check=True).stdout.strip()

    for _ in range(5):  # another agent may add a message at the same moment
        parent = git(c.home, "rev-parse", "--verify", "--quiet", outbox_ref(c.name), check=False)
        if parent and git_ok(c.home, "cat-file", "-e", f"{parent}:{peer}/{uid}.json"):
            push_outbox(c)
            return uid
        try:
            plumb("read-tree", *([parent] if parent else ["--empty"]))
            plumb("update-index", "--add", "--cacheinfo", f"100644,{blob},{peer}/{uid}.json")
            parents = ["-p", parent] if parent else []
            commit = plumb("commit-tree", plumb("write-tree"), *parents, "-m", f"campaign: message for {peer}")
        finally:
            index.unlink(missing_ok=True)
        if git_ok(c.home, "update-ref", outbox_ref(c.name), commit, parent or "0" * 40):
            push_outbox(c)
            return uid
    die("the outbox kept changing under this message; try again")


def push_outbox(c: Campaign) -> bool:
    """Publishes this campaign's outbox where its peers fetch it ($outbox remote, default origin)."""
    if not git_ok(c.home, "rev-parse", "--verify", "--quiet", outbox_ref(c.name)):
        return True
    remote = c.state.get("outbox") or "origin"
    ok = git_ok(c.home, "push", "--quiet", remote, f"{outbox_ref(c.name)}:{outbox_ref(c.name)}")
    if not ok:
        print(
            f"campaign: the outbox could not be pushed to {remote}; the next message or wait retries", file=sys.stderr
        )
    return ok


def fetch_peers(c: Campaign) -> list[str]:
    """Fetches each peer's outbox and leaves the messages it holds for this campaign; their ids."""
    imported = mailbox(c, "").parent / "imported.json"
    seen: set[str] = set(((read_json(imported) if imported.exists() else None) or {}).get("uids", []))
    left: list[str] = []
    for name, where in peers(c).items():
        local = f"refs/campaign/peer/{name}"
        if not git_ok(c.home, "fetch", "--quiet", "--no-tags", where, f"+{outbox_ref(name)}:{local}"):
            continue
        for path in git(c.home, "ls-tree", "-r", "--name-only", local, "--", f"{c.name}/", check=False).splitlines():
            with contextlib.suppress(ValueError, CampaignError):
                message = json.loads(git(c.home, "show", f"{local}:{path}"))
                if message["uid"] in seen:
                    continue
                pid = message.get("quest", "")
                open_quest = pid in c.quests and c.quests[pid]["status"] not in ("landed", "dropped")
                text = message["text"] if open_quest or not pid else f"for {pid}: {message['text']}"
                left.append(
                    leave_message(
                        c, pid if open_quest else "", text, f"{message['from']}: {message['by']}", message["uid"]
                    )
                )
                seen.add(message["uid"])
    if left:
        write_json(imported, {"uids": sorted(seen)})
    return left
