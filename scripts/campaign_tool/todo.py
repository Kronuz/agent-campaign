"""The lead's to-do list: what nothing moves without, critical path first, each item with its exact
next command. `status`, `brief` and every return of `wait --commits` end with it, and the worker
mails it to the lead until each item is done (`lead_todo`)."""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import sys
from pathlib import Path

import report

from .campaign import JOB_WAIT, READY, Campaign, activity, critical_path, duration, hunt_open, overview, park_met
from .ci import forget_ci
from .core import CampaignError, Json, atomic_write, git, plural, section
from .jobs import quest_jobs
from .journals import journal_text, state_of
from .mail import leave_message, recorded_lead, surface_spaces, todo_mail, todo_mail_file
from .map import first_paragraph
from .repo import ancestor, frozen, has_ref, rev
from .reuse import job_at
from .reviews import landing_gaps, task_gaps
from .state import registry
from .worker import light_slots, light_weight

KINDS = ("merge", "land", "review", "finish", "answer", "park", "resume", "start", "unpark", "slots")  # within a quest


def repeat_seconds() -> float:
    """How long an item the lead leaves undone waits before a wait fires it again."""
    return float(os.environ.get("CAMPAIGN_TODO_REPEAT", "1800"))


def item(kind: str, quest: str, text: str, cmd: str, key: str = "") -> Json:
    return {"key": key or f"{kind} {quest}", "kind": kind, "quest": quest, "text": text, "cmd": cmd}


def first_command(gaps: list[str], fallback: str) -> str:
    """The command the first gap names (`campaign ...`), else FALLBACK."""
    for gap in gaps:
        if m := re.search(r"`(campaign [^`]+)`", gap):
            return m.group(1)
    return fallback


def ready(state: str) -> bool:
    """A journal State that ends a round: ready for review, or waiting on the round's jobs."""
    return bool(READY.match(state) or JOB_WAIT.search(state))


def round_at_head(c: Campaign, k: str, p: Json, jobs: list[Json]) -> bool:
    """Whether quest K's round exists at its head's code: a job tagged for it, or a gate check of it, at
    or after the head's last code commit, or one whose result holds there (its inputs unchanged, so a
    submit reuses it). Only then is it worth a review."""
    repo = c.repo(p)
    head = rev(repo, p["branch"])
    code = git(repo, "log", "-1", "--first-parent", "--format=%H", head, "--", ".", ":(exclude).campaign")
    check = re.compile(rf"{re.escape(k)} (?:deferred )?check ")
    earlier = []
    for j in quest_jobs(jobs, c.ident, k):
        commit = j["spec"].get("commit") or ""
        if commit and (j.get("tags") or check.match(j["spec"].get("what", ""))):
            if not code or ancestor(repo, code, commit):
                return True
            earlier.append(j)
    for j in earlier:
        with contextlib.suppress(CampaignError, KeyError):
            if (twin := job_at(j["spec"], head, merged=True)) and twin["state"].get("state") == "done":
                return True
    return False


