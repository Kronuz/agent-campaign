# Where campaign v2's time went

This page splits the last 14 days of the xapiand and detent campaigns by cause: machine time, the
light slots, job waits, reruns, land refusals, quest wall time and tokens. The window is
2026-11-06T02:39Z to 2026-11-20T02:39Z. Every number comes from one pass,
`quiet light v2-replay-account 30 "v2-replay account" -- python3 tools/replay/replay.py account`
(run 2026-11-19 at about 19:50 PST, after the inventory pass in `replay-inventory.md`). It writes
`account.json`, and `replay.py report account` prints the tables. Each section names its query:
a function in `tools/replay/account.py`, whose docstring says exactly what it counts.

## The short answer

Most of the wall time was quests that nothing was moving, not machine time. In-flight quests
(active, review or blocked) spent 3,571 quest-hours in the window, and they were split like this:

| What an in-flight quest was doing | Quest-hours | Share |
|---|---|---|
| Nothing moving it while active: no job queued or running, no agent turn, every `after` landed | 941 | 26.4% |
| Blocked (on the owner or a decision) | 746 | 20.9% |
| A job of it waiting for the machine | 562 | 15.7% |
| Waiting on a dependency that had not landed | 524 | 14.7% |
| An agent turn | 365 | 10.2% |
| A job of it running | 253 | 7.1% |
| Nothing moving it while in review | 179 | 5.0% |

The machine was the smaller problem, but it was mostly idle. The light slots were 17.2% used.
Exclusive jobs drained the machine for 47.3 wall hours to run for 19.7. Failed or timed-out runs
took 102 of the 496 machine hours. Avoidable reruns, by contrast, were small: 12 hours identical, 39 hours counting every run no
question needed (`need`).

Sorted by hours, the top causes were:
1. Active quests with nothing moving them: 941 quest-hours.
2. Quests blocked on the owner: 746 quest-hours.
3. Idle light capacity while light work was ready: 601 slot-hours. Of that, 222 went to exclusive
   drains and runs, and 288 has no recorded reason.
4. Jobs waiting for the machine: 562 quest-hours, or 2,997 job-hours counted per job.
5. Waiting on dependencies: 524 quest-hours.

Failed runs (102 h), land-refusal episodes (68 h) and runs no question needed (39 h, 12 of them
identical reruns) come after these.

## Machine time by level (query `machine_time`)

Run time is a job's command from `began` to `ended`. A staged build is the light build of a heavy
job, run before its hold. Both are clipped to the window.

| Campaign | Level | Done | Failed or timed out | Cancelled | Staged build | Total |
|---|---|---|---|---|---|---|
| xapiand | light | 185.8 | 53.9 | 2.6 | 11.8 | 254.1 |
| xapiand | strict | 7.6 | 1.3 | 0.0 | 1.3 | 10.2 |
| xapiand | exclusive | 10.1 | 0.6 | 0 | 2.2 | 12.9 |
| detent | light | 147.9 | 45.1 | 2.9 | 3.9 | 199.8 |
| detent | strict | 3.2 | 0.1 | 0 | 1.4 | 4.7 |
| detent | exclusive | 8.0 | 0.9 | 0.1 | 2.4 | 11.4 |
| detent | none | 2.6 | 0 | 0 | 0 | 2.6 |

Total: 496 hours. That is 365 done (74%), 102 failed or timed out (20%), 23 staged builds (5%) and
6 cancelled while running (1%). A mutant run that must fail is recorded as done when it fails, so
the failed hours are runs that did not give the evidence they were sent for.
Light work is 91% of the machine time. Measurements (strict and exclusive) are 39 hours, 8%.

## The light slots (query `light_capacity`)

The host has 8 light slots (half its 16 CPUs), and 4 while a strict job runs. Sampled every minute,
they offered 2,688 slot-hours in the window, and jobs used 461 of them (17.2%). For the minutes
when light work was ready (submitted, its `after` jobs ended, not yet claimed) and slots stood
idle, this table names what held the machine at that minute:

| Idle slots while light work was ready, by what held the machine | Slot-hours |
|---|---|
| No recorded reason: measured-use charges, fair share between campaigns, queue order, quiet's load gate | 288 |
| An exclusive job draining the light holds | 143 |
| An exclusive job running | 79 |
| The job's build slot was held by another job of the same slot | 67 |
| A strict job running (light keeps 4 slots) | 25 |

The remaining 1,625 slot-hours were idle with no light work ready. Exclusive drains took
47.3 wall hours, counted from an exclusive job's claim to its command starting; exclusive jobs then
ran for 19.7 wall hours. So for each hour of exclusive measurement, the machine spent 2.4 hours
emptying itself first.

The 288 unexplained slot-hours are a recording gap. The worker charged busy light jobs several
slots, by their measured CPU, and quiet refuses holds above a load of twice the CPUs. Neither
charges nor load are recorded, so the replay cannot separate them (`recording-gaps.md`).

## Job waits (query `waits`)

These are job-hours, counted per job, for jobs submitted in the window. "Queued" runs from
submitted to claimed by the worker; "in hold" runs from claimed to the command's start (source
preparation, a staged build, admission and quiet's wait). Jobs that never ran count until they
were cancelled or skipped.

| Level | Jobs | Ran | Queued | In hold | Never ran | Median wait (min) | p90 wait (min) |
|---|---|---|---|---|---|---|---|
| light | 6,321 | 5,541 | 2,027 | 129 | 206 | 7.8 | 66 |
| strict | 448 | 341 | 267 | 13 | 60 | 22.9 | 124 |
| exclusive | 779 | 498 | 696 | 122 | 349 | 55.2 | 268 |
| none | 133 | 133 | 7 | 0 | 0 | 0.1 | 6 |

36% of exclusive jobs never ran: 281 of 779 were cancelled or skipped, or are still queued.
document-gate alone submitted 133 exclusive jobs. 25 of them finished; 48 were cancelled, 31
skipped and 23 are still queued.

## Reruns (query `classify_reruns`)

A rerun is a job submitted in the window whose shape ran before. Its shape is the repository,
command, directory, level, build and environment, with commit ids and `$$` masked, since commands
name their output paths after them. Of 7,681 jobs, 4,341 were first runs and 3,340 were reruns.
Whether a rerun was necessary is decided by `git diff` between its commit and the previous run's.
No job recorded what it read, so in order of precedence:
1. Docs only, or nothing changed at all, means avoidable.
2. A path the command or build names, or a build file, changed: necessary.
3. Otherwise, a file in the quest's file set changed: necessary.
4. Otherwise: avoidable.

| Verdict: why | Jobs | Ran | Run h | Wait job-h |
|---|---|---|---|---|
| necessary: a file in the quest's file set changed | 1,769 | 1,589 | 116.7 | 731 |
| necessary: a path its command names or a build file changed | 1,297 | 1,188 | 113.3 | 544 |
| avoidable: only documentation changed | 106 | 105 | 7.0 | 31 |
| retry: same code after a failure | 59 | 57 | 3.5 | 9 |
| avoidable: same code after a pass | 37 | 28 | 3.2 | 23 |
| necessary: an outside script it runs changed | 39 | 37 | 2.8 | 2 |
| avoidable: only files outside the quest's file set changed | 22 | 18 | 1.8 | 3 |
| retry: same code after an interruption or timeout | 9 | 9 | 1.3 | 2 |
| necessary: code changed, no file set to scope it | 2 | 2 | 0.1 | 0 |

So 233 hours of reruns were necessary by these rules, 5 were retries and 12 were avoidable. The avoidable figure
is a floor for what finer identity could save, and the file-set rule makes it a ceiling for what
file-level identity could save. The document-gate reruns show why. Its 23 queued exclusive jobs at
strata:dd02c63d6 rerun rows that passed at strata:a93f2b591. Between those commits, the file the
jobs run, `bench/kv/run.py`, gained three lines: a new workload set the queued rows do not use.
Their read set would contain that file, so input-scoped identity, even with strace, reruns all 23.
Only an identity finer than files would have kept them: the rows' own configuration, or the hash of
the binary the build produced.

## Runs no question needed (query `need`)

