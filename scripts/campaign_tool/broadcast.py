"""Broadcasts to every other campaign's lead: the campaign skill's own changes (a commit landing on
its main, its install moving), each announced once per receiving campaign, and `message --all`."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import os
import shutil
from collections.abc import Collection, Generator
from pathlib import Path
from typing import Any

from .campaign import Campaign, find_campaign, resolved
from .core import META, SKILL, CampaignError, Json, die, git, git_ok, jobs_dir, plural, read_json, write_json
from .mail import inbox, leave_message, peers, send_message
from .repo import SHA
from .state import campaigns, registry

LANDED, INSTALLED = "landed", "installed"
KINDS = (LANDED, INSTALLED)
UPSTREAM = "refs/remotes/origin/main"  # landed: main as the sender's clone last pushed or fetched it
LISTED = 15  # commits a notice names; the rest it counts


def notices() -> Path:
    """The broadcaster's records, beside the registry: seen.json (the skill's commits last looked at)
    and one file per receiving campaign (the last commit of each kind announced to it)."""
    return jobs_dir().parent / "broadcasts"


def checkout_of(path: Path) -> Path | None:
    """The work tree holding PATH, found by its .git (a directory, or a file in a submodule or a
    worktree), or None outside one."""
    for d in (path, *path.parents):
        if (d / ".git").exists():
            return d
    return None


def installed_skill() -> Path | None:
    """The installed skill's checkout when this process runs it: $CAMPAIGN_SKILL ("" for none), else
    the checkout the `campaign` on PATH runs from. None from any other checkout (a quest's worktree, a
    job's clone), whose commits are not the install."""
    named = os.environ.get("CAMPAIGN_SKILL")
    if named is not None:
        return checkout_of(Path(named).resolve()) if named else None
    found = shutil.which("campaign")
    if not found or Path(found).resolve().parent != SKILL / "scripts":
        return None
    return checkout_of(SKILL)


def git_dirs(top: Path) -> tuple[Path, Path]:
    """A checkout's own git dir (its HEAD) and its common one (its refs)."""
    gitdir = top / ".git"
    if gitdir.is_file():
        gitdir = (top / gitdir.read_text().partition("gitdir:")[2].strip()).resolve()
    common = gitdir
    if (gitdir / "commondir").is_file():
        common = (gitdir / (gitdir / "commondir").read_text().strip()).resolve()
    return gitdir, common


def read_ref(top: Path, ref: str) -> str:
    """The commit REF (HEAD or a full ref name) names in checkout TOP, or "": read from git's files,
    since a git process costs more than every other part of the check it serves."""
    try:
        gitdir, common = git_dirs(top)
        if (common / "reftable").is_dir():
            return git(top, "rev-parse", "--verify", "--quiet", ref, check=False)
        name = ref
        for _ in range(5):  # a symbolic ref names another
            try:
                text = ((gitdir if name == "HEAD" else common) / name).read_text().strip()
            except FileNotFoundError:
                text = packed_refs(common).get(name, "")
            if not text.startswith("ref: "):
                return text if SHA.fullmatch(text) else ""
            name = text[5:]
    except OSError:
        pass
    return ""


def packed_refs(common: Path) -> dict[str, str]:
    refs: dict[str, str] = {}
    with contextlib.suppress(FileNotFoundError):
        for line in (common / "packed-refs").read_text().splitlines():
            commit, _, name = line.partition(" ")
            if name and not line.startswith(("#", "^")):
                refs[name] = commit
    return refs


def origin(repo: Path) -> str:
    """The repository a clone belongs to: its origin's URL without a trailing .git, else its git dir."""
    url = git(repo, "config", "--get", "remote.origin.url", check=False).rstrip("/")
    return url.removesuffix(".git") if url else str(git_dirs(repo)[1])


def skill_clone(info: Json, project: str, origins: dict[str, str]) -> Path | None:
    """The campaign's clone of the skill's repository (its home, or a quest's repository), or None: a
    source to describe the skill's commits from. ORIGINS caches each repository's `origin` across commands."""
    home = Path(info["home"])
    repos = [home, *(resolved(home / q["repo"]) for q in info["state"].get("quests", {}).values() if q.get("repo"))]
    for repo in dict.fromkeys(repos):
        if str(repo) not in origins and repo.is_dir():
            origins[str(repo)] = origin(repo)
        if origins.get(str(repo)) == project:
            return repo
    return None


def registry_marks() -> dict[str, int]:
    """Each registered campaign's state.json modification time: a new campaign, or a quest moving to
    the skill's repository, changes who sends and who is told."""
    marks: dict[str, int] = {}
    folder = registry()
    for f in sorted(folder.iterdir()) if folder.is_dir() else []:
        with contextlib.suppress(OSError):
            marks[f.name] = (Path(f.read_text().strip()) / META / f.name / "state.json").stat().st_mtime_ns
    return marks


@contextlib.contextmanager
def broadcasting() -> Generator[bool, None, None]:
    """The broadcaster's lock: True when this process holds it, False when another does (and announces)."""
    notices().mkdir(parents=True, exist_ok=True)
    with (notices() / ".lock").open("w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def looked(skill: Path, seen: Json) -> dict[str, Any]:
    """What announce_skill compares with SEEN: the install, the landed commit and the registry's marks."""
    source = Path(seen["source"]) if seen.get("source") else None
    return {
        "skill": str(skill),
        INSTALLED: read_ref(skill, "HEAD"),
        LANDED: read_ref(source, UPSTREAM) if source else "",
        "registry": registry_marks(),
    }


def announce_skill() -> None:
    """Tells every other campaign's lead what changed in the installed skill since it was last told:
    commits landed on the skill repository's main, and the install moving. Each command runs this
    first; while nothing moved it reads a few small files and starts no process."""
    skill = installed_skill()
    if skill is None:
        return
    seen = read_json(notices() / "seen.json") or {}
    if all(seen.get(k) == v for k, v in looked(skill, seen).items()):
        return
    with broadcasting() as mine:
        if mine:
            broadcast_skill(skill)


def broadcast_skill(skill: Path) -> None:
    """The slow half of announce_skill, under its lock. Per receiver and kind it sends, then records:
    a crash between them leaves a message the rerun finds by its `notice` key and only records."""
    seen = read_json(notices() / "seen.json") or {}
    origins: dict[str, str] = dict(seen.get("origins") or {})
    project = origins.setdefault(str(skill), origin(skill))
    registered = campaigns()
    clones = {n: r for n, o in registered.items() if (r := skill_clone(o, project, origins)) is not None}
    source = next(iter(clones.values()), None)
    now = looked(skill, {"source": str(source or "")})
    if seen.get("skill") == str(skill):  # else a first look, or another install: a baseline, sent to nobody
        by = f"campaign {', '.join(clones)}" if clones else "campaign"
        for name, info in registered.items():
            tell_local(name, info, skill=skill, source=source, seen=seen, now=now, by=by)
        if source is not None and now[LANDED]:
            tell_far(registered, list(clones), skill=skill, source=source, seen=seen, head=now[LANDED])
    write_json(notices() / "seen.json", {**now, "source": str(source or ""), "origins": origins})


def landed_by(info: Json, repo: Path) -> set[str]:
    """The commits of REPO that campaign INFO's own landings contain: each record's commit, and for a
    fast-forward landing (a single parent) the commits it brought in from the record's `from`."""
    own: set[str] = set()
    for q in info["state"].get("quests", {}).values():
        for record in q.get("landed", []):
            commit = record.get("commit")
            if not commit:
                continue
            own.add(commit)
            parents = git(repo, "rev-list", "--first-parent", "--parents", "-n1", commit, check=False).split()[1:]
            if len(parents) == 1 and record.get("from"):
                own.update(git(repo, "rev-list", "--first-parent", f"{record['from']}..{commit}", check=False).split())
    return own


def tell_local(name: str, info: Json, *, skill: Path, source: Path | None, seen: Json, now: Json, by: str) -> None:
    """Campaign NAME's lead gets one message per kind whose commit moved since it was last told (a
    campaign never told starts from the commits seen before this change), naming, for a landing, the
    commits its own landings do not contain (none left: nothing is sent); an install names them all."""
    path = notices() / f"{name}.json"
    record = read_json(path) or {}
    for kind in KINDS:
        head = now[kind]
        last = record.get(kind) or seen.get(kind) or head
        if not head or last == head:
            continue
        c = Campaign(Path(info["home"]), name)
        key = f"skill {kind} {head}"
        held = [*inbox(c, "", settled_too=True), *c.state.get("messages", [])]
        if not any(m.get("notice") == key for m in held):
            repo = source if kind == LANDED and source else skill
            skip = landed_by(info, repo) if kind == LANDED else ()
            if text := describe(kind, skill, repo, last, head, skip=skip):
                leave_message(c, "", text, by, notice=key)
        record[kind] = head
        write_json(path, record)


def tell_far(registered: Json, senders: list[str], *, skill: Path, source: Path, seen: Json, head: str) -> None:
    """The senders' peers on other machines get the landed commits through the sender's outbox, under
    an id fixed by the commit, so a resend is a no-op; their own machine announces their install."""
    for sender in senders:
        c = Campaign(Path(registered[sender]["home"]), sender)
        for peer in peers(c):
            if peer in registered:
                continue  # told here, as a campaign of this machine
            path = notices() / "peers" / f"{peer}.json"
            record = read_json(path) or {}
            last = record.get(LANDED) or seen.get(LANDED) or head
            if last == head:
                continue
            text = describe(LANDED, skill, source, last, head)
            send_message(c, peer, "", text, "campaign", uid=f"{sender}-skill-{LANDED}-{head[:12]}")
            record[LANDED] = head
            write_json(path, record)


def describe(kind: str, skill: Path, repo: Path, old: str, new: str, *, skip: Collection[str] = ()) -> str:
    """The lead's message for a move of KIND from OLD to NEW in REPO: each commit's repository-prefixed
    hash and subject, with its `Leads:` trailer (what changes for a lead) when it has one; "" when
    every commit is in SKIP (the receiver's own)."""
    name = origin(skill).rpartition("/")[2].rpartition(":")[2] or skill.name
    fmt = "%h%x1f%s%x1f%(trailers:key=Leads,valueonly,separator=%x20)%x1f%H"
    listing = git(repo, "log", "--first-parent", "--reverse", f"--format={fmt}", f"{old}..{new}", check=False)
    rows = [line.split("\x1f") for line in listing.splitlines() if line.count("\x1f") == 3]
    if rows and all(row[3] in skip for row in rows):
        return ""
    rows = [row for row in rows if row[3] not in skip]
    items = [
        f"{name}:{h} {subject}" + (f" (for leads: {leads.strip()})" if leads.strip() else "")
        for h, subject, leads, _ in rows
    ]
    if len(items) > LISTED:
        items = [*items[:LISTED], f"and {len(items) - LISTED} more (git log {old[:12]}..{new[:12]})"]
    short = git(repo, "rev-parse", "--short", new, check=False) or new[:7]
    if kind == INSTALLED:
        if not rows:
            was = git(repo, "rev-parse", "--short", old, check=False) or old[:7]
            return f"campaign skill: the install here moved back to {name}:{short} (from {name}:{was})"
        return f"campaign skill: {name}:{short} installed here, {plural(len(rows), 'commit')}: " + "; ".join(items)
    installed = git_ok(skill, "merge-base", "--is-ancestor", new, "HEAD")
    where = "installed here" if installed else "not installed here yet"
    listed = "; ".join(items) or f"{name}:{short}"
    return f"campaign skill: {plural(max(len(rows), 1), 'commit')} landed on {name}'s main, {where}: {listed}"


def cmd_broadcast(a: argparse.Namespace) -> int:
    """`campaign message --all TEXT`: TEXT to the lead of every other campaign on this machine and of
    this campaign's peers, never to the sender and never twice: a lead holding it open is skipped."""
    if a.close:
        die("--all sends a message; close one without it")
    if a.text:
        die("--all takes only the message's TEXT: every other campaign's lead receives it")
    text = a.quest
    try:
        here = find_campaign(Path.cwd())
    except CampaignError as e:
        die(f"{e}; --all sends from a campaign (its checkouts, or --campaign NAME)")
    by = f"{here.name}: {a.by}"
    registered = campaigns()
    sent: list[str] = []
    for name, info in registered.items():
        if name == here.name:
            continue
        c = Campaign(Path(info["home"]), name)
        if any(m["text"] == text and m.get("origin") == here.name for m in inbox(c, "")):
            print(f"{name}'s lead holds this message open already")
            continue
        sent.append(f"{leave_message(c, '', text, by, here.name)} for {name}'s lead")
    uid = f"{here.name}-all-{hashlib.sha256(text.encode()).hexdigest()[:12]}"  # a resend is a no-op
    for peer in peers(here):
        if peer not in registered:
            sent.append(
                f"{send_message(here, peer, '', text, a.by, uid=uid)} for {peer}'s lead, in {here.name}'s outbox"
            )
    print("\n".join(sent) if sent else "no other campaign's lead to tell, here or among the peers")
    return 0
