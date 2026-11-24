# Host: jobs, quiet and the tools

How machine work runs. Rules about when to use what are in SKILL.md; this is the mechanics.
`campaign help COMMAND` and `quiet help COMMAND` document each command.

## Install

`install.sh` links the skill into `~/.agents/skills/campaign` (and `~/.claude/skills`), the tools
`campaign`, `quiet`, `pace` and `report` into `~/.local/bin`, the agent types into
`~/.claude/agents` when Claude Code is installed, and generates the Copilot and Codex agent files
for the harnesses installed. It replaces only links into a campaign checkout and dangling ones;
anything else in the way is reported and kept. `--dry-run` prints what it would change, `--check`
reports what is missing, `--uninstall` removes only what it linked and generated. Without it, run `python3 <skill>/scripts/campaign.py` and `python3
<skill>/scripts/quiet.py` directly. `campaign` needs only `python3` (3.9 or newer) and `git` (2.31
or newer).

## Jobs

```sh
campaign job submit [--quest ID [--for A1,A2 [--fails-without]]] [--commit REF] [--ref NAME=[PATH@]REF] \
    [--cwd DIR] --level none|light|strict|exclusive --minutes N [--long REASON] [--priority P] \
    [--after JOBS] [--build CMD] [--cpus N] [--env K=V] [--inputs NAME=GLOBS] [--task T] [--repo PATH] \
    [--slot NAME] [--fresh | --keep] [--retries N] [--what TEXT] [--force] [--sync] -- CMD
campaign verify ID [--for A1] # the quest's gate checks at its branch head (--all: deferred too)
campaign verify ID --deferred # a landed quest's deferred checks, again on main
campaign wait [JOBS | --quest ID | --commits] [--any] [--timeout S]   # exit 0 all done, 1 a failure, 3 timeout
campaign job list | show J | log J | cancel J
campaign job rerun J... [--commit REF] [--minutes N]   # same jobs at the quest's head; a mutant or --fails-without job needs --commit
campaign usage [--json]       # agent runs and jobs per quest, role and assignment
```

- **Identity.** A job runs at the commit resolved when it was submitted, in a fresh detached
  checkout under `$CAMPAIGN_JOBS/<id>/src` (default `~/.scratch/campaign/jobs`), never in the
  agent's worktree. Extra commits, of this or another repository, are checked out at
  `$JOB_REF_<NAME>`. `$JOB_OUT` is the attempt's output directory; `$JOB_SRC` the working directory.
- **Environment.** A job inherits the worker's environment, then the campaign's `campaign config
  env=NAME=VALUE,...` (read when it runs, so queued jobs follow a change; e.g.
  `CMAKE_GENERATOR=Ninja` where a project builds only with Ninja), then its own `--env`. Campaign
  settings stay out of the key.
- **Reuse.** A job's key is the host, the toolchain fingerprint (`uname`, compiler and interpreter
  versions on the submitter's PATH, plus the output of `$CAMPAIGN_FINGERPRINT`), the repository, the
  code (each commit's tree without `.campaign/`), refs, command, directory, level and `--env`. An
  identical job that is pending or succeeded is returned (`--force` reruns), and a quest job at
  newer code cancels its queued older instances (but one a pending job waits on). A passed job also
  counts wherever every tracked file in its read set holds what it held, and across up to 40
  first-parent commits, whatever jobs it ran after, each changing only documentation (prose:
  `*.md`, `*.mdx` and `*.rst` in any folder, `*.txt` under `docs/`, `site/` or `website/`; outside
  `tests/` and `bench/`, never a build file or `requirements*.txt`) its command does not name (a
  path it names names its folder too: a script reads its siblings) and no `tested_docs` glob
  matches; scripts, data and site source under `docs/` are code, or, for a quest, a merge from main
  whose changes touch none of the quest's files or own changes nor its `--inputs` (with their build
  files and named paths); a strict or exclusive job without `--inputs` crosses only main's
  documentation. The build-test, a gate check without `--inputs`, counts by neither read set nor
  merges unless `ci=github`, whose run at the exact head tests the merge. Upgrading `campaign` can
  change keys: run `campaign verify` again. What the key does not see is the submitter's to name:

  | Input | How it enters the identity |
  |---|---|
  | code, command, toolchain, host | the key |
  | a dataset version, a download, a feature flag | `--env NAME=VERSION` |
  | installed libraries, a service, the network | `$CAMPAIGN_FINGERPRINT` (e.g. `brew list --versions`), or `--force` |
  | a measurement | the key; a strict or exclusive job holds A and B (`--ref base=`), so its result stands for that code, host and toolchain |
  | files outside the checkout | a `--quest` job's key holds the bytes of files its command names in jobs, worktrees or `~`; `submit` warns: build them in the job |
