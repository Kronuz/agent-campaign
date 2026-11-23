"""Criteria and their evidence: the quest file's criteria, journal rows, tagged jobs and checks."""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Callable, Collection
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .ci import ci_covers, ci_gate, ci_job
from .core import CI_CHECK, CI_CITE, FAILED, CampaignError, Json, csv, die, jobs_dir, section, table_cells
from .jobs import FAILS_WITHOUT, all_jobs, derived, fails_without_miss, load_job
from .journals import decisions
from .repo import SHA, file_at, read_object, same_repo
from .reuse import descends, docs_note, job_at, job_for_check, shape
from .state import gates

if TYPE_CHECKING:  # annotations only: they import this module, so importing them here is a cycle
    from .campaign import Campaign


PROOF = ("job", "artifact", "decision", "manual", "ci")  # what a criterion's evidence must be; job by default


def criteria(c: Campaign, pid: str) -> dict[str, Json]:
    """A quest's acceptance criteria (A1, A2, ...) from its quest file: the first line of each and the
    kind of evidence it needs, `- A2 [artifact] ...` (job when untagged)."""
    brief = c.home / c.meta / "quests" / f"{pid}.md"
    text = section(brief.read_text() if brief.exists() else "", CRITERIA_HEADING)
    found: dict[str, Json] = {}
    tags = "|".join(PROOF)
    shape = rf"^\s*[-*]\s*\**(A\d+)\b\**[:.]?\**\s*(?:\[({tags})\]\s*)?(.*)$((?:\n[ \t]+\S.*)*)"
    for cid, kind, line, wrapped in re.findall(shape, text, re.M):
        if not line.startswith("<"):  # the template's unfilled placeholder
            whole = " ".join(f"{line}{wrapped}".split())
            found.setdefault(cid, {"text": line.strip(), "kind": kind or "job", "whole": whole})
    return found


CRITERIA_HEADING = "Acceptance criteria"


def unparsed_criteria(c: Campaign, pid: str, *, filled: bool = True) -> str:
    """Why a quest file's criteria cannot be read, or "": its `## Acceptance criteria` heading is missing
    (the commonest slip is another title, which parses as no criteria and lands unchecked), or, when
    FILLED, the section still holds only the template's placeholder. `None ...` is a deliberate answer."""
    brief = c.home / c.meta / "quests" / f"{pid}.md"
    text = brief.read_text() if brief.exists() else ""
    if not re.search(rf"^## {CRITERIA_HEADING}\s*$", text, re.M):
        other = re.findall(r"^## (.*[Cc]riteri.*?)\s*$", text, re.M)
        seen = f" (it has `## {other[0]}`)" if other else ""
        return f"{pid}'s quest file has no `## {CRITERIA_HEADING}` heading{seen}: its criteria parse as none"
    if (
        filled
        and not criteria(c, pid)
        and re.search(r"^\s*[-*]\s*\**A\d+\b[^\n]*<", section(text, CRITERIA_HEADING), re.M)
    ):
        return f"{pid}'s `## {CRITERIA_HEADING}` still holds the template's placeholder: write its criteria (or `None`)"
    return ""


