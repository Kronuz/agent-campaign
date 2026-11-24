"""Lead notices across campaigns: noting a lead, redelivering stuck messages, and telling peers."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .campaign import Campaign, blockers, find_campaign, park_met
from .core import CampaignError, Json, atomic_write, jobs_dir, plural, span, stamp_time
from .mail import (
    INBOX_STALE,
    REANNOUNCE,
    announce,
    campaigns_led_by,
    inbox,
    lead_gone,
    leads,
    leave_message,
    mailbox,
    notify,
    recorded_lead,
)
from .runner import spawn, worker_alive
from .state import campaigns, changes, consumed, cross_overlaps, overlap


def in_lead_directory(c: Campaign) -> bool:
    """Whether the current directory is in the home checkout: a quest's worktree is not the lead's
    session, and its agent's commands say nothing of the lead or its title."""
    try:
        Path.cwd().resolve().relative_to(c.home.resolve())
    except (ValueError, OSError):  # OSError: run from a removed directory
        return False
    return True


def note_lead() -> None:
    """Records the cmux surface of whoever runs `campaign` in a campaign's home as its lead, so a
    message can wake that lead (`wake_lead`), when none is recorded or cmux no longer lists the
    recorded one: any other session looking in leaves the lead where it is. Jobs and workers run
    detached and are not leads."""
    surface = os.environ.get("CMUX_SURFACE_ID", "")
    if not surface:
        return
    try:
        c = find_campaign(Path.cwd())
    except (CampaignError, OSError):  # OSError: run from a removed directory
        return
    if not in_lead_directory(c):
        return
    entry = leads() / c.name
    seen = {"surface": surface, "workspace": os.environ.get("CMUX_WORKSPACE_ID", "")}
    lead = recorded_lead(c.name)
    if lead == surface:
        mailer()
        return
    if lead and (campaigns_led_by(surface, but=c.name) or not lead_gone(lead)):
        return
    with contextlib.suppress(OSError):
        leads().mkdir(parents=True, exist_ok=True)
        entry.write_text(json.dumps(seen) + "\n")
    redeliver(c.name, now=True)
    mailer()


def mailer() -> None:
    """A worker when none runs, to mail the lead its to-do (`lead_todo`): it leaves once no job is
    queued and no lead's list holds items."""
    if not worker_alive():
        spawn(["worker"], jobs_dir() / "worker.log")


def redeliver(only: str = "", now: bool = False) -> list[Json]:
    """Wakes, again, each campaign's lead for its open messages that never woke it, or woke a surface
    it has since left (at most every REANNOUNCE, or at once with NOW); returns those still unannounced
    past INBOX_STALE. Messages from before delivery was recorded are left alone."""
    out: list[Json] = []
    for name, info in campaigns().items():
        if only and name != only:
            continue
        with contextlib.suppress(CampaignError, OSError, KeyError):
            c = Campaign(Path(info["home"]), name)
            lead = recorded_lead(name)
            root = mailbox(c, "").parent
            for folder in sorted(root.iterdir()) if root.is_dir() else []:
                pid = "" if folder.name == "@" else folder.name
                for m in inbox(c, pid):
                    tried = m.get("announced") or m.get("unannounced")
                    if not tried or (m.get("announced") and tried.get("surface") == lead):
                        continue
                    if not now and time.time() - stamp_time(tried["at"]) < REANNOUNCE:
                        continue
                    why = announce(c, folder / f"{m['id']}.json", m, pid or "the lead")
                    if why and time.time() - stamp_time(m["at"]) > INBOX_STALE:
                        out.append({**m, "campaign": name, "why": why})
    return out