Counting only identical reruns understates the waste. `quiet light v2-replay-need 30 "v2-replay
need" -- python3 tools/replay/replay.py need` (`tools/replay/need.py`, written to `need.json`)
assigns each job submitted in the window the first category below that applies. That way, no job
counts twice. A row is the job's shape with any trial index masked (`trial 2`, `-t3-`, `round 4`).

| Category | Jobs | Ran | Run h | Wait job-h |
|---|---|---|---|---|
| Identical rerun (the table above, avoidable) | 165 | 151 | 12.0 | 57 |
| Superseded: a newer submission of the same row, at another commit, arrived before it began | 631 | 332 | 22.3 | 196 |
| Its quest had landed or been dropped before it began | 18 | 18 | 4.5 | 8 |
| Landing-time rerun (after the quest's first accept or land refusal) whose changed paths miss its declared inputs | 1 | 1 | 0.3 | 0 |
| Trial past the first two of a passing row at one commit | 1 | 1 | 0.0 | 6 |
| All | 816 | 503 | 39.2 | 267 |

That is 39.2 of the 502 machine hours in the window. Superseded runs are the largest
category: 332 of them ran after their answer was already stale. 578 of the 631 superseded jobs are
light. The 15 exclusive ones never ran, but they waited 10 job-hours.

Three of the owner's cases cannot be counted exactly from v2's records:

1. **A criterion already evidenced.** A criterion is answered by many rows, and v2 keeps no
   verdict per criterion over time. So the row is the finest question the records name.
2. **Runs sized past what decided them.** Only 17 jobs logged a value per trial
   (`... trial N: VALUE`), and they ran 0.7 h. Using a 95% interval within 2% of the mean
   (`need.sizing`), 0.0 h of that came after the decision. The run sizes of the other
   jobs, such as "41 trials" or "6 rounds", went unrecorded per trial, so the waste there is
   unknowable. Trials submitted as separate jobs mostly never ran: document-gate's A6 VM trials
   were cancelled or skipped.
3. **Landing-time reruns with unchanged inputs.** v2 declares no inputs. The proxy for them is:
   - the paths the command and build name,
   - the quest's file set,
   - the top-level build files.

   By that proxy, 1 of 548 landing-time reruns (48.0 h) changed no
   input. The rest changed the quest's file set (454 jobs, 36.5 h) or a
   top-level build file (61, 10.2 h), a path the command names (16, 0.4 h) or an outside script (16, 0.6 h). File sets are broad globs, so
   48.0 h is the ceiling for what declared inputs finer than the file set could save, and
   0.3 h is the floor.

## Land refusals (query `land_refusals`)

v2 records no refusal. The replay reads each "cannot land yet" from the leads' transcripts and, for
background calls, from Claude Code's task files, which exist only from 2026-11-15. Calls inside the
lead's `landloop.sh` retry script count only when their output reached a task file, so these
numbers are a floor.

There were 125 refusals in the window. Some list several reasons, so the counts by reason add up
to more:
1. Evidence missing: 42.
2. CI running: 22.
3. Main moved: 25.
4. A check not green: 19.
5. CI missing: 16.
6. CI failed: 16.
7. Accepted, but the code changed since: 13.
8. Evidence ran at other code: 11.
9. Review not accepted: 6.
10. Scope: 6.

CI accounts for 54 of the 125. An episode runs from a quest's first refusal to its next landing.

| Episodes | Count | Refusals | Landed | Hours, first refusal to land | Median h | Jobs in them | Job run h | Accepts in them | Commits refused |
|---|---|---|---|---|---|---|---|---|---|
| all | 86 | 125 | 81 | 67.8 | 0.31 | 103 | 8.3 | 42 | 89 |
| including "main moved" | 22 | 39 | 18 | 22.4 | 0.82 | 17 | 0.8 | 13 | 19 |
| refused only because main moved | 12 | 23 | 9 | 10.5 | 0.87 | 10 | 0.5 | 7 | 7 |

For a quest whose sole gap was main moving outside its file set, landing took a median 0.87 hours
more, plus a re-accept in 7 of the 12 episodes. The longest episodes mixed causes: release-rows
(9.5 h, CI and stale acceptance), consensus-dns-threads (8.7 h, 17 jobs, 5 accepts) and
wide-search-loop (5.4 h, 23 jobs rerun at the merged code). CI rounds are not recorded locally.
The 89 distinct commits refused are the lower bound on rounds: each new head is a new CI run.

## Quests with nothing moving them (query `quest_hours`)

Each in-flight quest-hour gets the first label that holds, in this order:
1. An agent run of the quest (`campaign usage`).
2. A job of it running.
3. A job of it queued or in its hold.
4. An `after` dependency not landed.
5. Otherwise, nothing moving it, by status.

Status comes from state history, so it starts on 2026-11-08. Tasks (`QUEST--TASK`) are counted
through their quest. The totals are in "The short answer"; by day:

| Nothing moving it (h) | 11-09 | 11-10 | 11-11 | 11-12 | 11-13 | 11-14 | 11-15 | 11-16 | 11-17 | 11-18 | 11-19 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| xapiand active | 16 | 40 | 51 | 72 | 36 | 26 | 24 | 23 | 34 | 89 | 107 |
| xapiand review | 1 | 8 | 2 | 10 | 0 | 0 | 0 | 6 | 4 | 0 | 38 |
| xapiand blocked | 2 | 24 | 24 | 59 | 72 | 72 | 72 | 72 | 72 | 53 | 48 |
| detent active | 9 | 25 | 50 | 44 | 31 | 24 | 24 | 38 | 71 | 48 | 35 |
| detent review | 1 | 13 | 13 | 5 | 3 | 0 | 0 | 12 | 21 | 8 | 24 |
| detent blocked | 0 | 0 | 0 | 3 | 23 | 24 | 24 | 24 | 24 | 24 | 24 |

On 2026-11-18 and 2026-11-19, xapiand's idle active hours rise to 89 and 107. That matches the
lead's note at 2026-11-19 17h: ten active quests had no agent. Some of it is a recording gap:
`campaign usage` attributes no xapiand agent run after 2026-11-18T19:20Z, so the six agents
relaunched on 2026-11-19 count as idle for their 5 to 13 minutes each. The largest idle quests
over the window were reactor-on-wheel (detent, 133 h), x7-gate (67 h), exact-id-keys (55 h),
track-p (51 h) and namespace-measure (detent, 39 h); `quests.json` lists every quest.

"Waiting on a dependency" uses each quest's current `after` list. Dependencies added or removed
during the window are not recorded, so those 524 hours are an estimate.

## Agent turns and tokens (query `agent_tokens`)

| Measure | detent | xapiand |
|---|---|---|
| Quests landed in the window | 109 | 128 |
| of them with an attributed agent run | 62 | 64 |
| Agent runs per landed quest, median | 2 | 1 |
| Agent tokens per landed quest, median | 22.4M | 14.4M |
| Agent tokens per landed quest, mean | 38.6M | 27.7M |
| Output tokens per landed quest, median | 111k | 78k |
| Agent hours per landed quest, median | 1.4 | 1.6 |
| Agent tokens on landed quests | 2,392M | 1,772M |
| Agent tokens on quests not landed | 139M | 101M |
| Agent runs, all | 182 | 144 |
| Agent hours, all | 165 | 242 |
| Lead tokens (transcript, each response once) | 1,600M | 2,528M |
| Lead active hours (transcript gaps capped at 5 min) | 125 | 160 |

Tokens are cumulative per request, so cached context rereads are included; `campaign usage`
reports 96% of xapiand's as cached input. The leads used 4,128M tokens against 4,404M for every
agent together, so the leads alone cost about as much as all the work they directed. Lead tokens
cannot be split by quest: each lead is one long session. Cost is unknown, since every harness
reports `cost_available: false`.

## What this cannot tell

These numbers rest on inference wherever v2 recorded nothing; `recording-gaps.md` lists each gap
and the field that would close it. Four matter most here:
1. Without read sets, the necessary/avoidable split is a proxy.
2. The light-capacity "no recorded reason" bucket (288 slot-hours) has no recorded cause.
3. Land refusals are a floor: background calls before 2026-11-15 and the land-retry loop's
   attempts are missing.
4. Status before 2026-11-08 is missing, and dependencies are current values.
