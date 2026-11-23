"""Reviews and landing gaps: verdicts, acceptances, drift, and what keeps a quest or task from landing."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import re
import subprocess
import time
from collections.abc import Generator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from .ci import check_evidence, ci_carried, ci_covers, ci_gate
from .core import FAILED, CampaignError, Json, csv, die, git, jobs_dir, plural, section, table_cells, warm_fingerprint
from .evidence import criteria_evidence, evidence_shape, job_problem, rerun_hint
from .jobs import job_summary, load_job, quest_jobs
from .journals import id_ranges, journal_gaps, journal_state, journals_at, landing_journals
from .mail import answered, inbox
from .repo import (
    ancestor,
    blobs,
    changed,
    file_at,
    has_ref,
    is_meta,
    lead_file,
    main_branch,
    merge_base,
    rev,
    same_repo,
)
from .reuse import doc_path, job_at, job_with_key
from .state import criteria_hash, dead_end, lens_floor, outside

if TYPE_CHECKING:  # annotations only: they import this module, so importing them here is a cycle
    from .campaign import Campaign


def own_diffs(repo: Path, main_head: str, commit: str) -> dict[str, str]:
    """The quest's own changes at COMMIT (path -> hash of its changed lines against the merge base
    with main): unlike its blob, a path's hash survives main's lines merged into the same file."""
    base = merge_base(repo, main_head, commit)
    paths = [x for x in changed(repo, base, commit) if not is_meta(x)]
    text = git(repo, "diff", "--no-color", "--no-ext-diff", "-U0", base, commit, "--", *paths) if paths else ""
    out: dict[str, str] = {}
    for part in text.split("diff --git ")[1:]:
        lines = part.splitlines()
        path = next((x[6:] for x in lines if x.startswith("+++ b/")), "")
        path = path or next((x[6:] for x in lines if x.startswith("--- a/")), "")
        body = [x for x in lines[1:] if not x.startswith(("index ", "@@"))]  # no positions, no blob ids
        out[path] = hashlib.sha256("\n".join(body).encode()).hexdigest()[:16]
    return out


def confirmations(repo: Path, commit: str, reviewed: str, *, table: list[Json], given: list[str]) -> dict[str, Json]:
    """Each blocking finding of a CHANGES review with the jobs that confirm its fix: jobs of the quest's
    repository that passed at the accepted commit's code, named for that finding (`--confirm C1=J1,J2`).
    Records whether a run of the same command failed at the reviewed code (the failure reproduced)."""
    kinds = {f["id"]: f["kind"] for f in table}
    named: dict[str, list[str]] = {}
    for item in given:
        fid, sep, jobs = item.partition("=")
        if not sep or not csv(jobs):
            die(f"--confirm FINDING=JOB[,JOB], not {item!r}")
        if fid not in kinds:
            die(f"no finding {fid} in the review (its findings: {', '.join(kinds) or 'none'})")
        named.setdefault(fid, []).extend(csv(jobs))
    missing = [f["id"] for f in table if f["blocking"] and f["id"] not in named]
    if missing:
        die(
            "; ".join(f"{fid} has no confirming job" for fid in missing)
            + ": the suite job passing its fix's test at the fixed commit, as --confirm FINDING=JOB"
        )
    problem = job_problem(repo, [commit])
    out: dict[str, Json] = {}
    for fid, jids in named.items():
        why = [w for w in map(problem, dict.fromkeys(jids)) if w]
        if why:
            die(f"{fid}: " + "; ".join(why) + f" (a confirming job passed at {commit[:12]}'s code in {repo})")
        jobs = [passed_at(load_job(jobs_dir() / jid), commit) for jid in dict.fromkeys(jids)]
        out[fid] = {
            "kind": kinds[fid],
            "jobs": [job_summary(j) for j in jobs],
            "reproduced": bool(reviewed) and any(failed_at(j, reviewed) for j in jobs),
        }
    return out


def passed_at(job: Json, commit: str) -> Json:
    """The run of this job's command that passed at COMMIT's code: the job itself, or its rerun."""
    twin = job_at(job["spec"], commit, merged=True)
    return twin if twin is not None else job


def failed_at(job: Json, commit: str) -> bool:
    """Whether the same job (command, directory, level, environment) failed at COMMIT's code."""
    with contextlib.suppress(CampaignError, KeyError):
        twin = job_with_key({**job["spec"], "commit": commit})
        return twin is not None and twin["state"].get("state") in ("failed", "timeout")
    return False


