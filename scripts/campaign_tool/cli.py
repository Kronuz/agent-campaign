"""campaign: quests, worktrees, landing and a host job queue for multi-agent projects.

State lives in .campaign/NAME/state.json in the campaign's home repository and is written only by
this tool, from that repository's main checkout. Jobs live in $CAMPAIGN_JOBS
(~/.scratch/campaign/jobs) on the host that runs them. `campaign help COMMAND` documents each
command; the skill's SKILL.md says when to use it.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

from style import add_help, show_help

from .broadcast import announce_skill, cmd_broadcast
from .checks import cmd_check, cmd_verify
from .core import LENSES, LEVELS, LIGHT_RUN, RISKS, SHORT_RUN, CampaignError
from .disk import BUILD_DAYS, cmd_job_prune, cmd_sweep
from .jobcmds import cmd_job_cancel, cmd_job_list, cmd_job_log, cmd_job_rerun, cmd_job_show, cmd_job_submit, cmd_message
from .jobs import INPUTS_HELP
from .land import cmd_clean, cmd_land, cmd_pin, cmd_task_merge
from .leads import note_lead
from .map import cmd_map
from .mirrors import git_mirrored
from .quests import cmd_accept, cmd_config, cmd_get, cmd_init, cmd_new, cmd_set, cmd_start
from .runner import cmd_run
from .status import cmd_status
from .usage import cmd_usage
from .wait import cmd_wait
from .worker import cmd_window, cmd_worker

EXAMPLES = """examples:
  campaign init --name shop                          # in the home repository's main checkout
  campaign new cart --title "Cart totals" --risk normal --files 'src/cart/**' --check 'unit=make test'
  campaign start cart                                # its branch and worktree
  campaign brief                                     # in the worktree: what the quest still needs
  campaign job submit --quest cart --level strict --minutes 10 -- ./bench.sh
  campaign verify cart && campaign wait --quest cart
  campaign accept cart correctness && campaign land cart
  campaign message shop:cart "totals round half-even" --from explorer   # from anywhere on the machine"""