def proof(c: Campaign, repo: Path, commits: list[str]) -> Callable[[str, str], list[str]]:
    """What keeps an Evidence row's text from proving a criterion of a kind, at one of these commits:
    a job criterion needs a job that passed at the code; an artifact one a `path` in the commit; a
    decision one an id from PLAN.md's Decisions table; a manual one only a non-empty row. Every job a row
    names must have passed, whatever its kind."""
    job = job_problem(repo, commits)
    recorded = decisions(c)

    def check(kind: str, text: str) -> list[str]:
        cited = list(dict.fromkeys(JOB_ID.findall(text)))
        why = [w for w in map(job, cited) if w]
        for name in dict.fromkeys(CI_CITE.findall(text)):
            state = ci_job(repo, commits[0], name.strip()) if commits else "no commit"
            if state == "success":
                cited.append(f"ci:{name}")
            else:
                why.append(state)
        for path, quote in QUOTED.findall(text):
            if commits and not any(quoted(repo, k, path, quote) for k in commits):
                why.append(f'"{quote[:60]}" is not in {path} at {commits[0][:12]}')
        bare = QUOTE.sub("", text)  # what a quote says is never a path
        if kind == "job" and not cited:
            why.append(
                "names no job (a job criterion needs a job id or a `ci:JOB`; tag it [manual] or [artifact] otherwise)"
            )
        elif kind == "artifact":
            paths = re.findall(r"`([^`\s]+)`", bare)
            missing = [x for x in paths if not any(read_object(repo, f"{k}:{x}") for k in commits)]
            why += [f"{x} is not in {commits[0][:12]}" for x in missing] if commits else []
            words = [w.rstrip(".,;:") for w in re.findall(r"[\w./-]*[/.][\w./-]+", bare)]
            if not paths and not any(read_object(repo, f"{k}:{w}") for w in words for k in commits):
                why.append("names no file (an artifact criterion names a path in the repository)")
        elif kind == "decision" and not set(re.findall(r"[\w.-]+", text)) & recorded:
            why.append("names no decision in PLAN.md's Decisions table")
        return why

    return check


UNMET = re.compile(r"<|(pending|queued|running|failed|missing|open|todo|none)\b", re.I)
QUOTE = re.compile(r'"[^"]+"')
QUOTED = re.compile(r'`([^`\s]+)`:?\s*"([^"]+)"')  # `path`: "text the file holds"
_quotes: dict[tuple[str, str, str], str] = {}


def quoted(repo: Path, commit: str, path: str, quote: str) -> bool:
    """Whether PATH at COMMIT holds QUOTE, whitespace aside: evidence that cites what a record says,
    not only that the record exists."""
    key = (str(repo), commit, path)
    if key not in _quotes or not SHA.fullmatch(commit):
        _quotes[key] = " ".join(file_at(repo, commit, path).split())
    return " ".join(quote.split()) in _quotes[key]


JOB_ID = re.compile(r"\b\d{6}-\d{6}-[0-9a-f]{6}\b")


def job_problem(repo: Path | None, commits: list[str]) -> Callable[[str], str]:
    """What is wrong with a job as evidence; "" when it, or the same job rerun, passed at the code of
    one of these commits in REPO (`campaign job rerun` makes that one command)."""

    def problem(jid: str) -> str:
        d = jobs_dir() / jid
        if not (d / "spec.json").exists():
            return f"{jid} is not a job on this machine"
        job = load_job(d)
        state = job["state"].get("state")
        if state != "done":
            return f"{jid} {state or 'has no result'}"
        if not job["spec"].get("commit"):
            return f"{jid} is unversioned"
        if repo is not None and not same_repo(Path(job["spec"]["repo"]), repo):
            return f"{jid} ran in {job['spec']['repo']}, not the quest's repository"
        for commit in commits:
            with contextlib.suppress(CampaignError, KeyError):
                twin = job_at(job["spec"], commit, merged=True)
                if twin and twin["state"].get("state") == "done":
                    return ""
        return f"{jid} ran other code"

    return problem


def evidence(
    journals: dict[str, str], wanted: dict[str, Json], check: Callable[[str, str], list[str]] | None = None
) -> dict[str, list[Json]]:
    """Each criterion's rows in the journals' Evidence tables (the quest's and its tasks'): which
    journal, what it cites, and whether that is evidence: a row whose other cells are not empty, a
    <placeholder>, pending or failed, and that `check` finds proves its criterion's kind."""
    rows: dict[str, list[Json]] = {x: [] for x in wanted}
    for name, text in journals.items():
        for row in section(text, "Evidence").splitlines():
            cells = table_cells(row)
            if len(cells) < 2 or cells[0].startswith("<"):
                continue
            rest = [x for x in cells[1:] if x]
            met = bool(rest) and not any(UNMET.match(x) or set(x) <= set("-: ") for x in rest)
            for cid in re.findall(r"\bA\d+\b", cells[0]):
                if cid in rows:
                    why = check(wanted[cid]["kind"], " ".join(rest)) if met and check else []
                    rows[cid].append(
                        {"journal": name, "evidence": " | ".join(rest), "met": met and not why, "why": why}
                    )
    return rows


