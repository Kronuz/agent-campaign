"""Campaign state: finding a campaign, validating its state, and the relations between quests."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

from .core import (
    CI_CHECK,
    CODE_LENSES,
    HEAVY,
    LENSES,
    LEVELS,
    META,
    RISKS,
    SHORT_RUN,
    STATUSES,
    CampaignError,
    Json,
    csv,
    die,
    git,
    git_ok,
    jobs_dir,
    read_json_shared,
)
from .repo import is_meta, main_checkout

if TYPE_CHECKING:  # annotations only: they import this module, so importing them here is a cycle
    from .campaign import Campaign


def locate(cwd: Path) -> tuple[Path, str, bool]:
    """The home repository, the campaign a quest's worktree belongs to, and whether the home was named
    explicitly ($CAMPAIGN_HOME, or a $CAMPAIGN/--campaign this repository does not hold, found in the
    machine's registry) rather than found from the current directory."""
    env = os.environ.get("CAMPAIGN_HOME")
    if env:
        return Path(env).resolve(), "", True
    name = os.environ.get("CAMPAIGN", "")
    if git_ok(cwd, "rev-parse", "--git-dir"):
        pointer = Path(git(cwd, "rev-parse", "--path-format=absolute", "--git-dir")) / "campaign-home"
        if pointer.exists():
            home, _, pinned = pointer.read_text().strip().partition("\n")
            return Path(home), pinned.strip(), False
        home = main_checkout(cwd)
        if not name or name in campaigns_in(home):
            return home, "", False
    other = campaigns().get(name) if name else None
    if other:
        return Path(other["home"]), name, True
    if name:
        die(f"no campaign {name} here or on this machine (`campaign status --all` lists them)")
    die("not in a git repository: pass -C DIR, --campaign NAME, or set CAMPAIGN_HOME")


def campaigns_in(home: Path) -> dict[str, str]:
    """The campaigns a home holds: name -> their directory, relative to the home."""
    return {f.parent.name: f"{META}/{f.parent.name}" for f in sorted((home / META).glob("*/state.json"))}


def select(home: Path, name: str) -> tuple[str, str]:
    """(name, directory) of the campaign NAME, or of the only one the home holds."""
    found = campaigns_in(home)
    if not name and len(found) == 1:
        name = next(iter(found))
    if not name:
        if found:
            die(f"{home} holds campaigns {', '.join(found)}: pass --campaign NAME or set CAMPAIGN")
        die("no campaign here: run `campaign init` in the home repository's main checkout")
    if name not in found:
        die(f"no campaign {name} in {home}" + (f" (it holds {', '.join(found)})" if found else ""))
    return name, found[name]


QUEST_FIELDS: dict[str, type] = {
    "title": str,
    "status": str,
    "risk": str,
    "repo": str,
    "needs": list,
    "after": list,
    "provides": list,
    "consumes": list,
    "files": list,
    "checks": list,
    "lenses": list,
    "branch": str,
    "worktree": str,
    "journal": str,  # a quest in another repository: its journal's worktree in the home
    "base": str,
    "tasks": dict,
    "accepted": dict,
    "landed": list,
    "messages": list,
    "history": list,  # changes to risk, lenses and checks
    "reason": str,
    "into": str,  # a dropped quest's successor (ID or NAME:ID), whose dependents wait on it; "none": they stop waiting
    "until": list,  # a park's conditions: quest ids, PLAN decision ids, or owner
    "milestone": str,
    "hunt": str,  # the first words of the hunt row it closes when it lands
    "updated": str,
}


def validate(data: Json) -> list[str]:
    errors: list[str] = []
    if data.get("version") != 2:
        errors.append("version must be 2")
    if "landing" in data and not isinstance(data["landing"], dict):
        errors.append("landing must be an object")
    if not isinstance(data.get("ci_later", []), list):
        errors.append("ci_later must be a list of CI workflow names")
    if not isinstance(data.get("disk_warn", 0.0), (int, float)) or data.get("disk_warn", 0) < 0:
        errors.append("disk_warn must be GiB of free disk, a number")
    if not isinstance(data.get("suites", {}), dict):
        errors.append("suites must map a repository's name to its suite command")
    if not isinstance(data.get("tested_docs", []), list):
        errors.append("tested_docs must be a list of path globs")
    quests = data.get("quests")
    if not isinstance(quests, dict):
        return [*errors, "quests must be an object"]
    for pid, p in quests.items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", pid) or "--" in pid:
            errors.append(f"{pid}: ids are lowercase letters, digits and single dashes")
        if not isinstance(p, dict):
            errors.append(f"{pid}: must be an object")
            continue
        for key in ("title", "status", "risk", "repo"):
            if key not in p:
                errors.append(f"{pid}: missing {key}")
        for key, value in p.items():
            if key not in QUEST_FIELDS:
                errors.append(f"{pid}: unknown field {key}")
            elif not isinstance(value, QUEST_FIELDS[key]):
                errors.append(f"{pid}: {key} must be a {QUEST_FIELDS[key].__name__}")
        if p.get("status") not in STATUSES:
            errors.append(f"{pid}: status must be one of {', '.join(STATUSES)}")
        if p.get("risk") not in RISKS:
            errors.append(f"{pid}: risk must be one of {', '.join(RISKS)}")
        for dep in [*p.get("needs", []), *p.get("after", [])]:
            if dep not in quests and not re.fullmatch(r"[\w.-]+:[a-z0-9]+(-[a-z0-9]+)*", dep):
                errors.append(f"{pid}: depends on unknown quest {dep} (another campaign's is NAME:ID)")
        for lens in p.get("lenses", []):
            if lens not in LENSES:
                errors.append(f"{pid}: unknown lens {lens} (one of {', '.join(LENSES)})")
        for check in p.get("checks", []):
            ok = isinstance(check, dict) and isinstance(check.get("name"), str)
            ok = ok and isinstance(check.get("cmd"), str) and check.get("level") in LEVELS
            ok = ok and isinstance(check.get("minutes"), int) and check["minutes"] > 0
            ok = ok and isinstance(check.get("later", False), bool)
            if not ok:
                errors.append(f"{pid}: a check needs name, cmd, level and minutes: {check}")
        if p.get("status") in ("blocked", "dropped") and not p.get("reason"):
            errors.append(f"{pid}: {p.get('status')} needs a reason")
        into = p.get("into")
        if into and (p.get("status") != "dropped" or (into != "none" and ":" not in into and into not in quests)):
            errors.append(
                f"{pid}: into= names the quest a drop folds into (ID, NAME:ID or none), only on a dropped quest"
            )
        if (p.get("status") == "blocked") != bool(p.get("until")):
            errors.append(f"{pid}: a park, and only a park, names until= (quest ids, PLAN decision ids or owner)")
        if p.get("status") == "landed" and not any(x.get("final") for x in p.get("landed", [])):
            errors.append(f"{pid}: landed without a final landing record")
    errors.extend(cycles(quests))
    return errors


def registry() -> Path:
    """The machine's campaigns: one file per name holding its home, beside the job queue."""
    return jobs_dir().parent / "campaigns"


def campaigns() -> dict[str, Json]:
    """Every registered campaign whose home still holds it: name -> {home, state}."""
    out: dict[str, Json] = {}
    for f in sorted(registry().glob("*")) if registry().is_dir() else []:
        home = Path(f.read_text().strip())
        meta = campaigns_in(home).get(f.name)
        try:
            state = read_json_shared(home / meta / "state.json") if meta else None
        except CampaignError:
            continue
        if state and state.get("campaign") == f.name:
            out[f.name] = {"home": home, "state": state}
    return out


def follow(c: Campaign, dep: str) -> tuple[str, Json | None]:
    """The quest a dependency waits on: itself, or for a dropped quest the one its `into` names, to the
    end of the chain; (its NAME:ID or ID, the quest or None when no campaign here has it)."""
    seen: set[str] = set()
    while True:
        name, _, qid = dep.rpartition(":")
        q = foreign(dep) if name and name != c.name else c.quests.get(qid)
        if q is None or q.get("status") != "dropped" or q.get("into", "none") == "none" or dep in seen:
            return dep, q
        seen.add(dep)
        dep = q["into"] if ":" in q["into"] or not name or name == c.name else f"{name}:{q['into']}"


def most_waited_on(c: Campaign, top: int = 3) -> list[tuple[str, int]]:
    """The heads of the waiting chains across every campaign here: quests not landed that wait on
    nothing unlanded themselves (or are dead ends), with how many quests wait on each, most first."""
    every = {f"{n}:{k}": q for n, o in campaigns().items() for k, q in o["state"].get("quests", {}).items()}
    every.update({f"{c.name}:{k}": q for k, q in c.quests.items()})
    live = {k for k, q in every.items() if q.get("status") not in ("landed", "dropped")}

    def ups(key: str) -> list[str]:
        name, q = key.partition(":")[0], every[key]
        found = (follow(c, d if ":" in d else f"{name}:{d}") for d in [*q.get("needs", []), *q.get("after", [])])
        return [end for end, dq in found if dq is None or dq.get("status") not in ("landed", "dropped")]

    heads: dict[str, set[str]] = {}
    for key in live:
        seen: set[str] = set()
        todo = ups(key)
        while todo:
            d = todo.pop()
            if d in seen:
                continue
            seen.add(d)
            more: list[str] = ups(d) if d in live else []
            if not more:
                heads.setdefault(d, set()).add(key)
            todo += more
    mine = f"{c.name}:"
    ranked = sorted(heads.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:top]
    return [(k.removeprefix(mine), len(v)) for k, v in ranked]


def unstarted_waits(c: Campaign, p: Json) -> list[str]:
    """What keeps a quest from starting: needs not met, and any dependency on no quest."""
    return [d for d in p["needs"] if not c.met(d)] + [d for d in p["after"] if dead_end(c, d)]


def refuse_dead(c: Campaign, deps: list[str]) -> None:
    """Refuses a dependency on no quest, or on a drop with no successor, here or in another campaign."""
    for d in deps:
        here = (d.partition(":")[0] if ":" in d else c.name) in (c.name, *campaigns())
        if here and (why := dead_end(c, d) or abandoned(c, d)):
            die(f"{why}: depend on a quest that can land")


def dead_end(c: Campaign, dep: str) -> str:
    """Why a dependency blocks for good, or "": it names no quest any campaign here has."""
    return f"{dep} names no quest" if follow(c, dep)[1] is None else ""


def abandoned(c: Campaign, dep: str) -> str:
    """Why a dependency no longer blocks, or "": it ends in a drop with no successor (into=none),
    whose work nobody will land, so nothing waits on it."""
    end, q = follow(c, dep)
    return f"{end} was dropped ({q.get('reason', '')}) with no successor" if q and q["status"] == "dropped" else ""


def dependents(c: Campaign, pid: str) -> dict[str, list[str]]:
    """Every live quest, in each campaign here, whose dependency ends at quest PID (through drops)."""
    me, out = f"{c.name}:{pid}", dict[str, list[str]]()
    every = [(c.name, c.quests), *((n, o["state"].get("quests", {})) for n, o in campaigns().items() if n != c.name)]
    for name, quests in every:
        for qid, q in quests.items():
            deps = [
                d for d in [*q.get("needs", []), *q.get("after", [])] if name == c.name or d.startswith(f"{c.name}:")
            ]
            ends = [end if ":" in end else f"{c.name}:{end}" for end, _ in (follow(c, d) for d in deps)]
            if q.get("status") not in ("landed", "dropped") and me in ends:
                out.setdefault(name, []).append(qid)
    return out


def foreign(dep: str) -> Json | None:
    """The quest another campaign's `name:id` dependency names, or None."""
    name, _, qid = dep.partition(":")
    other = campaigns().get(name)
    quest = other["state"].get("quests", {}).get(qid) if other else None
    return quest if isinstance(quest, dict) else None


def clashes(c: Campaign, pid: str) -> list[str]:
    """The active or review quests whose files may overlap this one's in its repository: one writer
    per file set, so `start` refuses them."""
    p, repo = c.quests[pid], c.repo(c.quests[pid])
    return [
        x
        for x, q in c.quests.items()
        if x != pid and q["status"] in ("active", "review") and c.repo(q) == repo and overlap(p["files"], q["files"])
    ]


def starved(c: Campaign, pid: str) -> list[str]:
    """The unlanded quests PID must land after (`after` or `needs`) whose files overlap PID's: while
    PID is active they cannot start, so neither can land."""
    p, repo = c.quests[pid], c.repo(c.quests[pid])
    return [
        d
        for d in dict.fromkeys(p["needs"] + p["after"])
        if (q := c.quests.get(d))
        and q["status"] in ("planned", "blocked")
        and c.repo(q) == repo
        and overlap(p["files"], q["files"])
    ]


def cross_overlaps(c: Campaign, pid: str) -> list[str]:
    """Quests of other campaigns whose files may overlap this one's in the same repository, unless
    either orders the other (--after or needs): landing then enforces the order."""
    p = c.quests[pid]
    mine = {*p["needs"], *p["after"]}
    found: list[str] = []
    for name, o in campaigns().items():
        if name == c.name:
            continue
        for qid, q in o["state"].get("quests", {}).items():
            ordered = f"{name}:{qid}" in mine or f"{c.name}:{pid}" in {*q.get("needs", []), *q.get("after", [])}
            if (
                not ordered
                and q.get("status") in ("active", "review")
                and (o["home"] / q.get("repo", ".")).resolve() == c.repo(p)
                and overlap(p["files"], q.get("files", []))
            ):
                found.append(f"{name}:{qid}")
    return found


def changes(home: Path, q: Json) -> set[str]:
    """What a quest changes for other campaigns: its contracts, and for a quest in another repository
    that repository, by its directory's name."""
    repo = (home / q.get("repo", ".")).resolve()
    return set(q.get("provides", [])) | ({repo.name} if repo != home.resolve() else set())


def consumed(consumes: list[str], changing: set[str]) -> list[str]:
    """The contracts in CONSUMES that CHANGING touches: the same name, or a repository's name for one
    of its contracts named `repository:part`."""
    return sorted(x for x in consumes if x in changing or x.partition(":")[0] in changing)


def cycles(quests: dict[str, Json]) -> list[str]:
    seen: dict[str, int] = {}
    found: list[str] = []

    def visit(pid: str, path: list[str]) -> None:
        if seen.get(pid) == 2:
            return
        if seen.get(pid) == 1:
            found.append("dependency cycle: " + " -> ".join([*path[path.index(pid) :], pid]))
            return
        seen[pid] = 1
        p = quests.get(pid)
        if isinstance(p, dict):
            for dep in [*p.get("needs", []), *p.get("after", [])]:
                if dep in quests:
                    visit(dep, [*path, pid])
        seen[pid] = 2

    for pid in quests:
        visit(pid, [])
    return found


def register(name: str, home: Path) -> None:
    entry = registry() / name
    if not entry.exists() or entry.read_text().strip() != str(home):
        registry().mkdir(parents=True, exist_ok=True)
        entry.write_text(f"{home}\n")


def parse_check(spec: str, later: bool = False) -> Json:
    head, sep, cmd = spec.partition("=")
    parts = head.split(":")
    if not sep or not cmd.strip() or not parts[0] or len(parts) > 3:
        die(f"--check NAME[:LEVEL[:MINUTES]]=CMD, not {spec!r}")
    level = parts[1] if len(parts) > 1 and parts[1] else "light"
    minutes = parts[2] if len(parts) > 2 else str(SHORT_RUN if level in HEAVY else 30)
    if level not in LEVELS or not minutes.isdigit() or int(minutes) < 1:
        die(f"--check {spec!r}: level is one of {', '.join(LEVELS)}, minutes a positive integer")
    if level in HEAVY and int(minutes) > SHORT_RUN:
        die(f"--check {spec!r}: a {level} check lasts at most {SHORT_RUN} minutes; split it")
    check: Json = {"name": parts[0], "level": level, "minutes": int(minutes), "cmd": cmd.strip()}
    if later:
        check["later"] = True
    return check


MILESTONE_RUN = re.compile(r"asan|ubsan|tsan|crash|fuzz|mutant", re.I)


def placed(spec: str, risk: str) -> Json:
    """A --check: sanitizer, crash, fuzz and mutant runs are milestone checks, save on CI and, at high
    risk, crash and TSan runs, which gate a format, durability or concurrency claim."""
    runs = {x.lower() for x in MILESTONE_RUN.findall(spec)}
    gate = spec.partition("=")[2].strip().startswith(CI_CHECK) or (
        risk in ("high", "critical") and runs <= {"crash", "tsan"}
    )
    return parse_check(spec, later=bool(runs) and not gate)


def gates(p: Json) -> list[Json]:
    return [x for x in p["checks"] if not x.get("later")]


def declare_inputs(p: Json, pid: str, items: list[str]) -> None:
    """Applies `--inputs NAME=GLOBS` to the quest's checks: a job of that check is identified by the
    files under GLOBS (and the build files and paths its command names), not by the whole tree. An
    empty GLOBS clears them. At least one gate check keeps whole-tree identity: the build-test."""
    for item in items:
        name, sep, globs = item.partition("=")
        check = next((x for x in p["checks"] if x["name"] == name), None)
        if not sep or not name:
            die(f"--inputs NAME=GLOBS, not {item!r}")
        if check is None:
            die(f"--inputs {name}: {pid} has no check {name}")
        if check["cmd"].startswith(CI_CHECK):
            die(f"--inputs {name}: a CI check runs on GitHub at the exact commit; inputs scope jobs")
        check.pop("inputs", None)
        if csv(globs):
            check["inputs"] = csv(globs)
    if any(x.get("inputs") for x in p["checks"]) and all(x.get("inputs") for x in gates(p)):
        die(
            f"{pid}: every gate check declares --inputs; keep one without them, the build-test `land` runs at the exact code"
        )


def deferred(p: Json) -> list[Json]:
    return [x for x in p["checks"] if x.get("later")]


def edit_checks(p: Json, specs: list[str], later: list[str]) -> list[str]:
    """Applies `set --check SPECS --later LATER` by name: a named check is replaced in place or added,
    `NAME=` removes it, `--later ''` removes the deferred ones, and every other check stays. Returns
    the names it removed."""
    old = [x["name"] for x in p["checks"]]
    checks = [x for x in p["checks"] if not ("" in later and x.get("later"))]
    named: set[str] = set()
    for spec, is_later in [(x, False) for x in specs] + [(x, True) for x in later if x]:
        head, sep, cmd = spec.partition("=")
        name = head.split(":")[0]
        if name in named:
            die(f"--check {name}: named twice")
        named.add(name)
        at = next((i for i, x in enumerate(checks) if x["name"] == name), None)
        rest = [x for x in checks if x["name"] != name]
        if sep and not cmd.strip():
            if at is None:
                die(f"--check {name}=: the quest has no check {name} to remove")
            checks = rest
            continue
        new = parse_check(spec, later=True) if is_later else placed(spec, p["risk"])
        rest.insert(len(rest) if at is None else at, new)
        checks = rest
    p["checks"] = checks
    kept = {x["name"] for x in checks}
    return [x for x in old if x not in kept]


def lens_floor(risk: str, lenses: list[str]) -> list[str]:
    """The reviews RISK requires that LENSES lack: high and critical a code review, critical a design."""
    need = (
        []
        if risk in ("low", "normal") or set(lenses) & set(CODE_LENSES)
        else ["a code review (correctness, security or durability)"]
    )
    return need + (["a design review"] if risk == "critical" and "design" not in lenses else [])


def weakening(before: Json, after: Json) -> list[str]:
    """What a change to a quest's risk, lenses or checks removes from what it must pass."""
    out: list[str] = []
    if RISKS.index(after["risk"]) < RISKS.index(before["risk"]):
        out.append(f"risk {before['risk']} -> {after['risk']}")
    out += [f"lens {x} removed" for x in before["lenses"] if x not in after["lenses"]]
    new = {x["name"]: x for x in after["checks"]}
    for old in before["checks"]:
        kind = "deferred check" if old.get("later") else "gate check"
        now = new.get(old["name"])
        if now is None:
            out.append(f"{kind} {old['name']} removed")
        elif now.get("later") and not old.get("later"):
            out.append(f"gate check {old['name']} deferred")
        elif (now["cmd"], now["level"]) != (old["cmd"], old["level"]):
            out.append(f"{kind} {old['name']} changed")
        elif now.get("inputs") and not set(old.get("inputs") or ["**"]) <= set(now["inputs"]):
            out.append(f"{kind} {old['name']} inputs narrowed to {','.join(now['inputs'])}")
    return out


def stem(glob: str) -> str:
    cuts = [i for i in (glob.find(ch) for ch in "*?[") if i >= 0]
    return glob[: min(cuts)] if cuts else glob


def overlap(a: list[str], b: list[str]) -> bool:
    """Whether two file sets may share a file (no globs means the whole repository): exact when one
    side names a file, by the globs' fixed prefixes when both are patterns."""

    def pair(x: str, y: str) -> bool:
        if stem(x) == x or stem(y) == y:  # a file: does the other side match it
            return fnmatch.fnmatch(x, y) or fnmatch.fnmatch(y, x)
        return stem(x).startswith(stem(y)) or stem(y).startswith(stem(x))

    if not a or not b:
        return True
    return any(pair(x, y) for x in a for y in b)


def outside(paths: list[str], globs: list[str]) -> list[str]:
    return [x for x in paths if not is_meta(x) and globs and not any(fnmatch.fnmatch(x, g) for g in globs)]


CRITERION = re.compile(r"^\s*[-*]\s*\**A\d+\b.*(?:\n[ \t]+\S.*)*", re.M)  # `- A1 ...` and its wrapped lines


def criteria_hash(c: Campaign, pid: str) -> str:
    """The quest file's criterion lines, hashed: editing them, not its prose, voids acceptances."""
    brief = c.home / c.meta / "quests" / f"{pid}.md"
    lines = CRITERION.findall(brief.read_text() if brief.exists() else "")
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16]


def worktree_path(repo: Path, campaign: str, name: str) -> Path:
    return repo.parent / f"{repo.name}-wt" / campaign / name


def branches(c: Campaign, pid: str) -> list[tuple[str, str, str, str]]:
    """(name, branch, worktree, base) for the quest and its tasks."""
    p = c.quests[pid]
    out: list[tuple[str, str, str, str]] = []
    if p.get("branch"):
        out.append((pid, p["branch"], p.get("worktree", ""), p.get("base", "")))
    for task, t in sorted(p.get("tasks", {}).items()):
        if not t.get("merged"):  # a merged task's branch and worktree are gone; its journal is the quest's
            out.append((f"{pid}--{task}", t["branch"], t.get("worktree", ""), t.get("base", "")))
    return out
