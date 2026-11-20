# What v3's policies would have saved: a replay of v2's 14 days

The simulator replays the window's 7,681 job submissions on a model of v2's machine, under each
candidate v3 policy, alone and combined. The result is lopsided. The deterministic driver saves
about 790 quest-hours by itself, because quests sat idle between agent turns, and it halves the
leads' tokens. Exclusive batching cuts the drain time from 60 to 6 wall hours. The need
policy, which runs jobs only for unanswered questions, saves 39 machine hours and 506 job-hours of
queueing. A merge queue and input-scoped identity save little on this record. Calibration is good
on totals and poor hour by hour; the section on it says how poor.

## Sources

Two passes produce the numbers:

1. `quiet light v2-replay-need 30 "v2-replay need" -- python3 tools/replay/replay.py need`
   (`tools/replay/need.py`, written to `need.json` and `need.jsonl`). It classifies runs no
   question needed, as `where-time-went.md` explains.
2. `quiet light v2-replay-simulate 30 "v2-replay simulate" -- python3 tools/replay/replay.py
   simulate` (`tools/replay/simulate.py`, written to `simulation.json`, 2026-11-20 around 06:10Z).

Both run on `account.json`, `reruns.jsonl`, `episodes.json`, `quests.json` and `hourly.json` from
the account pass, and on `calls.json` from the calls pass. All of these live under
`~/.scratch/xapiand/v2-replay/`.

## The model

The model ticks every 30 s through the window, replaying the recorded jobs with their recorded run
and build durations, arrivals and `after` dependencies. The machine follows v2's rules:
1. Light slots, 4 of them beside a strict job.
2. One strict job at a time.
3. An exclusive job drains the light holds and then runs alone.
4. Light work goes shortest-first by each row's median run, and work queued over an hour goes in
   submit order.
5. Staged builds of heavy jobs run as light work.

A job that never ran in the record leaves the queue at its recorded cancel or skip time.

A quest that landed in the window lands in the simulation at the later of two things: its last
simulated evidence end, plus its recorded delay between last evidence and landing (review, CI,
refusals, the lead). A policy may shorten that delay. Quest-hours saved add up, over the quests
that land in both runs, how much earlier each lands than in the calibrated v2 run.

Tokens are modeled for only two policies, so the other rows repeat the recorded totals:
1. **Merge queue:** the agent runs that began inside a main-moved refusal episode. `campaign usage`
   attributes none of them, so the saving is 0.
2. **Driver:** the leads' routine share. Each lead's tokens in the window are scaled by its
   `campaign` calls per request: 1,969M of 4,128M.

Nothing models whether an agent turn would have been skipped because a job was never run.

## Calibration

v2 does not record three parameters of its machine, so the pass fits them to the recorded hourly
queue. The parameters are the light slots it effectively filled (6 or 8), how long an overdue drain
keeps draining (0 or 60 min), and whether a build slot serializes its jobs. The best of the 8 fits
uses 8 slots, no drain turn, and no slot serialization.

| Measure | Recorded | Simulated v2 | Error |
|---|---|---|---|
| Landings in the window | 237 | 234 | −1.3% |
| Mean jobs waiting, sampled hourly | 11.5 | 11.7 | +1.7% |
| Waiting job-hours, for jobs that ran | 3,303.5 | 3,690.4 | +11.7% |
| Median wait (min) | 9.4 | 3.7 | −61% |
| Hourly queue, mean absolute error per hour | | | 76.9% |
| Machine hours | 502.0 | 500.2 | −0.4% |
| Exclusive drain wall hours | 47.3 (`where-time-went.md`) | 60.0 | +27% |
| Exclusive running wall hours | 19.7 | 22.4 | +14% |