def stuck_leads() -> list[Json]:
    """The recorded leads (but the caller) whose cmux inbox holds messages undelivered for longer than
    INBOX_STALE, as a session blocked on a prompt holds them: campaign, surface, message ids and the
    oldest's age. [] when cmux is absent or does not answer."""
    exe = shutil.which("cmux")
    folder = leads()
    if not exe or not folder.is_dir():
        return []
    me = os.environ.get("CMUX_SURFACE_ID", "")
    out: list[Json] = []
    for entry in sorted(folder.iterdir()):
        try:
            surface = str(json.loads(entry.read_text()).get("surface", ""))
            if not surface or surface == me:
                continue
            args = [exe, "agent", "inbox", "--surface", surface, "--state", "queued", "--json"]
            done = subprocess.run(args, capture_output=True, text=True, timeout=10)
            data: Any = json.loads(done.stdout) if done.returncode == 0 else {}
        except (OSError, ValueError, AttributeError, subprocess.TimeoutExpired):
            continue
        messages: list[Any] = (
            data.get("messages", []) if isinstance(data, dict) else data if isinstance(data, list) else []
        )
        now = time.time()
        stale = {
            str(m.get("id")): now - float(m["created_at"])
            for m in messages
            if isinstance(m, dict)
            and isinstance(m.get("created_at"), (int, float))
            and now - float(m["created_at"]) > INBOX_STALE
        }
        if stale:
            out.append(
                {"campaign": entry.name, "surface": surface, "ids": sorted(stale), "oldest": max(stale.values())}
            )
    return out


def stuck_warnings() -> list[tuple[str, bool]]:
    """A warning per lead holding stale messages, and whether it names one not reported before: the
    owner is notified once per message (ids kept in leads-told.json beside the leads' records)."""
    record = jobs_dir().parent / "leads-told.json"
    try:
        told: list[str] = [str(x) for x in json.loads(record.read_text())]
    except (OSError, ValueError, TypeError):
        told = []
    out: list[tuple[str, bool]] = []
    for m in redeliver():
        text = f"{m['campaign']}'s {m['id']} from {m['by']} has not reached its lead in {span(time.time() - stamp_time(m['at']))}: {m['why']}"
        key = f"{m['campaign']}:{m['id']}:unannounced"
        fresh = key not in told
        if fresh:
            notify(f"campaign: a message to {m['campaign']} is not delivered", text)
            told.append(key)
        out.append((text, fresh))
    for lead in stuck_leads():
        text = (
            f"{plural(len(lead['ids']), 'message')} to {lead['campaign']}'s lead (cmux surface {lead['surface']}) "
            f"undelivered for up to {span(lead['oldest'])}: its session may be waiting on a prompt"
        )
        fresh = [x for x in lead["ids"] if x not in told]
        if fresh:
            notify(f"campaign: {lead['campaign']}'s lead is not reading messages", text)
            told += fresh
        out.append((text, bool(fresh)))
    if any(new for _, new in out):
        with contextlib.suppress(OSError):
            atomic_write(record, json.dumps(told[-500:]) + "\n")
    return out


_inbox_read = [0.0]


def watch_leads() -> None:
    """stuck_warnings at most once a minute of a wait; new ones go to stderr."""
    if time.time() - _inbox_read[0] < 60:
        return
    _inbox_read[0] = time.time()
    for text, new in stuck_warnings():
        if new:
            print(f"campaign: {text}", file=sys.stderr)


