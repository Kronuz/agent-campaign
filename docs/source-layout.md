# Source layout

What each source file of agent-campaign holds, and why a file over 1,000 lines stays whole. Files
are split along subsystems a maintainer recognizes, following cpp-libraries' `docs/SPLITTING.md`
(its judgment part; its C++ mechanics do not apply here).

## The command: `scripts/campaign.py` and `scripts/campaign_tool/`

`scripts/campaign.py` is the command; it imports the `campaign_tool` package so Python keeps the
compiled bytecode. The package holds one subsystem per module, and each module's docstring says
what it holds. A module imports only from the modules above it in this list, so the imports never
cycle; a name a module needs only for its annotations, from a module below it, is imported under
`TYPE_CHECKING`.

- `core.py` (274 lines): Constants, errors, and small utilities: processes, files, JSON, text, the
  host's boot and toolchain. Boundary: the vocabulary every other module shares and nothing of its
  own subsystem. Opened alone to change a shared constant or a primitive (`sh`, `git`, `read_json`)
  once for every caller.
- `repo.py` (349 lines): Git repositories: refs, history, objects and trees, read once per command
  where they cannot change. Boundary: the tool's questions to git and their caches. Opened alone to
  make a git query faster or correct without touching what asks it.
- `mirrors.py` (171 lines): Git mirrors: a job's git reaches GitHub through one bare mirror per
  repository. Boundary: the git shim and the mirrors it fills, a mechanism of its own
  (references/host.md). Opened alone when a job's clone or fetch misbehaves.
- `jobs.py` (400 lines): The job store: a job's identity, its records, the index of terminal jobs,
  and the tags that make it evidence. Boundary: the on-disk job record and its key. Opened alone to
  change what identifies a job or how its records are read and indexed.
- `reuse.py` (506 lines): Evidence reuse: which job speaks for a commit (its own run, an ancestor's,
  a twin whose read set is unchanged), and queueing jobs. Boundary: the rules for whether a result
  counts at other code, and submit, which applies them. Opened alone when a job is rerun that should
  not be, or reused that should not be.
- `runner.py` (681 lines): Running jobs: the worker's and jobs' processes, checkouts and build-slot
  locks, one job's run under quiet, its result, and dead runners. Boundary: one job's life from
  claim to result, with the processes and slots it holds. Opened alone when a job's run, kill,
  checkout or slot goes wrong.
- `state.py` (538 lines): Campaign state: finding a campaign, validating its state, and the
  relations between quests. Boundary: state.json and what follows from it alone (dependencies,
  file-set overlaps, checks, lenses). Opened alone to add a quest field or change a rule between
  quests.
- `mail.py` (329 lines): Messages: mailboxes for quests and leads, waking a lead in cmux, and
  outboxes between machines. Boundary: how a message is stored, delivered and closed. Opened alone
  when a message is lost, repeated or never wakes its reader.
- `alarm.py` (134 lines): The owner's alarm: a lead whose session holds one tool call unanswered (a
  permission prompt nobody sees), read from Claude Code's transcript. Boundary: reading a lead's
  transcript and telling the owner once per call. Opened alone when a harness changes its transcript
  or the alarm fires wrongly.
- `journals.py` (181 lines): Journals and the lead's Markdown records: where each journal lives, its
  sections, and PLAN.md. Boundary: reading the Markdown files agents and the lead write. Opened
  alone to change where a journal lives or how a section or table is parsed.
- `ci.py` (574 lines): GitHub CI: the runs and jobs at a commit, asked in the background and read
  once per command. Boundary: the tool's only conversation with GitHub (`gh`). Opened alone when CI
  evidence is missing or a command waits on GitHub.
- `evidence.py` (615 lines): Criteria and their evidence: the quest file's criteria, journal rows,
  tagged jobs and checks. Boundary: what proves an acceptance criterion. Opened alone to change what
  counts as evidence or how the Evidence table is generated.