Landings, mean queue length and machine hours are within 10%. Waiting job-hours are just outside
it. The hour-by-hour queue is not: its error is 77%, with the median wait too short, so the model
moves the right amount of work at the wrong times. The record shows slots serialized: of 489 slots that ran light jobs in the window, 481 never ran
two at once (counted from the recorded `began` and `ended` of each slot's light jobs). Yet the best serialized model left the queue 106% off, its excess
piled on single-slot quests such as logger-exit-lifetime (132 jobs) and reader-s6 (118). The fit
compensates for a dispatch order or batching rule of v2's worker that the model does not have.

Treat the deltas below as directional to within the waiting job-hours error, about 12%. A delta
smaller than that, such as the merge queue's, is noise.

## Results

| Policy | Landings | Machine h | Waiting job-h | Median wait (min) | Drain wall h | Quest-h saved | Agent tokens | Lead tokens |
|---|---|---|---|---|---|---|---|---|
| v2 (calibrated) | 234 | 500.2 | 3,690 | 3.7 | 60.0 | 0 | 4,404M | 4,128M |
| Input-scoped identity: identical reruns never run | 234 | 488.3 | 3,467 | 3.5 | 60.9 | 24.5 | 4,404M | 4,128M |
| Merge queue: no rerun or wait for main-moved refusals | 234 | 499.6 | 3,689 | 3.7 | 59.9 | 34.2 | 4,404M | 4,128M |
| Exclusive batching: a 4-hourly window, light packed before it | 236 | 500.9 | 2,721 | 1.4 | 5.6 | 41.9 | 4,404M | 4,128M |
| Deterministic driver: no idle time between steps | 232 | 501.1 | 4,970 | 5.7 | 65.0 | 793.8 | 4,404M | 2,159M |
| Need: only unanswered questions; evidence by declared inputs; a resubmission replaces | 234 | 461.4 | 3,184 | 3.0 | 57.9 | 41.8 | 4,404M | 4,128M |
| Need, plus no trial past a decided row | 234 | 461.3 | 3,178 | 3.0 | 57.9 | 41.8 | 4,404M | 4,128M |
| All four (identity, merge queue, batching, driver) | 237 | 488.7 | 3,101 | 1.9 | 7.0 | 904.9 | 4,404M | 2,159M |
| All four and need | 237 | 460.5 | 2,633 | 1.4 | 5.8 | 923.0 | 4,404M | 2,159M |

The window is 14 days, so 237 landings is 16.9 a day, and 234 is 16.7. The fit already uses all 8
light slots, so the pass runs no "8 light slots filled" variant.

## What each policy does here

1. **The driver dominates.** `quests.json` gives the nothing-moving intervals: active or in review,
   no job, no agent turn, every dependency landed. The driver submits each job earlier by the
   quest's idle time before it, and lands 15 minutes after the last evidence once nothing else
   waits. That saves 794 quest-hours. It also packs the same work into fewer hours, so waiting
   job-hours rise by 35%: a driver needs the scheduler's other fixes to be worth its full value.
   It lands 2 fewer quests in the window, because the extra contention pushes two past its end.
2. **Batching fixes the drains, not the machine hours.** A planned window every 4 hours replaces
   v2's on-demand drains. Drain wall time falls from 60.0 to 5.6 hours, waiting falls 26%, and the
   median wait falls from 3.7 to 1.4 min.
3. **Need is the largest machine-hour saving.** Compared with v2's 500.2 hours, need saves 38.8
   hours (8%), and waiting falls by 506 job-hours (14%). By count:
   - 631 superseded jobs are replaced instead of run.
   - 165 identical reruns, 18 runs for landed or dropped quests, and 1 landing-time rerun with
     unchanged inputs never run.

   The declared-input proxy is conservative. It treats a quest's whole file set as the inputs, so
   48 hours of landing-time reruns stay in. Finer declared inputs could save up to those 48 hours
   more (`where-time-went.md`).
4. **Sizing cannot be measured on this record.** Only 17 jobs logged per-trial values, and none ran
   past the trial count at which its rows' intervals cleared within 2%. The run sizes ("41 trials",
   "6 rounds") were never recorded per trial. So the 0.1-hour difference says nothing about sizing
   in general, only that v2 kept no record of it.
5. **The merge queue and input-scoped identity are small here.** Only 13 jobs fall inside
   main-moved refusal episodes, and the 34 quest-hours saved are from shortening those episodes'
   wall time. Identity removes 165 jobs, 151 of which ran, and 12 hours.
6. **Together** (all four and need), the policies save 923 quest-hours, 39.7 machine hours,
   1,058 waiting job-hours and 1,969M lead tokens. They land every quest the record landed.

## What it cannot tell

1. **Agent tokens a policy would save.** No job records the turn that submitted it, and v2 does not
   record why an agent turn happened. So the tokens of turns spent waiting on, rereading or resubmitting
   superseded work are unknowable (`recording-gaps.md`).
2. **The driver's real latency.** The 15-minute floor between last evidence and landing is a stated
   assumption, not a measurement.
3. **Second-order effects.** A run that never happens produces no failure, so no fix commit follows
   it and no further reruns either. The model replays the recorded submissions, so these savings
   are missing from every row.