- **Tags** (`for.json`). `--for A1` records that the job proves criterion A1 of its quest by
  passing; a later run of the same job (same command, level, directory, environment) carries the
  tag, and `land` reads the newest that counts at the head. `--fails-without` marks a test run
  where the fix is missing (a mutant, a revert or the base): the flag owns the inversion, so the
  job passes when its command exits nonzero, and never on 124, 126, 127 or a failed `--build`;
  give the plain test, not `! test`. Jobs recorded before the flag inverted keep their rule
  (command exits 0). Submit refuses both tags for one criterion. Otherwise
  a criterion counts the gate check its text names, or the journal's Evidence rows.
- **Read sets** (`reads.json`). A `none` or `light` job on Linux runs under `strace -f`
  (`$CAMPAIGN_STRACE` names another, `""` none); if it passes and the trace ends cleanly, its read
  set is every tracked file its processes touched, with its mode, and every path name. Any other
  job keeps whole-tree identity.
- **Input-scoped checks.** `campaign new|set ID --inputs NAME=GLOBS` scopes the quest check NAME
  (an empty GLOBS clears it): its jobs' code is then the blobs matching GLOBS, the build files
  (`BUILD_FILES`) and every path or folder its command names, so a job at an older commit with the
  same inputs is the same job (`verify`: "its inputs are unchanged since ..."). Adding or narrowing
  inputs needs `--weaken`, and a quest keeps the build-test, a gate check without them. GLOBS name
  everything the command executes: a binary the job builds, by its sources.
- **Slots.** `--slot NAME` reuses one checkout per name for incremental builds; its tracked files
  are verified clean at the job's commit before every run (build outputs stay), and one job uses it
  at a time: the worker skips a job whose slot is held, in any campaign; the runner holds the slot's
  lock (`NAME.lock`, an `flock` the kernel drops when the runner dies). `campaign verify` and every
  `job submit --quest` job use the quest's slot (`<campaign>-<quest>`, `@<repo>` in another
  repository) but for critical quests, `--slot`, `--fresh` and `--keep`: a job waits for it, so it
  finds the quest's build, and a drain waiting for it blocks no one. Landing removes the quest's slots. A slot's `--ref NAME` checkouts live at `slot/ref-NAME`, one path for every job
  there and reset to the ref per job (a build dir configured from `$JOB_REF_NAME` stays valid);
  without a slot they live under the job's own directory. `job submit` prints the parsed command
  and options to stderr, and warns when a submit flag such as `--ref` follows `--`.
- **Builds for measurements.** `--after` only orders jobs. `--build CMD` runs CMD as a light job (30
  minutes, outside `--minutes`) in the job's checkout; a passed build's command then queues for its
  heavy hold, keeping the slot.
