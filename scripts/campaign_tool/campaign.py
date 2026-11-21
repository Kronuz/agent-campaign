"""The Campaign: its state loaded and saved by the one writer, the overview and the sidebar it draws."""

from __future__ import annotations

import contextlib
import fcntl
import functools
import json
import os
import re
import sys
import time
from collections.abc import Callable, Collection, Generator
from pathlib import Path

import report

from .core import (
    FAILED,
    FLIGHT,
    STATUSES,
    TERMINAL,
    CampaignError,
    Json,
    atomic_write,
    die,
    git,
    git_ok,
    plural,
    read_json,
    stamp,
    table_cells,
    write_json,
)
from .jobs import all_jobs, quest_jobs
from .journals import decisions, journal_place, journal_text, milestone_groups, state_of
from .mail import settle_mail
from .repo import ancestor, code_tree, frozen, has_ref, main_branch, rev, toplevel
from .state import branches, clashes, cycles, follow, foreign, locate, register, select, unstarted_waits, validate


def find_campaign(cwd: Path) -> Campaign:
    home, pinned, _ = locate(cwd)
    return Campaign(home, os.environ.get("CAMPAIGN") or pinned)


class Campaign:
    def __init__(self, home: Path, name: str = "") -> None:
        self.home = home
        self.name, self.meta = select(home, name)
        self.path = home / self.meta / "state.json"
        data = read_json(self.path)
        if data is None:
            die(f"{self.path} does not exist")
        self.migrated = migrate_parks(data)
        self.closing: list[Path] = []  # message files the next save removes, once the state records them
        errors = validate(data)
        if errors:
            die("state.json is invalid:\n  " + "\n  ".join(errors))
        self.state: Json = data

    @functools.cached_property
    def ident(self) -> str:
        """How jobs and wait records name this campaign: its directory."""
        return str(self.home / self.meta)

    @property
    def quests(self) -> dict[str, Json]:
        return self.state["quests"]

    def quest(self, pid: str) -> Json:
        if pid not in self.quests:
            die(f"no quest {pid}")
        return self.quests[pid]

    def quest_file(self, pid: str) -> str:
        path = self.home / self.meta / "quests" / f"{pid}.md"
        return path.read_text() if path.exists() else ""

    def repo(self, p: Json) -> Path:
        return resolved(self.home / p["repo"])

    def met(self, dep: str) -> bool:
        """The dependency holds nothing back: its quest (through drops) landed, or was dropped with
        no successor."""
        _, q = follow(self, dep)
        return q is not None and q.get("status") in ("landed", "dropped")

    def save(self, message: str, extra: tuple[str, ...] = ()) -> None:
        errors = validate(self.state)
        if errors:
            die("refusing to write invalid state:\n  " + "\n  ".join(errors))
        closed = settle_mail(self)
        write_json(self.path, self.state)
        for f in closed:
            f.unlink(missing_ok=True)
        register(self.state["campaign"], self.home)
        sidebar(self)
        if os.environ.get("CAMPAIGN_NO_COMMIT") == "1":
            return
        paths = [f"{self.meta}/state.json", *extra]
        git(self.home, "add", "--", *paths)
        if git_ok(self.home, "diff", "--cached", "--quiet", "--", *paths):
            return
        git(self.home, "commit", "-q", "-m", message, "--", *paths)


@functools.lru_cache(maxsize=1 << 10)
def resolved(path: Path) -> Path:
    """PATH resolved once per command: a quest's repository, asked for many times."""
    return path.resolve()


def in_flight(counts: dict[str, int]) -> str:
    """ "2 in flight (1 active, 1 blocked)": the quests started and not landed, by stage."""
    stages = [f"{counts[s]} {'in review' if s == 'review' else s}" for s in FLIGHT if counts[s]]
    return f"{sum(counts[s] for s in FLIGHT)} in flight" + (f" ({', '.join(stages)})" if stages else "")


