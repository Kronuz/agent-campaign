"""Loaders for campaign v2's records, each read-only, into plain records and one event timeline.

Every loader names its source path so the inventory can state where a number came from. Nothing
here writes outside the replay's own output directory (`--out`, default ~/.scratch/xapiand/v2-replay).
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

Json = dict[str, Any]

HOME = Path.home()
JOBS = HOME / ".scratch" / "campaign" / "jobs"
CAMPAIGNS = HOME / ".scratch" / "campaign" / "campaigns"
LEADS = HOME / ".scratch" / "campaign" / "leads"
QUIET = HOME / ".scratch" / "quiet"
CLAUDE = HOME / ".claude" / "projects"
CODE = HOME / "code" / "Kronuz"
OUT = HOME / ".scratch" / "xapiand" / "v2-replay"

# The analysis window: 14 days ending when this quest started (Xapiand:e2c9643f5, 2026-11-19 18:39 PST).
UNTIL = datetime(2026, 11, 20, 2, 39, 0, tzinfo=timezone.utc).timestamp()
SINCE = UNTIL - 14 * 86400

LEVELS = ("none", "light", "strict", "exclusive")
HEAVY = ("strict", "exclusive")
TERMINAL = ("done", "failed", "timeout", "interrupted", "cancelled", "skipped", "corrupt")


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%MZ") if t else "-"


def stamp(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, errors="replace")
    return out.stdout if out.returncode == 0 else ""


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(errors="replace"))
    except (OSError, ValueError):
        return None


def campaign_homes() -> dict[str, Path]:
    """Each campaign's home, from the tool's registry (~/.scratch/campaign/campaigns/NAME)."""
    homes: dict[str, Path] = {}
    for f in sorted(CAMPAIGNS.iterdir()) if CAMPAIGNS.is_dir() else []:
        homes[f.name] = Path(f.read_text().strip())
    return homes


WORKTREE = re.compile(r"^(?P<base>.*/[^/]+?)-wt(?:-[^/]*)?(?:/.*)?$")


def base_repo(path: str) -> Path | None:
    """The repository whose objects a job's (perhaps removed) worktree shares."""
    if not path:
        return None
    for marker in ("/.claude/worktrees/", "/.scratch/"):
        if marker in path:
            path = path.split(marker, 1)[0]
    m = WORKTREE.match(path)
    if m and Path(m["base"]).is_dir():
        return Path(m["base"])
    return Path(path) if Path(path).is_dir() else None


# ---------------------------------------------------------------- jobs


@dataclass
class Job:
    id: str
    campaign: str
    quest: str
    repo: str
    commit: str
    cmd: str
    cwd: str
    env: Json
    level: str
    minutes: float
    slot: str
    after: list[str]
    priority: int
    key: str
    rkey: str
    build: str
    fails_without: int
    what: str
    scripts: Json
    inputs: list[str]
    cpus: int
    submitted: float
    state: str
    exit: int | None
    note: str
    claimed: float = 0.0
    started: float = 0.0
    began: float = 0.0
    ended: float = 0.0
    admitted: float = 0.0
    build_started: float = 0.0
    build_ended: float = 0.0
    attempt: int = 1
    reads: bool = False
    tags: list[Json] = field(default_factory=list)

    @property
    def run_s(self) -> float:
        """The command's own run time inside its hold (began to ended)."""
        return max(0.0, self.ended - self.began) if self.began and self.ended else 0.0

    @property
    def build_s(self) -> float:
        """A staged (heavy) job's build, run under light before its hold."""
        return max(0.0, self.build_ended - self.build_started) if self.build_started and self.build_ended else 0.0

    @property
    def queue_s(self) -> float:
        """Submitted to claimed by the worker (or to its end, when it never ran)."""
        end = self.claimed or self.ended
        return max(0.0, end - self.submitted) if end else 0.0

    @property
    def hold_s(self) -> float:
        """Claimed to command start: source preparation, the build of a staged job, admission and quiet's wait."""
        return max(0.0, self.began - self.claimed) if self.began and self.claimed else 0.0

    @property
    def ran(self) -> bool:
        return self.began > 0


def campaign_name(path: str) -> str:
    return Path(path).name if path else "(none)"


