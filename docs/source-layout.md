# Source layout

What each source file of agent-campaign holds, and why a file over 1,000 lines stays whole. Files
are split along subsystems a maintainer recognizes, following cpp-libraries' `docs/SPLITTING.md`
(its judgment part; its C++ mechanics do not apply here).

## The command: `scripts/campaign.py` and `scripts/campaign_tool/`

`scripts/campaign.py` is the command; it imports the `campaign_tool` package so Python keeps the
compiled bytecode. The package holds one subsystem per module, and each module's docstring says
what it holds. A module imports only from the modules above it in this table, so the imports never
cycle; a name a module needs only for its annotations, from a module below it, is imported under
`TYPE_CHECKING`.

| Module | Lines | What it holds |
|---|---:|---|
| `core.py` | 274 | Constants, errors, and small utilities: processes, files, JSON, text and the host's boot and toolchain. |
| `repo.py` | 349 | Git repositories: refs, history, objects and trees, read once per command where they cannot change. |
| `mirrors.py` | 171 | Git mirrors: a job's git reaches GitHub through one bare mirror per repository. |
| `jobs.py` | 389 | The job store: a job's identity, its records, the index of terminal jobs, and the tags that make it evidence. |
| `reuse.py` | 507 | Evidence reuse: which job speaks for a commit (its own run, an ancestor's, a twin whose read set is unchanged), and queueing jobs. |
| `runner.py` | 583 | Running jobs: the worker's and jobs' processes, checkouts and build-slot locks, one job's run under quiet, its result, and dead runners. |
| `state.py` | 510 | Campaign state: finding a campaign, validating its state, and the relations between quests. |
| `mail.py` | 258 | Messages: mailboxes for quests and leads, waking a lead in cmux, and outboxes between machines. |
| `journals.py` | 181 | Journals and the lead's Markdown records: where each journal lives, its sections, and PLAN.md. |
| `ci.py` | 242 | GitHub CI: the runs and jobs at a commit, asked in the background and read once per command. |
| `evidence.py` | 410 | Criteria and their evidence: the quest file's criteria, journal rows, tagged jobs and checks. |
| `reviews.py` | 396 | Reviews and landing gaps: verdicts, acceptances, drift, and what keeps a quest or task from landing. |
| `campaign.py` | 450 | The `Campaign`: its state loaded and saved by the one writer, the overview, and the sidebar it draws. |
| `leads.py` | 258 | Lead notices across campaigns: noting a lead, redelivering stuck messages, telling peers and dependents. |
| `disk.py` | 526 | Disk: pruning the job store, sweeping merged worktrees, and warning before the disk fills. |
| `worker.py` | 773 | The worker: its plan of which jobs run each tick, CPU metering, queue health, and its loop. |
| `map.py` | 324 | The campaign map (MAP.md): goals, milestones, and quests with their intent, result and criteria. |
| `status.py` | 727 | Status and brief: the problems status reports, a quest as status shows it, and both commands' output. |
| `usage.py` | 608 | Usage: agents' token use and cost, read from what the harnesses and jobs already record. |
| `wait.py` | 217 | Waiting: sleeping until a quest branch moves, a job ends, or a message arrives. |
| `jobcmds.py` | 546 | The job commands (submit, list, show, log, rerun, cancel, prune) and `message`. |
| `checks.py` | 190 | `check` and `verify`: the campaign's consistency, housekeeping, and a quest's gate checks as jobs. |
| `land.py` | 541 | Landing: version pins, task merges, merging main, landing a quest, and cleaning up after it. |
| `quests.py` | 567 | The quest commands: `init`, `config`, `new`, `get`, `set`, `start` and `accept`. |
| `cli.py` | 627 | The command line: the parser and `main`. Its docstring is the command's `--help` description. |

`mail.py` and `leads.py` are both about messages, split where the import order requires it:
`Campaign.save` settles the lead's mail (`mail.py`, below `campaign.py`), and the notices in
`leads.py` open other campaigns as `Campaign`s (above it).

Tests read the package through `tests/harness.py`'s `campaign_module()`, a fresh load per call
that reads any module's names (`tool.all_jobs`) and refuses to set one: a patch names the module
whose code reads the name (`mock.patch.object(tool.jobs, "read_json", ...)`).

## Files over 1,000 lines kept whole

- `scripts/quiet.py` (1,150 lines): one tool whose candidate pieces were each under 100 lines
  (lead's decision, 2026-11-21).
- `tests/test_campaign.py` (3,422) and `tests/test_jobs.py` (1,878): split by test topic, or kept
  with a reason, by the quest's task T3.