def unattended(c: Campaign, jobs: list[Json]) -> list[str]:
    """Active or in-review quests nothing moves unless the lead does: no job of theirs queued or running
    and their journal untouched for CAMPAIGN_IDLE seconds (an hour: no contributor at work), unless the
    journal says ready and only a dependency not yet landed holds them (`after` orders the landing,
    never the work); or whose journal says it waits on the lead, whatever runs."""
    busy = {
        j["spec"].get("quest")
        for j in jobs
        if j["spec"].get("campaign") == c.ident and j["state"].get("state") in ("queued", "running")
    }
    quiet_for = float(os.environ.get("CAMPAIGN_IDLE", "3600"))

    def idle(k: str) -> bool:
        p = c.quests[k]
        state = state_of(journal_text(c, k, p.get("branch", "")))
        if LEAD_WAIT.match(state):
            return True
        if k in busy or journal_age(c, k) < quiet_for:
            return False
        return not READY.match(state) or all(c.met(d) for d in [*p["needs"], *p["after"]])

    return sorted(k for k, p in c.quests.items() if p["status"] in ("active", "review") and idle(k))


def journal_age(c: Campaign, name: str) -> float:
    """Seconds since NAME's journal last changed in its worktree, or was last committed on its branch."""
    wt = journal_place(c, name)
    path = wt / f"{c.meta}/journal/{name}.md" if wt else None
    if path is not None and path.exists():
        return time.time() - path.stat().st_mtime
    branch = c.quests[name].get("branch", "")
    stamp = git(c.home, "log", "-1", "--format=%ct", branch, check=False).strip() if branch else ""
    return time.time() - float(stamp) if stamp.isdigit() else float("inf")


READY = re.compile(r"(?:ready|done)\b", re.I)
LEAD_WAIT = re.compile(r"(?:blocked on|waiting on|waits on) (?:the )?lead\b", re.I)


def overview(c: Campaign) -> Json:
    """Counts by status, the launchable quests, the ready ones a file set holds, the hunt list's open
    rows, and the current milestone, among the map's in its order: the first with a quest in review
    or active, else with one ready to start, else not landed."""
    live = {k: p for k, p in c.quests.items() if p["status"] != "dropped"}
    counts = {s: sum(1 for p in live.values() if p["status"] == s) for s in STATUSES}
    ready = [k for k, p in live.items() if p["status"] == "planned" and not unstarted_waits(c, p)]
    held = {k: x for k in ready if (x := clashes(c, k))}
    groups = [(name, ids) for name, _, ids in milestone_groups(c, {k: p["status"] for k, p in live.items()})]
    placed = {k for _, ids in groups for k in ids}
    groups.append(("", [k for k in live if k not in placed]))
    open_ = [(name, ids) for name, ids in groups if any(live[k]["status"] != "landed" for k in ids)]
    place = {k: (n, i) for n, (_, ids) in enumerate(groups) for i, k in enumerate(ids)}
    launchable = sorted(
        (k for k in ready if k not in held), key=lambda k: (place[k][0], -len(waiting_on(live, k)), place[k][1])
    )

    def first(test: Callable[[str], bool]) -> tuple[str, list[str]] | None:
        return next(((name, ids) for name, ids in open_ if any(test(k) for k in ids)), None)

    current = first(lambda k: live[k]["status"] in ("review", "active")) or first(lambda k: k in ready)
    current = current or next(iter(open_), None)
    view: Json = {"counts": counts, "launchable": launchable, "held": held, "hunt": hunt_open(c), "milestone": None}
    if current is not None:
        name, members = current
        view["milestone"] = {
            "name": name,
            "landed": sum(1 for k in members if live[k]["status"] == "landed"),
            "quests": len(members),
            "launchable": [k for k in launchable if k in members],
        }
    return view


