"""Quest commands: init, config, new, get, set, start and accept."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from .campaign import blockers, find_campaign, hunt_row, park_met, park_target, writing
from .core import (
    DEFAULT_LENSES,
    LENSES,
    META,
    NAME_RE,
    RISKS,
    SET_STATUS,
    Json,
    atomic_write,
    csv,
    die,
    git,
    git_ok,
    jobs_dir,
    stamp,
    template,
    write_json,
)
from .disk import DISK_WARN_GB
from .evidence import unparsed_criteria
from .jobs import load_job
from .journals import journal_worktree
from .leads import tell_dependents, tell_peers, unlocks
from .mail import notify
from .repo import has_ref, main_branch, main_checkout, merge_base, rev, same_repo, toplevel
from .reuse import own_blobs
from .reviews import blob_id, confirmations, drift, live_task, own_diffs, read_review, review_path
from .state import (
    campaigns,
    campaigns_in,
    ci_owned,
    clashes,
    criteria_hash,
    cross_overlaps,
    declare_inputs,
    deferred,
    dependents,
    edit_checks,
    follow,
    gates,
    lens_floor,
    overlap,
    parse_check,
    placed,
    refuse_dead,
    register,
    starved,
    weakening,
    worktree_path,
    writes,
)


def cmd_init(a: argparse.Namespace) -> int:
    top = toplevel(Path.cwd())
    if top != main_checkout(top):
        die("run init in the repository's main checkout, not in a worktree")
    name = a.name or top.name
    if not NAME_RE.fullmatch(name):
        die(f"campaign name {name!r}: use letters, digits, '.', '_' and '-' (--name)")
    if name in campaigns_in(top):
        die(f"{top} already holds campaign {name}")
    taken = campaigns().get(name)
    if taken and taken["home"] != top:
        die(f"campaign {name} already runs from {taken['home']} on this machine: pick another --name")
    folder = f"{META}/{name}"
    write_json(top / folder / "state.json", {"version": 2, "campaign": name, "ci": a.ci, "quests": {}})
    register(name, top)
    for doc in ("PLAN.md", "hunt.md"):
        if not (top / folder / doc).exists():
            atomic_write(top / folder / doc, template(doc, campaign=name))
    if os.environ.get("CAMPAIGN_NO_COMMIT") != "1":
        git(top, "add", "--", folder)
        git(top, "commit", "-q", "-m", f"campaign: init {name}", "--", folder)
    print(f"campaign {name} in {top}; write the goals in {folder}/PLAN.md")
    return 0


CONFIG = ("ci", "ci_later", "suite", "peers", "outbox", "disk_warn", "tested_docs", "pin_check", "env")


def cmd_config(a: argparse.Namespace) -> int:
    """Prints the campaign-wide settings as key=value lines, KEYs' values, or sets KEY=VALUEs."""
    keys = [x for x in a.assignments if "=" not in x]
    if keys and len(keys) < len(a.assignments):
        die("give either KEYs to read or KEY=VALUE assignments")
    unknown = [k for k in keys if k not in CONFIG and not k.startswith("suite.")]
    if unknown:
        die(f"no setting {', '.join(unknown)}; the settings are {', '.join(CONFIG)}")
    if not a.assignments or keys:
        c = find_campaign(Path.cwd())
    else:
        with writing() as c:
            for item in a.assignments:
                key, sep, value = item.partition("=")
                if key == "ci" and sep and value in ("github", ""):
                    c.state["ci"] = value
                elif key == "ci_later" and sep:
                    c.state["ci_later"] = csv(value)
                elif key == "peers" and sep and all(re.fullmatch(r"[\w.-]+=\S+", x) for x in csv(value)):
                    c.state["peers"] = csv(value)
                elif key in ("outbox", "suite", "pin_check") and sep:
                    c.state[key] = value
                elif key.startswith("suite.") and NAME_RE.fullmatch(key[6:]) and sep:
                    c.state.setdefault("suites", {})[key[6:]] = value
                    c.state["suites"] = {k: v for k, v in c.state["suites"].items() if v}
                elif key == "tested_docs" and sep:
                    c.state["tested_docs"] = csv(value)
                elif key == "env" and sep and all(re.fullmatch(r"[A-Za-z_]\w*=\S*", x) for x in csv(value)):
                    c.state["env"] = csv(value)  # every job's environment, read when it runs
                elif key == "disk_warn" and sep and re.fullmatch(r"\d+(\.\d+)?|", value):
                    c.state.pop("disk_warn", None)
                    if value:
                        c.state["disk_warn"] = float(value)
                else:
                    die(
                        "campaign config takes ci=github|'', ci_later=WORKFLOW,..., suite[.REPO]=CMD, peers=NAME=URL,..., "
                        f"outbox=REMOTE, disk_warn=GIB|'', tested_docs=GLOB,..., pin_check=CMD and env=NAME=VALUE,..., not {item!r}"
                    )
            c.save("campaign: config " + " ".join(a.assignments))
    suites = {f"suite.{k}": v for k, v in c.state.get("suites", {}).items()}
    for key in keys or [*CONFIG, *suites]:
        value = suites.get(key) or c.state.get(key, DISK_WARN_GB if key == "disk_warn" else "") or ""
        text = ",".join(value) if isinstance(value, list) else f"{value:g}" if isinstance(value, float) else str(value)
        print(text if keys else f"{key}={text}")
    return 0


