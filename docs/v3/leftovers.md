# What v2 leaves behind

v2 never cleans up after itself on a schedule. Disk is the large cost. `~/.scratch` holds 151 GiB,
and 112 GiB of that is the job store. Within the store, 35 GiB of build slots belong to quests
that have already landed. Of the 442 scratch task directories, 422 have no `.purpose` file, so
nothing can tell what they are for. Zombie jobs and quests are rare: none were found by the
definitions below. The process leak is real but small: the worker holds 41 unreaped children.
Duplicate work cost 12 machine hours of avoidable reruns, plus two quests dropped as redundant.

## Sources

Most numbers come from one pass:

`quiet light v2-replay-leftovers 30 "v2-replay leftovers" -- python3 tools/replay/replay.py
leftovers`

It finished 2026-11-20T04:02Z and wrote `leftovers.json` under `~/.scratch/xapiand/v2-replay/`.
The code is in `tools/replay/leftovers.py`, one function per query, named below. Disk sizes come
from a single `du -x -k -d 4 ~/.scratch` walk. Under a load of 10 to 20 that walk took 20 minutes,
and an earlier attempt with three walks did not fit its hold.

The zombie query was rerun after a fix:

`quiet light v2-replay-leftovers 10 ... replay.py leftovers --part zombies`

It ran at 04:46Z. The fix: `state.json` records worktree paths relative to the home, so the first
pass marked every worktree as gone.

Everything here is a snapshot. The store prunes jobs older than 14 days, and quests land
continuously, so ages and verdicts change hour by hour.

## Disk by owner and age (query `scratch`)