def waiting_on(live: dict[str, Json], pid: str) -> set[str]:
    """The quests not landed that need PID, or land after it, directly or through others: its
    critical-path weight."""
    found: set[str] = set()
    todo = [pid]
    while todo:
        k = todo.pop()
        for x, q in live.items():
            if x not in found and q["status"] != "landed" and k in (*q["needs"], *q.get("after", [])):
                found.add(x)
                todo.append(x)
    return found


def migrate_parks(data: Json) -> list[str]:
    """Turns stored `blocked` statuses without until= into derived blocks or parks: a planned quest whose
    reason names only quests (and not the owner) needs them instead; any other keeps its text, until owner."""
    quests = data.get("quests")
    notes: list[str] = []
    if not isinstance(quests, dict):
        return notes
    for pid, p in quests.items():
        if not isinstance(p, dict) or p.get("status") != "blocked" or p.get("until"):
            continue
        reason = str(p.get("reason", ""))
        named = [q for q in quests if q != pid and re.search(rf"(?<![\w-]){re.escape(q)}(?![\w-])", reason)]
        if named and not p.get("branch") and not re.search(r"\bowner\b", reason, re.I):
            needs = list(dict.fromkeys([*p.get("needs", []), *named]))
            if not cycles({**quests, pid: {**p, "needs": needs}}):
                p.update(status="planned", needs=needs)
                p.pop("reason", None)
                notes.append(f"{pid} was blocked on {', '.join(named)}: now it needs them, a derived block")
                continue
        p["until"] = ["owner"]
        notes.append(f"{pid} was blocked: parked until owner ({reason})")
    return notes


def park_target(c: Campaign, token: str) -> str:
    """What a park's until= item names: "owner", "quest" (this campaign's or NAME:ID) or "decision"."""
    if token == "owner":
        return "owner"
    if token in c.quests or re.fullmatch(r"[\w.-]+:[a-z0-9]+(-[a-z0-9]+)*", token):
        return "quest"
    if re.fullmatch(r"[A-Za-z][\w.-]*", token) and not re.fullmatch(r"[a-z0-9-]+", token):
        return "decision"
    die(f"until={token}: name quest ids, PLAN decision ids (D12) or owner")


def park_met(c: Campaign, p: Json) -> str:
    """The met condition of a parked quest ("until a landed"), or "" while one still holds."""
    if p["status"] != "blocked" or not p.get("until") or "owner" in p["until"]:
        return ""
    for token in p["until"]:
        if park_target(c, token) == "quest":
            q = foreign(token) if ":" in token else c.quests.get(token)
            if q is None or q.get("status") not in ("landed", "dropped"):
                return ""
        elif token not in decisions(c):
            return ""
    return f"until {', '.join(p['until'])} met"


def blockers(c: Campaign) -> dict[str, Json]:
    """Each quest not started: launchable ({}), or what it waits on: needs, file sets or a park."""
    out: dict[str, Json] = {}
    for k, p in c.quests.items():
        if p["status"] == "blocked":
            out[k] = {} if park_met(c, p) else {"park": p["until"]}
        elif p["status"] == "planned":
            unmet = unstarted_waits(c, p)
            held: list[str] = [] if unmet else clashes(c, k)
            out[k] = {"needs": unmet} if unmet else {"files": held} if held else {}
    return out


def hunt_open(c: Campaign) -> int | None:
    """The open rows of the hunt list (`hunt.md`, Status `open`), or None without one."""
    path = c.home / c.meta / "hunt.md"
    if not path.exists():
        return None
    return sum(1 for line in path.read_text().splitlines() if table_cells(line)[-1].lower() == "open")


def hunt_row(c: Campaign, prefix: str) -> str:
    """PREFIX, once it starts the first cell of exactly one open row of the hunt list."""
    path = c.home / c.meta / "hunt.md"
    rows = path.read_text().splitlines() if path.exists() else []
    hits = [r for r in rows if (cells := table_cells(r))[-1].lower() == "open" and cells[0].startswith(prefix)]
    if len(hits) != 1:
        die(f"hunt={prefix!r} starts {len(hits)} open rows of {c.meta}/hunt.md: give the first words of exactly one")
    return prefix