def stray_rows(journals: dict[str, str], wanted: Collection[str]) -> list[str]:
    """Criterion rows (a table row whose first cell names a criterion) under another heading than
    Evidence, where landing never reads them."""
    found: list[str] = []
    for name, text in journals.items():
        heading = ""
        for line in text.splitlines():
            if line.startswith("## "):
                heading = line[3:].strip()
            elif heading != "Evidence" and line.lstrip().startswith("|"):
                ids = [x for x in re.findall(r"\bA\d+\b", table_cells(line)[0]) if x in wanted]
                if ids:
                    where = f"under `## {heading}`" if heading else "above every heading"
                    found.append(
                        f"{name}'s journal has a row for {', '.join(ids)} {where}: landing reads only `## Evidence`"
                    )
    return found


def evidence_shape(spec: Json) -> str:
    """A job up to its code: what a tag names, so each run of the same job at later code counts."""
    refs = sorted((name, r.get("repo")) for name, r in spec.get("refs", {}).items())
    fields = [spec.get(k) for k in ("repo", "cmd", "cwd", "level", "inputs")]
    build = [spec["build"]] if spec.get("build") else []
    return json.dumps([*fields, refs, sorted(spec.get("env", {}).items()), *build])


def tagged(c: Campaign, pid: str, jobs: list[Json]) -> dict[str, Json]:
    """Each criterion the quest's jobs are tagged for (`job submit --for`): the newest job of each
    tagged command ("runs"), and the jobs tagged as failing without the fix ("fails")."""

    def index(jobs: list[Json]) -> dict[tuple[Any, Any], list[tuple[Json, Json]]]:
        tags: dict[tuple[Any, Any], list[tuple[Json, Json]]] = {}
        for j in sorted(jobs, key=lambda j: j["spec"].get("submitted", 0)):
            for t in j.get("tags", []):
                tags.setdefault((t.get("campaign"), t.get("quest")), []).append((j, t))
        return tags

    out: dict[str, Json] = {}
    for j, t in derived(jobs, "tags", index).get((c.ident, pid), []):
        for cid in t.get("for", []):
            entry = out.setdefault(cid, {"runs": {}, "fails": []})
            if t.get("fails_without"):
                entry["fails"].append(j)
            else:
                entry["runs"][evidence_shape(j["spec"])] = j
    return out


NAMED_CHECK = re.compile(r"`([\w.:-]+)`\s+check\b")  # "the `unit` check" in a criterion's text


def named_check(c: Campaign, p: Json, text: str, commits: list[str]) -> Json | None:
    """The evidence of the one gate check a criterion's text names ("the `unit` check"): its job or CI
    job passed at one of COMMITS' code. None when the text names no check."""
    names = list(dict.fromkeys(NAMED_CHECK.findall(text)))
    if not names:
        return None
    checks = {x["name"]: x for x in gates(p)}
    if len(names) > 1 or names[0] not in checks:
        why = f"names {', '.join(names)}: a criterion names one gate check of its quest ({', '.join(checks)})"
        return {"journal": "checks", "evidence": "", "met": False, "why": [why]}
    check, repo = checks[names[0]], c.repo(p)
    for k in commits:
        if check["cmd"].startswith(CI_CHECK):
            if ci_job(repo, k, check["cmd"][len(CI_CHECK) :].strip()) == "success":
                return {"journal": "checks", "evidence": check["cmd"], "met": True, "why": []}
            continue
        job = job_for_check(repo, k, check, c.state.get("tested_docs", []), ci=ci_covers(c), files=p["files"])
        if job and job["state"].get("state") == "done":
            return {"journal": "checks", "evidence": f"{job['id']} (the {names[0]} check)", "met": True, "why": []}
    at = f" at {commits[0][:12]}" if commits else ""
    return {"journal": "checks", "evidence": "", "met": False, "why": [f"the {names[0]} check has not passed{at}"]}