- `reviews.py` (456 lines): Reviews and landing gaps: verdicts, acceptances, drift, and what keeps a
  quest or task from landing. Boundary: the landing gate's judgment, short of landing itself. Opened
  alone when land refuses, or lets through, what it should not.
- `campaign.py` (547 lines): The `Campaign`: its state loaded and saved by the one writer, the
  overview, what moves each quest in flight, and the sidebar it draws. Boundary: the Campaign object and everything its save does.
  Opened alone to change how state is written, locked or summarized in the sidebar.
- `leads.py` (274 lines): Lead notices across campaigns: noting a lead (only from the home, and only
  when no live lead is recorded), redelivering stuck messages,
  telling peers and dependents. Boundary: notices that open other campaigns on the machine. Opened
  alone when a lead is not told about another campaign's work or a dropped dependency.
- `broadcast.py` (303 lines): Broadcasts to every other campaign's lead: the skill's own changes (a
  commit landing on its main, its install moving), each announced once per receiving campaign, and
  `message --all`. Boundary: what every campaign on the machine must hear, and the record that
  makes it exactly once. Opened alone when a lead misses or doubles a change notice.
- `disk.py` (526 lines): Disk: pruning the job store, sweeping merged worktrees, and warning before
  the disk fills. Boundary: reclaiming space, with the plans that decide what is safe to delete.
  Opened alone when prune or sweep deletes too much or too little.
- `worker.py` (780 lines): The worker: its plan of which jobs run each tick, CPU metering, queue
  health, and its loop, which also mails each lead its to-do. Boundary: scheduling, one unit: the plan, the charges it reads and the loop
  that runs it only make sense together. Opened alone to change who runs next, or to read why the
  queue is slow.
- `todo.py` (347 lines): The lead's to-do list: what nothing moves without, critical path first,
  each item with its command, which status, brief and every wait end with, and the worker mails to
  the lead until each item is done. Boundary: turning the
  overview, what moves each quest and the landing gaps into the lead's next commands. Opened alone
  to add a kind of item or change its order.
- `map.py` (324 lines): The campaign map (MAP.md): goals, milestones, and quests with their intent,
  result and criteria. Boundary: the map command and the file it writes. Opened alone to change how
  the map is drawn.
- `title.py` (134 lines): Session titles: a lead's session name leading with its campaign, checked
  against the harness's own record, and the cmux workspace named the same. Boundary: reading a
  harness's record of a session's name and the one line that says how to fix it. Opened alone when a
  harness changes where or how it records a session's name.
- `status.py` (825 lines): Status and brief: the problems status reports, a quest as status shows
  it, and both commands' output. Boundary: what the lead and agents read about the campaign's
  present. Opened alone to add a problem status reports or change what status or brief prints.
- `usage.py` (608 lines): Usage: agents' token use and cost, read from what the harnesses and jobs
  already record. Boundary: the usage command and its per-harness readers (Claude Code, Codex,
  Copilot). Opened alone when a harness changes its log format or a cost is wrong.
- `wait.py` (288 lines): Waiting: sleeping until a quest branch moves, a job ends, a message
  arrives, or the lead's to-do list has an item it has not been shown. Boundary: the wait command and what wakes it. Opened alone when a wait wakes too late or
  not at all.
- `jobcmds.py` (824 lines): The job commands (submit, list, show, log, rerun, cancel, prune) and
  `message`. Boundary: the `job` and `message` command lines over the job store and mail. Opened
  alone to change a job command's options or output.
- `checks.py` (255 lines): `check` and `verify`: the campaign's consistency, housekeeping, and a
  quest's gate checks as jobs. Boundary: the two commands that audit a campaign and queue its gates.
  Opened alone to add a consistency check or change what verify queues.
- `land.py` (574 lines): Landing: version pins, task merges, merging main, landing a quest, and
  cleaning up after it. Boundary: the commands that move main and the branches. Opened alone to
  change how a quest or task lands, or what land leaves behind.
