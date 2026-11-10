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
campaign job submit [--quest ID] [--commit REF] [--ref NAME=[PATH@]REF] [--cwd DIR] \
    --level none|light|strict|exclusive --minutes N [--priority P] [--after JOBS] \
    [--env K=V] [--slot NAME] [--retries N] [--sync] -- CMD
campaign verify ID            # the quest's gate checks at its branch head (--all: deferred too)
campaign verify ID --deferred # a landed quest's deferred checks, again on main
campaign wait [JOBS | --quest ID | --commits] [--any] [--timeout S]   # exit 0 all done, 1 a failure, 3 timeout
campaign job list | show J | log J | rerun J... | cancel J   # rerun: same jobs at the quest's head
campaign usage [--json]       # agent runs and jobs per quest, role and assignment
```

- **Identity.** A job runs at the commit resolved when it was submitted, in a fresh detached
  checkout under `$CAMPAIGN_JOBS/<id>/src` (default `~/.scratch/campaign/jobs`), never in the
  agent's worktree. Extra commits, of this or another repository, are checked out at
  `$JOB_REF_<NAME>`. `$JOB_OUT` is the attempt's output directory; `$JOB_SRC` the working directory.
- **Reuse.** A job's key is the host, the toolchain fingerprint (`uname`, compiler and interpreter
  versions found on the submitter's PATH, plus the output of `$CAMPAIGN_FINGERPRINT`), the
  repository, the code (each commit's tree without `.campaign/`), refs, command, directory, level
  and `--env`. The job runs with the submitter's PATH, whoever started the worker. An identical job
  that is pending or succeeded is returned instead of rerun; `--force` reruns. What the key does
  not see is the submitter's to name:

  | Input | How it enters the identity |
  |---|---|
  | code, command, toolchain, host | the key |
  | a dataset version, a download, a feature flag | `--env NAME=VERSION` |
  | installed libraries, a service, the network | `$CAMPAIGN_FINGERPRINT` (e.g. `brew list --versions`), or `--force` |
  | a measurement | the key; a strict or exclusive job is self-contained (A and B in one job, `--ref base=`), so its result stands for that code, host and toolchain |
  | files outside the checkout | not seen: `submit` warns about paths into jobs or worktrees; build them in the job |

  Upgrading `campaign` can change how keys are computed; run `campaign verify` again after an
  upgrade. A journal's Evidence row naming a job counts only while that job, or the same job rerun,
  passed at the quest's current code.
- **Documentation-only commits.** Evidence that passed at a commit still counts at a later one
  when every path that differs between them (outside `.campaign/`) is documentation (`DOC_PATHS`:
  `docs/**`, `*.md`) that the job's command does not name, itself or by a `docs/` folder. Gates,
  criteria, confirmations, `verify` (which reuses such a job instead of rerunning it), `land` and
  `status` accept it; the brief names each such job and the documentation paths it ignores, and
  the landing records them. The search follows first parents back at most 20 commits and stops at
  the first commit that changes code. A `tested_docs` glob (`campaign config`) marks documentation
  that tests execute: changing it voids evidence like code does.
- **Input-scoped checks.** `campaign new|set ID --inputs NAME=GLOBS` scopes the quest check NAME
  (an empty GLOBS clears it). Its jobs' code is then the blobs of the paths that match GLOBS, the
  build files (`CMakeLists*`, `*.cmake`, `CMakePresets*.json`, `*toolchain*`, Makefiles, `*.mk`,
  `meson.build`, `pyproject.toml`, `setup.py`, `setup.cfg`, `.gitmodules`) and every path or folder
  the command names, instead of the whole tree; refs, `--env`, host, toolchain, command, directory
  and level stay in the key. A job of the check at an older commit with the same inputs is the same
  job, so `verify` reuses it ("its inputs are unchanged since ..."), and Evidence rows, `accept
  --confirm` and `land` count it. Only checks the quest file declares carry inputs; adding or
  narrowing them needs `--weaken`. A quest keeps at least one gate check without inputs, the
  build-test, which `land` needs at the exact code.
- **Slots.** `--slot NAME` reuses one checkout per name for incremental builds; its tracked files
  are verified clean at the job's commit before every run (untracked build outputs stay, which is
  the point), and one job uses it at a time: the worker starts no job whose slot a running job
  holds, in any campaign, and starts the jobs behind it instead; the runner holds the slot's lock
  (`NAME.lock`, an `flock` the kernel drops when the runner dies). `campaign verify` uses a slot
  per quest (`<campaign>-<quest>`), except for critical quests and `--fresh`, which check out
  afresh. A light `job submit --quest` job uses the same slot by default (`<campaign>-<quest>@<repo>`
  in another repository) while no pending job of its own is there, else a fresh checkout beside it,
  so a batch of probes runs in parallel; `--slot`, `--fresh` and `--keep` choose otherwise. Landing
  removes the quest's slots. A Xapiand build-and-test reused its slot at the next commit in 310 and
  320 s against 586 and 688 s fresh (47% and 53% less; ccache warm both ways, ctest 160-190 s of each).
- **Scheduling.** The worker starts with the first submit or wait and exits when nothing is queued
  or running. It runs one strict or exclusive job at a time and up to half the CPUs' worth of light
  jobs (a quarter beside a strict or exclusive one; `CAMPAIGN_LIGHT_JOBS`), in priority order,
  campaigns sharing the light slots; `quiet` arbitrates the machine across every session. A job
  that drains the machine (exclusive, or strict on macOS) blocks every new light hold while it
  waits, so drains and light work take turns: a drain starts in a lull (no runnable light work, and
  the running light jobs end within 10 minutes by their `--minutes`), or once it has waited an hour
  since the last drain ended (`CAMPAIGN_DRAIN_WAIT`), when no new light job starts until it runs.
  An overdue drain opens a turn (`CAMPAIGN_DRAIN_TURN`, an hour) in which the queued drains run
  back to back; a priority-1 light job that has waited longer than a turn ends it once the running
  drain ends, so a long queue of drains never holds the direct path for hours, and no light job
  starts beside a drain.
  `campaign window --minutes N --for TEXT` lets queued drains run back to back for a batch of
  measurements (light jobs wait); it closes by itself or with `--close`. Give long jobs accurate
  `--minutes`: drains plan around them.
  `campaign worker --restart` (after updating the tool) has the worker finish its tick and
  re-execute in place on the new code; running jobs keep running. SIGINT or SIGTERM stops it
  between ticks, never mid-claim. Priorities: gate checks from `verify` and light jobs
  submitted with `--quest` for an active or in-review quest (probes, mutants, sanitizer runs: the
  direct path) run at 1 unless `--priority` says otherwise, other jobs and measurements (strict,
  exclusive) at 0, deferred checks queued by `land` at -1. Deferred light jobs use at most half the light slots, and a
  deferred strict or exclusive check lasts at most 10 minutes, so the direct path never waits long
  behind completeness work.
  Drains are what light work waits behind most: on a 16-CPU host on 2026-11-10, 319 light jobs
  waited 213 hours in all, 61% of it while a drain ran, 29% with the light slots full and 10% on
  dependencies, busy slots and fair share (median 36 minutes at priority 0, 6 at priority 1).
- **Build parallelism.** Several light jobs run at once, so each keeps to 4 build jobs
  (`CAMPAIGN_LIGHT_BUILD_JOBS`): a light job gets `CMAKE_BUILD_PARALLEL_LEVEL` unless its `--env`
  names one, so `cmake --build` needs no `-j`. `campaign check` warns about a light check asking for
  more. Strict and exclusive jobs have their cores to themselves and get no default.
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
| Job submitted twice | An identical job that is queued, running or done is returned; an interrupted or failed one runs anew |
| Dependency (`--after`) failed | `skipped` |
| Disk below 5 GiB | `quiet` admits nothing until space is freed |
| No worker can run | `--sync` runs the job in the foreground through the same runner and records the same result |
| A job's state file is corrupt | it counts as finished (`corrupt`), never as evidence |

Retries are off by default because a command may not be safe to run twice; checks queued by
`campaign verify` retry once.

`campaign job prune [--days N] [--build-days N] [--dry-run]` acts on finished jobs that ended more than N days ago
(14). It keeps whole every job of a quest not landed or dropped and every job such a quest's
journals, reviews, gates or confirmations name, in any campaign on the machine, and every job of a
campaign the machine does not know (a home moved by hand). It slims a job only landed or dropped
quests name (a landing, acceptance, deferred check, journal or review): its kept checkouts and
output files over 1 MiB go, its spec, state, log and smaller outputs stay, and it is never reused
by identity again. It deletes the rest, with their checkouts and identity links, and removes build
slots no open quest uses. Every deletion and slimming is logged in the job store's `prune.log`.
The worker runs it with these defaults once a day when its queue empties; `CAMPAIGN_AUTO_PRUNE=0`
turns that off.

## Disk

Every disk rule acts on the repositories the machine's campaigns use (their homes and quest
repositories) and on their linked worktrees outside the job store and outside any `.scratch` (a
review's probe). A worktree an unfinished quest
of any campaign owns, by branch or path, is open; main checkouts are never touched.

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
  five largest consumers the campaigns know with their sizes and what reclaims each: the job
  store's jobs and its build slots (`campaign job prune`), the build directories of each
  repository's worktrees (`campaign job prune --build-days 0`), and `~/.ccache` or `$CCACHE_DIR`
  (`ccache --evict-older-than 7d`). The worker's daily prune gives the same warning, at the
  largest `disk_warn` of the machine's campaigns, in its log, `prune.log` and the cmux feed. Sizes
  come from `du` and are reused for an hour (a prune or sweep measures again). Below 5 GiB free,
  `quiet` admits no hold at all.

## quiet: host holds

```sh
quiet light NAME MIN "what" -- cmd       # builds, tests, fuzz: a few at once, nice 19
quiet strict NAME MIN "what" -- cmd      # timing on its own cores (Linux, FreeBSD); alone on macOS
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
- Strict requests are served in arrival order and block new light holds; a light request waiting
  10 minutes (`QUIET_STARVE_MIN`) goes before new strict holds, so builds are never starved.
- Light holds run half the CPUs at once at nice 19 and let the scheduler share the machine: a busy
  host makes builds slower, not wrong. None starts below 4 GiB of free memory (`QUIET_MIN_MEM_GB`),
  above a load of twice the CPUs (a runaway's ceiling) or below 5 GiB free disk. The job worker
  launches as many, split fairly across the campaigns that have light work queued, shortest first;
  a `none` job takes no slot.
- **A hold counts jobs, not processes.** Cap fan-out inside the command (`-j4`, one interpreter at a
  time): one light hold running a five-interpreter extension matrix once took a workstation to a
  load of 211.
- **Heavy disk work is load:** bulk deletes and large copies take a hold or wait.
- **macOS has no CPU partitioning:** strict acts as exclusive (or, with `QUIET_PAUSE=1`, pauses light
  jobs with SIGSTOP while it runs). Where several machines exist, keep builds on one and timing on
  another.
- **Local VMs** share the host's CPUs: wrap the command that drives one in a hold. On Linux,
  `QUIET_VM=<pid or pattern> quiet strict ...` pins the VM's threads to the strict cores; on macOS
  use `exclusive`.
- **Remote hosts** run their own `quiet` and their own job queue (install this repository there, or
  copy the two scripts); a remote job takes no local hold. Check one through its session:
  `etctl run SESSION 'quiet check; quiet list'`.

## Several campaigns

Each campaign registers its name and home in `~/.scratch/campaign/campaigns/` on every write; names
are unique on the machine (they name build slots). Campaigns share the job queue, the worker,
`quiet` and `pace`, and reuse each other's identical evidence. A landing into a repository other
than the home also takes that repository's lock; a busy lock fails after `CAMPAIGN_LOCK_WAIT`
seconds (120) and the landing is retried. `campaign check` only reads other campaigns' state. From
outside a home, name it: `campaign -C HOME ...`, `--campaign NAME`, or `$CAMPAIGN_HOME`.

## Messages

`campaign message NAME:ID "text" --from WHO` leaves a message for a quest of any campaign on the
machine (`ID` alone from the quest's own home or worktree); `NAME:` (`:` for your own campaign)
leaves one for that campaign's lead. A quest's message blocks its landing until a journal names its
id or the lead closes it (`campaign message ID --close msg-N --reason R`); a lead's message never
blocks a landing and stays in `campaign brief` until closed (`campaign message : --close msg-N
--reason R`). The tool sends one itself when another campaign starts a quest on your files or
contracts: settle the order (`--after NAME:ID`) or confirm the two are independent, then close it.

Messages wait in the home's `.git` (`campaign-mail/<campaign>/`), so any agent can leave one
without being the campaign's one writer. A new one notifies the owner and wakes the lead's
`campaign wait`; `campaign brief` lists it for the quest's contributor and the lead. Landing
moves a quest's answered and closed messages into its state. In cmux, a lead that is not in
`campaign wait` is woken too: every `campaign` command (but `job`) records its cmux surface, and a
message from another surface is delivered there with `cmux agent message`, which an idle session
reads at once and a busy one at its next step.

A campaign on another machine is reached as a peer: `campaign config peers=NAME=URL,...`, where
URL is a repository both machines can push to and fetch from. A message to `NAME:ID` or `NAME:`
for a peer goes into this campaign's outbox, a ref only it writes (`refs/campaign/outbox/<name>`),
pushed to `outbox=REMOTE` (default `origin`). Each campaign's `campaign wait` fetches its peers'
outboxes every minute and imports each message once; one for a quest the peer has landed or does
not know goes to its lead. No receipt comes back: the sender learns the answer from the
landed journal, or from the owner.

## Other tools

- **Compiler caches** (ccache, sccache) are optional and work across job checkouts when configured
  to share entries; jobs inherit the submitter's PATH, so a launcher found there is used.
- **`pace [claude|copilot|codex]`** prints each quota window's use, its recent burn and whether
  it runs out before its reset, read from each harness's own records (`references/harness.md`).
  It is information for decisions (will this batch exhaust the week before the milestone?), never
  a reason to launch work. It does not see credits bought for Claude Code, so it can report a
  window as spent while those credits still cover it: ask the owner before stopping work for it.
- **`report`** draws the owner's sidebar progress and notifications (cmux; elsewhere the terminal
  or desktop). `campaign` and `quiet` keep it current by themselves: every state change and
  `campaign status` update the bar (the current milestone's quests: in the map's order, the first
  milestone with a quest in review or active, else with one ready to start), a `campaign-<name>`
  pill (the whole campaign) and a pill per quest in flight, with 🔥 and a count when jobs failed at
  its current code and no journal names them, sending only what changed; landings, blocked quests
  and new messages notify. It reads only the state, PLAN.md, the job store and the in-flight
  journals, so it stays instant; the map's criteria counts are `campaign map`'s. `quiet` adds a pill per hold, in the workspace that submitted
  the job, and notifies when a hold ran 5 minutes or more.