- **Scheduling.** The worker starts with the first submit or wait and exits when idle. It runs one strict or exclusive job at a time and up to half the CPUs' worth of light
  jobs (a quarter beside it; `CAMPAIGN_LIGHT_JOBS`), in priority order,
  campaigns splitting the light slots, rounded up; within each, the shortest job runs first, and
  one queued over an hour (`CAMPAIGN_AGE`) goes ahead in submit order, so a batch of hour-long jobs
  still delays everyone: plan it; `quiet` arbitrates across sessions. An exclusive
  job drains the machine, blocking new light holds while it waits: it starts in a lull (no runnable
  light work, the running light jobs ending within 10 minutes) or 20 minutes after the last drain
  (`CAMPAIGN_DRAIN_WAIT`); light work ending over 4 hours after it falls due waits
  (`CAMPAIGN_DRAIN_HOLD`). Ready heavy jobs run back to back, up to 6 jobs or 30 minutes
  (`CAMPAIGN_BATCH_JOBS`, `CAMPAIGN_BATCH_MINUTES`), then light work goes first once. An overdue
  drain opens an hour's turn (`CAMPAIGN_DRAIN_TURN`) in which queued drains are due and only light
  work ending before the running ones starts; a priority-1 light job that waited a whole turn ends
  it after the running drain. `campaign window --minutes N --for TEXT` opens a turn by hand;
  `--close` ends it.
  `campaign worker --restart` (after updating the tool) re-executes the worker on the current code
  after its tick; jobs keep
  running. Priorities: gate checks run at 2; light `--quest` jobs of an active or in-review quest
  (the direct path) at 1; other jobs and measurements at 0; deferred checks and `--long` jobs at -1, using at most half the light slots;
  `--priority` overrides.
- **Queue health.** `campaign status` prints the last hour's light slot-minutes used (by
  charged slots) against the slots, minutes drains held the host, minutes runnable work waited
  beside free slots, and the jobs that ended without a result. `check` warns when a
  queued job waited over 30 minutes beside idle slots.
- **Build and test defaults.** Several light jobs run at once, so each keeps to 4 jobs
  (`CAMPAIGN_LIGHT_BUILD_JOBS`): it gets `CMAKE_BUILD_PARALLEL_LEVEL` and `CTEST_PARALLEL_LEVEL`
  unless its `--env` names them; strict and exclusive jobs get none. Every job gets
  `CTEST_OUTPUT_ON_FAILURE=1`. ctest has no variable for `--stop-on-failure`, so `job submit` and
  `check` warn of a ctest run without it; `check` also flags a light check asking for more, and a
  `--minutes` over six times its passed runs' median.
- **Git mirrors.** Jobs reach `https://github.com/O/R.git` through a bare mirror in the job store
  (`git-mirrors/github.com/O/R.git`), fetched under its lock only for a missing commit (the CMake
  pins, before the command), so clones hardlink it and work while GitHub is down. A `git` shim on
  `PATH` fills mirrors; `GIT_CONFIG_COUNT` rewrites their URLs for a cached git.
- **Supervision.** Each attempt has a runner: the leader of its own process session, writing a
  heartbeat every 15 s. The command runs under `quiet` at the job's level, which stops it at its
  deadline; when the command ends, the runner kills every process left in its session, so helpers
  never outlive their job.

| Event | What happens |
|---|---|
| Timeout (`--minutes` after the hold is granted) | TERM, then KILL after 10 s; state `timeout`, exit 124; never valid evidence |
| Runner or worker killed, host suspended past a minute | The next reader (worker, `status`, `wait`, `check`) sees the stale heartbeat, kills what is left of the attempt's session (only if its leader is gone or is that runner: a reused pid is spared), and requeues it if `--retries` allows or its command never started (up to three attempts; nothing ran), else marks it `interrupted`. Runners start in the job store, never in the submitter's directory |
| Host rebooted | The same, without killing anything (the boot id changed, so old pids mean nothing) |
| Command finished but the runner died before recording | The exit code was written first; the reaper records it |
| An old runner wakes after its attempt was replaced | It no longer owns the attempt and writes nothing |
| Dependency (`--after`) failed | `skipped` |
| Disk below 5 GiB | `quiet` admits nothing until space is freed |
| No worker can run | `--sync` runs the job in the foreground through the same runner and records the same result |
| A job's state file is corrupt | it counts as finished (`corrupt`), never as evidence |

Retries are off by default (a command may not be safe twice); `campaign verify`'s checks retry once.

`campaign job prune [--days N] [--build-days N] [--dry-run]` acts on finished jobs that ended more
than N days ago (14). It keeps whole every job of a quest not landed or dropped and every job such a
quest's journals, reviews, gates or confirmations name, in any campaign, and every job of a campaign
the machine does not know (a home moved by hand). It slims a job only landed or dropped quests name
(a landing, acceptance, deferred check, journal or review): its kept checkouts and output files over
1 MiB go, its spec, state, log and smaller outputs stay, and it is never reused by identity again.
It deletes the rest, with their checkouts and identity links, and removes build slots no open quest,
pending job or quest job of the last 7 days uses. Every deletion and slimming is logged in the job
store's `prune.log`. The worker runs it with these defaults once a day when its queue empties;
`CAMPAIGN_AUTO_PRUNE=0` turns that off.

