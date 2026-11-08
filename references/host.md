# Host: jobs, quiet and the tools

How machine work runs. Rules about when to use what are in SKILL.md; this is the mechanics.
`campaign help COMMAND` and `quiet help COMMAND` document each command.

## Install

`install.sh` links `campaign`, `quiet`, `pace` and `report` into `~/.local/bin` and
the agent types into every installed harness; it never replaces a file it did not link. If one is
missing, run `install.sh` again (ask the patron before removing anything in its way). Without it, run
`python3 <skill>/scripts/campaign.py` and `python3 <skill>/scripts/quiet.py` directly. `campaign`
needs only `python3` (3.9 or newer) and `git` (2.31 or newer).

## Jobs

```sh
campaign job submit [--quest ID] [--commit REF] [--ref NAME=[PATH@]REF] [--cwd DIR] \
    --level none|light|strict|exclusive --minutes N [--priority P] [--after JOBS] \
    [--env K=V] [--slot NAME] [--retries N] [--sync] -- CMD
campaign verify ID            # the quest's gate checks at its branch head (--all: deferred too)
campaign verify ID --deferred # a landed quest's deferred checks, again on main
campaign wait [JOBS | --quest ID | --commits] [--any] [--timeout S]   # exit 0 all done, 1 a failure, 3 timeout
campaign job list | show J | log J | rerun J... | cancel J   # rerun: same jobs at the quest's head
campaign usage [--json]       # agent runs and jobs per quest, role and class
```

- **Identity.** A job runs at the commit resolved when it was submitted, in a fresh detached
  checkout under `$CAMPAIGN_JOBS/<id>/src` (default `~/.scratch/campaign/jobs`), never in the
  agent's worktree. Extra commits, of this or another repository, are checked out at
  `$JOB_REF_<NAME>`. `$JOB_OUT` is the attempt's output directory; `$JOB_SRC` the working directory.
- **Reuse.** Host, toolchain fingerprint (`uname`, compiler and interpreter versions, plus the
  output of `$CAMPAIGN_FINGERPRINT`), repository, commits, command, directory, level and `--env`
  form the job's key. An identical job that is pending or succeeded is returned instead of rerun;
  `--force` reruns. Name unpinned inputs (a dataset version, a download) in `--env` so they are part
  of the key. Upgrading `campaign` can change how keys are computed; run `campaign verify` again
  after an upgrade. A journal's Evidence row naming a job counts only while that job, or the same
  job rerun, passed at the quest's current code.
- **Slots.** `--slot NAME` reuses one checkout per name for incremental builds; it is verified
  clean at the job's commit before every run, and one job uses it at a time. `campaign verify` uses
  a slot per quest, except for critical quests and `--fresh`, which check out afresh.
- **Scheduling.** The worker starts with the first submit or wait and exits when nothing is queued
  or running. It runs one strict or exclusive job at a time and up to a quarter of the CPUs' worth
  of light jobs (`CAMPAIGN_LIGHT_JOBS`), in priority order; `quiet` arbitrates the machine across
  every session. Priorities: gate checks from `verify` run at 1, ordinary jobs at 0, deferred
  checks queued by `land` at -1. Deferred light jobs use at most half the light slots, and a
  deferred strict or exclusive check lasts at most 10 minutes, so the direct path never waits long
  behind completeness work.
- **Supervision.** Each attempt has a runner: the leader of its own process session, writing a
  heartbeat every 15 s. The command runs under `quiet` at the job's level, which stops it at its
  deadline; when the command ends, the runner kills every process left in its session, so helpers
  never outlive their job.

| Event | What happens |
|---|---|
| Timeout (`--minutes` after the hold is granted) | TERM, then KILL after 10 s; state `timeout`, exit 124; never valid evidence |
| Runner or worker killed, host suspended past 2 min | The next reader (worker, `status`, `wait`, `check`) sees the stale heartbeat, kills what is left of the attempt, and requeues it if `--retries` allows, else marks it `interrupted` |
| Host rebooted | The same, without killing anything (the boot id changed, so old pids mean nothing) |
| Command finished but the runner died before recording | The exit code was written first; the reaper records it |
| An old runner wakes after its attempt was replaced | It no longer owns the attempt and writes nothing |
| Job submitted twice | An identical job that is queued, running or done is returned; an interrupted or failed one runs anew |
| Dependency (`--after`) failed | `skipped` |
| Disk below 5 GiB | `quiet` admits nothing until space is freed |
| No worker can run | `--sync` runs the job in the foreground through the same runner and records the same result |

Retries are off by default because a command may not be safe to run twice; checks queued by
`campaign verify` retry once. Old job directories can be deleted once their quest is landed and
its deferred checks are green; the landing record in `state.json` keeps their summary, and
`campaign check` warns that their evidence is gone.

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
- Light holds are limited to a quarter of the CPUs, and none starts above a load of three quarters
  of the CPUs or below 5 GiB free disk.
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

Messages wait in the home's `.git` (`campaign-mail/<campaign>/`), so any agent can leave one
without being the campaign's one writer. A new one notifies the patron and wakes the game master's
`campaign wait`; `campaign brief` lists it for the quest's adventurer and the game master. Landing
moves a quest's answered and closed messages into its state.

A campaign on another machine is reached as a peer: `campaign config peers=NAME=URL,...`, where
URL is a repository both machines can push to and fetch from. A message to `NAME:ID` or `NAME:`
for a peer goes into this campaign's outbox, a ref only it writes (`refs/campaign/outbox/<name>`),
pushed to `outbox=REMOTE` (default `origin`). Each campaign's `campaign wait` fetches its peers'
outboxes every minute and imports each message once; one for a quest the peer has landed or does
not know goes to its game master. No receipt comes back: the sender learns the answer from the
landed journal, or from the patron.

## Other tools

- **ccache:** every CMake build goes through it (`~/.profile` sets the compiler launchers;
  `~/.ccache/ccache.conf` shares entries across worktrees and job checkouts). An old build
  directory needs a fresh configure to pick it up.
- **`pace [claude|copilot|codex]`** prints each quota window's use, its recent burn and whether
  it runs out before its reset, read from each harness's own records (`references/harness.md`).
  It is information for decisions (will this batch exhaust the week before the milestone?), never
  a reason to launch work. It does not see credits bought for Claude Code, so it can report a
  window as spent while those credits still cover it: ask the patron before stopping work for it.
- **`report`** draws the patron's sidebar progress and notifications (cmux; elsewhere the terminal
  or desktop). `campaign` and `quiet` keep it current by themselves: every state change and
  `campaign status` update the bar (the current milestone's quests), a `campaign-<name>` pill (the
  whole campaign) and a pill per quest in flight, sending only what changed; landings, blocked
  quests and new messages notify. `quiet` adds a pill per hold, in the workspace that submitted
  the job, and notifies when a hold ran 5 minutes or more.