def cmd_new(a: argparse.Namespace) -> int:
    with writing() as c:
        if a.id in c.quests:
            die(f"{a.id} exists")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", a.id) or "--" in a.id:
            die(f"quest id {a.id!r}: use lowercase letters, digits and single dashes")
        repo = (c.home / a.repo).resolve()
        if not git_ok(repo, "rev-parse", "--git-dir"):
            die(f"{repo} is not a git repository")
        lenses = csv(a.lenses) if a.lenses is not None else list(DEFAULT_LENSES[a.risk])
        need = lens_floor(a.risk, lenses)
        if need:
            die(f"{a.risk} risk needs {' and '.join(need)}: --lenses {','.join(lenses + DEFAULT_LENSES[a.risk])}")
        checks = [placed(x, a.risk) for x in a.check]
        moved = [x["name"] for x in checks if x.get("later")]
        checks += [parse_check(x, later=True) for x in a.later]
        if not gates({"checks": checks}):
            home = same_repo(repo, c.home)
            suite = c.state.get("suite") if home else c.state.get("suites", {}).get(repo.name)
            if not suite:
                key = "suite" if home else f"suite.{repo.name}"
                die(f"no gate: --check NAME=CMD, or `campaign config {key}=CMD` (the native build plus tests)")
            checks.insert(0, parse_check(f"suite={suite}"))
        if local := ci_owned(checks, repo):
            die(f"{a.id}: {'; '.join(local)}; to keep it local, add it by `campaign set --check` with --weaken REASON")
        refuse_dead(c, csv(a.needs) + csv(a.after))
        c.quests[a.id] = {
            "title": a.title,
            "status": "planned",
            "risk": a.risk,
            "repo": a.repo,
            "needs": csv(a.needs),
            "after": csv(a.after),
            "provides": csv(a.provides),
            "consumes": csv(a.consumes),
            "files": csv(a.files),
            "checks": checks,
            "lenses": list(dict.fromkeys(lenses)),
            "milestone": a.milestone or "",
            **({"hunt": hunt_row(c, a.hunt)} if a.hunt else {}),
            "accepted": {},
            "landed": [],
            "updated": stamp(),
        }
        declare_inputs(c.quests[a.id], a.id, a.inputs)
        brief = f"{c.meta}/quests/{a.id}.md"
        if not (c.home / brief).exists():
            atomic_write(c.home / brief, template("quest.md", id=a.id, title=a.title, risk=a.risk))
        c.save(f"campaign: new {a.id} ({a.risk}): {a.title}", (brief,))
    print(f"{a.id}: planned; write its outcome and criteria in {brief}")
    for name in moved:
        print(f"{name}: a milestone check (a sanitizer, crash, fuzz or mutant run)")
    return 0