def unlocks(c: Campaign, before: dict[str, Json], event: str) -> str:
    """What became launchable since BEFORE (`blockers`): needs met, a file set freed, a park's condition met.
    Leaves one lead message listing every such quest not started yet, which closes itself once they
    start, and returns the line to print, or ""."""
    items: dict[str, str] = {}
    for k, now in blockers(c).items():
        was = before.get(k)
        if now or was is None or not was:
            continue
        p = c.quests[k]
        if p["status"] == "blocked":
            items[k] = f"{k} ({park_met(c, p)}: `campaign set {k} status={'active' if p.get('branch') else 'planned'}`)"
        elif "files" in was:
            freed = [g for g in p["files"] if any(overlap([g], c.quests[x]["files"]) for x in was["files"])]
            items[k] = f"frees {', '.join(freed or p['files'])} for {k}"
        else:
            items[k] = k
    if not items:
        return ""
    rank = {k: (text.startswith("frees "), text != k) for k, text in items.items()}  # needs met, parks, file sets
    items = dict(sorted(items.items(), key=lambda kv: rank[kv[0]]))
    line = "unlocks: " + "; ".join(items.values())
    for m in inbox(c, ""):
        start = (m.get("settles") or {}).get("start")
        if m.get("by") == "campaign" and start:
            for k, text in (m.get("items") or {}).items():
                if k in start and k not in items and c.quests.get(k, {}).get("status") in ("planned", "blocked"):
                    items[k] = text
            c.state.setdefault("messages", []).append({**m, "closed": "superseded"})
            c.closing.append(mailbox(c, "") / f"{m['id']}.json")
    text = f"{event}: unlocks " + "; ".join(items.values())
    leave_message(c, "", text, "campaign", shown=True, settles={"start": sorted(items)}, items=items)
    return line


def tell_peers(c: Campaign, pid: str) -> list[str]:
    """One message per other campaign on this machine whose work a quest just started overlaps, listing
    every overlap: the same files in the same repository, a contract the quest changes and a started
    quest consumes, or a contract it consumes and another is changing. A planned quest consuming a
    contract is not told: it has not started. A message already left is not repeated, and each closes
    itself once the quest lands or drops, or every quest it names orders --after it (`settled`)."""
    p = c.quests[pid]
    found: dict[str, list[str]] = {}
    ours: dict[str, list[str]] = {}
    for other in cross_overlaps(c, pid):
        name, _, qid = other.partition(":")
        ours.setdefault(name, []).append(qid)
        found.setdefault(name, []).append(
            f"may change files your {qid} changes (order them with --after {c.name}:{pid}, or narrow the files)"
        )
    for name, o in campaigns().items():
        if name == c.name:
            continue
        for qid, q in o["state"].get("quests", {}).items():
            if q.get("status") not in ("active", "review"):
                continue
            items = found.setdefault(name, [])
            told = [
                f"changes contract {x}, which your {qid} consumes"
                for x in consumed(q.get("consumes", []), changes(c.home, p))
            ]
            told += [
                f"builds against contract {x}, which your {qid} is changing"
                for x in consumed(p["consumes"], changes(Path(o["home"]), q))
            ]
            items += told
            ours.setdefault(name, []).extend([qid] if told else [])
    told: list[str] = []
    for name, items in found.items():
        other = campaigns().get(name)
        if other is None or not items:
            continue
        text = f"{c.name}:{pid} started and " + "; ".join(dict.fromkeys(items))
        peer = Campaign(Path(other["home"]), name)
        if any(m["text"] == text for m in inbox(peer, "")):
            continue
        settles = {"quest": f"{c.name}:{pid}", "ours": sorted(set(ours.get(name, [])))}
        leave_message(peer, "", text, f"campaign {c.name}", settles=settles)
        told.append(name)
    return told


def tell_dependents(c: Campaign, pid: str, p: Json, waiting: dict[str, list[str]]) -> None:
    """A drop reaches every quest that waits on it: other campaigns' leads get a message, ours a line.
    Their dependencies follow the drop when read; the message only says so."""
    then = (
        f"it folds into {p['into']}, which they now wait on"
        if p["into"] != "none"
        else "it has no successor, so they no longer wait on it: check they need none of its work"
    )
    for name, ids in waiting.items():
        if name == c.name:
            print(f"{pid} dropped: {', '.join(ids)} wait on it; {then}")
            continue
        text = f"{c.name}:{pid} was dropped ({p.get('reason', '')}): your {', '.join(ids)} wait on it; {then}"
        leave_message(Campaign(Path(campaigns()[name]["home"]), name), "", text, f"{c.name} lead", origin=c.name)
        print(f"told {name}'s lead: {', '.join(ids)} wait on {pid}")