def load_jobs(store: Path = JOBS) -> list[Job]:
    """Every job directory under the store: spec.json, state.json, for.json and whether reads.json exists."""
    jobs: list[Job] = []
    for d in sorted(store.iterdir()) if store.is_dir() else []:
        if not re.fullmatch(r"\d{6}-\d{6}-[0-9a-f]{6}", d.name):
            continue
        spec = read_json(d / "spec.json") or {}
        st = read_json(d / "state.json") or {"state": "corrupt"}
        tags = (read_json(d / "for.json") or {}).get("tags", []) if (d / "for.json").exists() else []
        build = st.get("build") or {}
        fw = spec.get("fails_without") or 0
        jobs.append(
            Job(
                id=d.name,
                campaign=campaign_name(spec.get("campaign", "")),
                quest=spec.get("quest", "") or "",
                repo=spec.get("repo", ""),
                commit=spec.get("commit", "") or "",
                cmd=spec.get("cmd", ""),
                cwd=spec.get("cwd", "") or "",
                env=spec.get("env") or {},
                level=spec.get("level", "light"),
                minutes=float(spec.get("minutes") or 0),
                slot=spec.get("slot", "") or "",
                after=list(spec.get("after") or []),
                priority=int(spec.get("priority") or 0),
                key=spec.get("key", "") or "",
                rkey=spec.get("rkey", "") or "",
                build=spec.get("build", "") or "",
                fails_without=int(fw),
                what=spec.get("what", ""),
                scripts=spec.get("scripts") or {},
                inputs=list(spec.get("inputs") or []),
                cpus=int(spec.get("cpus") or 0),
                submitted=float(spec.get("submitted") or 0),
                state=st.get("state", "corrupt"),
                exit=st.get("exit"),
                note=st.get("note", ""),
                claimed=float(st.get("claimed") or 0),
                started=float(st.get("started") or 0),
                began=float(st.get("began") or 0),
                ended=float(st.get("ended") or 0),
                admitted=float(st.get("admitted") or 0),
                build_started=float(build.get("started") or 0),
                build_ended=float(build.get("ended") or 0),
                attempt=int(st.get("attempt") or 1),
                reads=(d / "reads.json").exists(),
                tags=tags,
            )
        )
    return jobs


# ---------------------------------------------------------------- campaign state and its history


@dataclass
class StateEvent:
    t: float
    campaign: str
    sha: str
    verb: str
    quest: str
    args: str


SUBJECT = re.compile(r"^campaign: (?P<verb>[a-z]+)\b ?(?P<rest>.*)$")
INTERRUPTED = re.compile(r"^the interrupted landing of (?P<q>\S+) at (?P<at>\S+)")


def state_history(home: Path, name: str) -> list[StateEvent]:
    """Every commit of .campaign/NAME/state.json in the home (`git log`), parsed from its subject."""
    log = git(home, "log", "--format=%H%x1f%at%x1f%s", "--", f".campaign/{name}/state.json")
    events: list[StateEvent] = []
    for line in log.splitlines():
        sha, at, subject = line.split("\x1f", 2)
        m = SUBJECT.match(subject)
        if not m:
            events.append(StateEvent(float(at), name, sha, "other", "", subject))
            continue
        verb, rest = m["verb"], m["rest"]
        quest = ""
        if verb == "complete":
            mi = INTERRUPTED.match(rest)
            verb, quest = "land", (mi["q"] if mi else "")
        elif verb in ("new", "start", "set", "accept", "land", "clean", "rerun", "record"):
            quest = rest.split(" ", 1)[0].removesuffix("'s")
            rest = rest[len(quest) :].strip()
        events.append(StateEvent(float(at), name, sha, verb, quest, rest))
    events.reverse()
    return events


def load_state(home: Path, name: str) -> Json:
    return read_json(home / ".campaign" / name / "state.json") or {}


# ---------------------------------------------------------------- git logs of the repositories


@dataclass
class RepoLog:
    repo: str
    main: str
    commits: int  # in the window, first-parent on main
    merges: int
    first: float
    last: float