def todo(c: Campaign, jobs: list[Json], gaps: dict[str, list[str]] | None = None) -> Json:
    """The list ("items", critical path first) and what the quests in flight are doing ("flight":
    worked on, waiting on a job, CI or a landing, needing the lead). GAPS are the landing gaps
    status already has, by quest; the others are asked here."""
    gaps = gaps or {}
    o = overview(c)
    doing = activity(c, jobs)
    items: list[Json] = [item("start", k, f"start {k}: launchable", f"campaign start {k}") for k in o["launchable"]]
    for k, p in c.quests.items():
        if met := park_met(c, p):
            back = "active" if p.get("branch") else "planned"
            items.append(item("unpark", k, f"unpark {k}: {met}", f"campaign set {k} status={back}"))
    for k, p in c.quests.items():
        repo = c.repo(p)
        if p["status"] not in ("active", "review") or not p.get("branch") or not has_ref(repo, p["branch"]):
            continue
        for task, t in sorted(p.get("tasks", {}).items()):
            name = f"{k}--{task}"
            if t.get("merged") or t.get("status") == "dropped":
                continue
            if not task_gaps(c, k, task, jobs):
                merge = f"campaign task merge {k} {task}"
                items.append(item("merge", k, f"merge {k}'s task {task}: its jobs passed", merge, f"merge {name}"))
            elif doing.get(name, ("",))[0] == "lead":
                why = doing[name][1]
                items.append(
                    item("resume", k, f"resume {k}'s task {task}: {why}", f"campaign status {k}", f"resume {name}")
                )
        missing = gaps[k] if k in gaps else landing_gaps(c, k, rev(repo, p["branch"]), final=True, ci=True, jobs=jobs)
        state = state_of(journal_text(c, k, p["branch"]))
        if not missing:
            items.append(item("land", k, f"land {k}: nothing keeps it from landing", f"campaign land {k}"))
        elif p["status"] == "active" and ready(state) and p["lenses"] and round_at_head(c, k, p, jobs):
            lenses = " and ".join(p["lenses"])
            text = f"review {k}: its journal says {state}; then launch its {lenses} review"
            items.append(item("review", k, text, f"campaign set {k} status=review"))
        elif doing.get(k, ("",))[0] == "lead":
            why = doing[k][1]
            if READY.match(state):
                items.append(
                    item(
                        "finish",
                        k,
                        f"finish {k}: {why}; to land: {missing[0]}",
                        first_command(missing, f"campaign status {k}"),
                    )
                )
            elif why.startswith("its journal waits on the lead"):
                items.append(item("answer", k, f"answer {k}: {why}", f"campaign status {k}"))
            elif why.startswith("its journal waits on the owner"):
                park = f"campaign set {k} status=blocked until=owner reason=owner"
                items.append(item("park", k, f"park {k}: {why}", park))
            else:
                items.append(item("resume", k, f"resume {k}: {why}", f"campaign status {k}"))
    if slots := idle_slots(jobs):
        free, total = slots
        if hunt := hunt_open(c):
            text = (
                f"fill {free} of {plural(total, 'light slot')} idle with {plural(hunt, 'hunt row')} open: an "
                "exploration batch (references/assignments/exploration.md), or --priority -1 work"
            )
            items.append(item("slots", "", text, "campaign new ID --title T --hunt WORDS", "slots"))
    order = critical_path(c)
    items.sort(key=lambda x: (*order(x["quest"]), KINDS.index(x["kind"])) if x["quest"] else (1 << 30, 0, 0, 0))
    flight = {
        s: sorted(k for k, (x, _) in doing.items() if x == s and "--" not in k) for s in ("working", "waiting", "lead")
    }
    return {"items": items, "flight": flight}