def fails_without_met(j: Json) -> bool:
    """Whether a job tagged --fails-without shows the test failing where the fix is missing, by the
    rule of its own record: a current job's test ran and failed (`fails_without_miss`, which its runner
    also applied to its log); an older job's self-inverted command exited 0."""
    st = j["state"]
    if st.get("state") != "done":
        return False
    return j["spec"].get("fails_without", 0) < FAILS_WITHOUT or not fails_without_miss(st)


def fails_without_why(j: Json) -> str:
    st, v = j["state"], j["spec"].get("fails_without", 0)
    state = st.get("state")
    if state == "failed" and v < FAILS_WITHOUT:
        return f"{j['id']} exited {st.get('exit')}: its command had to exit 0 where the fix is missing"
    if state in ("done", "failed") and v >= FAILS_WITHOUT:
        why = (state == "failed" and st.get("note")) or fails_without_miss(st)
        return f"{j['id']} {state}: {why or 'it did not fail without the fix'}"
    return f"{j['id']} {state}: no result yet" if state in ("queued", "running") else f"{j['id']} {state}"


def tagged_row(repo: Path, commits: list[str], entry: Json, named: Json | None = None) -> Json:
    """A tagged criterion's evidence: a run of one of its tagged jobs, or the check its text names,
    that passed at one of COMMITS' code (across merges from main), and, when any is tagged, a
    fails-without job that exited 0."""
    passed: list[str] = [named["evidence"]] if named and named["met"] else []
    why: list[str] = []
    for j in entry["runs"].values():
        mine = same_repo(Path(j["spec"].get("repo") or "/"), repo)
        twin = None
        for k in commits if mine else []:
            with contextlib.suppress(CampaignError, KeyError):
                found = job_at(j["spec"], k, merged=True)
                twin = twin or (found if found and found["state"].get("state") == "done" else None)
        state = j["state"].get("state")
        if twin is not None:
            passed.append(twin["id"] + (f" ({docs_note(twin)})" if docs_note(twin) else ""))
        elif not mine:
            why.append(f"{j['id']} ran in {j['spec'].get('repo')}, not the quest's repository")
        else:
            why.append(f"{j['id']} ran other code" if state == "done" else f"{j['id']} {state}")
    fails = [j["id"] for j in entry["fails"] if fails_without_met(j)]
    if entry["fails"] and not fails:
        why += [fails_without_why(j) for j in entry["fails"]]
    text = ", ".join(passed) + (f"; fails without: {', '.join(fails)}" if fails else "")
    met = bool(passed) and (bool(fails) or not entry["fails"])
    return {"journal": "jobs", "evidence": text or "none passed", "met": met, "why": [] if met else why}


def ci_row(c: Campaign, p: Json, commits: list[str], ci: bool) -> Json:
    """A [ci] criterion's evidence: the land gate's CI check (`ci_gate`), green at one of COMMITS, or
    waived by `land --no-ci` (CI false)."""
    if not ci:
        return {"journal": "ci", "evidence": "CI waived by --no-ci", "met": True, "why": []}
    if not ci_covers(c):
        return {"journal": "ci", "evidence": "", "met": False, "why": ["the campaign runs no CI (`config ci=github`)"]}
    why = ""
    for k in commits:
        why, at, carried = ci_gate(c, p, k)
        if why == "success":
            via = f" (carried to {k[:12]} across clean merges)" if carried else ""
            return {"journal": "ci", "evidence": f"CI green at {at[:12]}{via}", "met": True, "why": []}
    return {
        "journal": "ci",
        "evidence": "",
        "met": False,
        "why": [f"CI at {commits[0][:12]}: {why}" if commits else "no commit"],
    }