def drift(
    repo: Path,
    main_head: str,
    target: str,
    accepted: dict[str, str],
    *,
    diffs: dict[str, str] | None = None,
    tested: Sequence[str] = (),
) -> list[str]:
    """Quest paths whose own changes at target differ from what was accepted: a path's blob, or with
    DIFFS its changed lines, so main's lines merged into a quest file keep it. Only the quest's own
    changes count, and documentation no TESTED glob matches never does."""
    base = merge_base(repo, main_head, target)
    now_paths = [x for x in changed(repo, base, target) if not is_meta(x) and not doc_path(x, "", tested)]
    at_target = blobs(repo, target, now_paths)
    out = [x for x in now_paths if accepted.get(x) != at_target[x]]
    if diffs is not None and out:
        own = own_diffs(repo, main_head, target)
        out = [x for x in out if diffs.get(x) is None or diffs[x] != own.get(x)]
    rest = [x for x in accepted if x not in at_target and not doc_path(x, "", tested)]
    at_main = blobs(repo, main_head, rest)
    return out + [x for x in rest if at_main[x] != accepted[x]]


# the whole verdict is one word: "APPROVE WITH CONDITIONS" is no APPROVE
VERDICT = re.compile(r"^Verdict:\s*\**\s*(\S[^\n]*?)[\s*.]*$", re.M)
REVIEWED = re.compile(r"^Commit:\s*\**\s*`?(?:[\w.-]+:)?([0-9a-f]{7,64})\b", re.M)  # sha or repo:sha


def review_path(c: Campaign, pid: str, lens: str) -> Path:
    return c.home / c.meta / "reviews" / f"{pid}-{lens}.md"


def read_review(text: str) -> Json:
    """A review file's verdict, the commit it reviewed, and its findings table: ID, blocking, kind.
    An appended re-check counts: the last `Commit:` line starts the round that is read. A nit never
    blocks, and a CHANGES whose findings all do not block is an APPROVE."""
    rounds = list(REVIEWED.finditer(text))
    text = text[rounds[-1].start() :] if rounds else text
    verdict, reviewed = VERDICT.search(text), REVIEWED.search(text)
    findings: list[Json] = []
    for row in (section(text, "Findings") or text).splitlines():
        cells = [x.strip("*`") for x in table_cells(row)] if row.startswith("|") else []
        if len(cells) < 3 or cells[0].lower() in ("id", "") or set(cells[0]) <= set("-: "):
            continue
        kind = cells[2].lower()
        findings.append({"id": cells[0], "blocking": cells[1].lower() == "yes" and kind != "nit", "kind": kind})
    said = verdict.group(1) if verdict else ""
    return {
        "verdict": "APPROVE" if said == "CHANGES" and findings and not any(f["blocking"] for f in findings) else said,
        "commit": reviewed.group(1) if reviewed else "",
        "findings": findings,
    }


def blob_id(repo: Path, data: bytes) -> str:
    p = subprocess.run(["git", "-C", str(repo), "hash-object", "--stdin"], input=data, capture_output=True)
    if p.returncode != 0:
        die(f"git hash-object: {p.stderr.decode(errors='replace').strip()}")
    return p.stdout.decode().strip()


def review_identity(c: Campaign, pid: str, lens: str) -> str:
    """The review file's git blob id: what an acceptance accepted, "" when there is none."""
    path = review_path(c, pid, lens)
    return blob_id(c.home, path.read_bytes()) if path.exists() else ""


def verdict(c: Campaign, pid: str, lens: str) -> str:
    """A reviewer's verdict on the quest; "none yet" without a review file."""
    review = review_path(c, pid, lens)
    if not review.exists():
        return "none yet"
    return read_review(review.read_text())["verdict"] or "no verdict"


def review_step(said: str, pid: str, lens: str) -> str:
    """What a review's verdict asks for next."""
    if said == "APPROVE":
        return f"approved; `campaign accept {pid} {lens}`"
    if said == "CHANGES":
        return f"verdict CHANGES; fix what blocks, a test each, then `campaign accept {pid} {lens} --confirm FINDING=JOB ...` (the suite job)"
    if said == "REJECT":
        return "verdict REJECT; re-plan the quest"
    if said == "none yet":
        return f"not reviewed yet; launch a {lens} reviewer"
    return f"its review has {said}; the reviewer writes `Verdict: APPROVE|CHANGES|REJECT`"