def repo_logs(root: Path = CODE, since: float = SINCE, until: float = UNTIL) -> list[RepoLog]:
    """First-parent history of each repository's main (or master) under ~/code/Kronuz in the window."""
    out: list[RepoLog] = []
    for d in sorted(root.iterdir()):
        if d.name.endswith("-wt") or "-wt-" in d.name or d.is_symlink() or not (d / ".git").exists():
            continue
        main = next((b for b in ("main", "master") if git(d, "rev-parse", "--verify", "-q", b).strip()), "")
        if not main:
            continue
        log = git(d, "log", "--first-parent", "--format=%at %p", f"--since={int(since)}", f"--until={int(until)}", main)
        rows = [line.split() for line in log.splitlines() if line]
        if not rows:
            continue
        times = [float(r[0]) for r in rows]
        out.append(RepoLog(d.name, main, len(rows), sum(1 for r in rows if len(r) > 2), min(times), max(times)))
    return out


# ---------------------------------------------------------------- journals and efficiency.md


@dataclass
class JournalLog:
    quest: str
    campaign: str
    commits: list[float]  # commit times touching the journal, on every ref of the home


def journal_logs(home: Path, name: str) -> list[JournalLog]:
    """Commit times of each quest journal on all refs (`git log --all -- .campaign/NAME/journal/`)."""
    log = git(home, "log", "--all", "--format=@%at", "--name-only", "--", f".campaign/{name}/journal/")
    times: dict[str, set[float]] = {}
    t = 0.0
    for line in log.splitlines():
        if line.startswith("@"):
            t = float(line[1:])
        elif line.endswith(".md") and "/journal/" in line:
            times.setdefault(Path(line).stem, set()).add(t)
    return [JournalLog(q, name, sorted(ts)) for q, ts in sorted(times.items())]


HOURLY = re.compile(r"^## (?P<day>2026-\d\d-\d\d) (?P<hour>\d\d)(?::00 hourly|h)$", re.M)
QUEUE_NOTE = re.compile(r"Queue: (?P<n>\d+) queued(?: \((?P<ex>\d+) exclusive)?")


def efficiency_notes(path: Path) -> Json:
    """The lead's ledger: its tables' row counts and the queue lengths its hourly notes state."""
    text = path.read_text(errors="replace") if path.exists() else ""
    rows = sum(1 for line in text.splitlines() if line.startswith("| ") and not line.startswith("|---"))
    queues: list[Json] = []
    for m in HOURLY.finditer(text):
        body = text[m.end() : text.find("\n## ", m.end()) if "\n## " in text[m.end() :] else len(text)]
        q = QUEUE_NOTE.search(body)
        if q:
            t = datetime.fromisoformat(f"{m['day']}T{m['hour']}:00-08:00").timestamp()
            queues.append({"t": t, "queued": int(q["n"]), "exclusive": int(q["ex"] or 0)})
    return {"rows": rows, "hourly": len(HOURLY.findall(text)), "queues": queues, "bytes": len(text)}


# ---------------------------------------------------------------- agent usage (`campaign usage --json`)


def usage(home: Path, name: str, out: Path, refresh: bool = False) -> Json:
    """`campaign usage --json` run in the home, cached under OUT so a replay repeats on the same input."""
    cache = out / f"usage-{name}.json"
    if refresh or not cache.exists():
        res = subprocess.run(["campaign", "usage", "--json"], cwd=home, capture_output=True, text=True)
        if res.returncode == 0:
            out.mkdir(parents=True, exist_ok=True)
            cache.write_text(res.stdout)
    data = read_json(cache)
    return data if isinstance(data, dict) else {"runs": [], "lead": [], "jobs": []}


# ---------------------------------------------------------------- the lead's transcripts


@dataclass
class LeadCommand:
    t: float  # when the result came back
    asked: float  # when the call was made
    campaign: str
    command: str
    verb: str
    quest: str
    output: str
    error: bool


CAMPAIGN_CALL = re.compile(r"\bcampaign\s+(?P<verb>job\s+\w+|[a-z]+)(?:\s+(?P<quest>[A-Za-z0-9][\w.-]*))?")
LEAD_VERBS = ("land", "accept", "verify", "start", "set", "new", "job submit", "job rerun", "job cancel", "review")


def lead_transcript(session: str) -> Path | None:
    hits = sorted(CLAUDE.glob(f"*/{session}.jsonl"))
    return hits[0] if hits else None