QUEST_KEYS = (
    "title",
    "status",
    "risk",
    "reason",
    "into",
    "repo",
    "milestone",
    "hunt",
    "needs",
    "after",
    "provides",
    "consumes",
)
QUEST_KEYS += ("files", "lenses", "check", "later", "inputs", "branch", "worktree", "journal", "base", "tasks")
QUEST_KEYS += ("landed",)
TASK_KEYS = ("files", "status", "reason", "branch", "worktree", "journal", "base")


def fields(record: Json, keys: tuple[str, ...]) -> list[tuple[str, str]]:
    """A quest's or task's fields as (key, value) pairs, values in the form `set` takes."""
    out: list[tuple[str, str]] = []
    for key in keys:
        if key in ("check", "later"):
            out += [
                (key, f"{c['name']}:{c['level']}:{c['minutes']}={c['cmd']}")
                for c in record.get("checks", [])
                if c.get("later", False) == (key == "later")
            ]
        elif key == "inputs":
            out += [(key, f"{c['name']}={','.join(c['inputs'])}") for c in record.get("checks", []) if c.get("inputs")]
        elif key == "tasks":
            out.append((key, ",".join(record.get("tasks", {}))))
        elif key == "landed":
            out.append((key, ",".join(x["commit"][:12] for x in record.get("landed", []))))
        else:
            value = record.get(key, "active" if key == "status" else "")
            out.append((key, ",".join(value) if isinstance(value, list) else str(value)))
    return out


def cmd_get(a: argparse.Namespace) -> int:
    c = find_campaign(Path.cwd())
    p = c.quest(a.id)
    record, known = p, QUEST_KEYS
    if a.task:
        record = p.get("tasks", {}).get(a.task)
        if record is None:
            die(f"{a.id} has no task {a.task}")
        known = TASK_KEYS
    unknown = [k for k in a.keys if k not in known]
    if unknown:
        die(f"no field {', '.join(unknown)}; the fields are {', '.join(known)}")
    for key, value in fields(record, tuple(a.keys) or known):
        print(value if a.keys else f"{key}={value}")
    return 0