def criteria_evidence(
    c: Campaign,
    pid: str,
    commits: list[str],
    journals: dict[str, str],
    jobs: list[Json] | None = None,
    *,
    ci: bool = True,
) -> dict[str, list[Json]]:
    """Each criterion's evidence at one of COMMITS: the jobs tagged for it or the gate check its text
    names, CI for a [ci] one (waived when not CI), else its rows in the journals' Evidence tables
    (journals written before tags, and the other kinds)."""
    wanted = criteria(c, pid)
    p = c.quests[pid]
    repo = c.repo(p)
    tags = {
        x: t
        for x, t in tagged(c, pid, all_jobs() if jobs is None else jobs).items()
        if wanted.get(x, {}).get("kind") == "job"
    }
    rows = evidence(
        journals, {x: w for x, w in wanted.items() if x not in tags and w["kind"] != "ci"}, proof(c, repo, commits)
    )
    named = {x: named_check(c, p, w["whole"], commits) for x, w in wanted.items() if w["kind"] == "job"}
    ran = ci_row(c, p, commits, ci) if any(w["kind"] == "ci" for w in wanted.values()) else {}
    return {
        x: [tagged_row(repo, commits, tags[x], named.get(x))]
        if x in tags
        else [ran]
        if w["kind"] == "ci"
        else [n for n in [named.get(x)] if n] + rows[x]
        for x, w in wanted.items()
    }


def evidence_table(rows: dict[str, list[Json]]) -> list[str]:
    """The journal's Evidence table, generated: what proves each criterion, or what it lacks."""
    out = ["| Criterion | Evidence |", "|---|---|"]
    for cid, found in rows.items():
        met = [r["evidence"] for r in found if r["met"]]
        lacks = "; ".join(w for r in found for w in r["why"]) or "no evidence yet"
        out.append(f"| {cid} | {'; '.join(met) if met else f'<{lacks}>'} |")
    return out


def criteria_for(c: Campaign, pid: str, given: str) -> list[str]:
    """--for IDS, checked against the quest file: a job proves only job criteria."""
    wanted = criteria(c, pid)
    ids = csv(given)
    if ids and not wanted and (why := unparsed_criteria(c, pid)):
        die(f"--for {given}: {why}")
    for cid in ids:
        if cid not in wanted:
            die(f"--for {cid}: {pid}'s quest file has no criterion {cid} (it has {', '.join(wanted) or 'none'})")
        if wanted[cid]["kind"] == "ci":
            die(f"--for {cid}: a [ci] criterion is met by CI at the landed commit, not by a job")
        if wanted[cid]["kind"] != "job":
            die(f"--for {cid}: an [{wanted[cid]['kind']}] criterion is proven in the journal, not by a job")
    return ids


def replaced_citations(journals: dict[str, str], jobs: list[Json]) -> list[Json]:
    """Evidence rows citing a job a newer one of its shape replaced: at its code or a descendant,
    submitted later, ended, and cited by no journal of the quest. The row should name that result."""
    by_id = {j["id"]: j for j in jobs}
    shapes = {j["id"]: shape(j["spec"]) for j in jobs}
    cited = {x for text in journals.values() for x in JOB_ID.findall(section(text, "Evidence"))}
    out: list[Json] = []
    for name, text in journals.items():
        for row in section(text, "Evidence").splitlines():
            cells = table_cells(row)
            if len(cells) < 2 or cells[0].startswith("<"):
                continue
            for jid in dict.fromkeys(JOB_ID.findall(" ".join(cells[1:]))):
                old = by_id.get(jid)
                if old is None:
                    continue
                newer = [
                    j
                    for j in jobs
                    if shapes[j["id"]] == shapes[jid]
                    and j["spec"].get("submitted", 0) > old["spec"].get("submitted", 0)
                    and j["state"].get("state") in ("done", *FAILED)
                    and descends(old["spec"], j["spec"])
                ]
                if newer and not cited & {j["id"] for j in newer}:
                    n = max(newer, key=lambda j: j["spec"].get("submitted", 0))
                    out.append(
                        {
                            "journal": name,
                            "criteria": cells[0],
                            "job": jid,
                            "by": n["id"],
                            "state": n["state"].get("state"),
                            "commit": str(n["spec"].get("commit", ""))[:12],
                        }
                    )
    return out