def waits_line(c: Campaign, unmet: list[str]) -> str:
    dead = [why for d in unmet if (why := dead_end(c, d))]
    return f"land {', '.join(unmet)} first" + (f" ({'; '.join(dead)}: rewire it)" if dead else "")


def main_moved(repo: Path, main_head: str, base: str) -> list[str]:
    """The files main changed since a branch's merge base, its campaign records aside: what a landing
    needs merged into the branch. `landing_gaps` refuses with them and `--merge-main` merges for them."""
    return [x for x in changed(repo, base, main_head) if not is_meta(x)]


class Gap(NamedTuple):
    """What keeps a quest from landing: code decides by its KIND (needs, main, files, journal, tasks,
    criteria, messages, stale, check, review, ci), people read its TEXT, and a "stale" gap's JOBS are
    the evidence jobs to rerun."""

    kind: str
    text: str
    jobs: tuple[str, ...] = ()


EVIDENCE_GAPS = ("criteria", "stale", "check")  # what a merge from main can void


def landing_gaps(
    c: Campaign, pid: str, target: str, *, final: bool, ci: bool, jobs: list[Json] | None = None
) -> list[str]:
    """What keeps a quest from landing TARGET, each with what to do about it; empty when it can
    land. `land` refuses with these and `status` lists them."""
    return [g.text for g in landing_gap_kinds(c, pid, target, final=final, ci=ci, jobs=jobs)]


def landing_gap_kinds(
    c: Campaign, pid: str, target: str, *, final: bool, ci: bool, jobs: list[Json] | None = None
) -> list[Gap]:
    """`landing_gaps` with each gap's kind, for code that acts on them."""
    warm_fingerprint()
    p = c.quests[pid]
    repo = c.repo(p)
    main = main_branch(repo)
    gaps: list[Gap] = []
    unmet = [d for d in p["needs"] + (p["after"] if final else []) if not c.met(d)]
    if unmet:
        gaps.append(Gap("needs", waits_line(c, unmet)))
    main_head = rev(repo, main)
    base = merge_base(repo, main_head, target)
    moved = main_moved(repo, main_head, base)
    if moved:
        gaps.append(
            Gap(
                "main",
                f"{main} changed {plural(len(moved), 'file')} since {p['branch']} branched (e.g. {moved[0]}): merge {main} "
                f"into it (`campaign land {pid} --merge-main`)",
            )
        )
    paths = changed(repo, base, target)
    owned = [x for x in paths if lead_file(x)]
    if owned:
        gaps.append(Gap("files", f"{p['branch']} edits the lead's files: {', '.join(owned)}"))
    apart = not same_repo(repo, c.home)
    if apart:
        meta = [x for x in paths if is_meta(x)]
        if meta:
            gaps.append(
                Gap(
                    "files",
                    f"{p['branch']} adds campaign files to {repo.name} ({', '.join(meta[:4])}): its journal lives in the home (`campaign get {pid} journal`)",
                )
            )
        gaps += [Gap("journal", x) for x in journal_gaps(c, pid)]
    stray = outside(paths, p["files"])
    if stray:
        gaps.append(
            Gap(
                "files",
                f"{p['branch']} changes files outside its file set ({', '.join(p['files'])}): {', '.join(stray[:8])}",
            )
        )
    for task, t in sorted(p.get("tasks", {}).items()) if final else []:
        if t.get("status") != "dropped" and has_ref(repo, t["branch"]) and not ancestor(repo, t["branch"], target):
            gaps.append(
                Gap(
                    "tasks",
                    f"task {task} is not merged into {target[:12]}: merge {t['branch']}, or drop it (`campaign set {pid} --task {task} status=dropped reason=R`)",
                )
            )
    carried = (landing_journals(c, pid) if apart else journals_at(c, repo, target, pid)) if final else {}
    rows = criteria_evidence(c, pid, [target], carried, jobs, ci=ci) if final else {}
    unproven = [x for x, r in rows.items() if not any(e["met"] for e in r)]
    if unproven:
        gaps.append(
            Gap(
                "criteria",
                f"criteria without evidence at {target[:12]}: {id_ranges(unproven)} (`campaign status {pid}` lists them)",
            )
        )
    unanswered = [m["id"] for m in inbox(c, pid) if not answered(m, carried.values())] if final else []
    if unanswered:
        gaps.append(
            Gap(
                "messages",
                f"unanswered messages: {', '.join(unanswered)}: answer each in a journal by its id, or close it (`campaign message {pid} --close MSG --reason R`)",
            )
        )
    stale = sorted({w.split()[0] for x in unproven for e in rows[x] for w in e["why"] if w.endswith("ran other code")})
    if stale:
        text = f"evidence jobs ran other code than {target[:12]}: {rerun_hint(repo, stale)}"
        gaps.append(Gap("stale", text, tuple(stale)))
    for missing in check_evidence(c, p, target, ci=ci and ci_covers(c))[0]:
        name, _, why = missing.partition(": ")
        gaps.append(Gap("check", f"check {name} is not green at {target[:12]}: {why} (`campaign verify {pid}`)"))
    gaps += [Gap("review", x) for x in review_gaps(c, pid, target, main_head)] if final else []
    if ci and c.state.get("ci") == "github":
        gate = ci_gate(c, p, target)
        if gate.state != "success":
            at = f"{gate.commit[:12]} (carried to {target[:12]} across clean merges)" if gate.carried else target[:12]
            gaps.append(Gap("ci", f"CI at {at}: {gate.state} (--no-ci REASON to land without it)"))
    return gaps