def cmd_set(a: argparse.Namespace) -> int:
    with writing() as c:
        p = c.quest(a.id)
        if a.task:
            task = p.get("tasks", {}).get(a.task)
            if task is None:
                die(f"{a.id} has no task {a.task}")
            given = dict(x.partition("=")[::2] for x in a.assignments)
            if not given or set(given) - {"files", "status", "reason"} or a.check or a.later or a.inputs:
                die("set --task takes files=GLOBS, or status=dropped reason=R (status=active undoes it)")
            if "files" in given:
                files = csv(given["files"])
                if not files:
                    die("a task's file set cannot be empty")
                clash = [
                    t
                    for t, x in p["tasks"].items()
                    if t != a.task and live_task(x) and overlap(files, x.get("files", []))
                ]
                if clash:
                    die(f"task {a.task}'s files overlap tasks {', '.join(clash)}")
                task["files"] = files
            if given.get("status") == "dropped":
                if not given.get("reason"):
                    die("dropping a task needs reason=R: its unmerged work will not land")
                task.update(status="dropped", reason=given["reason"])
            elif given.get("status") == "active":
                task.pop("status", None)
                task.pop("reason", None)
            elif "status" in given:
                die("a task's status is dropped or active")
            p["updated"] = stamp()
            c.save(f"campaign: set {a.id} task {a.task} " + " ".join(a.assignments))
            print(f"{a.id} task {a.task}: " + ("dropped" if task.get("status") == "dropped" else "updated"))
            return 0
        if "status=dropped" not in a.assignments and (why := unparsed_criteria(c, a.id, filled=False)):
            die(why)
        waited, was, waiting = blockers(c), p["status"], dependents(c, a.id)
        held, crossed = clashes(c, a.id), cross_overlaps(c, a.id)  # overlaps it had: narrowing never refuses
        before = {k: json.loads(json.dumps(p[k])) for k in ("risk", "lenses", "checks")}
        for item in a.assignments:
            key, sep, value = item.partition("=")
            if not sep:
                die(f"expected key=value, not {item!r}")
            if key == "status":
                if value not in SET_STATUS.get(p["status"], ()):
                    die(f"{a.id}: {p['status']} cannot become {value} by set (start and land do that)")
                p["status"] = value
            elif key in ("needs", "after", "provides", "consumes", "files", "lenses", "until"):
                p[key] = csv(value)
            elif key == "risk" and value not in RISKS:
                die(f"risk is one of {', '.join(RISKS)}, not {value!r}")
            elif key in ("title", "risk", "reason", "repo", "milestone", "into"):
                p[key] = value
            elif key == "hunt":
                p.pop("hunt", None)
                if value:
                    p["hunt"] = hunt_row(c, value)
            else:
                die(
                    f"no field {key} to set; the fields are title, status, risk, reason, into, until, repo, milestone, hunt, "
                    f"needs, after, provides, consumes, files and lenses (`campaign get {a.id}`), and --check, "
                    "--later and --inputs"
                )
        forced = ""
        if writes(p) and (fresh := [x for x in clashes(c, a.id) if x not in held]):
            shared = "; ".join(
                f"{x} ({', '.join(g for g in c.quests[x]['files'] if overlap(p['files'], [g])) or 'every file'})"
                for x in fresh
            )
            if not a.force:
                die(f"{a.id}'s files would overlap {shared} (one writer per file set): narrow files, or --force REASON")
            forced = f" (forced over {', '.join(fresh)}: {a.force})"
        elif a.force:
            die("--force: these files overlap no other quest's held files")
        for other in cross_overlaps(c, a.id) if writes(p) else []:
            if other not in crossed:
                print(f"warning: {a.id}'s files may overlap {other}: order them (after={other}) or narrow files", file=sys.stderr)
        dropped = edit_checks(p, a.check, a.later)
        if a.check or a.later:
            kept = {x["cmd"] for x in deferred(p)}  # a removed deferred check's runs stop counting
            for record in p["landed"]:
                runs = [load_job(jobs_dir() / j) for j in record.get("later", [])]
                record["later"] = [j["id"] for j in runs if j["spec"].get("cmd", "") in kept or not j["spec"]]
        if a.check or a.later or a.inputs:
            declare_inputs(p, a.id, a.inputs)
        if RISKS.index(p["risk"]) > RISKS.index(before["risk"]):  # a raised risk brings its reviews
            p["lenses"] = list(dict.fromkeys(p["lenses"] + DEFAULT_LENSES[p["risk"]]))
        need = lens_floor(p["risk"], p["lenses"])
        if need:
            die(f"{a.id} is {p['risk']} risk and needs {' and '.join(need)}; lower the risk instead (--weaken REASON)")
        weakened = weakening(before, p)
        if weakened and not a.weaken:
            die(
                f"{a.id}: this weakens what it must pass ({'; '.join(weakened)}): --weaken REASON records why "
                "and voids its review acceptances"
            )
        local = ci_owned([x for x in gates(p) if x not in before["checks"]], c.repo(p))
        if local and not a.weaken:
            die(f"{a.id}: {'; '.join(local)}; --weaken REASON keeps it local")
        after = {k: p[k] for k in before}
        if after != before:
            voided: list[str] = sorted(p.get("accepted", {})) if weakened or p["risk"] != before["risk"] else []
            if voided:
                p["accepted"].clear()
            p.setdefault("history", []).append(
                {
                    "at": stamp(),
                    **{k: [before[k], after[k]] for k in before if before[k] != after[k]},
                    "weakened": weakened,
                    "reason": a.weaken or "",
                    "voided": voided,
                }
            )
        elif a.weaken:
            die("--weaken: nothing here weakens the quest")
        if p.get("into") and p["status"] != "dropped":
            die("into= names the quest a drop folds into: set it with status=dropped")
        if p.get("into", "none") != "none" and follow(c, p["into"])[1] is None:
            die(f"into={p['into']} names no quest")
        if any(x.startswith(("needs=", "after=")) for x in a.assignments):
            refuse_dead(c, [*p["needs"], *p["after"]])
        if p["status"] == "dropped" and was != "dropped" and waiting:
            if not p.get("into"):
                who = ", ".join(k if n == c.name else f"{n}:{k}" for n, ids in waiting.items() for k in ids)
                die(
                    f"{who} wait on {a.id}: drop it with into=SUCCESSOR (they wait on that quest) or "
                    f"into=none (nobody lands its work; they stop waiting on it)"
                )
            tell_dependents(c, a.id, p, waiting)
        if p["status"] not in ("blocked", "dropped") and not any(x.startswith("reason=") for x in a.assignments):
            p.pop("reason", None)
        if p["status"] != "blocked":
            p.pop("until", None)
        elif not p.get("until"):
            die("a park names until=IDS: quests to land, PLAN decision ids, or owner")
        else:
            kinds = {x: park_target(c, x) for x in p["until"]}
            needed = [x for x, kind in kinds.items() if kind == "quest" and x in p["needs"] and not p.get("branch")]
            if needed:
                die(f"{a.id} needs {', '.join(needed)} already: that block is derived; leave it planned")
            if met := park_met(c, p):
                die(f"{a.id}: its park is {met} already")
        p["updated"] = stamp()
        opened = unlocks(c, waited, f"{a.id} dropped" if p["status"] == "dropped" else f"set {a.id}")
        c.save(
            f"campaign: set {a.id} "
            + " ".join(
                a.assignments
                + [f"check={x}" for x in a.check]
                + [f"later={x}" for x in a.later]
                + [f"inputs={x}" for x in a.inputs]
            )
            + (f" (weakened: {'; '.join(weakened)}; why: {a.weaken})" if weakened else "")
            + forced
        )
        if any(x == "status=blocked" for x in a.assignments):
            notify(f"Blocked: {a.id}", p.get("reason", ""))
    if dropped:
        print(f"dropped: {', '.join(dropped)}")
    print(f"{a.id}: {p['status']}")
    if opened:
        print(opened)
    return 0