def idle_slots(jobs: list[Json]) -> tuple[int, int] | None:
    """(free, all) light slots when at least half idle and nothing is queued or draining the machine."""
    if any(j["state"].get("state") == "queued" for j in jobs):
        return None
    running = [j for j in jobs if j["state"].get("state") == "running"]
    if any(j["spec"].get("level") == "exclusive" for j in running):
        return None
    total = light_slots()
    used = sum(light_weight(j, {}) for j in running if j["spec"].get("level", "light") == "light")
    free = total - used
    return (free, total) if free >= max(1, total // 2) else None


SHOWN = 10  # items a wait or brief prints; status prints them all


def todo_lines(t: Json, shown: int | None = SHOWN) -> list[str]:
    """The list as status, brief and wait print it, its first SHOWN items (all with None); "wait
    again after this" only when it is empty."""
    f = t["flight"]
    counts = [f"{len(f['working'])} worked on", f"{len(f['waiting'])} waiting on jobs, CI or a landing"]
    counts.append(f"{len(f['lead'])} needing the lead")
    head = f"in flight: {', '.join(counts)}"
    if not t["items"]:
        return [f"{head}; the lead has nothing to do: wait again after this"]
    lines = [f"to do, critical path first ({head}):"]
    lines += [f"todo: {x['text']}: `{x['cmd']}`" for x in t["items"][:shown]]
    if shown is not None and len(t["items"]) > shown:
        lines.append(f"todo: and {len(t['items']) - shown} more: `campaign status`")
    return lines


def mail_every() -> float:
    """Seconds between two looks of the worker at one campaign's to-do."""
    return float(os.environ.get("CAMPAIGN_TODO_MAIL", "300"))


def escalate_after() -> float:
    """Seconds after its first send that an undone item is the owner's to see."""
    return float(os.environ.get("CAMPAIGN_TODO_ESCALATE", "7200"))


PARK = "or park it with a reason"
DEMANDS = {"start": PARK, "unpark": "or park it again with a reason", "resume": PARK, "answer": PARK}
JOB_ID = re.compile(r"\b\d{6}-\d{6}-[0-9a-f]{6}\b")


def jobs_note(state: str, jobs: list[Json], now: float) -> str:
    """What the jobs a journal waits on did ("its journal waits on jobs A and B, which passed 52 h
    ago"), or "" unless it names jobs that all ended."""
    ids = list(dict.fromkeys(JOB_ID.findall(state)))
    known = {j["id"].removeprefix("job-"): j for j in jobs}
    ended = [known[x]["state"] for x in ids if x in known and known[x]["state"].get("ended")]
    if not ids or not ended or len(ended) < len(ids):
        return ""
    states = {str(x.get("state")) for x in ended}
    how = "passed" if states == {"done"} else "ended (" + ", ".join(sorted(states)) + ")"
    names = " and ".join(ids) if len(ids) < 3 else f"{', '.join(ids[:-1])} and {ids[-1]}"
    return f"its journal waits on jobs {names}, which {how} {duration(now - max(float(x['ended']) for x in ended))} ago"


def mail_line(c: Campaign, x: Json, jobs: list[Json], age: float, now: float) -> str:
    """An item as the lead's mail says it: what, its evidence and age, then its command and what else
    the lead may do instead."""
    head, _, why = str(x["text"]).partition(": ")
    then = DEMANDS.get(x["kind"], "")
    p = c.quests.get(x["quest"], {})
    if x["kind"] in ("resume", "answer") and "--" not in x["key"] and p.get("branch"):
        journal = journal_text(c, x["quest"], p["branch"])
        state = state_of(journal)
        if JOB_WAIT.match(state) and (note := jobs_note(state, jobs, now)):
            why, then = note, f"read them, {PARK}"
        elif x["kind"] == "answer" and (ask := first_paragraph(section(journal, "Blocked on"), 300)):
            why, then = f"{why}; it asks: {ask}", f"answer it (`campaign message {x['quest']} TEXT`), {PARK}"
    listed = "new" if age < 60 else f"listed {duration(age)}"
    return f"todo: {head} ({why}; {listed}): `{x['cmd']}`" + (f", {then}" if then else "")


def pill_key(c: Campaign) -> str:
    """The lead's sidebar pill for its undone to-do: quest ids hold no "--", so no quest's pill is it."""
    return f"campaign-{c.name}--todo"


AGE = re.compile(r"\b\d+(?:\.\d+)? (?=[a-z])")  # "45 min", "2 h", "3 jobs": an age or a count


def said(x: Json) -> str:
    """What an item asks of the lead, its gap and command, without the ages and counts that change
    while it stands."""
    return f"{AGE.sub('# ', str(x['text']))}: `{x['cmd']}`"


def mail_todo(c: Campaign, jobs: list[Json], now: float, workspace: str) -> bool:
    """One look at C's to-do for its live lead, whose cmux workspace is WORKSPACE: when an item is new
    or was last sent `repeat_seconds` ago, or the list differs from the one last sent (an item came
    or left, or its gap or command changed: `said`), every item goes in one message, critical path
    first, that replaces the one before (`settled`); an item first sent `escalate_after` ago and
    undone is told to the owner once, and draws a pill in the lead's workspace while one is.
    Recorded, with when each item was first and last sent and what was sent, in `todo_mail_file`.
    True while the list holds items."""
    records = todo_mail(c)
    rec: Json = dict(records.get(c.ident) or {})
    forget_ci()  # CI that was running may have finished
    with frozen():
        items = [x for x in todo(c, jobs)["items"] if x["kind"] != "slots"]  # advice, not work a quest waits on
        seen = {str(k): dict(v) for k, v in dict(rec.get("items") or {}).items() if k in {x["key"] for x in items}}
        repeat = repeat_seconds()
        text = ""
        asks = {x["key"]: said(x) for x in items}
        due = any(now - float(seen.get(x["key"], {}).get("sent", float("-inf"))) >= repeat for x in items)
        if items and (due or asks != rec.get("sent")):
            for x in items:
                seen.setdefault(x["key"], {"first": now})["sent"] = now
            lines = [mail_line(c, x, jobs, now - float(seen[x["key"]]["first"]), now) for x in items]
            text = "to do, critical path first:\n" + "\n".join(lines)
    if text or not items:  # recorded before the message: its own token must not settle it
        rec["token"] = secrets.token_hex(4) if text else ""
        rec["sent"] = asks
    rec.update(looked=now, items=seen)
    late = [x for x in items if now - float(seen[x["key"]]["first"]) >= escalate_after()]
    if fresh := [x for x in late if not seen[x["key"]].get("escalated")]:
        for x in fresh:
            seen[x["key"]]["escalated"] = True
        heads = ", ".join(str(x["text"]).partition(": ")[0] for x in late)
        body = f"{c.name}: {plural(len(late), 'item')} untouched {duration(escalate_after())}+: {heads}"
        report.notify(f"campaign: {c.name}'s lead leaves its to-do undone", body, workspace=workspace)
    oldest = max((now - float(seen[x["key"]]["first"]) for x in late), default=0.0)
    want = f"🚨 {len(items)} to do, oldest {duration(oldest)}" if late else ""
    drawn: Json = dict(rec.get("pill") or {})
    if drawn and (not want or drawn.get("workspace") != workspace):
        report.unstatus(pill_key(c), str(drawn["workspace"]))
        drawn = {}
    if want and drawn.get("value") != want:
        report.status(pill_key(c), want, workspace=workspace)
        drawn = {"workspace": workspace, "value": want}
    rec["pill"] = drawn
    records[c.ident] = rec
    atomic_write(todo_mail_file(c), json.dumps(records, indent=2) + "\n")
    if text:
        leave_message(c, "", text, "campaign", quietly=True, settles={"todo": rec["token"]})  # the wake is the notice
    return bool(items)


SCAN = 30.0  # seconds between the worker's looks at which campaign's to-do is due
_scanned: list[Json] = [{"at": float("-inf"), "pending": False}]
_failed: dict[str, float] = {}  # campaign ident -> when its look last failed, waited out like a look


def lead_todo(jobs: list[Json], now: float) -> bool:
    """The worker's tick: at most every SCAN seconds, looks at the registered campaign longest
    unlooked whose recorded lead cmux lists, once `mail_every` passed for it (`mail_todo`); a
    campaign with no recorded lead is skipped unread. True while cmux lists a recorded lead: the
    worker stays to mail it."""
    last = _scanned[0]
    if now - float(last["at"]) < SCAN:
        return bool(last["pending"])
    led: list[tuple[str, Path, str]] = []
    folder = registry()
    for f in sorted(folder.iterdir()) if folder.is_dir() else []:
        if lead := recorded_lead(f.name):
            led.append((f.name, Path(f.read_text().strip()), lead))
    spaces = surface_spaces() if led else None
    due: list[tuple[float, Campaign, str]] = []
    live = [(name, home, lead) for name, home, lead in led if spaces is not None and lead in spaces]
    for name, home, lead in live:  # without a live lead, the items wait for the next one
        try:
            c = Campaign(home, name)
        except (CampaignError, OSError):
            continue
        rec = todo_mail(c).get(c.ident) or {}
        looked = max(float(rec.get("looked", float("-inf"))), _failed.get(c.ident, float("-inf")))
        if now - looked >= mail_every():
            due.append((looked, c, spaces[lead] if spaces else ""))
    if due:
        _, c, workspace = min(due, key=lambda d: d[0])
        try:
            mail_todo(c, jobs, now, workspace)
        except Exception as e:  # a campaign the worker cannot read never stops its queue
            _failed[c.ident] = now
            print(f"campaign: the to-do of {c.name} failed: {e}", file=sys.stderr)
    _scanned[0] = {"at": now if len(due) <= 1 else now - SCAN, "pending": bool(live)}  # the rest, next tick
    return bool(live)