def review_gaps(c: Campaign, pid: str, target: str, main_head: str) -> list[str]:
    """What the quest's reviews still lack for TARGET's code: a lens its risk requires, an acceptance,
    or one that no longer holds (a criterion, the review, or the code changed since)."""
    p = c.quests[pid]
    repo = c.repo(p)
    base = merge_base(repo, main_head, target)
    gaps: list[str] = []
    need = lens_floor(p["risk"], p["lenses"])
    if need:
        gaps.append(f"{p['risk']} risk needs {' and '.join(need)}: `campaign set {pid} lenses=...`")
    for lens in p["lenses"]:
        acc = p.get("accepted", {}).get(lens)
        if not acc:
            gaps.append(f"{lens} review not accepted: {review_step(verdict(c, pid, lens), pid, lens)}")
        elif acc.get("criteria") != criteria_hash(c, pid):
            gaps.append(f"a criterion changed after {lens} was accepted: re-accept against the current criteria")
        elif "review" not in acc:
            gaps.append(f"the {lens} acceptance names no review content: `campaign accept {pid} {lens}` again")
        elif acc["review"] != review_identity(c, pid, lens):
            gaps.append(
                f"the {lens} review changed after it was accepted (verdict now {verdict(c, pid, lens)}): "
                f"`campaign accept {pid} {lens}` again"
            )
        elif lens != "design":  # design approves the approach before the code exists
            moved_since = drift(
                repo, main_head, target, acc["blobs"], diffs=acc.get("diffs"), tested=c.state.get("tested_docs", [])
            )
            upstream = [x for x in changed(repo, acc.get("base", base), base) if not is_meta(x)]
            if moved_since:
                gaps.append(f"{lens} was accepted at {acc['commit'][:12]}; changed since: {', '.join(moved_since[:8])}")
            elif upstream and p["risk"] in ("high", "critical") and acc["commit"] not in ci_carried(c, p, target):
                # a clean merge from main keeps the review, as it keeps CI: main's CI runs after landing
                gaps.append(
                    f"{lens}: main changed {plural(len(upstream), 'code file')} under this {p['risk']}-risk quest "
                    f"since its review (`git diff {acc.get('base', base)[:12]} {base[:12]}`); read them, then re-accept"
                )
    return gaps


@contextlib.contextmanager
def repo_lock(c: Campaign, repo: Path) -> Generator[None, None, None]:
    """The lock of a repository other than the home: one campaign writes to it at a time."""
    common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    if common == Path(git(c.home, "rev-parse", "--path-format=absolute", "--git-common-dir")):
        yield
        return
    deadline = time.time() + float(os.environ.get("CAMPAIGN_LOCK_WAIT", "120"))
    with open(common / "campaign.lock", "w") as fh:
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() > deadline:  # bounded, so two campaigns landing into each other never deadlock
                    die(f"{repo} is busy: another campaign is writing to it; retry")
                time.sleep(0.2)
        yield