def cmd_start(a: argparse.Namespace) -> int:
    with writing() as c:
        p = c.quest(a.id)
        repo = c.repo(p)
        main = main_branch(repo)
        if a.task:
            if p["status"] not in ("active", "review"):
                die(f"{a.id} is {p['status']}: start the quest before its tasks")
            if a.task in p.get("tasks", {}):
                die(f"{a.id} already has task {a.task}")
            files = csv(a.files) or p["files"]
            clash = [t for t, x in p.get("tasks", {}).items() if live_task(x) and overlap(files, x.get("files", []))]
            if clash and not a.force:
                die(f"task {a.task}'s files overlap tasks {', '.join(clash)}: give --files, or --force REASON")
            recorded = p.get("tasks", {}).get(a.task, {}).get("branch")
            name, base = f"{a.id}--{a.task}", rev(repo, p["branch"])
            branch = recorded or f"quest/{c.name}/{name}"
        else:
            if p["status"] not in ("planned", "blocked"):
                die(f"{a.id} is {p['status']}")
            if why := unparsed_criteria(c, a.id):
                die(why)
            unmet = [d for d in p["needs"] if not c.met(d)]
            if unmet and not a.force:
                die(f"{a.id} needs {', '.join(unmet)} landed first (--force REASON to start anyway)")
            if (first := starved(c, a.id)) and not a.force:
                die(
                    f"{a.id} lands after {', '.join(first)}, whose files it would hold, so they could never start: "
                    f"narrow {a.id}'s files to what it changes before them, or --force REASON"
                )
            clash = clashes(c, a.id)
            if clash and not a.force:
                die(
                    f"{a.id}'s files may overlap {', '.join(clash)} (one writer per file set): narrow files, order them with needs, or --force REASON"
                )
            name, branch, base = a.id, p.get("branch") or f"quest/{c.name}/{a.id}", rev(repo, main)
            for other in cross_overlaps(c, a.id):
                print(
                    f"warning: {a.id}'s files may overlap {other}: order them (--after {other}) or narrow files",
                    file=sys.stderr,
                )
        wt = worktree_path(repo, c.name, name)
        apart = not same_repo(repo, c.home)  # a quest in another repository keeps its journal in the home
        for path in [wt, *([worktree_path(c.home, c.name, name)] if apart else [])]:
            if path.exists():
                die(f"{path} exists")
        if has_ref(repo, branch):
            git(repo, "worktree", "add", "-q", str(wt), branch)
            base = merge_base(repo, base, branch)
        else:
            git(repo, "worktree", "add", "-q", "-b", branch, str(wt), base)
        gitdir = Path(git(wt, "rev-parse", "--path-format=absolute", "--git-dir"))
        (gitdir / "campaign-home").write_text(f"{c.home}\n{c.name}\n")
        jwt = journal_worktree(c, name, branch) if apart else wt
        work = Path(c.meta) / "journal" / f"{name}.md"
        if not (jwt / work).exists():
            atomic_write(jwt / work, template("journal.md", id=name, brief=f"{c.meta}/quests/{a.id}.md"))
            git(jwt, "add", "--", str(work))
            git(jwt, "commit", "-q", "-m", f"campaign: start {name}", "--", str(work))
        rel = os.path.relpath(wt, c.home)
        journal = {"journal": os.path.relpath(jwt, c.home)} if apart else {}
        if a.task:
            p.setdefault("tasks", {})[a.task] = {
                "branch": branch,
                "worktree": rel,
                **journal,
                "base": base,
                "files": csv(a.files) or p["files"],
            }
        else:
            p.update(branch=branch, worktree=rel, base=base, status="active", **journal)
            if a.force:
                p["reason"] = f"started by force: {a.force}"
        p["updated"] = stamp()
        c.save(f"campaign: start {name} on {branch} at {base[:12]}")
    print(f"{name}: {branch} in {wt}" + (f"; its journal in {jwt}" if apart else ""))
    if not a.task:
        for peer in tell_peers(c, a.id):
            print(f"told campaign {peer}'s lead that {a.id} overlaps its work (a message)")
    return 0