| Owner (first level of `~/.scratch`) | GiB |
|---|---|
| `campaign` (the job store) | 112.3 |
| `vm-document-gate` (Linux 15.7, FreeBSD 7.1 VM images) | 22.9 |
| `prospect-idx` (two 1M-document indexes, 30 days old) | 2.6 |
| `preflight` | 2.1 |
| `leases` (AWS results, 16 days old) | 2.0 |
| `document-gate` | 1.9 |
| `release-rows` | 1.8 |
| `xapiand` (1.4 of it this quest's caches) | 1.7 |
| 120 other owners | 3.9 |
| Total | 151.2 |

There are 442 task directories, the second level. 422 of them have no `.purpose` file, and those
422 hold 148.9 GiB. Only 6.3 GiB has been idle for more than 14 days. So the 14-day sweep rule
reclaims almost nothing: the space is held by recent, unlabeled work.

## The job store (query `job_store`)

| Part | GiB |
|---|---|
| Build slots (144) | 81.3 |
| Job directories (logs, outputs, checkouts) | 30.2 |
| `git-mirrors` | 0.5 |

Build slots by whether a quest still needs them:

| Verdict | GiB | Meaning |
|---|---|---|
| leftover | 35.1 | Every quest that used the slot is landed or dropped. Example: `xapiand-name-index`, 4.9 GiB, idle 1.9 days, quest landed |
| quest in flight | 30.0 | Its quest is active or in review, but no job is using it now |
| in use | 16.4 | A queued or running job names it |

Job directories by job state, quest status and age, the largest rows:

| Row | GiB |
|---|---|
| done, quest landed, older than 7 days | 12.1 |
| done, quest landed, 1 to 3 days | 6.4 |
| failed, quest landed, 1 to 3 days | 2.1 |
| done, quest active, under 1 day | 1.4 |
| timeout, quest landed, 1 to 3 days | 1.3 |

25.9 of the 30.2 GiB belongs to landed quests. Their pass or fail is already recorded in the
history and in the evidence. Only the logs remain, and nothing reads them after the landing.

## Worktrees (query `worktrees`, and `git worktree list`)

The pass found 763 git worktrees across `~/code/Kronuz`. 35 of them belong to landed or dropped
quests and still exist on disk, holding 4.2 GiB. The largest is
`strata-wt/xapiand/cold-tier-step2`, 1.75 GiB, 8 days after its landing.

666 worktrees name no quest. The pass sums them to 126 GiB, but that figure overlaps the store:
build slots are worktrees too, and the 15 largest no-quest worktrees are all build slots
(38.8 GiB). A later listing (`git -C <repo> worktree list --porcelain` over each repository under
`~/code/Kronuz`, kept in `worktree-paths.txt`) found 865 worktrees. By path:

| Where | Count |
|---|---|
| `<repo>-wt/<campaign>/<quest>` (quest worktrees) | 96 |
| other `<repo>-wt/...` (hunts, explorations, pins) | 266 |
| store build slots | 182 |
| store job checkouts | 75 |
| other `~/.scratch` (copyright sweeps, CI-flake forcing) | 142 |
| main checkouts and others | 104 |

The difference between 144 slot directories and 182 slot worktrees means git still lists slots
whose directories are gone. Those are prunable entries. The size of the 266 non-quest worktrees
was not measured on its own, so it is unknown.

## Zombie jobs and quests (query `zombies`)

A zombie job is one that nothing will move forward:
1. A running job whose heartbeat is older than 2 minutes.
2. A queued job whose quest is landed or dropped.
3. A queued job waiting `after` a job that failed.

None was found at 04:46Z. A zombie quest is one that is active or in review, whose worktree is
gone or that nothing has touched in 3 days. None was found either.

The real stuck work is not a zombie by these rules. It is starved exclusive work. At 04:46Z, 65
jobs were queued. 38 of them were exclusive, including 22 for `document-gate` (the oldest 3.8 h)
and 4 for `dense-sync-stack-5m-times` (4.9 h). This was read from `spec.json` and `state.json` of
every queued job, read-only. Over the window, 281 of 779 exclusive jobs never ran
(`where-time-went.md`, query `waits`). v3 needs a deadline on every queued job, and a reason
recorded when one expires.

## Processes (query `processes`)

| Finding | Count |
|---|---|
| Zombie (defunct) processes on the host | 109 |
| ... whose parent is the campaign worker (`campaign.py worker`, pid 58801, up 2.5 days) | 41, the oldest about 45 h |
| ... whose parent is Chrome | 61 (not campaign) |
| Children of jobs already in a terminal state | 4 (one zsh 2 days old, from an agent's shell; three `quiet` and runner processes of jobs that had just ended) |

The worker reaps its job children only when it waits on them. The 41 are children it spawned and
never waited for, about one every 90 minutes of its 60 hours. Each one holds a process-table entry and
nothing else. This is harmless in size, but it is the same bug class as the leaked holds, and v3's
driver should own exactly the processes it starts.

## Duplicate work (query `duplicates`)

| Kind | Jobs | Machine h | Tokens |
|---|---|---|---|
| Avoidable reruns: only documentation changed | | 7.0 | unknown |
| Avoidable reruns: same code after a pass | | 3.2 | unknown |
| Avoidable reruns: only files outside the quest's file set changed | | 1.8 | unknown |
| All avoidable reruns (`account.py` `classify_reruns`) | 151 | 12.0 | unknown: no job records which turn submitted it |
| `fixture-500k` (detent), dropped as superseded by dense-pages | 7 | 1.8 (110 CPU min) | 5.1M (`campaign usage`) |
| `loop-yield-tsan` (xapiand), dropped as redundant: loop-yield-race had already landed the fix | 3, none ran | 0 | 1.4M (one agent run, 01:51:22 to 01:53:07Z, input, output and cache tokens summed over `agent-a36cc45fa1fe35211.jsonl`; `campaign usage` has no runs after 2026-11-18T19:20Z) |
| `evidence-by-inputs` (xapiand), superseded by v3 | 0 | 0 | 0 |

The bytes these jobs left were not measured per job: their directories fall within the store's 30 GiB.

`loop-yield-tsan` is the pattern to stop. A hunt row (F6) was turned into a quest after the fix it
asked for had already landed. The agent found that out in under two minutes. v3 can catch it before
any agent runs, by checking a new quest's named finding against the landed history.

## What v3 must do differently

1. **Every directory has an owner and an expiry, recorded when it is created.** Today 98.5% of
   scratch bytes carry no `.purpose`. Make the store's own directories (slots, job dirs) records in
   the store, not bare directories.
2. **Free a quest's build slot and job outputs when it lands.** Doing this at the landing would
   reclaim about 35 GiB of slots, plus 25.9 GiB of landed quests' job directories. Keep a
   compact result per job (see `recording-gaps.md`).
3. **Give each queued job a deadline.** An expired job ends `expired`, with the reason, instead of
   staying queued.
4. **The driver waits on every child it starts**, and its holds die with it.