def close_hunt(c: Campaign, quest: str, repo: Path, commit: str) -> tuple[str, ...]:
    """Marks the hunt row QUEST names done at its landed COMMIT; the path to commit with the state."""
    prefix, path = c.quests[quest].get("hunt"), c.home / c.meta / "hunt.md"
    if not prefix or not path.exists():
        return ()
    rows = path.read_text().splitlines(keepends=True)
    for i, row in enumerate(rows):
        cells = table_cells(row)
        if cells[-1].lower() == "open" and cells[0].startswith(prefix):
            body = row.rstrip()[:-1].rstrip()  # the row without its closing pipe
            rows[i] = f"{body[: body.rindex('|')]}| done: {repo.name}:{commit[:9]} ({quest}) |\n"
            atomic_write(path, "".join(rows))
            return (f"{c.meta}/hunt.md",)
    print(f"campaign: no open hunt row starts {prefix!r}: none marked done", file=sys.stderr)
    return ()


def sidebar(c: Campaign) -> None:
    """A bar for the current milestone, a pill for the project and one per quest in flight, drawn
    by commands run in the home. Only changes are sent; the last view per workspace is cached in the
    git directory."""
    workspace = os.environ.get("CMUX_WORKSPACE_ID", "")
    if not workspace or not Path.cwd().resolve().is_relative_to(c.home.resolve()):
        return  # drawn only from its home: a message to another campaign leaves that sidebar alone
    o = overview(c)
    counts, m = o["counts"], o["milestone"]
    name = c.state["campaign"]
    want: dict[str, str] = {}
    project = f"status:campaign-{c.name}"
    want[project] = f"{name}: {counts['landed']} landed · {in_flight(counts)} · {counts['planned']} planned"
    jobs = all_jobs() if any(p["status"] in FLIGHT and p.get("branch") for p in c.quests.values()) else []
    with frozen():  # one branch listing per repository, not two git calls per branch
        failing_by = {pid: unexplained_failures(c, pid, jobs) for pid, p in c.quests.items() if p["status"] in FLIGHT}
    for pid, p in c.quests.items():
        if p["status"] in ("active", "review"):  # blocked quests count in the campaign's pill only
            failing = failing_by[pid]
            want[f"status:campaign-{c.name}-{pid}"] = f"{pid}: {p['status']}" + (
                f" · 🔥 {plural(len(failing), 'failed job')}" if failing else ""
            )
    if m is not None:
        label = f"{name}{' ' + m['name'] if m['name'] else ''}: {m['landed']} of {m['quests']} quests landed" + (
            f" · launchable: {', '.join(m['launchable'][:3])}" if m["launchable"] else ""
        )
        want["bar"] = f"{m['landed']}/{m['quests']}\t{label}"
    cache = Path(git(c.home, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "campaign-sidebar.json"
    view = f"{workspace} {c.ident}"
    seen: dict[str, str] = {}
    with contextlib.suppress(FileNotFoundError, json.JSONDecodeError, AttributeError):
        seen = json.loads(cache.read_text()).get(view, {})
    for key in seen.keys() - want.keys():
        if key == "bar":
            report.clear()
        else:
            report.unstatus(key.split(":", 1)[1])
    for key, value in want.items():
        if seen.get(key) == value:
            continue
        if key == "bar":
            done, label = value.split("\t", 1)
            report.progress(report.fraction(done), label)
        else:
            report.status(key.split(":", 1)[1], value, "map" if key == project else "hammer")
    with contextlib.suppress(OSError, json.JSONDecodeError):
        views = json.loads(cache.read_text()) if cache.exists() else {}
        views.pop(workspace, None)
        views[view] = want
        cache.write_text(json.dumps(views))


@contextlib.contextmanager
def writing() -> Generator[Campaign, None, None]:
    """The one writer: the home repository's main checkout, on its main branch, under a lock."""
    cwd = Path.cwd()
    home, pinned, named = locate(cwd)
    if not named and (not git_ok(cwd, "rev-parse", "--git-dir") or toplevel(cwd) != home):
        die(f"state is written from the campaign's main checkout ({home}); run this there, or with -C {home}")
    branch = git(home, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if branch != main_branch(home):
        die(f"{home} is on {branch or 'a detached HEAD'}, not {main_branch(home)}")
    lock = Path(git(home, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "campaign.lock"
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        c = Campaign(home, os.environ.get("CAMPAIGN") or pinned)
        recover_landing(c)
        if c.migrated:
            c.save("campaign: derive dependency blocks; park the rest until owner")
            for note in c.migrated:
                print(f"campaign: {note}", file=sys.stderr)
        yield c


def landing_record(repo: Path, main: str, intent: Json) -> Json | None:
    """The landing an intent describes, when main contains its target."""
    target = intent["target"]
    if not ancestor(repo, target, main):
        return None
    commit = target
    if intent.get("mode") == "merge":
        for line in git(repo, "log", "--merges", "--format=%H %P", f"{target}..{main}").splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[2] == target:
                commit = parts[0]
    return {
        "commit": commit,
        "tested": target,
        "at": stamp(),
        "final": intent["final"],
        "reason": intent.get("reason", ""),
        "evidence": intent.get("evidence", []),
        **({"criteria": intent["criteria"]} if intent.get("criteria") else {}),
        **({"from": intent["from"]} if intent.get("from") else {}),
        **({"ci_skipped": intent["ci_skipped"]} if intent.get("ci_skipped") else {}),
    }


def recover_landing(c: Campaign) -> None:
    """Completes or clears a landing whose command died between moving main and recording it."""
    intent = c.state.pop("landing", None)
    if not intent:
        return
    p = c.quests.get(intent.get("id", ""))
    record = landing_record(c.repo(p), main_branch(c.repo(p)), intent) if p else None
    if p is not None and record is not None:
        p["landed"].append(record)
        if record["final"]:
            p["status"] = "landed"
        p["updated"] = stamp()
        c.save(f"campaign: complete the interrupted landing of {intent['id']} at {record['commit'][:12]}")
        print(f"campaign: completed the interrupted landing of {intent['id']}", file=sys.stderr)
    else:
        c.save(f"campaign: clear the interrupted landing of {intent.get('id')} (main never moved)")
        print(f"campaign: cleared an interrupted landing of {intent.get('id')}; main never moved", file=sys.stderr)


def unexplained_failures(
    c: Campaign, pid: str, jobs: list[Json], journals: Collection[str] | None = None
) -> list[Json]:
    """The quest's jobs that failed at its current code and that no journal of it names: what the map
    marks 🔥 and the sidebar counts."""
    ended = [j for j in ended_at_head(c, pid, jobs) if j["state"]["state"] in FAILED]
    if not ended:
        return []
    if journals is None:
        journals = [journal_text(c, name, branch) for name, branch, _, _ in branches(c, pid)]
    noted = "\n".join(journals)
    return [j for j in ended if j["id"] not in noted]


def ended_at_head(c: Campaign, pid: str, jobs: list[Json]) -> list[Json]:
    """The quest's finished jobs at the code its branches have now; failures count only there, older
    ones are history (`campaign job list`)."""
    p = c.quests[pid]
    if p["status"] not in FLIGHT:
        return []
    repo = c.repo(p)
    heads = {code_tree(repo, rev(repo, b)) for _, b, _, _ in branches(c, pid) if has_ref(repo, b)}

    def at_head(j: Json) -> bool:  # read through the quest's repository: one reader for every worktree
        with contextlib.suppress(CampaignError, KeyError):
            return code_tree(repo, j["spec"]["commit"]) in heads
        return False

    mine = quest_jobs(jobs, c.ident, pid)
    return [j for j in mine if j["state"].get("state") in TERMINAL and at_head(j)]