def cmd_accept(a: argparse.Namespace) -> int:
    with writing() as c:
        p = c.quest(a.id)
        if a.lens not in LENSES:
            die(f"unknown lens {a.lens}")
        if not p.get("branch"):
            die(f"{a.id} has no branch")
        repo = c.repo(p)
        commit = rev(repo, a.commit or p["branch"])
        main_head = rev(repo, main_branch(repo))
        path = review_path(c, a.id, a.lens)
        rel = path.relative_to(c.home)
        reviewed = ""
        findings: dict[str, Json] = {}
        fix_scope: Json = {}
        if not path.exists():
            die(f"no review at {rel}")
        data = path.read_bytes()  # parsed and identified from one read
        r = read_review(data.decode(errors="replace"))
        blocking = [f["id"] for f in r["findings"] if f["blocking"]]
        if r["verdict"] not in ("APPROVE", "CHANGES", "REJECT"):
            die(
                f"{rel}: the verdict is {r['verdict'] or 'missing'!r}, not one of APPROVE, CHANGES, REJECT; a "
                "conditional approval is CHANGES, its conditions blocking findings"
            )
        if r["verdict"] == "REJECT":
            die(f"{a.lens} review rejected {a.id}: re-plan the quest")
        if r["verdict"] == "APPROVE" and blocking:
            die(
                f"{rel}: APPROVE but lists blocking findings {', '.join(blocking)}: the reviewer corrects one or the other"
            )
        if r["verdict"] == "CHANGES" and not blocking:
            die(f"{rel}: CHANGES but lists no findings table")
        if r["verdict"] == "APPROVE" and a.confirm:
            die("an APPROVE needs no --confirm")
        seen: dict[str, str] = {}
        docs = c.state.get("tested_docs", [])
        if a.lens != "design":  # a design is reviewed before the code exists
            if not r["commit"] or not git_ok(repo, "cat-file", "-e", f"{r['commit']}^{{commit}}"):
                die(f"{rel}: no 'Commit:' line naming the reviewed commit in {repo}")
            reviewed = rev(repo, r["commit"])
            seen = own_blobs(repo, main_head, reviewed)
            since = drift(repo, main_head, commit, seen, diffs=own_diffs(repo, main_head, reviewed), tested=docs)
            if r["verdict"] == "APPROVE" and since:
                die(
                    f"the review approved {reviewed[:12]}; {commit[:12]} changes {', '.join(since[:8])} since: "
                    f"have the reviewer review them, or accept --commit {reviewed[:12]}"
                )
            defects = [f["id"] for f in r["findings"] if f["blocking"] and f["kind"] == "defect"]
            if r["verdict"] == "CHANGES" and not since and defects:
                die(
                    f"{', '.join(defects)}: blocking defects, and {commit[:12]} has the code the review saw: fix "
                    "them, or the reviewer withdraws them"
                )
            unseen = [x for x in since if x not in seen]
            if unseen and r["verdict"] == "CHANGES":
                if not a.fix_scope:
                    die(
                        f"the fix changes {', '.join(unseen[:8])}, which the {a.lens} review never saw: have the reviewer "
                        "review the fix (the review names the fixed commit and approves), or --fix-scope REASON when "
                        "it adds no mechanism (a regression test for a finding)"
                    )
                fix_scope = {"paths": unseen, "reason": a.fix_scope}
        if r["verdict"] == "CHANGES":
            findings = confirmations(repo, commit, reviewed, table=r["findings"], given=a.confirm)
        p.setdefault("accepted", {})[a.lens] = {
            "commit": commit,
            "base": merge_base(repo, main_head, commit),
            "criteria": criteria_hash(c, a.id),
            "by": "review",
            "verdict": r["verdict"],
            "note": a.note or "",
            "review": blob_id(c.home, data),
            "reviewed": reviewed,
            "findings": findings,
            **({"fix_scope": fix_scope} if fix_scope else {}),
            "blobs": own_blobs(repo, main_head, commit),
            "diffs": own_diffs(repo, main_head, commit),
            "at": stamp(),
        }
        if a.lens not in p["lenses"]:
            p["lenses"].append(a.lens)
        p["updated"] = stamp()
        # the review is committed with its acceptance, so git holds exactly what was accepted
        c.save(f"campaign: accept {a.id} {a.lens} at {commit[:12]}", (str(rel),))
    for fid, f in findings.items():
        if f["kind"] == "defect" and not f["reproduced"]:
            print(f"note: {fid} is a defect and no job shows its probe failing at the reviewed {reviewed[:12]}")
    print(f"{a.id}: {a.lens} accepted at {commit[:12]} ({len(p['accepted'][a.lens]['blobs'])} paths)")
    return 0