@dataclass
class LeadLog:
    commands: list[LeadCommand]
    activity: list[float]  # seconds with a transcript line
    tokens: dict[str, int]  # hour (ISO, UTC) -> tokens of the lead's responses (each message id once)
    prompts: list[Json]  # user turns that are not tool results: owner prompts and harness notices


BACKGROUND = re.compile(r"Command running in background with ID: (?P<id>\w+)")
TASKS = Path("/private/tmp/claude-501")  # Claude Code's background task outputs: <project>/<session>/tasks/<id>.output
TOKEN_FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")


def lead_log(log: Path, campaign: str, out: Path, refresh: bool = False) -> LeadLog:
    """The lead's `campaign` calls with their outputs, its activity, tokens per hour and prompts, from
    its Claude Code transcript (main thread only); cached under OUT (the transcripts are large)."""
    cache = out / f"lead-{campaign}.json"
    if not refresh and cache.exists():
        data = read_json(cache) or {}
        return LeadLog(
            [LeadCommand(**c) for c in data.get("commands", [])],
            [float(x) for x in data.get("activity", [])],
            dict(data.get("tokens", {})),
            list(data.get("prompts", [])),
        )
    asked: dict[str, tuple[float, str]] = {}
    tasks = TASKS / log.parent.name / log.stem / "tasks"
    commands: list[LeadCommand] = []
    activity: set[int] = set()
    tokens: dict[str, int] = {}
    seen: set[str] = set()
    prompts: list[Json] = []
    with log.open(errors="replace") as f:
        for line in f:
            if '"timestamp"' not in line or '"isSidechain":true' in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            t = stamp(e.get("timestamp", ""))
            if not t:
                continue
            activity.add(int(t))
            msg = e.get("message") or {}
            mid, use = msg.get("id"), msg.get("usage")
            if isinstance(use, dict) and mid and mid not in seen:
                seen.add(mid)
                hour = iso(t)[:13]
                tokens[hour] = tokens.get(hour, 0) + sum(int(use.get(k) or 0) for k in TOKEN_FIELDS)
            content = msg.get("content")
            if e.get("type") == "user" and isinstance(content, str):
                kind = "notice" if content.lstrip().startswith("<") else "prompt"
                prompts.append({"t": t, "kind": kind, "text": content[:200]})
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and block.get("name") == "Bash":
                    cmd = str((block.get("input") or {}).get("command", ""))
                    if "campaign " in cmd:
                        asked[str(block.get("id"))] = (t, cmd)
                elif block.get("type") == "tool_result" and block.get("tool_use_id") in asked:
                    t0, cmd = asked.pop(str(block["tool_use_id"]))
                    body = block.get("content")
                    text = (
                        "".join(x.get("text", "") for x in body if isinstance(x, dict))
                        if isinstance(body, list)
                        else str(body or "")
                    )
                    t_done = t
                    bg = BACKGROUND.match(text)
                    if bg:  # a background call's output is in its task file; it ended when that was written
                        task = tasks / f"{bg['id']}.output"
                        if task.is_file() and not task.is_symlink():
                            text, t_done = task.read_text(errors="replace"), task.stat().st_mtime
                    for m in CAMPAIGN_CALL.finditer(cmd):
                        verb = re.sub(r"\s+", " ", m["verb"])
                        if verb in LEAD_VERBS:
                            keep = text[:4000] if verb in ("land", "accept") else text[:300]
                            commands.append(
                                LeadCommand(
                                    t_done,
                                    t0,
                                    campaign,
                                    cmd[:500],
                                    verb,
                                    m["quest"] or "",
                                    keep,
                                    bool(block.get("is_error")),
                                )
                            )
    lead = LeadLog(commands, [float(x) for x in sorted(activity)], tokens, prompts)
    out.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps(
            {
                "commands": [c.__dict__ for c in commands],
                "activity": sorted(activity),
                "tokens": tokens,
                "prompts": prompts,
            }
        )
    )
    return lead


# ---------------------------------------------------------------- quiet


def quiet_holds(root: Path = QUIET) -> Json:
    """quiet keeps live holds only: files for current holds and pills; no history."""
    files: list[Path] = [p for p in root.rglob("*") if p.is_file()] if root.is_dir() else []
    holds = [p for p in files if p.parent == root and not p.name.startswith(".")]
    return {"files": len(files), "live_holds": len(holds), "names": sorted(p.name for p in holds)}