## Disk

Every disk rule acts on the repositories the machine's campaigns use (their homes and quest
repositories) and on their linked worktrees outside the job store and outside any `.scratch` (a
review's probe). A worktree an unfinished quest of any campaign owns, by branch or path, is open;
main checkouts are never touched.

- **Build directories.** `campaign job prune` also deletes the regenerable build directories
  (`BUILD_DIRS`: `build`, `build-*`, `.deps`, `_deps`) at the top of every worktree that is not
  open, when git ignores the directory whole (no tracked or unignored file inside) and nothing in
  it changed for `--build-days` (3). It never touches source files, tracked or untracked, or a
  worktree's `.scratch`. Each deletion is logged with its size in `prune.log`; `--dry-run` lists
  them with sizes.
- **Worktrees.** `campaign sweep [--dry-run] [--force]` removes, with `git worktree remove` (never
  `--force`), each worktree that is not open and whose branch is merged into main or whose quest
  landed or was dropped, and deletes its branch with `git branch -d` when merged. It keeps a
  worktree with uncommitted or untracked files, one a process runs in (a process's working
  directory, from `/proc` or `lsof`), and one a job's command, directory or environment names. A
  worktree whose `.scratch` holds files is listed with them and kept unless `--force`: commit the
  reusable ones first. Removals are logged in `prune.log`; detached worktrees are left alone.
- **Free disk.** `campaign check` warns when the home's disk has less than `disk_warn` GiB free
  (`campaign config disk_warn=N`, default 100; `CAMPAIGN_DISK_WARN_GB` overrides it), naming the
  five largest consumers (jobs, build slots, worktrees' build directories, the ccache) and what
  reclaims each. The worker's daily prune gives the same warning, at the
  largest `disk_warn` of the machine's campaigns, in its log, `prune.log` and the cmux feed.

## quiet: host holds

```sh
quiet light NAME MIN "what" -- cmd       # builds, tests, fuzz: a few at once, nice 19
quiet strict NAME MIN "what" -- cmd      # timing on own cores (Linux, FreeBSD) or beside light work
quiet exclusive NAME MIN "what" -- cmd   # timing with the machine to itself
quiet list | check                       # check: 0 free, 2 light work only, 1 nothing
```

- A hold ends with its process or at its deadline, so a crashed holder blocks no one for long. A
  name is one hold: a second request under a held name is refused. Inside a hold, `quiet` runs a
  request at that level or lower at once and refuses a stronger one, which would wait for its own
  parent.
- Strict and exclusive holds last at most 30 minutes (`QUIET_MAX_MIN`) and their command is
  stopped at the deadline: a measurement never runs unheld. The hold outlives the stop by 15 s
  (TERM, then KILL 10 s later), so the next measurement starts only after the last one is gone; a
  hold still listed just past its deadline is that grace, not a hang. A light command keeps running
  past its deadline unless `QUIET_KILL=1` (the job runner sets it).
- Strict and exclusive requests are served in arrival order; an exclusive one blocks new light
  holds, and a light request waiting 10 minutes (`QUIET_STARVE_MIN`) goes before it.
- Light holds run half the CPUs at once at nice 19 and let the scheduler share the machine: a busy
  host makes builds slower, not wrong. None starts below 4 GiB of free memory (`QUIET_MIN_MEM_GB`),
  or above a load of twice the CPUs; a `none` job takes no slot.
- **A light job takes the slots it uses:** one whose process tree averages over a slot's cores
  (CPUs over slots) for 2 minutes is charged the nearest whole number of slots, recorded in its
  `charge`; `--cpus N` declares them. `job submit` warns of undeclared copies (`-j`, `&` loops, `parallel`); `campaign
  status` names who is over.
- **Heavy disk work is load:** bulk deletes and large copies take a hold or wait.
- **macOS has no CPU partitioning:** strict runs beside light holds at nice 19, which cost CPU-bound
  timing 5-10% (`QUIET_PAUSE=1` pauses them with SIGSTOP instead); numbers that decide take
  `exclusive`. A strict hold prints the light holds beside it; its job records them (`beside`).
- **Local VMs** share the host's CPUs: wrap the command that drives one in a hold. On Linux,
  `QUIET_VM=<pid or pattern> quiet strict ...` pins the VM's threads to the strict cores; on macOS
  use `exclusive`.
- **Remote hosts** run their own `quiet` and their own job queue; a remote job takes no local hold.

## Several campaigns

Each campaign registers its name and home in `~/.scratch/campaign/campaigns/` on every write; names
are unique on the machine (they name build slots). Campaigns share the job queue, the worker,
`quiet` and `pace`, and reuse each other's identical evidence. A landing into a repository other
than the home also takes that repository's lock; a busy lock fails after `CAMPAIGN_LOCK_WAIT`
seconds (120) and the landing is retried. From
outside a home, name it: `campaign -C HOME ...`, `--campaign NAME`, or `$CAMPAIGN_HOME`.

## Messages

`campaign message NAME:ID "text" --from WHO` leaves a message for a quest of any campaign on the
machine (`ID` alone from the quest's own home or worktree); `NAME:` (`:` for your own campaign)
leaves one for that campaign's lead. A quest's message blocks its landing until a journal names its
id or the lead closes it (`campaign message ID --close msg-N --reason R`); a lead's message never
blocks a landing and stays in `campaign brief` until closed (`campaign message : --close msg-N
--reason R`). The tool sends one itself when another campaign starts a quest on your files or
contracts; it closes once that quest lands or drops, or yours order `--after NAME:ID`.

Messages wait in the home's `.git` (`campaign-mail/<campaign>/`), so any agent can leave one
without being the campaign's one writer. A new one notifies the owner and wakes the lead's
`campaign wait`; `campaign brief` lists it for the quest's contributor and the lead. Landing
moves a quest's answered and closed messages into its state. In cmux, a `campaign` command (but
`job`) records its surface as the lead's when none is recorded or cmux no longer lists the recorded
one, and only that surface names its workspace `<campaign> · lead`. Messages reach the lead
through `cmux agent message`, but for one the lead's own command printed or sent; a new lead is
sent every open message; `check` and `wait` notify the owner of one undelivered over 15 minutes.

A campaign on another machine is reached as a peer: `campaign config peers=NAME=URL,...`, where
URL is a repository both machines can push to and fetch from. A message to `NAME:ID` or `NAME:`
for a peer goes into this campaign's outbox, a ref only it writes (`refs/campaign/outbox/<name>`),
pushed to `outbox=REMOTE` (default `origin`). Each campaign's `campaign wait` fetches its peers'
outboxes every minute and imports each message once; one for a quest the peer has landed or does
not know goes to its lead. No receipt comes back: the sender learns the answer from the
landed journal.

## Other tools

- **Compiler caches** (ccache, sccache) are optional and work across job checkouts when configured
  to share entries; jobs inherit the submitter's PATH, so a launcher found there is used.
- **`pace [claude|copilot|codex]`** prints each quota window's use, its recent burn and whether
  it runs out before its reset, read from each harness's own records (`references/harness.md`).
  It is information, never a reason to launch work or to lower a tier, effort, review or check (SKILL.md, "Quota never decides quality"). It does not see credits bought for Claude Code, so it can report a
  window as spent while those credits still cover it: ask the owner before stopping work for it.
- **`report`** draws the owner's sidebar progress and notifications (cmux; elsewhere the terminal
  or desktop). `campaign` and `quiet` keep it current by themselves: every state change and
  `campaign status` update the bar (the current milestone's quests), a `campaign-<name>`
  pill (the whole campaign) and a pill per quest in flight, with 🔥 and a count when jobs failed at
  its current code and no journal names them, sending only what changed; landings, blocked quests
  and new messages notify. `quiet` adds a pill per hold, in the workspace that submitted
  the job, and notifies when a hold ran 5 minutes or more.