def message_or_all(a: argparse.Namespace) -> int:
    return cmd_broadcast(a) if a.all else cmd_message(a)


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="campaign", description=__doc__, epilog=EXAMPLES, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("-C", dest="chdir", metavar="DIR", help="run as if started in DIR, like git -C")
    ap.add_argument(
        "--campaign", metavar="NAME", help="which campaign: one its home holds, or any on this machine ($CAMPAIGN)"
    )
    sub = ap.add_subparsers(dest="command", metavar="COMMAND", title="commands", required=True)
    common = argparse.ArgumentParser(add_help=False)  # --campaign after the command too
    common.add_argument("--campaign", metavar="NAME", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    check = "NAME[:LEVEL[:MIN]]=CMD"

    def command(name: str, summary: str, description: str, *, parents: bool = True) -> argparse.ArgumentParser:
        return sub.add_parser(name, parents=[common] if parents else [], help=summary, description=description)

    p = command(
        "init",
        "start a campaign in this repository's main checkout",
        "Creates the campaign's directory (.campaign/NAME/: state.json, PLAN.md and hunt.md) in this repository's main "
        "checkout, registers it on this machine and commits it. This repository becomes the campaign's home; "
        "the owner writes its goals in PLAN.md.",
        parents=False,
    )
    p.add_argument("--name", help="campaign name, unique on this machine (default: the directory's)")
    p.add_argument(
        "--ci", choices=("github", ""), default="", metavar="github", help="landing waits for green GitHub CI"
    )
    p.set_defaults(fn=cmd_init)

    p = command(
        "config",
        "show or set campaign-wide settings: ci, ci_later, suite, peers, outbox",
        "With no argument, prints the settings as key=value lines; with KEYs, their values; with KEY=VALUE, sets "
        "them (in the home's main checkout). ci=github makes landing wait for green GitHub CI but its "
        "ci_later=WORKFLOW,... workflows. suite=CMD (the home's) and suite.REPO=CMD (another repository's, by "
        "directory name), its build plus tests, gate a quest `new` gives no check. peers=NAME=URL,... names "
        "other machines' campaigns and their outbox repositories; outbox=REMOTE is where this one pushes "
        "(default origin). disk_warn=GIB is the free disk under which `check` warns (default 100).",
    )
    p.add_argument("assignments", nargs="*", metavar="KEY[=VALUE]", help="settings to read or set")
    p.set_defaults(fn=cmd_config)

    p = command(
        "new",
        "plan a quest",
        "Records a quest, writing .campaign/NAME/quests/ID.md. Its gate: its repository's suite, or --check; "
        "sanitizer, crash, fuzz, mutant checks go --later, but crash and TSan at high risk.",
    )
    p.add_argument("id", metavar="ID", help="the quest's id: lowercase letters, digits and single dashes")
    p.add_argument("--title", required=True, help="what it delivers, in a few words")
    p.add_argument("--risk", choices=RISKS, default="normal", help="sets the reviews it needs (default normal)")
    p.add_argument("--repo", default=".", metavar="PATH", help="its repository, relative to the home checkout")
    p.add_argument("--needs", metavar="IDS", help="quests that must land before it starts")
    p.add_argument(
        "--after", metavar="IDS", help="quests that must land before it lands (NAME:ID for another campaign's)"
    )
    p.add_argument("--files", metavar="GLOBS", help="the files it may change")
    p.add_argument("--provides", metavar="CONTRACTS", help="APIs, formats or protocols it defines or changes")
    p.add_argument("--consumes", metavar="CONTRACTS", help="APIs, formats or protocols it builds against")
    p.add_argument("--lenses", metavar="LENSES", help="the reviews it needs (default: by risk)")
    p.add_argument("--milestone", metavar="NAME", help="the PLAN milestone it belongs to")
    p.add_argument(
        "--hunt", metavar="WORDS", help="the first words of the hunt row it fixes; landing marks the row done"
    )
    p.add_argument(
        "--check", action="append", default=[], metavar=check, help="a gate check: must pass on the landed code"
    )
    p.add_argument(
        "--later",
        action="append",
        default=[],
        metavar=check,
        help="a deferred check: runs on the landed code at low priority; gates the milestone",
    )
    p.add_argument("--inputs", action="append", default=[], metavar="NAME=GLOBS", help=INPUTS_HELP)
    p.set_defaults(fn=cmd_new)

    p = command(
        "get",
        "print a quest's fields",
        "Prints a quest's fields as key=value lines, the form `set` takes (checks as check= and later= lines); "
        "with KEYs, only their values, one per line. With --task T, the task's.",
    )
    p.add_argument("id", metavar="ID", help="the quest")
    p.add_argument("keys", nargs="*", metavar="KEY", help="print only these fields' values")
    p.add_argument("--task", metavar="TASK", help="one of the quest's tasks")
    p.set_defaults(fn=cmd_get)

    p = command(
        "set",
        "change a quest's fields",
        "Sets a quest's fields: title, status, risk, reason, until, repo, milestone, needs, after, provides, "
        "consumes, files, lenses, and with --check or --later its checks. A dropped status needs reason=R; a "
        "park, status=blocked, also until=IDS (quests to land, PLAN decision ids, or owner): unmet needs block a "
        "quest without one. "
        "Lowering the risk, removing a lens, or removing, changing, deferring or narrowing the inputs of a check "
        "needs --weaken REASON: "
        "the change is recorded in the quest's history and voids its review acceptances; any risk change voids "
        "them too. A risk's minimum reviews (a code review at high, a design review at critical) stay. "
        "With --task T: the task's files=GLOBS, or status=dropped reason=R. `campaign get ID` prints them.",
    )
    p.add_argument("id", metavar="ID", help="the quest")
    p.add_argument("--task", metavar="TASK", help="one of the quest's tasks")
    p.add_argument("assignments", nargs="*", metavar="KEY=VALUE", help="the fields to set (lists comma-separated)")
    p.add_argument("--check", action="append", default=[], metavar=check, help="replace the gate checks")
    p.add_argument(
        "--later", action="append", default=[], metavar=check, help="replace the deferred checks ('' clears them)"
    )
    p.add_argument("--inputs", action="append", default=[], metavar="NAME=GLOBS", help=INPUTS_HELP)
    p.add_argument(
        "--weaken",
        metavar="REASON",
        help="needed to lower the risk, remove a lens, or remove, change, defer or narrow a check: recorded, voids acceptances",
    )
    p.set_defaults(fn=cmd_set)

    p = command(
        "start",
        "branch and worktree for a quest or one of its tasks",
        "Creates the quest's branch (quest/CAMPAIGN/ID) in its own worktree with a journal, and makes it active. "
        "With --task T, a parallel writer inside the quest: its own branch, worktree and journal, for --files.",
    )
    p.add_argument("id", metavar="ID", help="the quest")
    p.add_argument("--task", metavar="TASK", help="a parallel writer inside the quest, branched from it")
    p.add_argument("--files", metavar="GLOBS", help="the task's files (default: the quest's)")
    p.add_argument("--force", metavar="REASON", help="start before its needs landed, or over an overlapping file set")
    p.set_defaults(fn=cmd_start)

    p = command(
        "status",
        "each quest and what it still needs, for people",
        "Each quest and what it still needs to land, with its criteria met; `status ID` lists each criterion "
        "with its evidence. Two lines give the host's last hour: slot use, drains, idle minutes with runnable "
        "work, and jobs that produced nothing. Colored on a terminal (NO_COLOR turns it off); CAMPAIGN_BAR "
        "sets the progress bar's two characters. Agents read `campaign brief`.",
    )
    p.add_argument("id", metavar="ID", nargs="?", help="only this quest")
    p.add_argument("--json", action="store_true", help="the same as JSON")
    p.add_argument("--all", action="store_true", help="every campaign on this machine")
    p.set_defaults(fn=cmd_status, brief=False)

    p = command(
        "brief",
        "what each quest still needs, for agents",
        "What a quest still needs, one line each: landing gaps, unanswered messages, failed jobs no journal "
        "names, pending jobs. In a quest's worktree, that quest, its file set and its generated Evidence table; "
        "elsewhere, every quest not landed.",
    )
    p.add_argument("id", metavar="ID", nargs="?", help="only this quest")
    p.add_argument("--json", action="store_true", help="the same as JSON")
    p.set_defaults(fn=cmd_status, brief=True, all=False)

    p = command(
        "usage",
        "tokens, cost and time per quest, role and assignment",
        "Agent runs and job time since the campaign began, per quest and per role, assignment, model and effort, "
        "read from the harnesses' session logs (Copilot CLI, Codex) and the job records. Tokens are cumulative "
        "(context rereads included); cost is in each harness's own unit.",
    )
    p.add_argument("--json", action="store_true", help="the same as JSON")
    p.set_defaults(fn=cmd_usage)

    p = command(
        "map",
        "the campaign's milestones and quests, with intent, result and criteria",
        "The campaign as a map, in Markdown: its goals, then each milestone of PLAN.md with a checkbox per "
        "quest, its intent (the quest file's Outcome), its result (the journal's Result), the documents its "
        "landing added to its repository's docs/, its reviews, and the open quests' criteria as checkboxes. "
        "With ID, that quest in full. --write keeps it as .campaign/NAME/MAP.md, which each landing refreshes. "
        "Its emoji mark each quest's and milestone's state in the file too; color only on a terminal "
        "(NO_COLOR turns it off).",
    )
    p.add_argument("id", metavar="ID", nargs="?", help="only this quest, in full")
    p.add_argument("--json", action="store_true", help="the same as JSON")
    p.add_argument("--write", action="store_true", help="write and commit .campaign/NAME/MAP.md")
    p.set_defaults(fn=cmd_map)

    p = command(
        "check",
        "contradictions between state, git, worktrees and jobs",
        "Reports errors and warnings where state.json, git, the worktrees and the job records disagree, and "
        "what other campaigns on this machine overlap; exits 1 on an error. Notes report what is left behind "
        "(uncleaned quests, prunable jobs, unknown campaigns' jobs). Warnings: free disk under disk_warn, with "
        "the largest consumers; a queued job that waited over 30 minutes beside idle slots; a message to a "
        "lead undelivered in cmux over 15 minutes (the owner is told once). It changes nothing.",
    )
    p.set_defaults(fn=cmd_check)

    p = command(
        "sweep",
        "remove clean worktrees of merged branches and finished quests",
        "Lists the worktrees of the repositories this machine's campaigns use (outside the job store, never an "
        "unfinished quest's) whose branch is merged into main or whose quest landed or was dropped, and removes "
        "each with `git worktree remove`, deleting its branch when merged. It keeps a worktree with "
        "uncommitted or untracked files, one a process runs in, one a job reads from, and one whose .scratch "
        "holds files unless --force (commit reusable .scratch files first). Removals are logged in the job "
        "store's prune.log.",
        parents=False,
    )
    p.add_argument("--dry-run", action="store_true", help="list what it would remove or keep, with sizes")
    p.add_argument("--force", action="store_true", help="remove worktrees whose .scratch holds files too")
    p.set_defaults(fn=cmd_sweep)

    p = command(
        "verify",
        "queue a quest's gate checks at its branch head",
        "Queues the quest's gate checks at its branch head (or --commit), reusing results that still count.",
    )
    p.add_argument("id", metavar="ID", help="the quest")
    p.add_argument("--commit", metavar="REF", help="verify this commit instead of the head")
    p.add_argument("--for", dest="criteria", metavar="A1,A2", help="the criteria the gate jobs prove")
    p.add_argument("--force", action="store_true", help="rerun even if an identical job succeeded")
    p.add_argument("--fresh", action="store_true", help="a fresh checkout instead of the quest's build slot")
    p.add_argument("--all", action="store_true", help="the deferred checks too")
    p.add_argument("--deferred", action="store_true", help="a landed quest's deferred checks, on main")
    p.set_defaults(fn=cmd_verify)

    p = command(
        "accept",
        "record a review lens as passed for the code it reviewed",
        "Records a review lens as accepted and commits the review file with it: the review's content, the "
        "criteria, and the quest's changed "
        "files at the accepted commit. An APPROVE covers the code at the review's Commit: line (metadata-only "
        "commits after it keep it). After CHANGES, --confirm names, for each blocking finding, jobs that passed "
        "at the fixed code; a fix that changes files the review never saw needs the reviewer or --fix-scope.",
    )
    p.add_argument("id", metavar="ID", help="the quest")
    p.add_argument("lens", metavar="LENS", choices=LENSES, help=f"the review: {', '.join(LENSES)}")
    p.add_argument("--commit", metavar="REF", help="the commit accepted (default: the branch head)")
    p.add_argument(
        "--confirm",
        action="append",
        default=[],
        metavar="FINDING=JOBS",
        help="after CHANGES, for each blocking finding: jobs that passed at the fixed code (repeat per finding)",
    )
    p.add_argument(
        "--fix-scope",
        metavar="REASON",
        help="the fix changes files the review never saw; why they add no mechanism (else the reviewer reviews the fix)",
    )
    p.add_argument("--note", metavar="TEXT", help="a remark kept with the acceptance")
    p.set_defaults(fn=cmd_accept)

    p = command(
        "land",
        "land a quest on main",
        "Lands the quest when nothing `campaign brief` lists is left: dependencies, a merge of main, file set, "
        "tasks, criteria (from the jobs tagged for them), messages, checks, reviews and CI. It fast-forwards "
        "main (or merges when only campaign files differ), checks that the landed code is the tested code, "
        "records the landing, cleans the quest as `clean` does (--keep keeps it) and refreshes MAP.md. --upto "
        "lands a prefix safe alone; --record records a landing that already happened.",
    )
    p.add_argument("id", metavar="ID", help="the quest")
    p.add_argument("--upto", metavar="REF", help="land the branch up to this commit, as an increment")
    p.add_argument("--reason", metavar="TEXT", help="why the increment is safe to land alone")
    p.add_argument(
        "--push",
        action="store_true",
        help="merge origin's main first when it changed only campaign files, push, and record only what origin took",
    )
    p.add_argument("--no-ci", metavar="REASON", help="land without green CI, saying why (recorded with the landing)")
    p.add_argument(
        "--merge-main",
        action="store_true",
        help="merge main into the quest's branch first (never a rebase), push it, and land when the evidence "
        "carries; a conflict or voided evidence refuses with the gap, and when voided evidence is all the quest "
        "lacks, its reruns are queued at landing priority, ahead of the queue",
    )
    p.add_argument(
        "--record", metavar="REF", help="record a landing that already happened (with --reason; unchecked gates kept)"
    )
    p.add_argument(
        "--earlier",
        action="store_true",
        help="with --record: it landed before this campaign, reviewed and verified by the process of its time, "
        "which --reason names (its ledger, review and CI); no gate is listed as unverified",
    )
    p.add_argument("--keep", action="store_true", help="keep the quest's worktrees, branches and build slot")
    p.set_defaults(fn=cmd_land)

    tasks = command(
        "task",
        "merge a quest's task into its branch",
        "Tasks are the steps of a quest (`campaign start ID --task T`), each on its own branch.",
        parents=False,
    )
    task = tasks.add_subparsers(dest="task_command", metavar="COMMAND", title="commands", required=True)
    p = task.add_parser(
        "merge",
        parents=[common],
        help="merge a task into its quest's branch",
        description="Merges task T's branch into quest ID's branch (no fast-forward) once the newest run of each "
        "of the task's own jobs (`job submit --quest ID --task T`) passed at its head: no review and no gate "
        "beyond them. Then removes the task's worktree and branch; its journal stays on the quest's branch.",
    )
    p.add_argument("id", metavar="ID", help="the quest")
    p.add_argument("task", metavar="T", help="the task")
    p.set_defaults(fn=cmd_task_merge)

    p = command(
        "pin",
        "record a version-pin bump the lead committed on main",
        "Records a commit on main that changes only version strings (tags, release numbers, commit ids) in "
        "existing lines: the lead's lane for dependency pins, outside any quest. It runs `config pin_check` "
        "at the commit when one is set and refuses on a failure; CI gates it after --push.",
    )
    p.add_argument("commit", nargs="?", metavar="REF", help="the pin commit (default: main's newest code commit)")
    p.add_argument("--repo", metavar="PATH", help="the repository (default: the home)")
    p.add_argument("--push", action="store_true", help="push main after recording it")
    p.set_defaults(fn=cmd_pin)

    p = command(
        "message",
        "send a finding to a quest or a lead, from anywhere",
        "Sends a message to a quest (ID, or NAME:ID from outside its home) or to a campaign's lead "
        "(NAME:, or : for this campaign's), from anywhere on the machine. A quest's message stays open, shown by "
        "status and brief and blocking its landing, until one of its journals names the message's id or the "
        "lead closes it; a lead's message until it is closed (--close MSG --reason R, from the "
        "home). For a campaign on another machine named in `campaign config peers`, the message goes to this "
        "campaign's outbox, which `campaign wait` there fetches. Starting a quest sends a message to the "
        "leads of the other campaigns on this machine whose work it overlaps. --all sends TEXT to the lead of "
        "every other campaign, on this machine and among the peers; the tool itself tells them of each "
        "change to the campaign skill, landed and installed.",
    )
    p.add_argument("quest", metavar="TARGET", help="ID, NAME:ID, NAME: or :")
    p.add_argument("text", metavar="TEXT", nargs="?", default="", help="what was found")
    p.add_argument(
        "--from",
        dest="by",
        metavar="WHO",
        default=os.environ.get("USER", "someone"),
        help="who found it (default $USER)",
    )
    p.add_argument("--close", metavar="MSG", help="close an open message (the lead)")
    p.add_argument("--reason", metavar="TEXT", help="why it is closed")
    p.add_argument(
        "--all",
        action="store_true",
        help="send TEXT, the one argument, to every other campaign's lead (here and the peers), none twice",
    )
    p.set_defaults(fn=message_or_all)

    p = command(
        "clean",
        "remove a landed or dropped quest's worktrees and branches",
        "Removes a landed or dropped quest's worktrees, its merged branches and its build slot (a final `land` "
        "does this itself). A worktree with uncommitted or untracked files, or that a job's command, directory or "
        "environment names, stops it unless --force; an unmerged branch is kept unless --force. Committed "
        "files, journals, reviews and job records stay.",
    )
    p.add_argument("id", metavar="ID", help="the quest")
    p.add_argument("--force", action="store_true", help="remove them anyway")
    p.set_defaults(fn=cmd_clean)

    p = command(
        "wait",
        "block until jobs finish, or until anything new happens; no tokens",
        "Blocks until the jobs finish (exit 0 all done, 1 a failure, 3 the timeout). With --commits, until "
        "anything new happens in the campaign since the last such wait: a quest branch moves, a job ends, or "
        "a message arrives. Like `check`, it tells the owner of a message to a lead undelivered in cmux over 15 "
        "minutes.",
    )
    p.add_argument("jobs", nargs="*", metavar="JOB", help="the jobs to wait for")
    p.add_argument("--quest", metavar="ID", help="every pending job of this quest")
    p.add_argument("--any", action="store_true", help="return when the first one finishes")
    p.add_argument(
        "--commits",
        action="store_true",
        help="return at the first quest-branch commit, ended job or message of this campaign not seen by an earlier wait",
    )
    p.add_argument("--timeout", type=float, default=0, metavar="SECONDS", help="exit 3 when it passes (default: none)")
    p.set_defaults(fn=cmd_wait)

    p = command(
        "worker",
        "run queued jobs until the queue is empty",
        "Runs queued jobs under quiet, in priority order, and exits when nothing is queued or running. A "
        "submit or a wait starts one, so it is rarely run by hand.",
        parents=False,
    )
    p.add_argument(
        "--restart",
        action="store_true",
        help="have the running worker finish its tick and restart on the current code; running jobs keep running",
    )
    p.set_defaults(fn=cmd_worker)

    p = command(
        "window",
        "let queued drains run back to back for a while",
        "Opens a window in which every queued exclusive job, which drains the machine, "
        "is due, the drains running back to back in batches (light work goes first once after each full one); "
        "it closes after --minutes or with --close. Without a window, drains and light work take "
        "turns. With no option it shows the open window.",
        parents=False,
    )
    p.add_argument("--minutes", type=float, default=0, metavar="N", help="open it for N minutes")
    p.add_argument("--for", dest="purpose", default="", metavar="TEXT", help="what it is for, recorded with it")
    p.add_argument("--close", action="store_true", help="close the open window now")
    p.set_defaults(fn=cmd_window)

    p = sub.add_parser("_run")  # a job's runner, started by the worker
    p.add_argument("jobdir")
    p.set_defaults(fn=cmd_run)

    jobs = command(
        "job",
        "submit and inspect machine jobs",
        "Machine jobs: commands run at an exact commit in their own checkout under quiet, their results "
        "recorded and reused by identity (code, command, toolchain).",
        parents=False,
    )
    job = jobs.add_subparsers(dest="job_command", metavar="COMMAND", title="commands", required=True)

    def job_command(name: str, summary: str, description: str) -> argparse.ArgumentParser:
        return job.add_parser(name, parents=[common], help=summary, description=description)

    p = job_command(
        "submit",
        "queue a command at an exact commit",
        "Queues CMD at an exact commit (the quest's branch with --quest, else HEAD) in its own checkout, under "
        "quiet at --level. An identical job that is pending or still counts is returned instead. Paths "
        "outside the checkout are not part of its identity (a --quest job's holds the files its command "
        "names). A --quest job cancels the quest's queued jobs it replaces.",
    )
    p.add_argument("--quest", metavar="ID", help="the quest it is evidence for")
    p.add_argument("--for", dest="criteria", metavar="A1,A2", help="the criteria it proves by passing")
    p.add_argument(
        "--fails-without",
        action="store_true",
        help="--for a test run where the fix is missing: the job passes when CMD exits nonzero (not 124, 126, 127)",
    )
    p.add_argument("--repo", metavar="PATH", help="repository (default: this one, or the quest's)")
    p.add_argument("--commit", metavar="REF", help="the commit to run (default: HEAD, or the quest's branch)")
    p.add_argument(
        "--ref",
        action="append",
        default=[],
        metavar="NAME=[PATH@]REF",
        help="another commit, of any repository, at $JOB_REF_NAME",
    )
    p.add_argument("--cwd", metavar="DIR", help="the directory to run in, inside the checkout")
    p.add_argument("--level", choices=LEVELS, default="light", help="its quiet level (default light)")
    p.add_argument(
        "--minutes",
        type=int,
        metavar="N",
        help=f"its bound (default 30; at most {SHORT_RUN} if heavy, {LIGHT_RUN} for a light fuzz or soak)",
    )
    p.add_argument("--long", metavar="REASON", help="lift that cap; the job is deferred")
    p.add_argument(
        "--priority",
        type=int,
        metavar="N",
        help="higher runs first (default 0; 1 for a light job of an active --quest)",
    )
    p.add_argument("--after", metavar="JOBS", help="jobs that must succeed first")
    p.add_argument(
        "--build",
        metavar="CMD",
        help=f"build first in the same checkout, under light (at most {LIGHT_RUN} minutes), then run CMD at --level",
    )
    p.add_argument(
        "--cpus",
        type=int,
        metavar="N",
        help="the cores a light job keeps busy at once: it takes their light slots from its start",
    )
    p.add_argument("--env", action="append", default=[], metavar="K=V", help="part of the job's identity")
    p.add_argument(
        "--inputs",
        metavar="GLOBS",
        help="the files it depends on (comma-separated): its code is their bytes, the build files' and the paths "
        "its command names, so its result counts at any commit where they are unchanged",
    )
    p.add_argument("--task", metavar="T", help="with --quest, a task's job: at the task's branch, its merge gate")
    p.add_argument("--slot", metavar="NAME", help="reuse a named checkout (default for a --quest job: its quest's)")
    p.add_argument("--fresh", action="store_true", help="a fresh checkout instead of the quest's build slot")
    p.add_argument("--keep", action="store_true", help="keep the source checkout")
    p.add_argument("--retries", type=int, default=0, metavar="N", help="requeues after a lost runner")
    p.add_argument("--what", metavar="TEXT", help="what it is for, as status shows it")
    p.add_argument("--unversioned", action="store_true", help="run in --cwd as it is (probes; never evidence)")
    p.add_argument("--force", action="store_true", help="run even if an identical job succeeded")
    p.add_argument("--sync", action="store_true", help="run now and wait (no worker); same records")
    p.add_argument("cmd", nargs=argparse.REMAINDER, metavar="-- CMD", help="the command and its arguments")
    p.usage = "%(prog)s [options] -- CMD [ARG ...]"
    p.set_defaults(fn=cmd_job_submit)
    p = job_command(
        "list",
        "list jobs",
        "Lists jobs, oldest first: id, state, level, quest, what and exit code. Where a campaign is found, only "
        "its jobs; --all lists every campaign's on this machine.",
    )
    p.add_argument("--quest", metavar="ID", help="only this quest's")
    p.add_argument("--state", help="queued, running, done, failed, timeout, interrupted, cancelled or skipped")
    p.add_argument("--all", action="store_true", help="every campaign's jobs on this machine")
    p.set_defaults(fn=cmd_job_list)
    p = job_command(
        "show", "a job's spec and state", "Prints a job's spec and state, and where its log and output are, as JSON."
    )
    p.add_argument("job", metavar="JOB", help="the job's id")
    p.set_defaults(fn=cmd_job_show)
    p = job_command("log", "the end of a job's log", "Prints the last lines of a job's log.")
    p.add_argument("job", metavar="JOB", help="the job's id")
    p.add_argument("-n", "--lines", type=int, default=40, metavar="N", help="how many (default 40)")
    p.set_defaults(fn=cmd_job_log)
    p = job_command(
        "rerun",
        "the same jobs at the quest's head",
        "Resubmits jobs with their command, level, directory, environment and other repositories at another "
        "commit: by default the head of their quest's branch, else of their repository; a job whose commit was "
        "never on the quest's branches or main (a mutant) is refused without --commit. Each cancels its "
        "quest's still-queued jobs it replaces (the same job at newer code), unless a pending job waits on one.",
    )
    p.add_argument("jobs", nargs="+", metavar="JOB", help="the jobs to run again")
    p.add_argument("--commit", metavar="REF", help="the commit to run (default: the quest's branch, else HEAD)")
    p.add_argument("--minutes", type=int, metavar="N", help="the minutes each needs, for a job that timed out")
    p.set_defaults(fn=cmd_job_rerun)
    p = job_command(
        "prune",
        "delete old finished jobs nothing needs; slim the ones evidence names",
        "For finished jobs that ended more than --days ago: deletes those nothing names, with their kept "
        "checkouts and identity links; slims those only landed or dropped quests name (a landing, acceptance, "
        "deferred check, journal or review): their kept checkouts and output files over 1 MiB go, their spec, "
        "state, log and smaller outputs stay, and they are never reused by identity again. Jobs of quests not "
        "landed or dropped, and of campaigns this machine does not know, are kept whole. Build slots no open "
        "quest uses go too, unless a pending job names one or a quest's job ran in it within 7 days, "
        "and the build directories (build, build-*, .deps, _deps, ignored by git) of worktrees "
        "of the campaigns' repositories that no unfinished quest owns, once nothing in them changed for "
        "--build-days. Each deletion is logged in the job store's prune.log. The worker runs this with its "
        "defaults once a day when its queue empties (CAMPAIGN_AUTO_PRUNE=0 turns that off).",
    )
    p.add_argument(
        "--days", type=float, default=14, metavar="N", help="keep jobs that ended within N days (default 14)"
    )
    p.add_argument(
        "--build-days",
        type=float,
        default=BUILD_DAYS,
        metavar="N",
        help=f"keep build directories changed within N days (default {BUILD_DAYS:g})",
    )
    p.add_argument("--dry-run", action="store_true", help="list what would be deleted")
    p.set_defaults(fn=cmd_job_prune)
    p = job_command("cancel", "cancel a queued or running job", "Cancels a queued job, or stops a running one.")
    p.add_argument("job", metavar="JOB", help="the job's id")
    p.set_defaults(fn=cmd_job_cancel)

    add_help(sub, "campaign")
    return ap


# options that bypass or weaken a safeguard: each needs a non-empty reason, which is recorded
REASONS = {"no_ci": "--no-ci", "weaken": "--weaken", "fix_scope": "--fix-scope"}
REASONS.update(reason="--reason", force="--force", long="--long")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["_git"]:  # the git shim's slow path: git's own arguments follow, unparsed
        return git_mirrored(int(argv[1]), argv[2:])
    ap = parser()
    args, extra = ap.parse_known_args(argv)
    if args.chdir:
        try:
            os.chdir(args.chdir)
        except OSError as e:
            ap.error(f"-C {args.chdir}: {e.strerror}")
    if args.command == "help":
        return show_help(ap, args.topic)
    if getattr(args, "campaign", None):
        os.environ["CAMPAIGN"] = args.campaign  # job runners and the worker inherit it
    # Python 3.9's argparse leaves a trailing list after an option unparsed (`get ID --task T files status`)
    trailing = {"set": "assignments", "get": "keys", "config": "assignments", "wait": "jobs", "rerun": "jobs"}
    dest = trailing.get(getattr(args, "job_command", None) or args.command, "")
    if extra and dest and not any(x.startswith("-") for x in extra):
        setattr(args, dest, [*getattr(args, dest), *extra])
    elif extra:
        ap.error(f"unrecognized arguments: {' '.join(extra)}")
    for dest, flag in REASONS.items():
        value = getattr(args, dest, None)
        if isinstance(value, str) and not value.strip():
            ap.error(f"{flag} needs a reason")
    if args.command not in ("job", "_run", "worker"):
        note_lead()
    if args.command != "_run":
        try:
            announce_skill()
        except (CampaignError, OSError, subprocess.SubprocessError) as e:
            print(f"campaign: the campaign skill's changes were not announced: {e}", file=sys.stderr)
    try:
        return int(args.fn(args))
    except CampaignError as e:
        print(f"campaign: {e}", file=sys.stderr)
        return 2