- `quests.py` (568 lines): The quest commands: `init`, `config`, `new`, `get`, `set`, `start` and
  `accept`. Boundary: the commands that create and edit quests and the campaign's settings. Opened
  alone to add a command option or a settable field.
- `cli.py` (657 lines): The command line: the parser and `main`. Its docstring is the command's
  `--help` description. Boundary: argument parsing and dispatch, kept apart from what the commands
  do. Opened alone to add a command or change its help.

`mail.py` and `leads.py` are both about messages, split where the import order requires it:
`Campaign.save` settles the lead's mail (`mail.py`, below `campaign.py`), and the notices in
`leads.py` open other campaigns as `Campaign`s (above it).

Tests read the package through `tests/harness.py`'s `campaign_module()`, a fresh load per call
that reads any module's names (`tool.all_jobs`) and refuses to set one: a patch names the module
whose code reads the name (`mock.patch.object(tool.jobs, "read_json", ...)`).

## Files over 1,000 lines kept whole

- `scripts/quiet.py` (1,150 lines): one tool whose candidate pieces were each under 100 lines
  (lead's decision, 2026-11-21).

No test file is over 1,000 lines.

## Tests

`tests/run.py` discovers every `tests/test_*.py` by module and filters ids by prefix
(`test_job_lifecycle.Crashes`). A test file holds one topic's TestCase classes whole: no class is
cut, and a topic under about 100 lines joins its neighbor. Helpers more than one file needs live in
`tests/harness.py`; a helper one file uses stays in it. Opened alone to change one subsystem's tests.

The campaign tests:

- `test_quests.py` (377 lines): `Quests`, `Ownership`. Creating, starting and owning quests and
  their files.
- `test_evidence.py` (743): `Evidence`. Criteria and the jobs that prove them. One class of 700
  lines, so it is its own file.
- `test_evidence_once.py` (649): `Hold` (the lifecycle table, a test per row), `Mutants`, `MutantFootprint`, `Guard`, `ExaminedJobs`, `CitedInFlight`, `RoundOrder`. Evidence once per
  round: what the hold queues while a quest iterates, when a fails-without result stops holding, the
  treadmill guard, and what an Evidence row may cite.
- `test_landing.py` (650): `FeatureModel`, `Increments`, `DirectPath`, `Integration`, `Recovery`.
  Everything that moves main: merging, the direct path, CI and push, interrupted landings, clean-up.
- `test_reviews.py` (371): `Reviews`, `ReviewBinding`, `RiskPolicy`. Who reviews a quest and what
  the review binds to.
- `test_campaigns.py` (672): `Campaigns`, `QueueHealth`. Several campaigns on one machine and the
  queue they share.
- `test_usage.py` (337): `Usage`. What sessions and jobs cost; owns its `at` timestamp helper.
- `test_messages.py` (220): `SharedHome`, `SharedHomeMessages`, `Messages`. Mail between leads and
  contributors, and the home they share.
- `test_views.py` (321): `Style`, `Sidebar`, `Map`. What brief, status, the cmux sidebar and the map
  show.

The job queue tests:

- `test_job_scheduling.py` (819): `Batches`, `CpuUse`, `Scheduling`, `Slots`, `Contention`. Which
  job runs next and on how many cores.
- `test_job_reuse.py` (519): `ReadSets`, `FalseReuse`, `Identity`, and the CMake and strace
  fixtures they share. When a finished job is reused for another commit.
- `test_job_lifecycle.py` (406): `Races`, `Limits`, `Crashes`, `Prune`, `Disk`. A job's heartbeat,
  limits, crash, pruning and disk use.
- `test_job_store.py` (255): `WorkerCache`, `JobIndex`, `CiCache`, `GitMirrors`, and the `recording`
  helper. The caches and indexes beside the job records.

Unchanged: `test_quiet.py` (424), `test_scenarios.py` (293), `test_pace.py` (155),
`test_cli.py` (114), `test_install.py` (72), `test_agents.py` (62), `test_context.py` (37), each one
topic.