def merged_journals(c: Campaign, pid: str) -> dict[str, str]:
    """The journals of the quest's merged tasks, on its branch (in the home, where its journal is)."""
    p = c.quests[pid]
    names = [f"{pid}--{t}" for t, x in sorted(p.get("tasks", {}).items()) if x.get("merged")]
    return {x: file_at(c.home, p["branch"], f"{c.meta}/journal/{x}.md") for x in names}


def live_task(t: Json) -> bool:
    """Whether a task still holds its files: started, and neither merged nor dropped."""
    return bool(t.get("worktree")) and not t.get("merged") and t.get("status") != "dropped"


def task_jobs(c: Campaign, pid: str, task: str, jobs: list[Json]) -> list[Json]:
    """A task's own jobs (`job submit --quest ID --task T`), oldest first."""
    return [j for j in quest_jobs(jobs, c.ident, pid) if j["spec"].get("task") == task]


def task_gaps(c: Campaign, pid: str, task: str, jobs: list[Json]) -> list[str]:
    """What keeps a task from merging into its quest's branch: its own tests (none and light jobs),
    the newest run of each passed at the task's head (across docs-only commits and merges from main);
    no review, no gate. Its strict and exclusive runs are measurements the quest's landing checks."""
    p = c.quests[pid]
    t, repo = p["tasks"][task], c.repo(p)
    if not has_ref(repo, t["branch"]):
        return [f"its branch {t['branch']} is missing"]
    head = rev(repo, t["branch"])
    own = task_jobs(c, pid, task, jobs)
    if not own:
        return [f"no jobs of its own: run its tests as `campaign job submit --quest {pid} --task {task} -- CMD`"]

    def counts(j: Json) -> bool:
        """A test, not withdrawn, nor run at other code on purpose (a mutant or revert commit), nor one
        that must fail without the fix (`--fails-without`: the landing's evidence, never the head's)."""
        commit = str(j["spec"].get("commit", ""))
        test = j["spec"].get("level", "light") in ("none", "light") and not j["spec"].get("fails_without")
        return test and j["state"].get("state") != "cancelled" and (not commit or ancestor(repo, commit, head))

    def probe(j: Json) -> bool:
        """A probe that must fail (`--task T --fails-without`, no `--for`): its command tests no head."""
        return bool(j["spec"].get("fails_without")) and not j.get("tags") and j["state"].get("state") != "cancelled"

    newest: dict[str, Json] = {}
    for j in own:  # a later mutant run hides nothing; a later probe withdraws its command's runs
        if counts(j):
            newest[evidence_shape(j["spec"])] = j
        elif probe(j):
            newest.pop(evidence_shape(j["spec"]), None)
    if not newest:
        return [
            f"no jobs of its own at its head: run its tests as `campaign job submit --quest {pid} --task {task} -- CMD`"
        ]
    gaps: list[str] = []
    for j in newest.values():
        twin = None
        with contextlib.suppress(CampaignError, KeyError):
            twin = job_at(j["spec"], head, merged=True, files=p["files"])
        state = twin["state"].get("state") if twin else None
        if twin is None:
            gaps.append(f"{j['id']} ran other code than {head[:12]}: `campaign job rerun {j['id']}`")
        elif state != "done":
            out = f"`campaign job rerun {twin['id']}`, or, if it must fail, submit it again with --fails-without"
            gap = f"{twin['id']} {state} at {head[:12]}: {twin['spec'].get('what', '')}"
            gaps.append(gap + (f" ({out})" if state in FAILED else ""))
    return gaps


def task_states(c: Campaign, pid: str, jobs: list[Json]) -> dict[str, str]:
    """Each task's state as status and brief show it: merged, dropped, or its journal's State and
    what its merge waits for."""
    p = c.quests[pid]
    out: dict[str, str] = {}
    for task, t in sorted(p.get("tasks", {}).items()):
        if t.get("merged"):
            out[task] = f"merged at {t['merged'][:12]}"
        elif t.get("status") == "dropped":
            out[task] = f"dropped: {t.get('reason', '')}"
        else:
            gaps = task_gaps(c, pid, task, jobs)
            merge = "; ".join(gaps) if gaps else f"ready to merge: `campaign task merge {pid} {task}`"
            out[task] = f"{journal_state(c, f'{pid}--{task}', t['branch'])}; {merge}"
    return out
